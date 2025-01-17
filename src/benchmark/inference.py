from dataclasses import dataclass
from typing import List, Tuple
from logging import getLogger

import statistics
from omegaconf import DictConfig
from pandas import DataFrame

from src.backend.base import Backend
from src.tracker.memory import MemoryTracker
from src.tracker.latency import LatencyTracker
from src.benchmark.base import Benchmark, BenchmarkConfig
from src.dummy_generator import DummyGenerator

BENCHMARK_NAME = "inference"
LOGGER = getLogger(BENCHMARK_NAME)


@dataclass
class InferenceConfig(BenchmarkConfig):
    name: str = BENCHMARK_NAME
    _target_: str = "src.benchmark.inference.InferenceBenchmark"

    # run options
    memory: bool = False
    profile: bool = False

    # loop options
    warmup_runs: int = 10
    benchmark_duration: int = 10

    # input options
    input_shapes: DictConfig = DictConfig(
        {
            "batch_size": 1,
            # text
            "sequence_length": 16,
            "num_choices": 4,
            # image
            "width": 64,
            "height": 64,
            "num_channels": 3,
            "point_batch_size": 3,
            "nb_points_per_image": 2,
            # audio
            "feature_size": 80,
            "nb_max_frames": 3000,
            "audio_sequence_length": 16000,
        }
    )
    # output options
    new_tokens: int = 100


class InferenceBenchmark(Benchmark):
    def __init__(self, model: str, task: str, device: str, model_kwargs: dict):
        super().__init__(model, task, device, model_kwargs)

        # initialize dummy inputs generator
        self.dummy_generator = DummyGenerator(
            model, task, device, model_kwargs)

        # initialize inference results
        self.forward_peak_memory: int = 0
        self.forward_latencies: List[float] = []
        self.generate_latencies: List[float] = []
        # might be better to seperate profiling benchmark from inference benchmark
        # kernel/node, op, time
        self.forward_profile: List[Tuple[str, str, float]] = []

    def configure(self, config: InferenceConfig):
        super().configure(config)
        self.memory = config.memory
        self.profile = config.profile

        self.warmup_runs = config.warmup_runs
        self.benchmark_duration = config.benchmark_duration

        self.input_shapes = config.input_shapes
        self.new_tokens = config.new_tokens

    def run(self, backend: Backend) -> None:
        LOGGER.info("Running inference benchmark")

        self._run_forward_tracking(backend)

        if self.memory:
            self._run_memory_tracking(backend)

        self._run_generate_tracking(backend)

        if self.profile:
            self._run_forward_profile(backend)

    def _run_memory_tracking(self, backend: Backend) -> None:
        memory_inputs = self.dummy_generator.generate(
            mode="forward", **self.input_shapes
        )

        LOGGER.info("\t+ Tracking forward pass peak memory")
        memory_tracker = MemoryTracker(device=self.device)
        with memory_tracker.track(interval=self.forward_latency / 10):
            outputs = backend.forward(memory_inputs)

        self.forward_peak_memory = memory_tracker.get_peak_memory()
        LOGGER.info(
            f"\t+ Forward pass peak memory: {self.forward_peak_memory} (MB)")

    def _run_forward_tracking(self, backend: Backend) -> None:
        forward_inputs = self.dummy_generator.generate(
            mode="forward", **self.input_shapes
        )

        LOGGER.info("\t+ Warming up the forward pass")
        for _ in range(self.warmup_runs):
            outputs = backend.forward(forward_inputs)

        LOGGER.info("\t+ Tracking forward pass latency and throughput")
        latency_tracker = LatencyTracker(device=self.device)
        while sum(latency_tracker.get_latencies()) < self.benchmark_duration:
            with latency_tracker.track():
                outputs = backend.forward(forward_inputs)

        self.forward_latencies = latency_tracker.get_latencies()
        LOGGER.info(
            f"\t+ Forward pass latency: {self.forward_latency:.2e} (s)")
        LOGGER.info(
            f"\t+ Forward pass throughput: {self.forward_throughput:.2f} (samples/s)"
        )

    def _run_generate_tracking(self, backend: Backend) -> None:
        generate_inputs = self.dummy_generator.generate(
            mode="generate", **self.input_shapes
        )

        LOGGER.info("\t+ Testing and warming up the generation pass")
        try:
            outputs = backend.generate(
                generate_inputs, new_tokens=self.new_tokens)
        except Exception as e:
            LOGGER.info("\t+ Generation pass failed or not supported")
            LOGGER.info(f"\t+ Raised exception: {e}")
            self.can_generate = False
            return None
        else:
            self.can_generate = True

        LOGGER.info("\t+ Tracking generation throughput")
        latency_tracker = LatencyTracker(device=self.device)
        while sum(latency_tracker.get_latencies()) < self.benchmark_duration:
            with latency_tracker.track():
                outputs = backend.generate(
                    generate_inputs, new_tokens=self.new_tokens)

        self.generate_latencies = latency_tracker.get_latencies()
        LOGGER.info(
            f"\t+ Generation pass latency: {self.generate_latency:.2e} (s)")

        LOGGER.info(
            f"\t+ Generation pass throughput: {self.generate_throughput:.2f} (tokens/s)"
        )

    def _run_forward_profile(self, backend: Backend) -> None:
        profile_inputs = self.dummy_generator.generate(
            mode="forward", **self.input_shapes
        )

        LOGGER.info("\t+ Preparing backend for profiling")
        backend.prepare_for_profiling(list(profile_inputs.keys()))

        LOGGER.info("\t+ Running profiling")
        backend.forward(profile_inputs)

        self.forward_profile = backend.pretrained_model.get_forward_profile()  # type: ignore

    # Metrics
    @property
    def forward_latency(self) -> float:
        return statistics.mean(self.forward_latencies)

    @property
    def forward_throughput(self) -> float:
        return self.input_shapes.batch_size / self.forward_latency

    @property
    def generate_latency(self) -> float:
        return statistics.mean(self.generate_latencies)

    @property
    def generate_throughput(self) -> float:
        return self.new_tokens * self.input_shapes.batch_size / self.generate_latency

    def get_results_df(self) -> DataFrame:
        results_dict = dict()

        results_dict["forward.latency(s)"] = significant_figures(
            self.forward_latency)
        results_dict["forward.throughput(samples/s)"] = significant_figures(
            self.forward_throughput
        )

        if self.memory:
            results_dict["forward.peak_memory(MB)"] = significant_figures(
                self.forward_peak_memory
            )

        if self.can_generate:
            results_dict["generate.latency(s)"] = significant_figures(
                self.generate_latency
            )
            results_dict["generate.throughput(tokens/s)"] = significant_figures(
                self.generate_throughput
            )

        return DataFrame(results_dict, index=[0])

    def get_profile_df(self) -> DataFrame:
        return DataFrame(
            self.forward_profile,
            columns=["Node/Kernel", "Operator", "Latency (s)"],
        )

    def save(self) -> None:
        LOGGER.info("Saving inference results")
        results_df = self.get_results_df()
        results_df.to_csv("inference_results.csv")

        if self.profile:
            LOGGER.info("Saving profiling results")
            profile_df = self.get_profile_df()
            profile_df.to_csv("inference_profile.csv")


def significant_figures(x):
    return float(f"{x:.3g}")
