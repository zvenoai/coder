"""Kubernetes client for pod logs and status inspection.

Thin sync wrapper around ``kubernetes.client.CoreV1Api`` with
in-cluster ServiceAccount auth.  Designed for supervisor agent tools.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContainerInfo:
    """Summary of a single container in a pod."""

    name: str
    image: str
    ready: bool
    restart_count: int
    state: str  # "running", "waiting", "terminated"
    state_reason: str | None = None


@dataclass(frozen=True)
class PodInfo:
    """Lightweight pod summary for list operations."""

    name: str
    namespace: str
    phase: str  # Running, Pending, Succeeded, Failed, Unknown
    containers: list[ContainerInfo] = field(default_factory=list)


@dataclass(frozen=True)
class PodDetail:
    """Detailed pod information including conditions and labels."""

    name: str
    namespace: str
    phase: str
    containers: list[ContainerInfo] = field(default_factory=list)
    conditions: list[dict[str, str]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    node_name: str | None = None
    start_time: str | None = None


def _parse_container_status(cs: Any) -> ContainerInfo:
    """Extract ContainerInfo from a V1ContainerStatus object."""
    state = "unknown"
    state_reason: str | None = None
    if cs.state:
        if cs.state.running:
            state = "running"
        elif cs.state.waiting:
            state = "waiting"
            state_reason = cs.state.waiting.reason
        elif cs.state.terminated:
            state = "terminated"
            state_reason = cs.state.terminated.reason

    return ContainerInfo(
        name=cs.name,
        image=cs.image or "",
        ready=bool(cs.ready),
        restart_count=cs.restart_count or 0,
        state=state,
        state_reason=state_reason,
    )


class K8sClient:
    """Sync Kubernetes client for pod inspection and log retrieval.

    Uses in-cluster ServiceAccount auth.  If loading the config fails
    (e.g. running outside a cluster), ``available`` is set to ``False``
    and all methods return empty/error results gracefully.
    """

    def __init__(self, namespace: str = "dev") -> None:
        self.namespace = namespace
        self.available = False
        self._api: object | None = None

        try:
            from kubernetes import client, config  # type: ignore[import-untyped]

            config.load_incluster_config()
            self._api = client.CoreV1Api()
            self.available = True
            logger.info("K8s client initialized (namespace=%s)", namespace)
        except Exception:
            logger.info("K8s client not available (not in cluster or missing deps)")

    def list_pods(self, namespace: str | None = None) -> list[PodInfo]:
        """List pods in the given namespace (defaults to configured namespace)."""
        if not self.available or self._api is None:
            return []

        ns = namespace or self.namespace
        try:
            result = self._api.list_namespaced_pod(namespace=ns)  # type: ignore[attr-defined]
            pods: list[PodInfo] = []
            for pod in result.items:
                containers: list[ContainerInfo] = []
                for cs in pod.status.container_statuses or []:
                    containers.append(_parse_container_status(cs))
                pods.append(
                    PodInfo(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace or ns,
                        phase=pod.status.phase or "Unknown",
                        containers=containers,
                    )
                )
            return pods
        except Exception:
            logger.exception("Failed to list pods in namespace %s", ns)
            return []

    def get_pod_logs(
        self,
        pod_name: str,
        container: str | None = None,
        tail_lines: int = 100,
        since_seconds: int | None = None,
        timestamps: bool = False,
        previous: bool = False,
        namespace: str | None = None,
    ) -> str:
        """Get logs from a pod/container.

        Returns the log text, or an error message string on failure.
        """
        if not self.available or self._api is None:
            return "K8s client not available"

        ns = namespace or self.namespace
        try:
            kwargs: dict[str, object] = {
                "name": pod_name,
                "namespace": ns,
                "tail_lines": tail_lines,
                "timestamps": timestamps,
                "previous": previous,
            }
            if container:
                kwargs["container"] = container
            if since_seconds is not None:
                kwargs["since_seconds"] = since_seconds

            logs: str = self._api.read_namespaced_pod_log(**kwargs)  # type: ignore[attr-defined]
            return logs
        except Exception as e:
            logger.exception("Failed to get logs for pod %s in namespace %s", pod_name, ns)
            return f"Error reading logs: {e}"

    def get_pod_status(self, pod_name: str, namespace: str | None = None) -> PodDetail | None:
        """Get detailed status of a specific pod.

        Returns None if the pod is not found or an error occurs.
        """
        if not self.available or self._api is None:
            return None

        ns = namespace or self.namespace
        try:
            pod = self._api.read_namespaced_pod(name=pod_name, namespace=ns)  # type: ignore[attr-defined]
            containers: list[ContainerInfo] = []
            for cs in pod.status.container_statuses or []:
                containers.append(_parse_container_status(cs))

            conditions: list[dict[str, str]] = []
            for cond in pod.status.conditions or []:
                conditions.append(
                    {
                        "type": cond.type or "",
                        "status": cond.status or "",
                        "reason": cond.reason or "",
                        "message": cond.message or "",
                    }
                )

            return PodDetail(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace or ns,
                phase=pod.status.phase or "Unknown",
                containers=containers,
                conditions=conditions,
                labels=dict(pod.metadata.labels or {}),
                node_name=getattr(pod.spec, "node_name", None),
                start_time=str(pod.status.start_time) if pod.status.start_time else None,
            )
        except Exception:
            logger.exception("Failed to get status for pod %s in namespace %s", pod_name, ns)
            return None
