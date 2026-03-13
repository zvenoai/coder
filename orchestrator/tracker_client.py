"""Yandex Tracker REST API client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from orchestrator.constants import ResolutionType
from orchestrator.tracker_enums import (
    matches_cancelled,
    matches_close,
    matches_needs_info,
    matches_open,
    matches_progress,
    matches_review,
)
from orchestrator.tracker_types import (
    TrackerAttachmentDict,
    TrackerChecklistItemDict,
    TrackerCommentDict,
    TrackerLinkDict,
    TrackerTransitionDict,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tracker.yandex.net/v2"

# Transition search depth limits
MAX_TRANSITION_DEPTH_STANDARD = 3  # for in_progress, review, needs_info
MAX_TRANSITION_DEPTH_CLOSED = 4  # for closed (may need more hops)

# HTTP status codes
HTTP_TOO_MANY_REQUESTS = 429
HTTP_NO_CONTENT = 204

# Retry configuration
MAX_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_AFTER_SECONDS = 5

# Issue defaults
DEFAULT_ISSUE_TYPE_TASK = 2

# Search pagination
SEARCH_PER_PAGE = 100

# Attachment constraints
MAX_ATTACHMENT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# Text MIME types that can be returned as text content
TEXT_MIMETYPES = frozenset(
    {
        "text/xml",
        "application/xml",
        "application/json",
        "text/csv",
        "text/plain",
        "text/markdown",
        "text/html",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
    }
)


@dataclass
class TransitionStrategy:
    """Strategy for finding and executing a specific status transition.

    Encapsulates the logic for transitioning to a target status, including:
    - Direct matcher: function to check if transition goes to target status
    - Intermediate matchers: ordered list of functions to check intermediate steps
    - Max depth: maximum recursion depth before giving up
    - Name: human-readable name for logging
    """

    name: str
    max_depth: int
    direct_matcher: Any  # Callable[[str, str], bool]
    intermediate_matchers: list[Any]  # list[Callable[[str, str], bool]]


# Predefined strategies for each transition type
STRATEGY_IN_PROGRESS = TransitionStrategy(
    name="in progress",
    max_depth=MAX_TRANSITION_DEPTH_STANDARD,
    direct_matcher=matches_progress,
    intermediate_matchers=[matches_open],
)

STRATEGY_REVIEW = TransitionStrategy(
    name="review",
    max_depth=MAX_TRANSITION_DEPTH_STANDARD,
    direct_matcher=matches_review,
    intermediate_matchers=[matches_progress, matches_open],
)

STRATEGY_CLOSED = TransitionStrategy(
    name="closed",
    max_depth=MAX_TRANSITION_DEPTH_CLOSED,
    direct_matcher=matches_close,
    intermediate_matchers=[matches_review, matches_progress, matches_open],
)

STRATEGY_NEEDS_INFO = TransitionStrategy(
    name="needs info",
    max_depth=MAX_TRANSITION_DEPTH_STANDARD,
    direct_matcher=matches_needs_info,
    intermediate_matchers=[matches_progress, matches_open],
)

STRATEGY_CANCELLED = TransitionStrategy(
    name="cancelled",
    max_depth=MAX_TRANSITION_DEPTH_STANDARD,
    direct_matcher=matches_cancelled,
    intermediate_matchers=[matches_progress, matches_open],
)


@dataclass
class TrackerIssue:
    key: str
    summary: str
    description: str
    components: list[str]
    tags: list[str]
    status: str
    type_key: str = ""


class TrackerClient:
    """Minimal Yandex Tracker REST API client."""

    def __init__(self, token: str, org_id: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"OAuth {token}",
                "X-Org-ID": org_id,
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        max_retries: int = MAX_RETRY_ATTEMPTS,
    ) -> Any:
        """Make an API request with retry on rate-limit (429)."""
        url = f"{BASE_URL}{path}"
        for attempt in range(max_retries):
            resp = self._session.request(method, url, json=json, params=params)
            if resp.status_code == HTTP_TOO_MANY_REQUESTS:
                retry_after = int(resp.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER_SECONDS)))
                logger.warning("Rate limited, retrying after %ds (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            if resp.status_code == HTTP_NO_CONTENT:
                return None
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str) -> list[TrackerIssue]:
        """Search issues by Tracker Query Language.

        Paginates through all result pages to avoid silently dropping issues
        beyond the default API page size (50).
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(
                "POST",
                "/issues/_search",
                json={"query": query},
                params={"page": page, "perPage": SEARCH_PER_PAGE},
            )
            if not data:
                break
            all_items.extend(data)
            if len(data) < SEARCH_PER_PAGE:
                break
            page += 1
        return [self._parse_issue(item) for item in all_items]

    def get_issue(self, issue_key: str) -> TrackerIssue:
        """Get a single issue by key."""
        data = self._request("GET", f"/issues/{issue_key}")
        return self._parse_issue(data)

    def get_comments(self, issue_key: str) -> list[TrackerCommentDict]:
        """Get comments for an issue."""
        return self._request("GET", f"/issues/{issue_key}/comments")

    def get_checklist(self, issue_key: str) -> list[TrackerChecklistItemDict]:
        """Get checklist items for an issue."""
        data = self._request("GET", f"/issues/{issue_key}/checklistItems")
        return data if data else []

    def get_transitions(self, issue_key: str) -> list[TrackerTransitionDict]:
        """Get available status transitions for an issue."""
        return self._request("GET", f"/issues/{issue_key}/transitions")

    def get_links(self, issue_key: str) -> list[TrackerLinkDict]:
        """Get links for an issue."""
        return self._request("GET", f"/issues/{issue_key}/links")

    def get_attachments(self, issue_key: str) -> list[TrackerAttachmentDict]:
        """Get list of attachments for an issue."""
        return self._request("GET", f"/issues/{issue_key}/attachments")

    def download_attachment(self, attachment_id: int) -> tuple[bytes, str]:
        """Download attachment content. Returns (content_bytes, content_type).

        Raises ValueError if file exceeds MAX_ATTACHMENT_SIZE_BYTES.
        """
        url = f"{BASE_URL}/attachments/{attachment_id}"
        for attempt in range(MAX_RETRY_ATTEMPTS):
            resp = self._session.get(url, stream=True)
            try:
                if resp.status_code == HTTP_TOO_MANY_REQUESTS:
                    # Parse Retry-After header (supports both integer seconds and HTTP-date)
                    retry_after_header = resp.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER_SECONDS))
                    try:
                        retry_after = int(retry_after_header)
                    except ValueError:
                        # If not an integer, assume HTTP-date format and use default
                        retry_after = DEFAULT_RETRY_AFTER_SECONDS
                    logger.warning(
                        "Rate limited, retrying after %ds (attempt %d)",
                        retry_after,
                        attempt + 1,
                    )
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                # Check Content-Length header as a first defense
                content_length_header = resp.headers.get("Content-Length")
                if content_length_header:
                    content_length = int(content_length_header)
                    if content_length > MAX_ATTACHMENT_SIZE_BYTES:
                        raise ValueError(
                            f"Attachment {attachment_id} is too large: "
                            f"{content_length} bytes (limit: {MAX_ATTACHMENT_SIZE_BYTES})"
                        )

                # Stream content and check size incrementally to prevent memory exhaustion
                # Content-Length can be missing, incorrect, or smaller than decompressed content
                chunks = []
                total_size = 0
                for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                    if chunk:  # filter out keep-alive new chunks
                        total_size += len(chunk)
                        if total_size > MAX_ATTACHMENT_SIZE_BYTES:
                            raise ValueError(
                                f"Attachment {attachment_id} is too large: "
                                f">{total_size} bytes (limit: {MAX_ATTACHMENT_SIZE_BYTES})"
                            )
                        chunks.append(chunk)

                content = b"".join(chunks)
                content_type = resp.headers.get("Content-Type", "application/octet-stream")
                return content, content_type
            finally:
                # Always close response to prevent connection leaks when using stream=True
                resp.close()
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

    def execute_transition(self, issue_key: str, transition_id: str, comment: str | None = None) -> Any:
        """Execute a status transition on an issue."""
        body: dict[str, Any] = {}
        if comment:
            body["comment"] = comment
        return self._request("POST", f"/issues/{issue_key}/transitions/{transition_id}/_execute", json=body)

    def add_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        """Add a comment to an issue."""
        return self._request("POST", f"/issues/{issue_key}/comments", json={"text": text})

    def update_comment(
        self,
        issue_key: str,
        comment_id: int,
        text: str,
    ) -> dict[str, Any]:
        """Update an existing comment on an issue."""
        return self._request(
            "PATCH",
            f"/issues/{issue_key}/comments/{comment_id}",
            json={"text": text},
        )

    def update_issue_tags(self, issue_key: str, tags: list[str]) -> dict[str, Any]:
        """Replace all issue tags with the provided list."""
        return self.update_issue(issue_key, tags=tags)

    def update_issue(
        self,
        issue_key: str,
        *,
        summary: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update fields of an existing issue via PATCH.

        Only provided fields are sent; omitted fields remain unchanged.
        Tags replaces all existing tags — pass the full desired tag list.

        Args:
            issue_key: Issue key (e.g., "QR-123").
            summary: New summary/title, or None to leave unchanged.
            description: New description, or None to leave unchanged.
            tags: Full replacement tag list, or None to leave unchanged.

        Returns:
            Updated issue dict from the Tracker API.

        Raises:
            ValueError: If no fields are provided.
        """
        payload: dict[str, Any] = {}
        if summary is not None:
            payload["summary"] = summary
        if description is not None:
            payload["description"] = description
        if tags is not None:
            payload["tags"] = tags
        if not payload:
            raise ValueError("At least one field must be provided.")
        return self._request("PATCH", f"/issues/{issue_key}", json=payload)

    def _execute_strategy(
        self,
        issue_key: str,
        strategy: TransitionStrategy,
        *,
        resolution: str | None = None,
        comment: str | None = None,
        _depth: int = 0,
    ) -> None:
        """Generic transition executor using strategy pattern.

        Finds and executes a transition to reach the target status defined by the strategy.
        Supports chaining through intermediate statuses with configurable depth limit.

        Args:
            issue_key: Issue key (e.g., "QR-123")
            strategy: TransitionStrategy defining target status and intermediates
            resolution: Optional resolution for closed transitions
            comment: Optional comment to add with transition
            _depth: Internal recursion depth counter
        """
        # Check recursion depth
        if _depth > strategy.max_depth:
            logger.warning(
                "Could not reach '%s' for %s after %d hops",
                strategy.name,
                issue_key,
                _depth,
            )
            return None

        transitions = self.get_transitions(issue_key)

        # Try direct match
        for t in transitions:
            display = t.get("display", "").lower()
            t_id = t.get("id", "")
            if strategy.direct_matcher(display, t_id):
                # Special handling for closed transition (needs resolution)
                if resolution is not None:
                    body: dict[str, Any] = {"resolution": resolution}
                    if comment:
                        body["comment"] = comment
                    self._request(
                        "POST",
                        f"/issues/{issue_key}/transitions/{t_id}/_execute",
                        json=body,
                    )
                # Only pass comment if it's not None to match test expectations
                elif comment:
                    self.execute_transition(issue_key, t_id, comment)
                else:
                    self.execute_transition(issue_key, t_id)
                return None

        # Try intermediates in priority order
        for intermediate_matcher in strategy.intermediate_matchers:
            for t in transitions:
                display = t.get("display", "").lower()
                t_id = t.get("id", "")
                if intermediate_matcher(display, t_id):
                    self.execute_transition(issue_key, t_id)
                    return self._execute_strategy(
                        issue_key,
                        strategy,
                        resolution=resolution,
                        comment=comment,
                        _depth=_depth + 1,
                    )

        # No transition found
        logger.warning(
            "No '%s' transition found for %s, available: %s",
            strategy.name,
            issue_key,
            transitions,
        )

    def transition_to_in_progress(self, issue_key: str, _depth: int = 0) -> None:
        """Find and execute an 'in progress' transition, chaining through intermediates."""
        self._execute_strategy(issue_key, STRATEGY_IN_PROGRESS, _depth=_depth)

    def transition_to_review(self, issue_key: str, _depth: int = 0) -> None:
        """Find and execute a 'review' transition, chaining through intermediates."""
        self._execute_strategy(issue_key, STRATEGY_REVIEW, _depth=_depth)

    def transition_to_closed(
        self, issue_key: str, resolution: str = ResolutionType.FIXED, comment: str | None = None, _depth: int = 0
    ) -> None:
        """Find and execute a 'close/done' transition with resolution, chaining through intermediates."""
        self._execute_strategy(issue_key, STRATEGY_CLOSED, resolution=resolution, comment=comment, _depth=_depth)

    def transition_to_needs_info(self, issue_key: str, _depth: int = 0) -> None:
        """Find and execute a 'needs info' transition, chaining through intermediates."""
        self._execute_strategy(issue_key, STRATEGY_NEEDS_INFO, _depth=_depth)

    def transition_to_cancelled(self, issue_key: str, comment: str | None = None, _depth: int = 0) -> None:
        """Find and execute a 'cancelled' transition, chaining through intermediates."""
        self._execute_strategy(issue_key, STRATEGY_CANCELLED, comment=comment, _depth=_depth)

    def create_issue(
        self,
        queue: str,
        summary: str,
        description: str,
        *,
        issue_type: int = DEFAULT_ISSUE_TYPE_TASK,
        components: list[str] | None = None,
        assignee: str | None = None,
        project_id: int | None = None,
        boards: list[int] | None = None,
        tags: list[str] | None = None,
        parent: str | None = None,
    ) -> dict[str, Any]:
        """Create a new issue in the given queue."""
        body: dict[str, Any] = {
            "queue": queue,
            "summary": summary,
            "description": description,
            "type": {"id": str(issue_type)},
        }
        if components:
            body["components"] = [{"name": c} for c in components]
        if assignee:
            body["assignee"] = assignee
        if project_id is not None:
            body["project"] = {"id": str(project_id)}
        if boards:
            body["boards"] = [{"id": b} for b in boards]
        if tags:
            body["tags"] = tags
        if parent:
            body["parent"] = parent
        return self._request("POST", "/issues", json=body)

    def add_link(
        self,
        issue_key: str,
        target_key: str,
        relationship: str = "relates",
    ) -> dict[str, Any]:
        """Add a link between two issues."""
        return self._request(
            "POST",
            f"/issues/{issue_key}/links",
            json={"relationship": relationship, "issue": target_key},
        )

    def create_subtask(
        self,
        parent_key: str,
        queue: str,
        summary: str,
        description: str,
        *,
        issue_type: int = DEFAULT_ISSUE_TYPE_TASK,
        components: list[str] | None = None,
        assignee: str | None = None,
        project_id: int | None = None,
        boards: list[int] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a task and link it to parent as a subtask.

        Attempts to create the issue with parent parameter first. If that fails,
        creates the issue without parent and manually adds the link. If manual
        linking also fails, returns the created issue with link_failed flag set.

        Returns:
            Created issue dict with optional link_failed boolean flag.
        """
        try:
            # Try creating with parent parameter (Tracker creates link automatically)
            return self.create_issue(
                queue=queue,
                summary=summary,
                description=description,
                issue_type=issue_type,
                components=components,
                assignee=assignee,
                project_id=project_id,
                boards=boards,
                tags=tags,
                parent=parent_key,
            )
        except requests.HTTPError:
            # Parent parameter failed, try creating without parent then manually link
            result = self.create_issue(
                queue=queue,
                summary=summary,
                description=description,
                issue_type=issue_type,
                components=components,
                assignee=assignee,
                project_id=project_id,
                boards=boards,
                tags=tags,
                parent=None,
            )
            try:
                # Try to manually add the subtask link
                self.add_link(result["key"], parent_key, "is subtask for")
                return result
            except requests.HTTPError:
                # Manual linking also failed, flag it
                result["link_failed"] = True
                return result

    def get_myself_login(self) -> str:
        """Get the login of the authenticated user (bot identity)."""
        data = self._request("GET", "/myself")
        return data.get("login", "")

    @staticmethod
    def _parse_issue(data: dict[str, Any]) -> TrackerIssue:
        components = [c.get("display", c.get("name", "")) for c in data.get("components", [])]
        tags = data.get("tags", [])
        status = data.get("status", {}).get("key", "")
        type_key = data.get("type", {}).get("key", "")
        return TrackerIssue(
            key=data["key"],
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            components=components,
            tags=tags,
            status=status,
            type_key=type_key,
        )
