"""Enums and helpers for Tracker status matching."""

from enum import StrEnum


class StatusKeyword(StrEnum):
    """Keywords for matching transition displays and IDs."""

    # In Progress
    PROGRESS = "progress"
    IN_WORK_RU = "работ"

    # Open
    OPEN = "open"
    OPEN_RU = "откры"

    # Review
    REVIEW = "review"
    REVIEW_RU = "ревью"
    REVIEW_CHECK_RU = "на проверк"
    TESTING = "testing"

    # Close/Done
    CLOSE = "close"
    DONE = "done"
    CLOSED_RU = "закры"
    RESOLVED_RU = "решен"
    READY_RU = "готов"
    RESOLVE = "resolve"

    # Needs Info
    INFO_RU = "информац"
    INFO = "info"
    NEED = "need"
    REQUIRED_RU = "требуется"
    CLARIFY_RU = "уточн"

    # Cancelled
    CANCEL = "cancel"
    CANCEL_RU = "отмен"


def matches_progress(display: str, t_id: str) -> bool:
    """Check if transition matches 'in progress' pattern.

    Args:
        display: Transition display name (lowercased)
        t_id: Transition ID

    Returns:
        True if matches progress keywords
    """
    return StatusKeyword.PROGRESS in display or StatusKeyword.IN_WORK_RU in display or StatusKeyword.PROGRESS in t_id


def matches_open(display: str, t_id: str) -> bool:
    """Check if transition matches 'open' pattern.

    Args:
        display: Transition display name (lowercased)
        t_id: Transition ID

    Returns:
        True if matches open keywords
    """
    return StatusKeyword.OPEN in t_id or StatusKeyword.OPEN_RU in display


def matches_review(display: str, t_id: str) -> bool:
    """Check if transition matches 'review' pattern.

    Args:
        display: Transition display name (lowercased)
        t_id: Transition ID

    Returns:
        True if matches review keywords
    """
    display_keywords = (
        StatusKeyword.REVIEW,
        StatusKeyword.REVIEW_RU,
        StatusKeyword.REVIEW_CHECK_RU,
    )
    id_keywords = (StatusKeyword.REVIEW, StatusKeyword.TESTING)

    return any(kw in display for kw in display_keywords) or any(kw in t_id for kw in id_keywords)


def matches_close(display: str, t_id: str) -> bool:
    """Check if transition matches 'close/done' pattern.

    Args:
        display: Transition display name (lowercased)
        t_id: Transition ID

    Returns:
        True if matches close keywords
    """
    display_keywords = (
        StatusKeyword.CLOSE,
        StatusKeyword.DONE,
        StatusKeyword.CLOSED_RU,
        StatusKeyword.RESOLVED_RU,
        StatusKeyword.READY_RU,
    )
    id_keywords = (
        StatusKeyword.CLOSE,
        StatusKeyword.DONE,
        StatusKeyword.RESOLVE,
    )

    return any(kw in display for kw in display_keywords) or any(kw in t_id for kw in id_keywords)


def matches_needs_info(display: str, t_id: str) -> bool:
    """Check if transition matches 'needs info' pattern.

    Args:
        display: Transition display name (lowercased)
        t_id: Transition ID

    Returns:
        True if matches needs-info keywords
    """
    display_keywords = (
        StatusKeyword.INFO_RU,
        StatusKeyword.INFO,
        StatusKeyword.NEED,
        StatusKeyword.REQUIRED_RU,
        StatusKeyword.CLARIFY_RU,
    )
    id_keywords = ("needInfo", "need_info", "needsInfo")  # camelCase/snake_case variations

    return any(kw in display for kw in display_keywords) or any(kw in t_id for kw in id_keywords)


def matches_cancelled(display: str, t_id: str) -> bool:
    """Check if transition matches 'cancelled' pattern."""
    return (
        StatusKeyword.CANCEL in display
        or StatusKeyword.CANCEL_RU in display
        or StatusKeyword.CANCEL in t_id
        or StatusKeyword.CANCEL_RU in t_id
    )
