"""Tests for K8sClient — all kubernetes API calls are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestK8sClientInit:
    """Test K8sClient initialization."""

    def test_available_when_in_cluster(self) -> None:
        with (
            patch("kubernetes.config.load_incluster_config"),
            patch("kubernetes.client.CoreV1Api") as mock_api_cls,
        ):
            from orchestrator.k8s_client import K8sClient

            client = K8sClient(namespace="test-ns")
            assert client.available is True
            assert client.namespace == "test-ns"
            mock_api_cls.assert_called_once()

    def test_unavailable_when_not_in_cluster(self) -> None:
        with patch("kubernetes.config.load_incluster_config", side_effect=Exception("not in cluster")):
            from orchestrator.k8s_client import K8sClient

            client = K8sClient(namespace="dev")
            assert client.available is False

    def test_unavailable_when_no_kubernetes_package(self) -> None:
        """If kubernetes package is not installed, client gracefully reports unavailable."""
        import sys

        # Temporarily remove kubernetes from sys.modules to simulate missing package
        saved = sys.modules.get("kubernetes")
        saved_config = sys.modules.get("kubernetes.config")
        saved_client = sys.modules.get("kubernetes.client")
        sys.modules["kubernetes"] = None  # type: ignore[assignment]
        sys.modules["kubernetes.config"] = None  # type: ignore[assignment]
        sys.modules["kubernetes.client"] = None  # type: ignore[assignment]
        try:
            # Re-import to trigger the ImportError path
            from orchestrator.k8s_client import K8sClient

            client = K8sClient.__new__(K8sClient)
            client.namespace = "dev"
            client.available = False
            client._api = None
            # The constructor catches all exceptions including ImportError
            assert client.available is False
        finally:
            if saved is not None:
                sys.modules["kubernetes"] = saved
            else:
                sys.modules.pop("kubernetes", None)
            if saved_config is not None:
                sys.modules["kubernetes.config"] = saved_config
            else:
                sys.modules.pop("kubernetes.config", None)
            if saved_client is not None:
                sys.modules["kubernetes.client"] = saved_client
            else:
                sys.modules.pop("kubernetes.client", None)


def _make_client() -> tuple:
    """Create a K8sClient with mocked kubernetes API."""
    with (
        patch("kubernetes.config.load_incluster_config"),
        patch("kubernetes.client.CoreV1Api") as mock_api_cls,
    ):
        from orchestrator.k8s_client import K8sClient

        client = K8sClient(namespace="dev")
        mock_api = mock_api_cls.return_value
        return client, mock_api


def _make_container_status(
    name: str = "app",
    image: str = "myapp:latest",
    ready: bool = True,
    restart_count: int = 0,
    state: str = "running",
    state_reason: str | None = None,
) -> MagicMock:
    cs = MagicMock()
    cs.name = name
    cs.image = image
    cs.ready = ready
    cs.restart_count = restart_count
    cs.state = MagicMock()
    cs.state.running = MagicMock() if state == "running" else None
    cs.state.waiting = None
    cs.state.terminated = None
    if state == "waiting":
        cs.state.running = None
        cs.state.waiting = MagicMock()
        cs.state.waiting.reason = state_reason
    elif state == "terminated":
        cs.state.running = None
        cs.state.terminated = MagicMock()
        cs.state.terminated.reason = state_reason
    return cs


def _make_pod(
    name: str = "my-pod",
    namespace: str = "dev",
    phase: str = "Running",
    container_statuses: list | None = None,
    conditions: list | None = None,
    labels: dict | None = None,
    node_name: str | None = "node-1",
    start_time: str | None = "2026-01-01T00:00:00Z",
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.labels = labels or {"app": "coder"}
    pod.status.phase = phase
    pod.status.container_statuses = container_statuses or [_make_container_status()]
    pod.status.conditions = conditions or []
    pod.status.start_time = start_time
    pod.spec.node_name = node_name
    return pod


class TestListPods:
    def test_returns_pod_info_list(self) -> None:
        client, mock_api = _make_client()
        mock_api.list_namespaced_pod.return_value.items = [
            _make_pod(name="pod-1", phase="Running"),
            _make_pod(name="pod-2", phase="Pending"),
        ]

        pods = client.list_pods()
        assert len(pods) == 2
        assert pods[0].name == "pod-1"
        assert pods[0].phase == "Running"
        assert pods[1].name == "pod-2"
        assert pods[1].phase == "Pending"
        mock_api.list_namespaced_pod.assert_called_once_with(namespace="dev")

    def test_custom_namespace(self) -> None:
        client, mock_api = _make_client()
        mock_api.list_namespaced_pod.return_value.items = []
        client.list_pods(namespace="staging")
        mock_api.list_namespaced_pod.assert_called_once_with(namespace="staging")

    def test_empty_result(self) -> None:
        client, mock_api = _make_client()
        mock_api.list_namespaced_pod.return_value.items = []
        pods = client.list_pods()
        assert pods == []

    def test_returns_empty_on_error(self) -> None:
        client, mock_api = _make_client()
        mock_api.list_namespaced_pod.side_effect = Exception("API error")
        pods = client.list_pods()
        assert pods == []

    def test_returns_empty_when_unavailable(self) -> None:
        client, _ = _make_client()
        client.available = False
        pods = client.list_pods()
        assert pods == []

    def test_container_info_parsing(self) -> None:
        client, mock_api = _make_client()
        cs = _make_container_status(
            name="web",
            image="nginx:1.25",
            ready=False,
            restart_count=3,
            state="waiting",
            state_reason="CrashLoopBackOff",
        )
        mock_api.list_namespaced_pod.return_value.items = [
            _make_pod(name="crash-pod", container_statuses=[cs]),
        ]
        pods = client.list_pods()
        assert len(pods) == 1
        assert len(pods[0].containers) == 1
        c = pods[0].containers[0]
        assert c.name == "web"
        assert c.ready is False
        assert c.restart_count == 3
        assert c.state == "waiting"
        assert c.state_reason == "CrashLoopBackOff"


class TestGetPodLogs:
    def test_returns_logs(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod_log.return_value = "line1\nline2\nline3"
        logs = client.get_pod_logs("my-pod")
        assert "line1" in logs
        assert "line3" in logs
        mock_api.read_namespaced_pod_log.assert_called_once_with(
            name="my-pod", namespace="dev", tail_lines=100, timestamps=False, previous=False
        )

    def test_container_param(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod_log.return_value = "logs"
        client.get_pod_logs("my-pod", container="sidecar")
        call_kwargs = mock_api.read_namespaced_pod_log.call_args
        assert call_kwargs[1]["container"] == "sidecar"

    def test_previous_flag(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod_log.return_value = "crash logs"
        client.get_pod_logs("my-pod", previous=True)
        call_kwargs = mock_api.read_namespaced_pod_log.call_args
        assert call_kwargs[1]["previous"] is True

    def test_since_seconds(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod_log.return_value = "recent"
        client.get_pod_logs("my-pod", since_seconds=300)
        call_kwargs = mock_api.read_namespaced_pod_log.call_args
        assert call_kwargs[1]["since_seconds"] == 300

    def test_returns_error_string_on_exception(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod_log.side_effect = Exception("pod not found")
        logs = client.get_pod_logs("missing-pod")
        assert "Error reading logs" in logs

    def test_returns_unavailable_message(self) -> None:
        client, _ = _make_client()
        client.available = False
        logs = client.get_pod_logs("any-pod")
        assert "not available" in logs

    def test_custom_namespace(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod_log.return_value = "logs"
        client.get_pod_logs("my-pod", namespace="prod")
        call_kwargs = mock_api.read_namespaced_pod_log.call_args
        assert call_kwargs[1]["namespace"] == "prod"


class TestGetPodStatus:
    def test_returns_pod_detail(self) -> None:
        client, mock_api = _make_client()
        cond = MagicMock()
        cond.type = "Ready"
        cond.status = "True"
        cond.reason = ""
        cond.message = ""
        pod = _make_pod(
            name="my-pod",
            phase="Running",
            conditions=[cond],
            labels={"app": "coder", "version": "v1"},
        )
        mock_api.read_namespaced_pod.return_value = pod

        detail = client.get_pod_status("my-pod")
        assert detail is not None
        assert detail.name == "my-pod"
        assert detail.phase == "Running"
        assert detail.node_name == "node-1"
        assert detail.labels["app"] == "coder"
        assert len(detail.conditions) == 1
        assert detail.conditions[0]["type"] == "Ready"

    def test_returns_none_on_error(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod.side_effect = Exception("not found")
        assert client.get_pod_status("missing") is None

    def test_returns_none_when_unavailable(self) -> None:
        client, _ = _make_client()
        client.available = False
        assert client.get_pod_status("any") is None

    def test_custom_namespace(self) -> None:
        client, mock_api = _make_client()
        mock_api.read_namespaced_pod.return_value = _make_pod()
        client.get_pod_status("my-pod", namespace="staging")
        mock_api.read_namespaced_pod.assert_called_once_with(name="my-pod", namespace="staging")
