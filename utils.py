#!/usr/bin/python3
"""
Utility classes and functions for Jojo LLM Training

This module provides utility classes for progress tracking, metrics collection,
tensor management, and other common training utilities.

Author: Jason A. Cox
2025 July 4
https://github.com/jasonacox/jojo
"""

import time
import datetime
import logging
from collections import defaultdict
from typing import Optional, Dict, List, Tuple, Any
import torch
from config import Constants


class ProgressTracker:
    """Real-time progress tracking for training"""
    
    def __init__(self, total_batches: int, epoch: int, max_epochs: int):
        self.total_batches = total_batches
        self.epoch = epoch
        self.max_epochs = max_epochs
        self.start_time = time.time()
        
    def update(self, batch_idx: int, loss: float, lr: float, 
               samples_per_sec: float, mfu: Optional[float] = None) -> str:
        """Update progress and return formatted progress string"""
        
        # Calculate progress percentage
        progress = batch_idx / self.total_batches * 100
        bar_length = Constants.PROGRESS_BAR_LENGTH
        bar_filled = int(bar_length * batch_idx / self.total_batches)
        progress_bar = '█' * bar_filled + '░' * (bar_length - bar_filled)
        
        # Calculate ETA
        if batch_idx > 0:
            time_per_batch = (time.time() - self.start_time) / batch_idx
            eta_seconds = time_per_batch * (self.total_batches - batch_idx)
            eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
        else:
            eta_str = "N/A"
        
        # Build progress line
        progress_line = (
            f"{Constants.YELLOW}[{progress_bar}]{Constants.ENDC} "
            f"{Constants.BOLD}Epoch {self.epoch+1}/{self.max_epochs}{Constants.ENDC} | "
            f"{Constants.CYAN}Batch {batch_idx}/{self.total_batches}{Constants.ENDC} | "
            f"{Constants.MAGENTA}{progress:.1f}%{Constants.ENDC} | "
            f"Loss: {loss:.4f} | "
            f"LR: {lr:.2e} | "
            f"{Constants.GREEN}ETA: {eta_str}{Constants.ENDC} | "
            f"Samples/s: {samples_per_sec:.1f}"
        )
        
        if mfu is not None and mfu > 0:
            progress_line += f" | MFU: {mfu*100:.1f}%"
            
        return progress_line
    
    def completion_summary(self, avg_loss: float, duration: float) -> str:
        """Generate epoch completion summary"""
        progress_bar = '█' * Constants.PROGRESS_BAR_LENGTH
        duration_str = str(datetime.timedelta(seconds=int(duration)))
        
        summary = (
            f"{Constants.YELLOW}[{progress_bar}]{Constants.ENDC} "
            f"{Constants.BOLD}Epoch {self.epoch+1}/{self.max_epochs}{Constants.ENDC} | "
            f"{Constants.CYAN}Batch {self.total_batches}/{self.total_batches}{Constants.ENDC} | "
            f"{Constants.MAGENTA}100.0%{Constants.ENDC} | "
            f"Loss: {avg_loss:.4f} | "
            f"{Constants.GREEN}Complete!{Constants.ENDC}\n\n"
            f"{Constants.BOLD}{Constants.GREEN}==== Epoch {self.epoch+1}/{self.max_epochs} Complete ===={Constants.ENDC}\n"
            f"{Constants.CYAN}Duration: {duration_str}{Constants.ENDC}\n"
            f"{Constants.CYAN}Average Loss: {avg_loss:.4f}{Constants.ENDC}"
        )
        
        return summary


class MetricsTracker:
    """Track and manage training metrics"""
    
    def __init__(self):
        self.metrics = defaultdict(list)
        self.step_counters = defaultdict(int)
        
    def log_metric(self, name: str, value: float, step: Optional[int] = None) -> None:
        """Log a metric value"""
        if step is None:
            step = self.step_counters[name]
            self.step_counters[name] += 1
        
        self.metrics[name].append((step, value))
        #print(f"{Constants.YELLOW}Logged metric: {name} at step {step} with value {value}{Constants.ENDC}")
        
    def get_metric_history(self, name: str) -> List[Tuple[int, float]]:
        """Get full history of a metric"""
        return self.metrics[name]
    
    def get_latest_metric(self, name: str) -> Optional[float]:
        """Get the latest value of a metric"""
        if name in self.metrics and self.metrics[name]:
            return self.metrics[name][-1][1]
        return None
    
    def get_best_metric(self, name: str, minimize: bool = True) -> Optional[float]:
        """Get the best value of a metric"""
        if name not in self.metrics or not self.metrics[name]:
            return None
            
        values = [value for _, value in self.metrics[name]]
        return min(values) if minimize else max(values)
    
    def get_metric_stats(self, name: str) -> Dict[str, float]:
        """Get statistics for a metric"""
        if name not in self.metrics or not self.metrics[name]:
            return {}
            
        values = [value for _, value in self.metrics[name]]
        return {
            'min': min(values),
            'max': max(values),
            'mean': sum(values) / len(values),
            'count': len(values),
            'latest': values[-1]
        }
    
    def get_steps_and_values(self, name: str) -> Tuple[List[int], List[float]]:
        """Get steps and values for plotting"""
        if name not in self.metrics:
            return [], []
            
        data = self.metrics[name]
        steps = [step for step, _ in data]
        values = [value for _, value in data]
        return steps, values
    
    def get_metric_values(self, name: str) -> List[float]:
        """Get metric values only (for plotting)"""
        if name not in self.metrics or not self.metrics[name]:
            return []
        return [value for _, value in self.metrics[name]]


class TensorBuffer:
    """Reusable tensor buffers to reduce memory allocation"""
    
    def __init__(self, batch_size: int, block_size: int, device: torch.device):
        self.batch_size = batch_size
        self.block_size = block_size
        self.device = device
        
        # Pre-allocate buffers
        self.x_buffer = torch.zeros((batch_size, block_size), dtype=torch.long, device=device)
        self.y_buffer = torch.zeros((batch_size, block_size), dtype=torch.long, device=device)
    
    def get_buffers(self) -> Tuple[torch.Tensor, torch.Tensor]:
        """Get clean tensor buffers"""
        # Zero out the buffers (faster than creating new tensors)
        self.x_buffer.zero_()
        self.y_buffer.zero_()
        return self.x_buffer, self.y_buffer
    
    def resize_if_needed(self, new_batch_size: int, new_block_size: int) -> None:
        """Resize buffers if needed"""
        if new_batch_size != self.batch_size or new_block_size != self.block_size:
            self.batch_size = new_batch_size
            self.block_size = new_block_size
            self.x_buffer = torch.zeros((new_batch_size, new_block_size), dtype=torch.long, device=self.device)
            self.y_buffer = torch.zeros((new_batch_size, new_block_size), dtype=torch.long, device=self.device)


class GracefulShutdown:
    """Handle graceful shutdown on interruption"""
    
    def __init__(self):
        self.shutdown_requested = False
        import signal
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.shutdown_requested = True
        print(f"\n{Constants.YELLOW}Graceful shutdown requested... Saving checkpoint...{Constants.ENDC}")
    
    def should_stop(self) -> bool:
        """Check if shutdown was requested"""
        return self.shutdown_requested


class DeviceManager:
    """Manage device selection and memory optimization"""
    
    @staticmethod
    def select_best_device() -> str:
        """Automatically select the best available device"""
        if not torch.cuda.is_available():
            return 'cpu'
        
        # Find device with most free memory
        device_free_memory = []
        max_free_memory = 0
        best_device = 0
        
        print("CUDA devices available:")
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            mem_free = torch.cuda.mem_get_info(i)[0] / (1024 ** 3)  # Free memory in GB
            mem_total = props.total_memory / (1024 ** 3)  # Total memory in GB
            device_free_memory.append(mem_free)
            
            # Track device with most free memory
            if mem_free > max_free_memory:
                max_free_memory = mem_free
                best_device = i
                
            print(f"  [{i}] {props.name} - Free: {mem_free:.2f} GB / Total: {mem_total:.2f} GB")
        
        return f'cuda:{best_device}'
    
    @staticmethod
    def optimize_memory(device: str, memory_fraction: float = 0.9) -> None:
        """Optimize memory usage for the given device"""
        if device.startswith('cuda'):
            torch.cuda.empty_cache()
            # Set memory fraction to prevent OOM
            torch.cuda.set_per_process_memory_fraction(memory_fraction)
            print(f"{Constants.GREEN}CUDA memory optimization enabled: {memory_fraction*100:.0f}% memory fraction{Constants.ENDC}")


class CheckpointManager:
    """Manage checkpoint saving and loading with metadata"""
    
    @staticmethod
    def create_checkpoint_metadata(config: Any, git_commit: Optional[str] = None) -> Dict[str, Any]:
        """Create comprehensive checkpoint metadata"""
        from config import Constants
        return {
            'trainer_version': Constants.VERSION,
            'trainer_version_name': Constants.VERSION_NAME,
            'pytorch_version': torch.__version__,
            'timestamp': datetime.datetime.now().isoformat(),
            'config': config.to_dict() if hasattr(config, 'to_dict') else str(config),
            'git_commit': git_commit or CheckpointManager._get_git_commit(),
        }
    
    @staticmethod
    def _get_git_commit() -> Optional[str]:
        """Get current git commit hash"""
        try:
            import subprocess
            result = subprocess.run(['git', 'rev-parse', 'HEAD'], 
                                  capture_output=True, text=True, cwd='.')
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
    
    @staticmethod
    def save_checkpoint_atomic(checkpoint_data: Dict[str, Any], filepath: str) -> bool:
        """Save checkpoint atomically to prevent corruption"""
        import os
        
        temp_filepath = filepath + Constants.CHECKPOINT_TEMP_SUFFIX
        
        try:
            # Save to temporary file first
            torch.save(checkpoint_data, temp_filepath)
            
            # Atomic rename (on Unix systems)
            if os.name == 'posix':
                os.replace(temp_filepath, filepath)
            else:
                # On Windows, remove destination first (not atomic)
                if os.path.exists(filepath):
                    os.remove(filepath)
                os.rename(temp_filepath, filepath)
            
            return True
            
        except Exception as e:
            logging.error(f"Failed to save checkpoint: {e}")
            # Clean up temp file if it exists
            try:
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
            except:
                pass
            return False


class PlotManager:
    """Manage training plots and visualizations"""
    
    @staticmethod
    def plot_training_curves(metrics: 'MetricsTracker', save_path: str, title: str = "Training Progress") -> bool:
        """Plot training and validation loss curves and save as PNG"""
        try:
            import matplotlib
            matplotlib.use('Agg')  # Use non-interactive backend
            import matplotlib.pyplot as plt
            
            # Get loss data (prioritize batch data for real-time training visualization)
            # For training loss: prefer batch data > evaluation data > epoch data
            train_losses = None
            batch_losses = metrics.get_metric_values('train_loss_batch')
            if batch_losses and len(batch_losses) > 10:  # Use batch data if we have enough points
                # For frequent plotting during training, use all batch data (no smoothing needed for real-time view)
                train_losses = batch_losses
            else:
                # Fallback to evaluation metrics if batch data is insufficient
                train_losses = metrics.get_metric_values('train_loss_eval')
                if not train_losses:
                    epoch_losses = (metrics.get_metric_values('train_loss_epoch') or 
                                   metrics.get_metric_values('train_loss'))
                    train_losses = epoch_losses or batch_losses  # Use whatever we have
            
            # For validation loss: prefer evaluation data
            val_losses = metrics.get_metric_values('val_loss_eval')
            if not val_losses:
                val_losses = (metrics.get_metric_values('val_loss_epoch') or 
                             metrics.get_metric_values('val_loss'))
            
            if not train_losses and not val_losses:
                return False
            
            # Create figure with subplots
            fig, axes = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [3, 1]})
            
            # Plot loss curves on top subplot
            ax1 = axes[0]
            
            if train_losses:
                train_steps = list(range(len(train_losses)))
                marker_style = '.' if len(train_losses) > 20 else 'o'
                ax1.plot(train_steps, train_losses, label='Train Loss', color='blue', 
                        marker=marker_style, alpha=0.7)
                
                # Add min value annotation
                min_train_idx = train_losses.index(min(train_losses))
                min_train_loss = train_losses[min_train_idx]
                ax1.annotate(f'Min Train: {min_train_loss:.4f}', 
                           (min_train_idx, min_train_loss),
                           xytext=(10, -20),
                           textcoords='offset points',
                           arrowprops=dict(arrowstyle='->', color='blue', alpha=0.7))
            
            if val_losses:
                # If we have fewer validation points than training points, spread them across the same x-range
                if train_losses and len(val_losses) < len(train_losses):
                    # Scale validation points to match training timeline
                    max_train_step = len(train_losses) - 1
                    if len(val_losses) == 1:
                        val_steps = [max_train_step]  # Put single val point at the end
                    else:
                        val_steps = [int(i * max_train_step / (len(val_losses) - 1)) for i in range(len(val_losses))]
                else:
                    val_steps = list(range(len(val_losses)))
                
                marker_style = '.' if len(val_losses) > 20 else 's'
                ax1.plot(val_steps, val_losses, label='Val Loss', color='orange', 
                        marker=marker_style, alpha=0.7, markersize=8)
                
                # Add min value annotation
                min_val_idx = val_losses.index(min(val_losses))
                min_val_loss = val_losses[min_val_idx]
                actual_x = val_steps[min_val_idx]
                ax1.annotate(f'Min Val: {min_val_loss:.4f}', 
                           (actual_x, min_val_loss),
                           xytext=(10, 20),
                           textcoords='offset points',
                           arrowprops=dict(arrowstyle='->', color='orange', alpha=0.7))
            
            # Set appropriate labels based on data type
            batch_losses = metrics.get_metric_values('train_loss_batch')
            if train_losses and train_losses == batch_losses and len(train_losses) > 10:
                ax1.set_xlabel('Batch Number')
            elif train_losses and len(train_losses) > 50:
                ax1.set_xlabel('Training Step')
            else:
                ax1.set_xlabel('Step')
            ax1.set_ylabel('Loss')
            ax1.set_title(title)
            ax1.legend(loc='upper right')
            ax1.grid(True, linestyle='--', alpha=0.5)
            
            # Plot learning rate on bottom subplot if available
            ax2 = axes[1]
            lr_values = metrics.get_metric_values('learning_rate')
            
            if lr_values:
                lr_steps = list(range(len(lr_values)))
                ax2.plot(lr_steps, lr_values, color='green', marker='.', alpha=0.7)
                ax2.set_xlabel('Step')
                ax2.set_ylabel('Learning Rate')
                ax2.set_title('Learning Rate Schedule')
                ax2.grid(True, linestyle='--', alpha=0.5)
                ax2.ticklabel_format(axis='y', style='sci', scilimits=(0,0))
            else:
                ax2.text(0.5, 0.5, 'No learning rate data available', 
                        ha='center', va='center', transform=ax2.transAxes)
                ax2.set_xlabel('Step')
                ax2.set_ylabel('Learning Rate')
                ax2.set_title('Learning Rate Schedule')
            
            # Add timestamp
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            plt.figtext(0.5, 0.01, f"Generated: {timestamp}", ha="center", fontsize=8, 
                       bbox={"facecolor":"white", "alpha":0.5, "pad":5})
            
            plt.tight_layout()
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            plt.close(fig)
            
            return True
            
        except ImportError:
            logging.warning("Matplotlib not available. Cannot generate plots.")
            return False
        except Exception as e:
            logging.error(f"Error generating plot: {e}")
            return False


class Logger:
    """Enhanced logging utility"""
    
    @staticmethod
    def setup_logging(debug: bool = False, log_file: str = Constants.LOG_FILE) -> None:
        """Set up logging configuration"""
        log_level = logging.DEBUG if debug else logging.INFO
        
        # Create formatter
        formatter = logging.Formatter(
            f'{Constants.BLUE}%(asctime)s{Constants.ENDC} '
            f'{Constants.GREEN}%(levelname)s:{Constants.ENDC} %(message)s'
        )
        
        # Set up root logger
        logger = logging.getLogger()
        logger.setLevel(log_level)
        
        # Clear existing handlers
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # File handler (if log_file is provided)
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(log_level)
            # Use simpler format for file logging (no colors)
            file_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)


def format_time_delta(seconds: float) -> str:
    """Format time delta in human-readable format"""
    return str(datetime.timedelta(seconds=int(seconds)))


def count_parameters(model: torch.nn.Module) -> int:
    """Count the total number of parameters in a model"""
    return sum(p.numel() for p in model.parameters())


def get_model_size_mb(model: torch.nn.Module) -> float:
    """Get model size in megabytes"""
    param_size = 0
    buffer_size = 0
    
    for param in model.parameters():
        param_size += param.nelement() * param.element_size()
    
    for buffer in model.buffers():
        buffer_size += buffer.nelement() * buffer.element_size()
    
    return (param_size + buffer_size) / (1024 ** 2)


class MFUCalculator:
    """Calculate and track Model FLOPs Utilization (MFU)"""
    
    def __init__(self, model_config, device_peak_flops=None):
        self.model_config = model_config
        
        # RTX 3090 peak performance for bfloat16
        if device_peak_flops is None:
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name()
                if "3090" in gpu_name:
                    # RTX 3090: ~35 TFLOPS for bfloat16
                    self.device_peak_flops = 35e12
                elif "4090" in gpu_name:
                    # RTX 4090: ~83 TFLOPS for bfloat16  
                    self.device_peak_flops = 83e12
                elif "A100" in gpu_name:
                    # A100: ~156 TFLOPS for bfloat16
                    self.device_peak_flops = 156e12
                else:
                    # Conservative estimate for unknown GPUs
                    self.device_peak_flops = 20e12
            else:
                self.device_peak_flops = 1e12  # CPU fallback
        else:
            self.device_peak_flops = device_peak_flops
        
        # Calculate model FLOPs per forward pass
        self.model_flops = self._calculate_model_flops()
        
    def _calculate_model_flops(self):
        """Calculate FLOPs for one forward pass of the model"""
        config = self.model_config
        
        # Transformer FLOPs calculation
        # Based on: https://github.com/karpathy/nanoGPT/blob/master/model.py
        
        N = config.n_layer
        H = config.n_head  
        D = config.n_embd
        T = config.block_size
        V = config.vocab_size
        
        # Attention FLOPs: 4 * N * H * T^2 * (D/H)
        attention_flops = 4 * N * H * T * T * (D // H)
        
        # MLP FLOPs: 8 * N * T * D^2 (for typical 4*D hidden size)
        mlp_flops = 8 * N * T * D * D
        
        # Embedding + output projection: 2 * T * D * V
        embedding_flops = 2 * T * D * V
        
        # Layer norm is negligible
        total_flops = attention_flops + mlp_flops + embedding_flops
        
        return total_flops
    
    def calculate_mfu(self, batch_size, seq_len, dt):
        """Calculate MFU for a given batch and timing"""
        if dt <= 0:
            return 0.0
            
        # FLOPs for this batch (forward + backward ≈ 3x forward)
        batch_flops = self.model_flops * batch_size * 3
        
        # Achieved FLOPS/s
        achieved_flops_per_sec = batch_flops / dt
        
        # MFU = achieved / peak
        mfu = achieved_flops_per_sec / self.device_peak_flops
        
        return min(mfu * 100, 100.0)  # Return as percentage, cap at 100%
    
    def get_optimization_hints(self, current_mfu, batch_size, seq_len):
        """Provide optimization hints based on current MFU"""
        hints = []
        
        if current_mfu < 20:
            hints.append("🔴 Very low MFU - try increasing batch size")
            if batch_size < 32:
                hints.append(f"  → Increase batch_size from {batch_size} to {batch_size * 2}")
        elif current_mfu < 35:
            hints.append("🟡 Low MFU - room for improvement")
            hints.append(f"  → Try batch_size {int(batch_size * 1.5)}")
        elif current_mfu < 50:
            hints.append("🟢 Good MFU - minor optimizations possible")
        else:
            hints.append("🚀 Excellent MFU!")
            
        if seq_len < 2048:
            hints.append(f"  → Consider longer sequences ({seq_len} → {seq_len * 2})")
            
        return hints
