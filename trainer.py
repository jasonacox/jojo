#!/usr/bin/python3
"""
Main Trainer class for Jojo LLM Training

This module provides the main training orchestration with improved
organization, efficiency, and monitoring capabilities.

Author: Jason A. Cox
2025 July 4
https://github.com/jasonacox/jojo
"""

import os
import time
import logging
import datetime
import math
from typing import Optional, Dict, Any, Tuple
import torch
import torch.nn as nn

# Try to import matplotlib for plotting
try:
    import matplotlib
    matplotlib.use('Agg')  # Use non-interactive backend
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

from config import Config, Constants
from utils import (
    ProgressTracker, MetricsTracker, GracefulShutdown, DeviceManager,
    CheckpointManager, PlotManager, format_time_delta, count_parameters
)
from simple_packed_loader import create_simple_packed_loaders

logger = logging.getLogger(__name__)


class CustomLRScheduler:
    """
    Custom learning rate scheduler that matches the behavior from story-notebook.py:
    1. Linear warmup for warmup_iters steps
    2. Cosine decay from warmup_iters to lr_decay_iters
    3. Constant min_lr after lr_decay_iters
    """
    
    def __init__(self, optimizer, learning_rate, warmup_iters, lr_decay_iters, min_lr):
        self.optimizer = optimizer
        self.learning_rate = learning_rate
        self.warmup_iters = warmup_iters
        self.lr_decay_iters = lr_decay_iters
        self.min_lr = min_lr
        self.iter_num = 0
    
    def get_lr(self, it):
        """Get learning rate for given iteration"""
        # 1) linear warmup for warmup_iters steps
        if it < self.warmup_iters:
            return self.learning_rate * it / self.warmup_iters
        # 2) if it > lr_decay_iters, return min learning rate
        if it > self.lr_decay_iters:
            return self.min_lr
        # 3) in between, use cosine decay down to min learning rate
        decay_ratio = (it - self.warmup_iters) / (self.lr_decay_iters - self.warmup_iters)
        assert 0 <= decay_ratio <= 1
        coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
        return self.min_lr + coeff * (self.learning_rate - self.min_lr)
    
    def step(self):
        """Update learning rate for all parameter groups"""
        lr = self.get_lr(self.iter_num)
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr
        self.iter_num += 1
        return lr
    
    def state_dict(self):
        """Return scheduler state for checkpointing"""
        return {'iter_num': self.iter_num}
    
    def load_state_dict(self, state_dict):
        """Load scheduler state from checkpoint"""
        self.iter_num = state_dict.get('iter_num', 0)


class Trainer:
    """Main training orchestrator"""
    
    def __init__(self, config: Config, model: nn.Module, tokenizer: Any, output_checkpoint: Optional[str] = None):
        self.config = config
        self.model = model
        self.tokenizer = tokenizer
        self.output_checkpoint = output_checkpoint
        
        # Initialize components
        self.metrics = MetricsTracker()
        self.shutdown_handler = GracefulShutdown()
        self.device = torch.device(config.system.device)
        
        # Determine device type early for use in other setup methods
        self.device_type = 'cuda' if 'cuda' in config.system.device else 'cpu'
        
        # Training state
        self.epoch = 0
        self.batch_counter = 0
        self.global_iter_num = 0  # For custom LR scheduler
        self.best_val_loss = float('inf')
        self.worst_val_loss = float('-inf')
        self.best_train_loss = float('inf')
        self.worst_train_loss = float('-inf')
        
        # Initialize data loaders first
        self._setup_data_loaders()
        
        # Initialize optimizer and scheduler (needs data loaders)
        self._setup_optimizer_and_scheduler()
        
        # Initialize mixed precision training with correct device_type
        if self.device_type == 'cuda':
            self.scaler = torch.amp.GradScaler('cuda', enabled=(config.system.dtype == 'float16'))
        else:
            self.scaler = torch.amp.GradScaler('cpu', enabled=(config.system.dtype == 'float16'))
        
        # Autocast context - use nullcontext for CPU to avoid overhead
        from contextlib import nullcontext
        dtype_map = {
            'float32': torch.float32,
            'bfloat16': torch.bfloat16,
            'float16': torch.float16
        }
        
        if self.device_type == 'cpu':
            # Use nullcontext for CPU to avoid autocast overhead
            self.autocast_ctx = nullcontext()
        else:
            # Use autocast for GPU training
            self.autocast_ctx = torch.amp.autocast(
                device_type=self.device_type,
                dtype=dtype_map[config.system.dtype]
            )
        
        logger.info("Trainer initialized successfully")
    
    def _setup_optimizer_and_scheduler(self) -> None:
        """Initialize optimizer and learning rate scheduler"""
        # Configure optimizer - use correct device_type
        self.optimizer = self.model.configure_optimizers(
            self.config.optimizer.weight_decay,
            self.config.optimizer.learning_rate,
            (self.config.optimizer.beta1, self.config.optimizer.beta2),
            self.device_type  # Use the device_type we computed
        )
        
        # Setup learning rate scheduler
        if self.config.scheduler.decay_lr:
            # Use custom learning rate scheduler
            self.lr_scheduler = CustomLRScheduler(
                self.optimizer,
                self.config.optimizer.learning_rate,
                self.config.scheduler.warmup_iters,
                self.config.scheduler.lr_decay_iters,
                self.config.scheduler.min_lr
            )
        else:
            self.lr_scheduler = None
    
    def _setup_data_loaders(self) -> None:
        """Initialize data loaders"""
        # Construct file paths
        train_file = os.path.join(
            self.config.data.data_dir,
            f"{self.config.data.dataset_name}-train.jsonl"
        )
        val_file = os.path.join(
            self.config.data.data_dir,
            f"{self.config.data.dataset_name}-val.jsonl"
        )
        
        print(f"{Constants.CYAN}Loading training dataset: {train_file}{Constants.ENDC}")
        print(f"{Constants.CYAN}Loading validation dataset: {val_file}{Constants.ENDC}")
        
        # Create packed data loaders for efficient training
        # This loader packs conversations tightly, achieving >98% efficiency
        # Note: batch_size = number of sequences per batch, block_size = sequence length (context window)
        train_batches = getattr(self.config.training, 'train_batches', None)
        val_batches = getattr(self.config.training, 'val_batches', None)
        
        if train_batches is None:
            print(f"  Using entire training dataset per epoch")
        else:
            print(f"  Limiting to {train_batches:,} training batches per epoch")
            
        if val_batches is None:
            print(f"  Using entire validation dataset per epoch")
        else:
            print(f"  Limiting to {val_batches:,} validation batches per epoch")
        
        print(f"{Constants.YELLOW}Creating packed data loaders...{Constants.ENDC}", end=" ", flush=True)
        
        self.train_loader, self.val_loader = create_simple_packed_loaders(
            train_file, val_file, self.tokenizer,
            self.config.training.batch_size,
            self.config.model.block_size,
            train_batches, val_batches
        )
        
        print(f"{Constants.GREEN}Done!{Constants.ENDC}")
        print(f"{Constants.GREEN}Training conversations loaded: {len(self.train_loader.conversations):,}{Constants.ENDC}")
        print(f"{Constants.GREEN}Validation conversations loaded: {len(self.val_loader.conversations):,}{Constants.ENDC}")
    
    def train_epoch(self) -> Dict[str, float]:
        """Train for one epoch"""
        self.model.train()
        epoch_start_time = time.time()
        epoch_loss = 0.0
        num_batches = 0
        
        # Create progress tracker - use estimated batches for iterable dataset
        estimated_batches = self.train_loader.estimated_batches
        progress_tracker = ProgressTracker(
            estimated_batches, self.epoch, self.config.training.max_epochs
        )
        
        # Training loop
        batch_idx = 0
        running_mfu = -1.0
        
        for batch_data in self.train_loader:
            if self.shutdown_handler.should_stop():
                logger.info("Graceful shutdown requested during training")
                break
            
            # Check max_iters termination condition
            if (self.config.training.max_iters is not None and 
                self.global_iter_num >= self.config.training.max_iters):
                logger.info(f"Reached max_iters ({self.config.training.max_iters}) during epoch")
                break
            
            batch_idx += 1
            batch_start_time = time.time()
            
            # Get batch data and optimize transfer for CUDA
            X, Y = batch_data
            
            # Optimize data transfer for CUDA devices
            if self.device_type == 'cuda' and self.config.system.pin_memory:
                X = X.pin_memory().to(self.device, non_blocking=True)
                Y = Y.pin_memory().to(self.device, non_blocking=True)
            else:
                X = X.to(self.device)
                Y = Y.to(self.device)
            
            # Forward pass with gradient accumulation
            total_loss = 0.0
            
            # Gradient accumulation loop
            for micro_step in range(self.config.training.gradient_accumulation_steps):
                with self.autocast_ctx:
                    logits, loss = self.model(X, Y)
                    loss = loss / self.config.training.gradient_accumulation_steps
                    total_loss += loss.item()
                
                # Backward pass
                self.scaler.scale(loss).backward()
            
            # Gradient clipping
            if self.config.optimizer.grad_clip > 0.0:
                self.scaler.unscale_(self.optimizer)
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), 
                    self.config.optimizer.grad_clip
                )
            
            # Optimizer step
            self.scaler.step(self.optimizer)
            self.scaler.update()
            self.optimizer.zero_grad(set_to_none=True)
            
            # Update learning rate with global iteration counter
            if self.lr_scheduler is not None:
                current_lr = self.lr_scheduler.step()
            else:
                current_lr = self.optimizer.param_groups[0]['lr']
            
            # Update global iteration counter
            self.global_iter_num += 1
            
            # Iteration-based evaluation (like legacy script)
            if (self.global_iter_num % 100 == 0 or 
                (self.config.training.max_iters is not None and 
                 self.global_iter_num >= self.config.training.max_iters)):
                
                print()  # New line for evaluation output
                eval_results = self.evaluate()
                
                # Log evaluation results
                print(f"\n{Constants.GREEN}Iteration {self.global_iter_num}: "
                      f"Train Loss: {eval_results['train']:.4f}{Constants.ENDC}  "
                      f"{Constants.MAGENTA}Val Loss: {eval_results['val']:.4f}{Constants.ENDC}")
                
                # Record evaluation metrics
                self.metrics.log_metric('train_loss_iter', eval_results['train'], self.global_iter_num)
                self.metrics.log_metric('val_loss_iter', eval_results['val'], self.global_iter_num)
            
            # Calculate metrics
            batch_time = time.time() - batch_start_time
            samples_per_sec = self.config.training.batch_size / batch_time if batch_time > 0 else 0
            
            # Update MFU (Model FLOPs Utilization)
            if self.batch_counter >= Constants.MFU_WARMUP_BATCHES:
                mfu = self.model.estimate_mfu(
                    self.config.training.batch_size * self.config.training.gradient_accumulation_steps,
                    batch_time
                )
                running_mfu = mfu if running_mfu == -1.0 else 0.9 * running_mfu + 0.1 * mfu
            
            # Track metrics - Log training loss at every batch for plotting
            self.metrics.log_metric('train_loss_batch', total_loss, self.batch_counter)
            
            # Log additional metrics at specified intervals to avoid overwhelming logs
            if self.config.training.log_interval > 0 and self.batch_counter % self.config.training.log_interval == 0:
                self.metrics.log_metric('learning_rate', current_lr, self.batch_counter)
                self.metrics.log_metric('samples_per_sec', samples_per_sec, self.batch_counter)
                if running_mfu > 0:
                    self.metrics.log_metric('mfu', running_mfu, self.batch_counter)
                
                # Generate plot every log_interval batches
                #print(f"{Constants.CYAN}Generating plot at batch {self.batch_counter} (log_interval={self.config.training.log_interval}){Constants.ENDC}")
                self._generate_training_plot(f"Training Progress - Batch {self.batch_counter}")
            
            # Save checkpoint at specified batch intervals (if configured)
            if (self.config.training.checkpoint_interval > 0 and 
                self.config.training.save_checkpoints and
                self.batch_counter % self.config.training.checkpoint_interval == 0):
                
                # Use output_checkpoint if provided, otherwise use default naming
                if self.output_checkpoint:
                    checkpoint_path = self.output_checkpoint
                else:
                    # Default naming scheme
                    checkpoint_path = f"models/{self.config.data.dataset_name}_batch{self.batch_counter}.pt"

                print(f"\n{Constants.CYAN}Saving checkpoint at batch {self.batch_counter}... {checkpoint_path}{Constants.ENDC}")
                
                self.save_checkpoint(checkpoint_path, is_best=False)
                print()  # Add newline after checkpoint save
            
            # Update running totals
            epoch_loss += total_loss
            num_batches += 1
            self.batch_counter += 1
            
            # Real-time progress display
            progress_line = progress_tracker.update(
                batch_idx, total_loss, current_lr, samples_per_sec, running_mfu
            )
            print(f"{progress_line}", flush=True)
            
            # Periodic evaluation during epoch
            if (self.config.training.eval_interval > 0 and 
                batch_idx % self.config.training.eval_interval == 0):
                
                print()  # New line for evaluation output
                eval_results = self.evaluate()
                
                # Log evaluation results
                print(f"\n{Constants.GREEN}Epoch {self.epoch+1} "
                      f"({batch_idx/estimated_batches*100:.1f}%): "
                      f"Train Loss: {eval_results['train']:.4f}{Constants.ENDC}  "
                      f"{Constants.MAGENTA}Val Loss: {eval_results['val']:.4f}{Constants.ENDC}")
                
                # Track best and worst validation loss
                if eval_results['val'] < self.best_val_loss:
                    self.best_val_loss = eval_results['val']
                if eval_results['val'] > self.worst_val_loss:
                    self.worst_val_loss = eval_results['val']
                
                # Track best and worst training loss
                if eval_results['train'] < self.best_train_loss:
                    self.best_train_loss = eval_results['train']
                if eval_results['train'] > self.worst_train_loss:
                    self.worst_train_loss = eval_results['train']
                
                # Record evaluation metrics - Log at every evaluation for plotting
                self.metrics.log_metric('train_loss_eval', eval_results['train'], self.batch_counter)
                self.metrics.log_metric('val_loss_eval', eval_results['val'], self.batch_counter)
        
        # Epoch completion
        print()  # New line after progress bar
        epoch_duration = time.time() - epoch_start_time
        avg_epoch_loss = epoch_loss / num_batches if num_batches > 0 else 0.0
        
        # Display epoch completion summary
        summary = progress_tracker.completion_summary(avg_epoch_loss, epoch_duration)
        print(summary)
        
        return {
            'avg_loss': avg_epoch_loss,
            'duration': epoch_duration,
            'num_batches': num_batches
        }
    
    @torch.no_grad()
    def evaluate(self) -> Dict[str, float]:
        """Evaluate model on train and validation sets"""
        self.model.eval()
        
        results = {}
        
        for split_name, loader in [('train', self.train_loader), ('val', self.val_loader)]:
            losses = []
            
            # Calculate appropriate number of evaluation batches for iterable datasets
            if split_name == 'train':
                max_batches = self.train_loader.estimated_batches
            else:
                max_batches = self.val_loader.estimated_batches
                
            eval_batches = min(
                self.config.training.eval_iters // self.config.training.batch_size,
                max_batches
            )
            eval_batches = max(10, eval_batches)  # At least 10 batches
            
            for i, (X, Y) in enumerate(loader):
                if i >= eval_batches:
                    break
                
                # Optimize data transfer for CUDA devices
                if self.device_type == 'cuda' and self.config.system.pin_memory:
                    X = X.pin_memory().to(self.device, non_blocking=True)
                    Y = Y.pin_memory().to(self.device, non_blocking=True)
                else:
                    X = X.to(self.device)
                    Y = Y.to(self.device)
                
                with self.autocast_ctx:
                    logits, loss = self.model(X, Y)
                    losses.append(loss.item())
            
            results[split_name] = sum(losses) / len(losses) if losses else float('inf')
        
        self.model.train()
        return results
    
    def save_checkpoint(self, filepath: str, is_best: bool = False) -> bool:
        """Save training checkpoint"""
        # Prepare checkpoint data
        checkpoint_data = {
            'model': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'lr_scheduler': self.lr_scheduler.state_dict() if self.lr_scheduler else None,
            'epoch': self.epoch,
            'batch_counter': self.batch_counter,
            'global_iter_num': self.global_iter_num,
            'best_val_loss': self.best_val_loss,
            'worst_val_loss': self.worst_val_loss,
            'best_train_loss': self.best_train_loss,
            'worst_train_loss': self.worst_train_loss,
            'config': self.config.to_dict(),
            'metrics': dict(self.metrics.metrics),
            'metadata': CheckpointManager.create_checkpoint_metadata(self.config)
        }
        
        # Save checkpoint atomically
        success = CheckpointManager.save_checkpoint_atomic(checkpoint_data, filepath)
        
        if success:
            logger.info(f"Checkpoint saved: {filepath}")
            
            # Create loss curve plot
            self.plot_loss_curves(filepath)
            
            if is_best:
                # Also save as best model
                best_path = filepath.replace('.pt', '_best.pt')
                CheckpointManager.save_checkpoint_atomic(checkpoint_data, best_path)
                logger.info(f"Best model saved: {best_path}")
                # Create plot for best model too
                self.plot_loss_curves(best_path)
        else:
            logger.error(f"Failed to save checkpoint: {filepath}")
        
        return success
    
    def load_checkpoint(self, filepath: str, resume_training: bool = True) -> bool:
        """Load training checkpoint"""
        try:
            checkpoint = torch.load(filepath, map_location=self.device, weights_only=False)
            
            # Load model state
            self.model.load_state_dict(checkpoint['model'])
            
            if resume_training:
                # Load training state
                self.optimizer.load_state_dict(checkpoint['optimizer'])
                if self.lr_scheduler and checkpoint.get('lr_scheduler'):
                    self.lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
                
                self.epoch = checkpoint.get('epoch', 0)
                self.batch_counter = checkpoint.get('batch_counter', 0)
                self.global_iter_num = checkpoint.get('global_iter_num', 0)
                self.best_val_loss = checkpoint.get('best_val_loss', float('inf'))
                self.worst_val_loss = checkpoint.get('worst_val_loss', float('-inf'))
                self.best_train_loss = checkpoint.get('best_train_loss', float('inf'))
                self.worst_train_loss = checkpoint.get('worst_train_loss', float('-inf'))
                
                # Load metrics if available
                if 'metrics' in checkpoint:
                    for name, data in checkpoint['metrics'].items():
                        self.metrics.metrics[name] = data
            
            logger.info(f"Checkpoint loaded: {filepath}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load checkpoint {filepath}: {e}")
            return False
    
    def train(self, checkpoint_path: Optional[str] = None, input_checkpoint: Optional[str] = None) -> Dict[str, Any]:
        """Main training loop"""
        
        # Training setup
        start_time = time.time()
        checkpoint_path = checkpoint_path or f"models/{self.config.data.dataset_name}_epoch{self.config.training.max_epochs}.pt"
        
        # Print comprehensive training summary
        self.print_training_summary(checkpoint_path, input_checkpoint)
        
        logger.info(f"Starting training for {self.config.training.max_epochs} epochs")
        logger.info(f"Model parameters: {count_parameters(self.model):,}")
        logger.info(f"Estimated batches per epoch: {self.train_loader.estimated_batches:,}")
        
        try:
            # Training loop
            while self.epoch < self.config.training.max_epochs:
                if self.shutdown_handler.should_stop():
                    logger.info("Graceful shutdown requested")
                    break
                
                # Check max_iters termination condition (like legacy script)
                if (self.config.training.max_iters is not None and 
                    self.global_iter_num >= self.config.training.max_iters):
                    logger.info(f"Reached max_iters ({self.config.training.max_iters}), stopping training")
                    break
                
                # Print epoch header
                self._print_epoch_header()
                
                # Train one epoch
                epoch_results = self.train_epoch()
                
                # End-of-epoch evaluation
                eval_results = self.evaluate()
                
                # Log results
                print(f"{Constants.GREEN}Train Loss: {eval_results['train']:.4f}{Constants.ENDC}  "
                      f"{Constants.MAGENTA}Val Loss: {eval_results['val']:.4f}{Constants.ENDC}")
                print(f"{Constants.YELLOW}Progress: {self.epoch+1}/{self.config.training.max_epochs} epochs "
                      f"({(self.epoch+1)/self.config.training.max_epochs*100:.1f}%){Constants.ENDC}\n")
                
                # Track metrics
                self.metrics.log_metric('train_loss_epoch', eval_results['train'], self.epoch)
                self.metrics.log_metric('val_loss_epoch', eval_results['val'], self.epoch)
                
                # Also log with standard names for plotting
                self.metrics.log_metric('train_loss', eval_results['train'], self.epoch)
                self.metrics.log_metric('val_loss', eval_results['val'], self.epoch)
                
                # Check if this is the best model and update all metrics
                is_best = eval_results['val'] < self.best_val_loss
                if is_best:
                    self.best_val_loss = eval_results['val']
                if eval_results['val'] > self.worst_val_loss:
                    self.worst_val_loss = eval_results['val']
                
                # Update train loss tracking
                if eval_results['train'] < self.best_train_loss:
                    self.best_train_loss = eval_results['train']
                if eval_results['train'] > self.worst_train_loss:
                    self.worst_train_loss = eval_results['train']
                
                # Save checkpoint
                if self.config.training.save_checkpoints:
                    self.save_checkpoint(checkpoint_path, is_best=is_best)
                
                # Clear GPU cache
                if 'cuda' in self.config.system.device:
                    torch.cuda.empty_cache()
                
                # Move to next epoch
                self.epoch += 1
            
            # Training completion
            total_time = time.time() - start_time
            
            # Final evaluation
            final_eval = self.evaluate()
            
            # Update final metrics (in case this final evaluation is the best/worst)
            if final_eval['val'] < self.best_val_loss:
                self.best_val_loss = final_eval['val']
            if final_eval['val'] > self.worst_val_loss:
                self.worst_val_loss = final_eval['val']
            if final_eval['train'] < self.best_train_loss:
                self.best_train_loss = final_eval['train']
            if final_eval['train'] > self.worst_train_loss:
                self.worst_train_loss = final_eval['train']
            
            logger.info(f"Training completed in {format_time_delta(total_time)}")
            logger.info(f"Final train loss: {final_eval['train']:.4f}")
            logger.info(f"Final validation loss: {final_eval['val']:.4f}")
            logger.info(f"Best train loss: {self.best_train_loss:.4f}")
            logger.info(f"Worst train loss: {self.worst_train_loss:.4f}")
            logger.info(f"Best validation loss: {self.best_val_loss:.4f}")
            logger.info(f"Worst validation loss: {self.worst_val_loss:.4f}")
            
            return {
                'success': True,
                'final_train_loss': final_eval['train'],
                'final_val_loss': final_eval['val'],
                'best_train_loss': self.best_train_loss,
                'worst_train_loss': self.worst_train_loss,
                'best_val_loss': self.best_val_loss,
                'worst_val_loss': self.worst_val_loss,
                'total_time': total_time,
                'epochs_completed': self.epoch
            }
            
        except Exception as e:
            logger.error(f"Training failed: {e}")
            
            # Save emergency checkpoint
            if self.config.training.save_checkpoints:
                emergency_path = checkpoint_path.replace('.pt', '_emergency.pt')
                self.save_checkpoint(emergency_path)
            
            return {
                'success': False,
                'error': str(e),
                'epochs_completed': self.epoch
            }
    
    def _print_epoch_header(self) -> None:
        """Print epoch header"""
        header_length = 42
        epoch_text = f"EPOCH {self.epoch+1} OF {self.config.training.max_epochs}"
        padding = " " * ((header_length - len(epoch_text)) // 2)
        right_padding = " " * (header_length - len(epoch_text) - len(padding))
        
        print(f"\n{Constants.BOLD}{Constants.BLUE}╔══════════════════════════════════════════╗{Constants.ENDC}")
        print(f"{Constants.BOLD}{Constants.BLUE}║{padding}{epoch_text}{right_padding}║{Constants.ENDC}")
        print(f"{Constants.BOLD}{Constants.BLUE}╚══════════════════════════════════════════╝{Constants.ENDC}\n")
    
    def plot_loss_curves(self, checkpoint_path: str) -> None:
        """Generate and save loss curve plots"""
        try:
            # Create plot filename
            plot_path = checkpoint_path.replace('.pt', '.png')
            
            # Generate plot title
            dataset_name = self.config.data.dataset_name
            title = f"Training Progress - {dataset_name} (Epoch {self.epoch+1})"
            
            # Generate the plot
            success = PlotManager.plot_training_curves(self.metrics, plot_path, title)
            
            if success:
                logger.info(f"Loss curve plot saved: {plot_path}")
            else:
                logger.warning("Could not generate loss curve plot")
                
        except Exception as e:
            logger.warning(f"Error generating loss curve plot: {e}")
    
    def _generate_training_plot(self, title: str) -> None:
        """Generate training plot during training (not just at checkpoints)"""
        try:
            # Create plot filename using same pattern as checkpoints
            if self.output_checkpoint:
                # Use the output checkpoint filename as base
                plot_path = self.output_checkpoint.replace('.pt', '.png')
            else:
                # Use default naming scheme similar to checkpoints
                plot_path = f"models/{self.config.data.dataset_name}_epoch{self.epoch+1}.png"
            
            # Generate the plot
            from utils import PlotManager
            success = PlotManager.plot_training_curves(self.metrics, plot_path, title)
            
            if success:
                logger.info(f"Training plot saved: {plot_path}")
            else:
                logger.warning("Could not generate training plot")
                
        except Exception as e:
            logger.warning(f"Error generating training plot: {e}")
    
    def print_training_summary(self, checkpoint_path: str, input_checkpoint: Optional[str] = None) -> None:
        """Print comprehensive training summary before starting training"""
        
        # Calculate model size
        total_params = count_parameters(self.model)
        trainable_params = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        
        # Calculate dataset info for packed loaders
        train_conversations = len(self.train_loader.conversations)
        val_conversations = len(self.val_loader.conversations)
        
        # Estimate tokens for packed datasets
        avg_tokens_per_conv = self.config.model.block_size * 0.8  # Rough estimate
        train_tokens = int(train_conversations * avg_tokens_per_conv)
        val_tokens = int(val_conversations * avg_tokens_per_conv)
        
        # Calculate training volume
        batches_per_epoch = self.train_loader.estimated_batches
        total_batches = batches_per_epoch * self.config.training.max_epochs
        total_training_tokens = batches_per_epoch * self.config.training.batch_size * self.config.model.block_size * self.config.training.max_epochs
        
        # Calculate tokens per iteration (matches legacy script)
        tokens_per_iter = (self.config.training.gradient_accumulation_steps * 
                          self.config.training.batch_size * 
                          self.config.model.block_size)
        
        print(f"\n{Constants.BOLD}{Constants.YELLOW}Tokens per iteration will be: {tokens_per_iter:,}{Constants.ENDC}")
        print()
        
        # Print comprehensive summary
        print(f"\n{Constants.BOLD}{Constants.BLUE}╔═══════════════════════════════════════════════════════╗{Constants.ENDC}")
        print(f"{Constants.BOLD}{Constants.BLUE}║                  TRAINING SUMMARY                     ║{Constants.ENDC}")
        print(f"{Constants.BOLD}{Constants.BLUE}╚═══════════════════════════════════════════════════════╝{Constants.ENDC}")
        
        # Model Information
        print(f"{Constants.BOLD}{Constants.CYAN}📊 Model Configuration:{Constants.ENDC}")
        print(f"   Architecture:       {Constants.GREEN}GPT-{self.config.model.n_layer}L-{self.config.model.n_head}H-{self.config.model.n_embd}D{Constants.ENDC}")
        print(f"   Total Parameters:   {Constants.GREEN}{total_params:,}{Constants.ENDC} ({total_params/1e6:.1f}M)")
        print(f"   Trainable Params:   {Constants.GREEN}{trainable_params:,}{Constants.ENDC} ({trainable_params/1e6:.1f}M)")
        print(f"   Context Length:     {Constants.GREEN}{self.config.model.block_size:,} tokens{Constants.ENDC}")
        print(f"   Vocabulary Size:    {Constants.GREEN}{self.config.model.vocab_size:,}{Constants.ENDC}")
        
        # Model layers (like in legacy script)
        print(f"{Constants.BOLD}   Model Layers:{Constants.ENDC}")
        for number, (name, param) in enumerate(self.model.named_parameters()):
            if number < 10:  # Show first 10 layers to avoid overwhelming output
                print(f"     {number}: {name}")
            elif number == 10:
                print(f"     ... ({total_params//1000}K more parameters)")
                break
        print()
        
        # Dataset Information
        print(f"{Constants.BOLD}{Constants.CYAN}📚 Dataset Information:{Constants.ENDC}")
        print(f"   Dataset Name:       {Constants.GREEN}{self.config.data.dataset_name}{Constants.ENDC}")
        print(f"   Training Set:       {Constants.GREEN}{train_conversations:,} conversations{Constants.ENDC} ({train_tokens:,} tokens)")
        print(f"   Validation Set:     {Constants.GREEN}{val_conversations:,} conversations{Constants.ENDC} ({val_tokens:,} tokens)")
        print(f"   Total Dataset:      {Constants.GREEN}{train_conversations + val_conversations:,} conversations{Constants.ENDC} ({train_tokens + val_tokens:,} tokens)")
        print()
        
        # Training Schedule
        print(f"{Constants.BOLD}{Constants.CYAN}🚀 Training Schedule:{Constants.ENDC}")
        print(f"   Epochs to Train:    {Constants.GREEN}{self.config.training.max_epochs}{Constants.ENDC}")
        print(f"   Batches per Epoch:  {Constants.GREEN}{batches_per_epoch:,}{Constants.ENDC}")
        print(f"   Total Batches:      {Constants.GREEN}{total_batches:,}{Constants.ENDC}")
        print(f"   Batch Size:         {Constants.GREEN}{self.config.training.batch_size}{Constants.ENDC}")
        print(f"   Gradient Accum:     {Constants.GREEN}{self.config.training.gradient_accumulation_steps}{Constants.ENDC}")
        print(f"   Effective Batch:    {Constants.GREEN}{self.config.training.batch_size * self.config.training.gradient_accumulation_steps}{Constants.ENDC}")
        print(f"   Training Tokens:    {Constants.GREEN}{total_training_tokens:,}{Constants.ENDC}")
        print()
        
        # Checkpoint Information
        print(f"{Constants.BOLD}{Constants.CYAN}💾 Checkpoint Configuration:{Constants.ENDC}")
        if input_checkpoint:
            print(f"   Input Checkpoint:   {Constants.GREEN}{input_checkpoint}{Constants.ENDC}")
            print(f"   Resume Training:    {Constants.GREEN}Yes{Constants.ENDC} (from epoch {self.epoch + 1})")
        else:
            print(f"   Input Checkpoint:   {Constants.YELLOW}None - Training from scratch{Constants.ENDC}")
        print(f"   Output Checkpoint:  {Constants.GREEN}{checkpoint_path}{Constants.ENDC}")
        if self.config.training.checkpoint_interval > 0:
            print(f"   Save Interval:      {Constants.GREEN}Every {self.config.training.checkpoint_interval} batches{Constants.ENDC}")
        else:
            print(f"   Save Interval:      {Constants.GREEN}End of each epoch only{Constants.ENDC}")
        print()
        
        # Training Configuration
        print(f"{Constants.BOLD}{Constants.CYAN}⚙️  Training Configuration:{Constants.ENDC}")
        print(f"   Learning Rate:      {Constants.GREEN}{self.config.optimizer.learning_rate:.1e}{Constants.ENDC}")
        print(f"   Weight Decay:       {Constants.GREEN}{self.config.optimizer.weight_decay}{Constants.ENDC}")
        print(f"   Gradient Clipping:  {Constants.GREEN}{self.config.optimizer.grad_clip}{Constants.ENDC}")
        print(f"   Device:             {Constants.GREEN}{self.config.system.device}{Constants.ENDC}")
        print(f"   Precision:          {Constants.GREEN}{self.config.system.dtype}{Constants.ENDC}")
        print(f"   Model Compilation:  {Constants.GREEN}{'Enabled' if self.config.training.compile_model else 'Disabled'}{Constants.ENDC}")
        print()
        
        # Evaluation Configuration
        print(f"{Constants.BOLD}{Constants.CYAN}📈 Monitoring Configuration:{Constants.ENDC}")
        print(f"   Eval Interval:      {Constants.GREEN}Every {self.config.training.eval_interval} batches{Constants.ENDC}")
        print(f"   Log Interval:       {Constants.GREEN}Every {self.config.training.log_interval} batches{Constants.ENDC}")
        print(f"   Eval Iterations:    {Constants.GREEN}{self.config.training.eval_iters}{Constants.ENDC}")
        print()
        
        print(f"{Constants.BOLD}{Constants.GREEN}Ready to begin training!{Constants.ENDC}")
        print(f"{Constants.YELLOW}{'='*55}{Constants.ENDC}\n")
