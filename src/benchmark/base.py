from dataclasses import dataclass, MISSING
from abc import ABC, abstractmethod
from logging import getLogger

from src.backend.base import Backend
from src.utils import set_seed

LOGGER = getLogger("benchmark")


@dataclass
class BenchmarkConfig(ABC):
    name: str = MISSING
    _target_: str = MISSING

    # seed for reproducibility
    seed: int = 42


class Benchmark(ABC):
    def __init__(self, model: str, task: str, device: str, model_kwargs: dict):
        self.model = model
        self.task = task
        self.device = device
        self.model_kwargs = model_kwargs

    def configure(self, config: BenchmarkConfig) -> None:
        LOGGER.info(f"Configuring {config.name} benchmark")

        LOGGER.info(f"\t+ Setting seed({config.seed})")
        set_seed(config.seed)

    @abstractmethod
    def run(self, backend: Backend) -> None:
        raise NotImplementedError("Benchmark must implement run method")

    @abstractmethod
    def save(self, path: str = "") -> None:
        raise NotImplementedError("Benchmark must implement save method")
