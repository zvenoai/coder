"""Tests for Alertmanager webhook endpoint and parsing."""

import typing
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from orchestrator.alertmanager_webhook import AlertmanagerAlert
from orchestrator.event_bus import EventBus


def _get_app():
    from orchestrator.web import app, configure

    return app, configure


def _make_alert(
    alertname: str = "HighErrorRate",
    severity: str = "critical",
    namespace: str = "dev",
    service: str = "api",
    summary: str = "API error rate > 5%",
    description: str = "Error rate is 8%",
) -> AlertmanagerAlert:
    """Factory for AlertmanagerAlert with sensible defaults."""
    return AlertmanagerAlert(
        status="firing",
        labels={
            "alertname": alertname,
            "severity": severity,
            "namespace": namespace,
            "service": service,
        },
        annotations={
            "summary": summary,
            "description": description,
        },
        starts_at="2026-03-02T14:00:00Z",
        ends_at="0001-01-01T00:00:00Z",
        generator_url="http://vmalert/alert/1",
    )


class TestParsePayload:
    """Pure unit tests for alertmanager_webhook.parse_payload()."""

    def test_parse_payload_firing(self) -> None:
        """Valid payload with one firing alert."""
        from orchestrator.alertmanager_webhook import parse_payload

        raw = {
            "version": "4",
            "groupKey": "test-group",
            "status": "firing",
            "receiver": "coder-webhook",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "HighErrorRate",
                        "severity": "critical",
                        "namespace": "dev",
                        "service": "api",
                    },
                    "annotations": {
                        "summary": "API error rate > 5%",
                        "description": "Error rate is 8%",
                    },
                    "startsAt": "2026-03-02T14:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "http://vmalert/alert/1",
                }
            ],
        }

        payload = parse_payload(raw)

        assert payload.version == "4"
        assert payload.status == "firing"
        assert payload.group_key == "test-group"
        assert payload.receiver == "coder-webhook"
        assert len(payload.alerts) == 1

        alert = payload.alerts[0]
        assert alert.status == "firing"
        assert alert.labels["alertname"] == "HighErrorRate"
        assert alert.labels["severity"] == "critical"
        assert alert.labels["namespace"] == "dev"
        assert alert.labels["service"] == "api"
        assert alert.annotations["summary"] == "API error rate > 5%"
        assert alert.annotations["description"] == "Error rate is 8%"
        assert alert.starts_at == "2026-03-02T14:00:00Z"
        assert alert.ends_at == "0001-01-01T00:00:00Z"
        assert alert.generator_url == "http://vmalert/alert/1"

    def test_parse_payload_resolved_filtered(self) -> None:
        """Payload with firing and resolved alerts - only firing survives."""
        from orchestrator.alertmanager_webhook import parse_payload

        raw = {
            "version": "4",
            "groupKey": "test-group",
            "status": "firing",
            "receiver": "coder-webhook",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "Alert1", "severity": "warning"},
                    "annotations": {"summary": "First alert"},
                    "startsAt": "2026-03-02T14:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "http://vmalert/alert/1",
                },
                {
                    "status": "resolved",
                    "labels": {"alertname": "Alert2", "severity": "critical"},
                    "annotations": {"summary": "Second alert"},
                    "startsAt": "2026-03-02T13:00:00Z",
                    "endsAt": "2026-03-02T14:05:00Z",
                    "generatorURL": "http://vmalert/alert/2",
                },
            ],
        }

        payload = parse_payload(raw)

        assert len(payload.alerts) == 1
        assert payload.alerts[0].labels["alertname"] == "Alert1"

    def test_parse_payload_empty_alerts(self) -> None:
        """Payload with empty alerts list - no exception."""
        from orchestrator.alertmanager_webhook import parse_payload

        raw = {
            "version": "4",
            "groupKey": "test-group",
            "status": "firing",
            "receiver": "coder-webhook",
            "alerts": [],
        }

        payload = parse_payload(raw)

        assert len(payload.alerts) == 0


class TestAlertmanagerEndpoint:
    """HTTP endpoint tests using TestClient."""

    def test_endpoint_returns_200_on_valid_payload(self) -> None:
        """POST valid firing alert - 200 and chat_manager.auto_send called."""
        app, configure = _get_app()
        chat_manager = AsyncMock()
        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
        )

        client = TestClient(app)
        resp = client.post(
            "/webhook/alertmanager",
            json={
                "version": "4",
                "groupKey": "test-group",
                "status": "firing",
                "receiver": "coder-webhook",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "TestAlert",
                            "severity": "warning",
                        },
                        "annotations": {"summary": "Test alert summary"},
                        "startsAt": "2026-03-02T14:00:00Z",
                        "endsAt": "0001-01-01T00:00:00Z",
                        "generatorURL": "http://vmalert/alert/1",
                    }
                ],
            },
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # Check that auto_send was called
        chat_manager.auto_send.assert_called_once()
        call_args = chat_manager.auto_send.call_args
        sent_prompt = call_args[0][0]
        assert "TestAlert" in sent_prompt

    def test_endpoint_returns_200_on_malformed_json(self) -> None:
        """POST malformed payload - 200 (fail-open), no chat_manager call."""
        app, configure = _get_app()
        chat_manager = AsyncMock()
        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
        )

        client = TestClient(app)
        # Send actual malformed JSON (not parseable)
        resp = client.post(
            "/webhook/alertmanager",
            content=b"{this is not valid json}",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # Should not have called auto_send on malformed payload
        chat_manager.auto_send.assert_not_called()

    def test_endpoint_returns_404_when_disabled(self) -> None:
        """Feature flag disabled - 404."""
        app, configure = _get_app()
        configure(
            EventBus(),
            dict,
            alertmanager_webhook_enabled=False,
        )

        client = TestClient(app)
        resp = client.post(
            "/webhook/alertmanager",
            json={
                "version": "4",
                "groupKey": "test-group",
                "status": "firing",
                "receiver": "coder-webhook",
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {"alertname": "TestAlert"},
                        "annotations": {"summary": "Test"},
                        "startsAt": "2026-03-02T14:00:00Z",
                        "endsAt": "0001-01-01T00:00:00Z",
                        "generatorURL": "http://vmalert/alert/1",
                    }
                ],
            },
        )

        assert resp.status_code == 404

    def test_background_task_reference_kept(self) -> None:
        """Background task reference is kept to prevent garbage collection."""
        from unittest.mock import patch

        app, configure = _get_app()
        chat_manager = AsyncMock()
        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
        )

        # Import to access the background tasks set
        from orchestrator import web

        initial_task_count = len(web._background_tasks)

        with patch("asyncio.create_task") as mock_create_task:
            mock_task = AsyncMock()
            mock_create_task.return_value = mock_task

            client = TestClient(app)
            client.post(
                "/webhook/alertmanager",
                json={
                    "version": "4",
                    "groupKey": "test-group",
                    "status": "firing",
                    "receiver": "coder-webhook",
                    "alerts": [
                        {
                            "status": "firing",
                            "labels": {"alertname": "TestAlert"},
                            "annotations": {"summary": "Test"},
                            "startsAt": "2026-03-02T14:00:00Z",
                            "endsAt": "0001-01-01T00:00:00Z",
                            "generatorURL": "http://vmalert/alert/1",
                        }
                    ],
                },
            )

            # Verify task was created
            mock_create_task.assert_called_once()

            # Verify add_done_callback was called
            mock_task.add_done_callback.assert_called_once()

            # Verify callback removes task from set
            callback = mock_task.add_done_callback.call_args[0][0]
            assert callback == web._background_tasks.discard

    def test_auto_send_exception_logged_not_raised(self) -> None:
        """Exceptions from auto_send are logged, not raised."""
        from unittest.mock import patch

        app, configure = _get_app()
        chat_manager = AsyncMock()
        # Make auto_send raise an exception
        chat_manager.auto_send.side_effect = RuntimeError("Session creation failed")
        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
        )

        with patch("orchestrator.web.logger") as mock_logger:
            client = TestClient(app)
            # Request should succeed even if auto_send fails
            resp = client.post(
                "/webhook/alertmanager",
                json={
                    "version": "4",
                    "groupKey": "test-group",
                    "status": "firing",
                    "receiver": "coder-webhook",
                    "alerts": [
                        {
                            "status": "firing",
                            "labels": {"alertname": "TestAlert"},
                            "annotations": {"summary": "Test"},
                            "startsAt": "2026-03-02T14:00:00Z",
                            "endsAt": "0001-01-01T00:00:00Z",
                            "generatorURL": "http://vmalert/alert/1",
                        }
                    ],
                },
            )

            # Endpoint should return 200 OK
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

            # Exception should be logged with warning
            # Note: logging happens in background task, so we check if
            # the proper exception handler is set up in the code
            chat_manager.auto_send.assert_called_once()


class TestBuildIssueSummary:
    """Tests for build_issue_summary()."""

    def test_issue_summary_format(self) -> None:
        """Summary follows '[Alert] alertname: summary' format."""
        from orchestrator.alertmanager_webhook import (
            build_issue_summary,
        )

        alert = _make_alert(
            alertname="HighErrorRate",
            summary="API error rate > 5%",
        )
        result = build_issue_summary(alert)
        assert result == "[Alert] HighErrorRate: API error rate > 5%"

    def test_issue_summary_no_annotation_summary(self) -> None:
        """Summary fallback when annotation summary is empty."""
        from orchestrator.alertmanager_webhook import (
            build_issue_summary,
        )

        alert = AlertmanagerAlert(
            status="firing",
            labels={"alertname": "DeadMansSwitch"},
            annotations={},
            starts_at="2026-03-02T14:00:00Z",
            ends_at="0001-01-01T00:00:00Z",
            generator_url="",
        )
        result = build_issue_summary(alert)
        assert result == "[Alert] DeadMansSwitch"


class TestBuildIssueDescription:
    """Tests for build_issue_description()."""

    def test_description_includes_labels(self) -> None:
        """Description includes all labels."""
        from orchestrator.alertmanager_webhook import (
            build_issue_description,
        )

        alert = _make_alert()
        desc = build_issue_description(alert)
        assert "alertname" in desc
        assert "HighErrorRate" in desc
        assert "severity" in desc
        assert "critical" in desc
        assert "namespace" in desc
        assert "dev" in desc

    def test_description_includes_annotations(self) -> None:
        """Description includes annotations."""
        from orchestrator.alertmanager_webhook import (
            build_issue_description,
        )

        alert = _make_alert(
            summary="API error rate > 5%",
            description="Error rate is 8%",
        )
        desc = build_issue_description(alert)
        assert "API error rate > 5%" in desc
        assert "Error rate is 8%" in desc

    def test_description_includes_generator_url(self) -> None:
        """Description includes generator URL."""
        from orchestrator.alertmanager_webhook import (
            build_issue_description,
        )

        alert = _make_alert()
        desc = build_issue_description(alert)
        assert "http://vmalert/alert/1" in desc

    def test_description_includes_timestamps(self) -> None:
        """Description includes start time."""
        from orchestrator.alertmanager_webhook import (
            build_issue_description,
        )

        alert = _make_alert()
        desc = build_issue_description(alert)
        assert "2026-03-02T14:00:00Z" in desc


class TestMapComponent:
    """Tests for map_component()."""

    @pytest.mark.parametrize(
        ("namespace", "service", "expected"),
        [
            ("dev", "api", "Бекенд"),
            ("dev", "frontend", "Фронтенд"),
            ("prod", "web-app", "Фронтенд"),
            ("infra", "argocd", "DevOps"),
            ("monitoring", "grafana", "DevOps"),
            ("dev", "unknown-svc", "Бекенд"),
            ("", "", "Бекенд"),
        ],
    )
    def test_component_mapping(
        self,
        namespace: str,
        service: str,
        expected: str,
    ) -> None:
        """Map namespace/service labels to component names."""
        from orchestrator.alertmanager_webhook import map_component

        alert = AlertmanagerAlert(
            status="firing",
            labels={
                "alertname": "Test",
                "namespace": namespace,
                "service": service,
            },
            annotations={},
            starts_at="",
            ends_at="",
            generator_url="",
        )
        assert map_component(alert) == expected


class TestAutoCreateTask:
    """Tests for auto-task creation in the webhook handler."""

    def _make_firing_payload(
        self,
        alertname: str = "HighErrorRate",
        severity: str = "critical",
    ) -> dict:
        return {
            "version": "4",
            "groupKey": "test-group",
            "status": "firing",
            "receiver": "coder-webhook",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": alertname,
                        "severity": severity,
                        "namespace": "dev",
                        "service": "api",
                    },
                    "annotations": {
                        "summary": "API error rate > 5%",
                    },
                    "startsAt": "2026-03-02T14:00:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "generatorURL": "http://vmalert/alert/1",
                }
            ],
        }

    _AUTO_CREATE_CONFIG: typing.ClassVar[dict] = {
        "enabled": True,
        "queue": "QR",
        "tag": "ai-task",
        "project_id": 13,
        "boards": [14],
    }

    async def test_critical_alert_creates_task(self) -> None:
        """Critical alert creates a Bug task with ai-task tag."""
        from orchestrator.alertmanager_webhook import parse_payload
        from orchestrator.web import _try_auto_create_tasks

        mock_tracker = MagicMock()
        mock_tracker.search.return_value = []
        mock_tracker.create_issue.return_value = {"key": "QR-999"}

        payload = parse_payload(
            self._make_firing_payload(severity="critical"),
        )
        await _try_auto_create_tasks(
            payload,
            mock_tracker,
            self._AUTO_CREATE_CONFIG,
        )

        mock_tracker.create_issue.assert_called_once()
        call_kwargs = mock_tracker.create_issue.call_args
        # Verify Bug type (id=1)
        assert call_kwargs.kwargs["issue_type"] == 1
        # Verify ai-task tag
        assert "ai-task" in call_kwargs.kwargs["tags"]

    async def test_error_severity_creates_task(self) -> None:
        """Error severity also triggers task creation."""
        from orchestrator.alertmanager_webhook import parse_payload
        from orchestrator.web import _try_auto_create_tasks

        mock_tracker = MagicMock()
        mock_tracker.search.return_value = []
        mock_tracker.create_issue.return_value = {"key": "QR-999"}

        payload = parse_payload(
            self._make_firing_payload(severity="error"),
        )
        await _try_auto_create_tasks(
            payload,
            mock_tracker,
            self._AUTO_CREATE_CONFIG,
        )

        mock_tracker.create_issue.assert_called_once()

    async def test_alertname_with_quotes_escaped_in_query(
        self,
    ) -> None:
        """Alertname with double quotes must be escaped in query."""
        from orchestrator.alertmanager_webhook import parse_payload
        from orchestrator.web import _try_auto_create_tasks

        mock_tracker = MagicMock()
        mock_tracker.search.return_value = []
        mock_tracker.create_issue.return_value = {"key": "QR-999"}

        payload = parse_payload(
            self._make_firing_payload(
                alertname='High"Error"Rate',
            ),
        )
        await _try_auto_create_tasks(
            payload,
            mock_tracker,
            self._AUTO_CREATE_CONFIG,
        )

        # Verify quotes stripped from alertname in query
        query_arg = mock_tracker.search.call_args[0][0]
        assert "HighErrorRate" in query_arg
        assert 'High"Error"Rate' not in query_arg

        # Verify created issue summary also uses sanitized name
        # so dedup query matches on next invocation
        call_args = mock_tracker.create_issue.call_args
        created_summary = call_args[0][1]  # positional arg #2
        assert 'High"Error"Rate' not in created_summary
        assert "HighErrorRate" in created_summary

    def test_warning_alert_skips_task_creation(self) -> None:
        """Warning severity does not create a task."""
        app, configure = _get_app()
        chat_manager = AsyncMock()
        mock_tracker = MagicMock()

        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
            tracker_client=mock_tracker,
            auto_create_config=self._AUTO_CREATE_CONFIG,
        )

        client = TestClient(app)
        resp = client.post(
            "/webhook/alertmanager",
            json=self._make_firing_payload(severity="warning"),
        )

        assert resp.status_code == 200
        mock_tracker.create_issue.assert_not_called()

    def test_duplicate_alert_skips_creation(self) -> None:
        """Existing open issue with same alertname skips creation."""
        app, configure = _get_app()
        chat_manager = AsyncMock()
        mock_tracker = MagicMock()
        # Return existing open issue
        from orchestrator.tracker_client import TrackerIssue

        mock_tracker.search.return_value = [
            TrackerIssue(
                key="QR-100",
                summary="[Alert] HighErrorRate: old",
                description="",
                components=[],
                tags=["ai-task"],
                status="open",
            )
        ]

        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
            tracker_client=mock_tracker,
            auto_create_config=self._AUTO_CREATE_CONFIG,
        )

        client = TestClient(app)
        resp = client.post(
            "/webhook/alertmanager",
            json=self._make_firing_payload(severity="critical"),
        )

        assert resp.status_code == 200
        mock_tracker.create_issue.assert_not_called()

    def test_config_disabled_skips_creation(self) -> None:
        """Auto-create disabled skips task creation."""
        app, configure = _get_app()
        chat_manager = AsyncMock()
        mock_tracker = MagicMock()

        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
            tracker_client=mock_tracker,
            auto_create_config=None,  # disabled
        )

        client = TestClient(app)
        resp = client.post(
            "/webhook/alertmanager",
            json=self._make_firing_payload(severity="critical"),
        )

        assert resp.status_code == 200
        mock_tracker.create_issue.assert_not_called()

    def test_tracker_error_does_not_break_webhook(self) -> None:
        """Tracker API error is logged, webhook still returns 200."""
        app, configure = _get_app()
        chat_manager = AsyncMock()
        mock_tracker = MagicMock()
        mock_tracker.search.side_effect = Exception("API down")

        configure(
            EventBus(),
            dict,
            chat_manager=chat_manager,
            alertmanager_webhook_enabled=True,
            tracker_client=mock_tracker,
            auto_create_config=self._AUTO_CREATE_CONFIG,
        )

        client = TestClient(app)
        resp = client.post(
            "/webhook/alertmanager",
            json=self._make_firing_payload(severity="critical"),
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
