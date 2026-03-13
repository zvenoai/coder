"""Prometheus metrics registry with text format serialization.

This module provides a minimal implementation of Prometheus metrics (Counter, Gauge, Histogram)
and text format exposition without external dependencies. Used by the /metrics endpoint
to export orchestrator telemetry to VictoriaMetrics.
"""

from __future__ import annotations


class _Counter:
    """Prometheus Counter metric — monotonically increasing value."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._label_values: tuple[str, ...] = ()

    def labels(self, **kwargs: str) -> _Counter:
        """Return a labeled instance of this counter."""
        if set(kwargs.keys()) != set(self.label_names):
            raise ValueError(f"Expected labels {self.label_names}, got {set(kwargs.keys())}")
        label_values = tuple(kwargs[k] for k in self.label_names)

        # Create a child counter with the same name but specific label values
        child = _Counter(self.name, self.help, self.label_names)
        child._values = self._values
        child._label_values = label_values
        return child

    def inc(self, amount: float = 1) -> None:
        """Increment the counter."""
        key = self._label_values if self._label_values else ()
        self._values[key] = self._values.get(key, 0) + amount

    def value(self) -> float:
        """Get current value."""
        key = self._label_values if self._label_values else ()
        return self._values.get(key, 0)

    def render(self) -> str:
        """Render this counter in Prometheus text format."""
        lines = []
        if not self._values:
            # Only output unlabeled zero for metrics without labels
            if not self.label_names:
                lines.append(f"{self.name} 0")
        else:
            for label_values, value in sorted(self._values.items()):
                if label_values:
                    labels_str = ",".join(
                        f'{name}="{val}"' for name, val in zip(self.label_names, label_values, strict=True)
                    )
                    lines.append(f"{self.name}{{{labels_str}}} {value}")
                else:
                    lines.append(f"{self.name} {value}")
        return "\n".join(lines)


class _Gauge:
    """Prometheus Gauge metric — arbitrary up/down value."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._label_values: tuple[str, ...] = ()

    def labels(self, **kwargs: str) -> _Gauge:
        """Return a labeled instance of this gauge."""
        if set(kwargs.keys()) != set(self.label_names):
            raise ValueError(f"Expected labels {self.label_names}, got {set(kwargs.keys())}")
        label_values = tuple(kwargs[k] for k in self.label_names)

        child = _Gauge(self.name, self.help, self.label_names)
        child._values = self._values
        child._label_values = label_values
        return child

    def set(self, value: float) -> None:
        """Set the gauge to a specific value."""
        key = self._label_values if self._label_values else ()
        self._values[key] = value

    def inc(self, amount: float = 1) -> None:
        """Increment the gauge."""
        key = self._label_values if self._label_values else ()
        self._values[key] = self._values.get(key, 0) + amount

    def dec(self, amount: float = 1) -> None:
        """Decrement the gauge."""
        key = self._label_values if self._label_values else ()
        self._values[key] = self._values.get(key, 0) - amount

    def value(self) -> float:
        """Get current value."""
        key = self._label_values if self._label_values else ()
        return self._values.get(key, 0)

    def render(self) -> str:
        """Render this gauge in Prometheus text format."""
        lines = []
        if not self._values:
            # Only output unlabeled zero for metrics without labels
            if not self.label_names:
                lines.append(f"{self.name} 0")
        else:
            for label_values, value in sorted(self._values.items()):
                if label_values:
                    labels_str = ",".join(
                        f'{name}="{val}"' for name, val in zip(self.label_names, label_values, strict=True)
                    )
                    lines.append(f"{self.name}{{{labels_str}}} {value}")
                else:
                    lines.append(f"{self.name} {value}")
        return "\n".join(lines)


class _Histogram:
    """Prometheus Histogram metric — distribution of observations."""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")),
        label_names: tuple[str, ...] = (),
    ):
        self.name = name
        self.help = help_text
        self.buckets = buckets
        self.label_names = label_names
        self._sum = 0.0
        self._count = 0
        self._bucket_counts: dict[float, int] = dict.fromkeys(buckets, 0)

    def observe(self, value: float) -> None:
        """Record an observation."""
        self._sum += value
        self._count += 1
        for bucket in self.buckets:
            if value <= bucket:
                self._bucket_counts[bucket] += 1

    def render(self) -> str:
        """Render this histogram in Prometheus text format."""
        lines = []
        # Render buckets (cumulative)
        for bucket in self.buckets:
            le = "+Inf" if bucket == float("inf") else str(bucket)
            lines.append(f'{self.name}_bucket{{le="{le}"}} {self._bucket_counts[bucket]}')
        # Render sum and count
        lines.append(f"{self.name}_sum {self._sum}")
        lines.append(f"{self.name}_count {self._count}")
        return "\n".join(lines)


class MetricsRegistry:
    """Registry for Prometheus metrics with text format rendering."""

    def __init__(self):
        self._metrics: dict[str, _Counter | _Gauge | _Histogram] = {}

    def counter(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
    ) -> _Counter:
        """Register and return a Counter metric."""
        if name in self._metrics:
            return self._metrics[name]  # type: ignore
        metric = _Counter(name, help_text, label_names)
        self._metrics[name] = metric
        return metric

    def gauge(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
    ) -> _Gauge:
        """Register and return a Gauge metric."""
        if name in self._metrics:
            return self._metrics[name]  # type: ignore
        metric = _Gauge(name, help_text, label_names)
        self._metrics[name] = metric
        return metric

    def histogram(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")),
        label_names: tuple[str, ...] = (),
    ) -> _Histogram:
        """Register and return a Histogram metric."""
        if name in self._metrics:
            return self._metrics[name]  # type: ignore
        metric = _Histogram(name, help_text, buckets, label_names)
        self._metrics[name] = metric
        return metric

    def render(self) -> str:
        """Render all metrics in Prometheus text format."""
        lines = []
        for name, metric in sorted(self._metrics.items()):
            # HELP line
            lines.append(f"# HELP {name} {metric.help}")
            # TYPE line
            if isinstance(metric, _Counter):
                lines.append(f"# TYPE {name} counter")
            elif isinstance(metric, _Gauge):
                lines.append(f"# TYPE {name} gauge")
            elif isinstance(metric, _Histogram):
                lines.append(f"# TYPE {name} histogram")
            # Metric values
            lines.append(metric.render())
        return "\n".join(lines) + "\n"


# Global registry singleton
REGISTRY = MetricsRegistry()

# Pre-defined metrics for the orchestrator
AGENTS_RUNNING = REGISTRY.gauge(
    "coder_agents_running",
    "Number of active agents right now",
)

TASKS_TOTAL = REGISTRY.counter(
    "coder_tasks_total",
    "Total tasks started",
    label_names=("status",),
)

TASK_DURATION = REGISTRY.histogram(
    "coder_task_duration_seconds",
    "Task execution time",
    buckets=(60, 120, 300, 600, 1200, 1800, 3600, float("inf")),
)

TASK_COST = REGISTRY.histogram(
    "coder_task_cost_usd",
    "Task cost in USD",
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, float("inf")),
)

PRS_TRACKED = REGISTRY.gauge(
    "coder_prs_tracked",
    "Number of PRs being monitored",
)

EPICS_ACTIVE = REGISTRY.gauge(
    "coder_epics_active",
    "Active epics",
)

COMPACTION_TOTAL = REGISTRY.counter(
    "coder_compaction_total",
    "How many times compaction has triggered",
)

HEARTBEAT_STUCK = REGISTRY.gauge(
    "coder_heartbeat_stuck_tasks",
    "Tasks in stuck state right now",
)
