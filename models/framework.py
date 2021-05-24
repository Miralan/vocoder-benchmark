"""
Vocoder modeling framework.
"""
import difflib
import glob
import math
import os
import signal
import time
import warnings
from typing import Type, List, Union, Dict, Any, Optional, Iterator, Tuple

import click
import langtech.tts.vocoders.datasets as datasets
import torch
from langtech.tts.vocoders.path_utils import get_default_config_path
from langtech.tts.vocoders.utils import die_if, hard_exit
from omegaconf import OmegaConf
from torch import Tensor
from typing_extensions import Protocol

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from torch.utils.tensorboard import (  # @manual=//caffe2:torch_tensorboard
        SummaryWriter,
    )

# Where to save the config file.
CONFIG_YAML: str = "config.yaml"

# Where to store checkpoints.
CHECKPOINT_DIR = "checkpoints"

# Where to store logs.
TENSORBOARD_DIR = "logs"

# How often to run validation set.
EVAL_FREQUENCY: int = 5_000

# How often to generate samples to put in Tensorboard.
GENERATE_FREQUENCY: int = 100_000

# How many samples to generate for Tensorboard.
GENERATE_NUM_SAMPLES: int = 2

# How often to log training samples to Tensorboard.
LOG_FREQUENCY: int = 10

# How long you can wait before starting training.
MAX_FIRST_ITERATION_DELAY = 600

# How long you can wait for an iteration to finish.
MAX_TRAINING_ITERATION_DELAY = 60


class ConfigProtocol(Protocol):
    """
    Protocol Config classes must follow.
    """

    dataset: datasets.DatasetConfig

    def __init__(self) -> None:
        """Create a new config object."""


class Vocoder(torch.nn.Module):
    """
    Superclass for all vocoders.
    """

    command: str = ""

    def __init__(self, config: ConfigProtocol) -> None:
        """
        Initialize this vocoder model.
        """
        super().__init__()
        self.config = config
        self.register_buffer("global_step_buffer", torch.zeros((), dtype=torch.int32))

    @property
    def global_step(self) -> int:
        """
        Global step as a regular integer.
        """
        return int(self.global_step_buffer.cpu())  # pyre-ignore

    @global_step.setter
    def global_step(self, value: int) -> None:
        """
        Set integer global_step.

        Args:
          value: New value to set global_step to.
        """
        self.global_step_buffer.fill_(value)  # pyre-ignore

    # ========================================
    # ========================================
    # Below are methods models must implement.
    # ========================================
    # ========================================

    @staticmethod
    def default_config() -> ConfigProtocol:
        """
        Returns the OmegaConf config for this model.
        """
        raise NotImplementedError("Every Vocoder model must implement default_config()")

    def get_optimizers(
        self,
    ) -> List[
        Tuple[torch.optim.Optimizer, Optional[torch.optim.lr_scheduler._LRScheduler]]
    ]:
        """
        Get optimizers and schedulers used in this model. This is a method
        instead of just a pair of attributes because GANs tend to have
        multiple optimizers.

        Returns:
          A list of tuples. Each tuple contains an optimizer used by the
          model and an optional scheduler used for that optimizer. These are
          saved and restored from the checkpoints.
        """
        raise NotImplementedError("Every Vocoder model must implement get_optimizers()")

    def is_done(self) -> bool:
        """
        Checks if a model is done training.

        Returns:
          Whether the model is done training.
        """
        raise NotImplementedError("Vocoder subclass must implement is_done()")

    def initialize(self) -> None:
        """
        Called after model creation.
        """
        raise NotImplementedError("Vocoder subclass must implement initialize()")

    def train_step(
        self, _spectrograms: Tensor, _waveforms: Tensor
    ) -> Tuple[Tensor, Dict[str, Tensor]]:
        """
        Runs a single train step of the model.

        Returns:
          A tuple containing overall model loss and a list of losses to log
          to Tensorboard. The first loss is printed to the console and logged
          to Tensorboard.
        """
        raise NotImplementedError("Vocoder subclass must implement train_step()")

    def validation_losses(
        self, _spectrograms: Tensor, _waveforms: Tensor
    ) -> Dict[str, Tensor]:
        """
        Compute validation losses.

        Returns:
          A dictionary mapping loss name (e.g. 'nll_loss') to the validation value.
        """
        raise NotImplementedError("Vocoder subclass must implement validation_losses()")

    def generate(self, _spectrograms: Tensor) -> Tensor:
        """
        Generate a sample from this model.

        Returns:
          A 1D float tensor containing the output waveform.
        """
        raise NotImplementedError("Vocoder subclass must implement generate()")


def create_model_commands(model: Type[Vocoder]) -> click.Group:
    """
    Create a 'create' command.
    """
    name: str = model.command
    if not name:
        raise ValueError(f"Missing 'command' attribute on model '{model.__class__}'")

    def group() -> None:
        pass

    group.__doc__ = f"Create, train, or use {name} models."
    group = click.group(name)(group)

    @group.command("train")
    @click.option("--path", required=True, help="Directory for the model")
    @click.option("--dataset", required=True, help="Name of the dataset to use")
    @click.argument("config_updates", nargs=-1)
    def train(path: str, dataset: str, config_updates: List[str]) -> None:
        """
        Train the model.
        """
        cli_train(name, model, path, dataset, config_updates)

    @group.command("synthesize")
    @click.option("--path", required=True, help="Directory for the model")
    @click.argument("input_file")
    @click.argument("output_file")
    def synthesize(path: str, input_file: str, output_file: str) -> None:
        """
        Synthesize with the model.
        """
        cli_synthesize(name, path, input_file, output_file)

    return group


def cli_train(
    model_name: str,
    model_class: Type[Vocoder],
    path: str,
    dataset_name: str,
    config_updates: List[str],
) -> None:
    """
    Train the model.

    Args:
      model_name: The model type, e.g. 'wavernn'.
      model_class: The class for the model.
      path: Path to the model directory.
      dataset_name: Name of the dataset to use.
      config_updates: Dotlist formatted updates to the base config.
    """
    create_if_missing(model_name, model_class, path, config_updates)
    print(f"Training {model_name} model located at {path}.")

    model = load_model(model_class, path, eval_mode=False)
    train_dataloader, valid_dataloader = datasets.load_dataset(
        dataset_name, model.config.dataset
    )
    train_loop(
        model=model,
        checkpoint_dir=os.path.join(path, CHECKPOINT_DIR),
        log_dir=os.path.join(path, TENSORBOARD_DIR),
        train_set=train_dataloader,
        valid_set=valid_dataloader,
    )


def load_model(
    model_class: Type[Vocoder],
    model_dir: str,
    eval_mode: Optional[bool] = None,
) -> Vocoder:
    """
    Load a model for evaluation.

    Args:
      model_class: The SpeechModel module class.
      model_dir: Path to the model directory.
      eval_mode: Whether to call model.eval() before returning.

    Returns:
      The model object.
    """
    if eval_mode is None:
        eval_mode = True

    # Load model configuration.
    model_exists = os.path.exists(model_dir) and bool(os.listdir(model_dir))
    die_if(not model_exists, f"Model does not exist at {model_dir}")

    config_path = os.path.join(model_dir, CONFIG_YAML)
    die_if(
        not os.path.exists(config_path), f"{CONFIG_YAML} does not exist at {model_dir}"
    )

    config = OmegaConf.structured(model_class.default_config())
    config.merge_with(OmegaConf.load(config_path))
    model = model_class(config)
    model.initialize()

    # Load checkpoint into model if model has been initialized.
    checkpoint_path = last_checkpoint_path(model_dir)
    if checkpoint_path is not None:
        load_model_from_checkpoint(model, checkpoint_path)

    if torch.cuda.is_available():
        model.to("cuda")

    if eval_mode:
        model.eval()

    return model


def create_if_missing(
    model_name: str, model_class: Type[Vocoder], path: str, config_updates: List[str]
) -> None:
    """
    If the model doesn't exist, create it. If it exists, verify that it's the
    same as expected and the options haven't changed.

    Args:
      model_name: The model type, e.g. 'wavernn'.
      model_class: The class for the model.
      path: Path to the model directory.
      config_updates: Dotlist formatted updates to the base config.
    """
    # Load the config object from default config, YAML, and dotlist.
    default_config_path = get_default_config_path(model_name)
    config = OmegaConf.structured(model_class.default_config())
    config.merge_with(OmegaConf.load(default_config_path))
    config.merge_with_dotlist(config_updates)

    # Check if the config already exists.
    config_path = os.path.join(path, CONFIG_YAML)
    if os.path.exists(config_path):
        # If it exists, look for differences from the expected config.
        old_config = OmegaConf.structured(model_class.default_config())
        old_config.merge_with(OmegaConf.load(config_path))
        differences = "\n".join(
            difflib.context_diff(
                OmegaConf.to_yaml(old_config).split("\n"),
                OmegaConf.to_yaml(config).split("\n"),
                fromfile="stored Manifold config",
                tofile="new config",
            )
        )
        die_if(
            bool(differences),
            "Found difference between saved and expected config:\n" + differences,
        )
        return

    print("Full Model Configuration:")
    print("=========================")
    print(OmegaConf.to_yaml(config, resolve=True))
    print("=========================")

    os.makedirs(path, exist_ok=True)
    output_config_path = os.path.join(path, CONFIG_YAML)
    OmegaConf.save(config, output_config_path, resolve=True)
    print(f"Config saved to {output_config_path}.")


def cli_synthesize(
    model_name: str, path: str, input_file: str, output_file: str
) -> None:
    """
    Synthesize with the model.

    Args:
      model_name: The model type, e.g. 'wavernn'.
      path: Path to the model directory.
      input_file: Path to an input WAV file.
      output_file: Path where to place output WAV file.
    """
    die_if(not os.path.exists(path), f"Model path {path} does not exist")
    die_if(
        not input_file.endswith(".wav"), f"Input path {input_file} must end with '.wav'"
    )
    die_if(
        not output_file.endswith(".wav"),
        f"Output path {output_file} must end with '.wav'",
    )

    print(f"Synthesizing with {model_name} model located at {path}.")
    print(f"Loading audio features from {input_file}.")
    print(f"Synthesizing waveform into {output_file}.")
    print("Not implemented yet.")


# Linter complains that this function is too complex, but it's a bit tricky to
# refactor it without making it more confusing to read, hence the 'noqa' below.
def train_loop(  # noqa
    model: Vocoder,
    checkpoint_dir: str,
    log_dir: str,
    train_set: torch.utils.data.DataLoader,
    valid_set: torch.utils.data.DataLoader,
) -> None:
    """
    Run the training loop.

    Args:
      model: The model to train.
      checkpoint_dir: Directory to which to write checkpoints.
      log_dir: Where to put the logs.
      train_set: A DataLoader for loading training data.
      valid_set: A DataLoader for loading validation data.
    """
    writer = SummaryWriter(log_dir=log_dir)

    if torch.cuda.is_available():
        # Move model to GPU.
        model.to("cuda")
        print("Moved model to GPU.")
    else:
        print("WARNING: No GPU detected, running on CPU. Training may be very slow.")

    def repeat_training_data_forever() -> Iterator[Any]:  # pyre-ignore
        """
        Repeat all training data forever.
        """
        while True:
            yield from train_set
            print("Completed epoch.", flush=True)

    total_start_time: Optional[float] = None
    iter_start_time = time.time()
    signal.signal(signal.SIGALRM, training_iteration_took_too_long)

    data_loading_total_time = 0.0
    num_iterations = 0

    train_iterator = repeat_training_data_forever()
    while not model.is_done():
        signal.alarm(
            MAX_FIRST_ITERATION_DELAY
            if total_start_time is None
            else MAX_TRAINING_ITERATION_DELAY
        )

        data_loading_start_time = time.time()
        spectrograms, waveforms = next(train_iterator)
        if num_iterations > 0:
            # Skip the first iteration because it's very long and not representative.
            data_loading_total_time += time.time() - data_loading_start_time

        if torch.cuda.is_available():
            spectrograms = spectrograms.cuda()
            waveforms = waveforms.cuda()
        loss, tb_logs = model.train_step(spectrograms, waveforms)
        num_iterations += 1
        model.global_step += 1

        # Print elapsed time.
        if total_start_time is None:
            total_start_time = time.time()

        elapsed = time.time() - iter_start_time
        elapsed_total = int((time.time() - total_start_time) / 60)
        print_loss = float(loss.detach().cpu().numpy())
        print(
            f"{elapsed_total}m - {elapsed:.3f}s",
            "-",
            f"Iteration {model.global_step}: {print_loss:.3f}",
            flush=True,
        )
        if model.global_step % 50 == 0:
            print(
                f"Average data loading time: {data_loading_total_time / num_iterations:.3f}"
            )
        iter_start_time = time.time()
        signal.alarm(0)

        if model.global_step % LOG_FREQUENCY == 0:
            writer.add_scalar("train/loss", print_loss, global_step=model.global_step)
            for key, value in tb_logs.items():
                writer.add_scalar("train/" + key, value, global_step=model.global_step)

        if math.isnan(print_loss):
            print("Detected NaN loss. Exiting!")
            hard_exit(1)

        if model.global_step % EVAL_FREQUENCY == 0:
            model.eval()
            compute_validation_metrics(model, valid_set, writer)
            model.train()

            # Save model every time we eval.
            save_model(model, checkpoint_dir)

        if model.global_step % GENERATE_FREQUENCY == 0:
            generate_tensorboard_samples(model, valid_set, writer)

    print("Completed training.")
    save_model(model, checkpoint_dir)


def save_model(model: Vocoder, checkpoint_dir: str) -> None:
    """
    Save the model to disk.

    Args:
      model: The model to save.
      checkpoint_dir: Directory in which to put checkpoint.
    """
    optimizers, schedulers = [], []
    for opt, sched in model.get_optimizers():
        optimizers.append(opt.state_dict())
        schedulers.append(sched.state_dict() if sched is not None else None)

    checkpoint = {
        "model": model.state_dict(),
        "optimizers": optimizers,
        "lr_schedulers": schedulers,
    }

    # Move checkpoint to CPU.
    checkpoint = move_state_dict_to_device(checkpoint, cpu=True)

    os.makedirs(checkpoint_dir, exist_ok=True)
    torch.save(
        checkpoint, os.path.join(checkpoint_dir, f"{model.global_step:08d}.ckpt")
    )


def move_state_dict_to_device(  # pyre-ignore
    item: Union[Dict[str, Any], List[Any], Tensor],  # pyre-ignore
    cpu: bool,
) -> Union[Dict[str, Any], List[Any], Tensor]:
    """
    Recursively move a state_dict to CPU.

    Args:
      item: The item to move to CPU. Can be a Tensor or a list or dict.
        If it's a list or dict, recurse. If it's a tensor, move it to CPU.
        If it's anything else, do nothing and return the input value.
      cpu: Whether to move to CPU (true) or GPU (false).

    Returns:
      A structure identical to the input but with all tensors moved to CPU.
    """
    if isinstance(item, torch.Tensor):
        return item.cpu() if cpu else item.cuda()
    if isinstance(item, dict):
        return {
            key: move_state_dict_to_device(value, cpu=cpu)
            for key, value in item.items()
        }
    if isinstance(item, list):
        return [move_state_dict_to_device(value, cpu=cpu) for value in item]
    return item


def load_model_from_checkpoint(model: Vocoder, checkpoint_path: str) -> None:
    """
    Restore a model from a checkpoint.

    Args:
      model: The model to restore.
      checkpoint_path: The path to the checkpoint.
    """
    print(f"Loading model from checkpoint: {checkpoint_path}")
    checkpoint = torch.load(checkpoint_path)

    if torch.cuda.is_available():
        checkpoint = move_state_dict_to_device(checkpoint, cpu=False)

    # Check that we don't have NaN in the checkpoint.
    # This should never happen; this is a sanity check.
    for key, tensor in checkpoint["model"].items():
        if isinstance(tensor, torch.Tensor):
            assert not torch.any(
                torch.isnan(tensor)
            ), f"Found NaN in checkpoint tensor {key}"

    # All checks have passed. Load the state_dict.
    model.load_state_dict(checkpoint["model"])

    for (opt, sched), opt_dict, sched_dict in zip(
        model.get_optimizers(), checkpoint["optimizers"], checkpoint["lr_schedulers"]
    ):
        opt.load_state_dict(opt_dict)
        if sched is not None:
            sched.load_state_dict(sched_dict)


def last_checkpoint_path(model_dir: str) -> Optional[str]:
    """
    Get path to latest checkpoint for a model.

    Args:
      model_dir: The model directory.

    Returns:
      The absolute path to the last checkpoint for this model.
      If no checkpoints exist, return None.
    """
    checkpoint_dir = os.path.join(model_dir, CHECKPOINT_DIR)
    checkpoint_paths = sorted(glob.glob(os.path.join(checkpoint_dir, "*.ckpt")))
    if checkpoint_paths:
        return checkpoint_paths[-1]
    return None


def training_iteration_took_too_long(_signum: int, _frame: Any) -> None:
    """
    Signal handler for SIGALARM.

    Called when a training iteration takes too long. This signal handler is
    registered because a training iteration that takes more than a minute or
    two is likely indicative of a hang, and so to prevent FBLearner Flow from running
    an expensive job forever and not reporting a failure, we instead kill the process.
    """
    print("Iteration took too long!", flush=True)
    hard_exit(1)


def generate_tensorboard_samples(
    model: Vocoder,
    dataloader: torch.utils.data.DataLoader,
    writer: SummaryWriter,
    n_samples: int = GENERATE_NUM_SAMPLES,
) -> None:
    """
    Generate audio samples for a model.

    Args:
      model: The model for which to compute validation metrics.
      dataloader: The validation data DataLoader.
      writer: Tensorboard to write to.
      n_samples: How many samples to generate.
    """
    # Grab needed samples from the dataloader.
    data_iter = iter(dataloader)
    data = [next(data_iter) for _ in range(n_samples)]

    # Generate the samples from the model.
    with torch.no_grad():
        wavs = [model.generate(spec) for spec, _wav in data]

    # Write samples, including original if needed, to Tensorboard.
    for idx, wav in enumerate(wavs):
        if model.global_step == 0:
            writer.add_audio(
                f"audio/{idx}_real",
                data[idx][1],
                global_step=model.global_step,
                sample_rate=datasets.AUDIO_SAMPLE_RATE,
            )
        writer.add_audio(
            f"audio/{idx}_synthesized",
            wav,
            global_step=model.global_step,
            sample_rate=datasets.AUDIO_SAMPLE_RATE,
        )


def compute_validation_metrics(
    model: Vocoder, dataloader: torch.utils.data.DataLoader, writer: SummaryWriter
) -> None:
    """
    Compute and log validation metrics.

    Args:
      model: The model for which to compute validation metrics.
      dataloader: The validation data DataLoader.
      writer: Tensorboard to write to.
    """
    losses = []
    with torch.no_grad():
        print("Computing validation loss...", flush=True)

        for spectrograms, waveforms in dataloader:
            if torch.cuda.is_available():
                spectrograms = spectrograms.cuda()
                waveforms = waveforms.cuda()
            losses.append(model.validation_losses(spectrograms, waveforms))

        if not losses:
            print("No validation data available!")
            return

        for key in sorted(losses[0].keys()):
            key_losses = [batch_losses[key] for batch_losses in losses]
            mean_loss = torch.mean(torch.stack(key_losses)).cpu().numpy().item()
            print(f"Validation {key}: {mean_loss:.3f}", flush=True)
            writer.add_scalar("valid/" + key, mean_loss, global_step=model.global_step)