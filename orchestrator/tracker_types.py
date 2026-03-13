"""Type definitions for Tracker API responses."""

from typing import TypedDict


class TrackerUserDict(TypedDict, total=False):
    """Tracker user structure."""

    login: str
    display: str
    uid: str


class TrackerStatusDict(TypedDict):
    """Tracker status structure."""

    key: str
    display: str


class TrackerComponentDict(TypedDict, total=False):
    """Tracker component structure."""

    name: str
    display: str


class TrackerTransitionDict(TypedDict):
    """Tracker transition structure."""

    id: str
    display: str
    to: TrackerStatusDict


class TrackerCommentDict(TypedDict, total=False):
    """Tracker comment structure."""

    id: int
    text: str
    createdBy: TrackerUserDict
    createdAt: str
    updatedAt: str


class TrackerChecklistItemDict(TypedDict, total=False):
    """Tracker checklist item structure."""

    id: str
    text: str
    checked: bool


class IssueCreateBody(TypedDict, total=False):
    """Request body for creating an issue."""

    queue: str
    summary: str
    description: str
    type: dict[str, str]  # {"id": "2"}
    components: list[dict[str, str]]  # [{"name": "Backend"}]
    assignee: str
    project: dict[str, str]  # {"id": "13"}
    boards: list[dict[str, int]]  # [{"id": 14}]
    tags: list[str]


class IssueLinkBody(TypedDict):
    """Request body for creating an issue link."""

    relationship: str
    issue: str


class TrackerLinkDict(TypedDict, total=False):
    """Tracker issue link structure."""

    id: str
    relationship: str
    direction: str
    type: str
    issue: dict[str, str]


class TrackerAttachmentDict(TypedDict, total=False):
    """Tracker attachment structure."""

    id: int
    name: str
    mimetype: str
    size: int
    createdBy: TrackerUserDict
    createdAt: str
