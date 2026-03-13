"""Alertmanager webhook dataclasses, parsing, and prompt formatting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertmanagerAlert:
    """Single alert from Alertmanager webhook payload."""

    status: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: str
    ends_at: str
    generator_url: str


@dataclass(frozen=True)
class AlertmanagerPayload:
    """Alertmanager webhook payload (v4)."""

    version: str
    status: str
    group_key: str
    receiver: str
    alerts: list[AlertmanagerAlert]


def parse_payload(raw: dict) -> AlertmanagerPayload:
    """Parse Alertmanager webhook payload and filter firing alerts.

    Args:
        raw: Raw dict from Alertmanager webhook JSON body

    Returns:
        Parsed payload with only firing alerts
    """
    raw_alerts = raw.get("alerts", [])

    alerts = []
    for alert_dict in raw_alerts:
        status = alert_dict.get("status", "")
        if status != "firing":
            continue

        alert = AlertmanagerAlert(
            status=status,
            labels=alert_dict.get("labels", {}),
            annotations=alert_dict.get("annotations", {}),
            starts_at=alert_dict.get("startsAt", ""),
            ends_at=alert_dict.get("endsAt", ""),
            generator_url=alert_dict.get("generatorURL", ""),
        )
        alerts.append(alert)

    return AlertmanagerPayload(
        version=raw.get("version", ""),
        status=raw.get("status", ""),
        group_key=raw.get("groupKey", ""),
        receiver=raw.get("receiver", ""),
        alerts=alerts,
    )


# Severity levels that trigger auto-task creation
AUTO_CREATE_SEVERITIES = frozenset({"critical", "error"})

# Service-to-component mapping keywords
_FRONTEND_KEYWORDS = frozenset({"frontend", "web-app", "web", "nextjs", "react"})
_DEVOPS_KEYWORDS = frozenset({"argocd", "grafana", "prometheus", "victoria", "nginx", "k8s"})
_DEVOPS_NAMESPACES = frozenset({"infra", "monitoring", "kube-system"})


def build_issue_summary(alert: AlertmanagerAlert) -> str:
    """Format alert as Tracker issue summary.

    Args:
        alert: Parsed alert from Alertmanager webhook.

    Returns:
        Summary string: "[Alert] alertname: summary" or
        "[Alert] alertname" if no summary annotation.
    """
    alertname = alert.labels.get("alertname", "unknown")
    # Sanitize to match dedup query sanitization in web.py
    safe = alertname.replace('"', "").replace("\\", "")
    summary = alert.annotations.get("summary", "")
    if summary:
        return f"[Alert] {safe}: {summary}"
    return f"[Alert] {safe}"


def build_issue_description(alert: AlertmanagerAlert) -> str:
    """Format alert details as Tracker issue description.

    Includes all labels, annotations, generator URL, and timestamps
    in a structured markdown format.

    Args:
        alert: Parsed alert from Alertmanager webhook.

    Returns:
        Markdown-formatted description string.
    """
    lines: list[str] = []

    lines.append("## Alert Details\n")

    # Labels
    lines.append("### Labels")
    for key, value in sorted(alert.labels.items()):
        lines.append(f"- **{key}:** {value}")
    lines.append("")

    # Annotations
    if alert.annotations:
        lines.append("### Annotations")
        for key, value in sorted(alert.annotations.items()):
            lines.append(f"- **{key}:** {value}")
        lines.append("")

    # Timestamps
    lines.append("### Timing")
    lines.append(f"- **Started:** {alert.starts_at}")
    if alert.ends_at and alert.ends_at != "0001-01-01T00:00:00Z":
        lines.append(f"- **Ended:** {alert.ends_at}")
    lines.append("")

    # Generator URL
    if alert.generator_url:
        lines.append(f"**Source:** {alert.generator_url}")

    return "\n".join(lines)


def map_component(alert: AlertmanagerAlert) -> str:
    """Map alert namespace/service labels to a Tracker component.

    Args:
        alert: Parsed alert from Alertmanager webhook.

    Returns:
        Component name string.
    """
    namespace = alert.labels.get("namespace", "").lower()
    service = alert.labels.get("service", "").lower()

    # Check DevOps namespace first
    if namespace in _DEVOPS_NAMESPACES:
        return "DevOps"

    # Check service keywords (substring match)
    if service:
        if any(kw in service for kw in _DEVOPS_KEYWORDS):
            return "DevOps"
        if any(kw in service for kw in _FRONTEND_KEYWORDS):
            return "Фронтенд"

    # Default to backend
    return "Бекенд"


def format_alert_prompt(payload: AlertmanagerPayload) -> str:
    """Format alert payload as a supervisor prompt.

    Args:
        payload: Parsed Alertmanager webhook payload

    Returns:
        Formatted prompt string for supervisor
    """
    count = len(payload.alerts)
    lines = [f"[System] Alertmanager: {count} firing alert(s) received.\n"]

    for alert in payload.alerts:
        alertname = alert.labels.get("alertname", "unknown")
        severity = alert.labels.get("severity", "unknown")
        namespace = alert.labels.get("namespace", "")
        service = alert.labels.get("service", "")

        # Build label info string
        label_parts = []
        if namespace:
            label_parts.append(f"namespace={namespace}")
        if service:
            label_parts.append(f"service={service}")
        label_info = ", ".join(label_parts) if label_parts else "no labels"

        lines.append(f"- **{alertname}** ({severity}) in {label_info}")

        summary = alert.annotations.get("summary", "")
        if summary:
            lines.append(f"  Summary: {summary}")

        lines.append(f"  Started: {alert.starts_at}")

        if alert.generator_url:
            lines.append(f"  Generator: {alert.generator_url}")

        lines.append("")

    lines.append("Please investigate and take appropriate action.")

    return "\n".join(lines)
