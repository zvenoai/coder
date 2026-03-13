"""Tests for tracker_client module."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest
import requests

from orchestrator.tracker_client import TrackerClient


def _make_client(**overrides) -> TrackerClient:
    """Create a TrackerClient with mocked internals."""
    return TrackerClient(token="t", org_id="o", **overrides)


class TestParseIssue:
    def test_parse_full_issue(self) -> None:
        raw = {
            "key": "QR-1",
            "summary": "Test task",
            "description": "Do something",
            "components": [{"display": "Бекенд"}],
            "tags": ["ai-task"],
            "status": {"key": "open"},
            "type": {"key": "epic"},
        }
        issue = TrackerClient._parse_issue(raw)
        assert issue.key == "QR-1"
        assert issue.summary == "Test task"
        assert issue.description == "Do something"
        assert issue.components == ["Бекенд"]
        assert issue.tags == ["ai-task"]
        assert issue.status == "open"
        assert issue.type_key == "epic"

    def test_parse_minimal_issue(self) -> None:
        raw = {"key": "QR-2"}
        issue = TrackerClient._parse_issue(raw)
        assert issue.key == "QR-2"
        assert issue.summary == ""
        assert issue.components == []
        assert issue.tags == []
        assert issue.type_key == ""


class TestSearch:
    @patch("orchestrator.tracker_client.requests.Session")
    def test_search_returns_issues(self, mock_session_cls) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "key": "QR-10",
                "summary": "Found task",
                "description": "",
                "components": [],
                "tags": ["ai-task"],
                "status": {"key": "open"},
            }
        ]
        mock_session = MagicMock()
        mock_session.request.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = _make_client()
        client._session = mock_session

        issues = client.search('Tags: "ai-task"')
        assert len(issues) == 1
        assert issues[0].key == "QR-10"

    def test_search_paginates_through_all_pages(self) -> None:
        from orchestrator.tracker_client import SEARCH_PER_PAGE

        page1 = [{"key": f"QR-{i}", "status": {"key": "open"}} for i in range(SEARCH_PER_PAGE)]
        page2 = [{"key": f"QR-{SEARCH_PER_PAGE + i}", "status": {"key": "open"}} for i in range(3)]

        resp1 = MagicMock(status_code=200)
        resp1.json.return_value = page1
        resp2 = MagicMock(status_code=200)
        resp2.json.return_value = page2

        client = _make_client()
        client._session = MagicMock()
        client._session.request.side_effect = [resp1, resp2]

        issues = client.search("test query")
        assert len(issues) == SEARCH_PER_PAGE + 3

        calls = client._session.request.call_args_list
        assert len(calls) == 2
        assert calls[0][1]["params"]["page"] == 1
        assert calls[0][1]["params"]["perPage"] == SEARCH_PER_PAGE
        assert calls[1][1]["params"]["page"] == 2

    def test_search_stops_on_empty_page(self) -> None:
        resp = MagicMock(status_code=200)
        resp.json.return_value = []

        client = _make_client()
        client._session = MagicMock()
        client._session.request.return_value = resp

        issues = client.search("test query")
        assert issues == []
        assert client._session.request.call_count == 1


class TestRetry:
    @patch("orchestrator.tracker_client.time.sleep")
    def test_retries_on_429(self, mock_sleep) -> None:
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "1"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = []

        mock_session = MagicMock()
        mock_session.request.side_effect = [rate_resp, ok_resp]

        client = _make_client()
        client._session = mock_session

        result = client.search("test")
        assert result == []
        mock_sleep.assert_called_once_with(1)


class TestTransitionToInProgress:
    @pytest.mark.parametrize(
        ("transition_id", "display"),
        [
            ("start_progress", "In Progress"),
            ("in_work", "В работу"),
        ],
    )
    def test_finds_matching_transition(self, transition_id, display) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": transition_id, "display": display}])
        client.execute_transition = MagicMock()

        client.transition_to_in_progress("QR-1")
        client.execute_transition.assert_called_once_with("QR-1", transition_id)

    def test_no_matching_transition_logs_warning(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": "close", "display": "Close"}])
        client.execute_transition = MagicMock()

        client.transition_to_in_progress("QR-1")
        client.execute_transition.assert_not_called()


class TestTransitionToClosed:
    @pytest.mark.parametrize(
        ("transition_id", "display"),
        [
            ("close", "Close"),
            ("resolve", "Решить"),
        ],
    )
    def test_finds_close_transition(self, transition_id, display) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": transition_id, "display": display}])
        client._request = MagicMock()

        client.transition_to_closed("QR-1", resolution="fixed", comment="Done")
        client._request.assert_called_once()
        call_args = client._request.call_args
        assert transition_id in call_args[0][1]

    def test_no_close_transition(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": "start", "display": "Start"}])
        client._request = MagicMock()

        client.transition_to_closed("QR-1")
        client._request.assert_not_called()


class TestChainTransitionToInProgress:
    def test_chains_through_open_to_in_progress(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(
            side_effect=[
                [{"id": "open", "display": "Открыть"}],
                [{"id": "start_progress", "display": "In Progress"}],
            ]
        )
        client.execute_transition = MagicMock()

        client.transition_to_in_progress("QR-1")

        assert client.execute_transition.call_count == 2
        client.execute_transition.assert_any_call("QR-1", "open")
        client.execute_transition.assert_any_call("QR-1", "start_progress")

    def test_stops_after_max_depth(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": "open", "display": "Открыть"}])
        client.execute_transition = MagicMock()

        client.transition_to_in_progress("QR-1")
        assert client.execute_transition.call_count == 4


class TestChainTransitionToReview:
    def test_chains_through_progress_to_review(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(
            side_effect=[
                [{"id": "start_progress", "display": "В работу"}],
                [{"id": "need_review", "display": "На ревью"}],
            ]
        )
        client.execute_transition = MagicMock()

        client.transition_to_review("QR-1")

        assert client.execute_transition.call_count == 2
        client.execute_transition.assert_any_call("QR-1", "start_progress")
        client.execute_transition.assert_any_call("QR-1", "need_review")

    def test_chains_through_open_then_progress_to_review(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(
            side_effect=[
                [{"id": "open", "display": "Открыт"}],
                [{"id": "start_progress", "display": "В работу"}],
                [{"id": "need_review", "display": "На ревью"}],
            ]
        )
        client.execute_transition = MagicMock()

        client.transition_to_review("QR-1")

        assert client.execute_transition.call_count == 3
        client.execute_transition.assert_any_call("QR-1", "open")
        client.execute_transition.assert_any_call("QR-1", "start_progress")
        client.execute_transition.assert_any_call("QR-1", "need_review")


class TestChainTransitionToClosed:
    def test_chains_through_review_to_closed(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(
            side_effect=[
                [{"id": "review", "display": "На ревью"}],
                [{"id": "close", "display": "Закрыть"}],
            ]
        )
        client.execute_transition = MagicMock()
        client._request = MagicMock()

        client.transition_to_closed("QR-1", resolution="fixed", comment="Done")

        client.execute_transition.assert_called_once_with("QR-1", "review")
        client._request.assert_called_once()
        call_args = client._request.call_args
        assert "close" in call_args[0][1]
        assert call_args[1]["json"]["resolution"] == "fixed"


class TestTransitionToNeedsInfo:
    @pytest.mark.parametrize(
        ("transition_id", "display"),
        [
            ("needInfo", "Требуется информация"),
            ("needsInfo", "Some other name"),
        ],
    )
    def test_finds_needs_info_transition(self, transition_id, display) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": transition_id, "display": display}])
        client.execute_transition = MagicMock()

        client.transition_to_needs_info("QR-1")
        client.execute_transition.assert_called_once_with("QR-1", transition_id)

    @pytest.mark.parametrize(
        ("chain_transitions", "expected_calls"),
        [
            # chains through in_progress
            (
                [
                    [{"id": "start_progress", "display": "В работу"}],
                    [
                        {
                            "id": "needInfo",
                            "display": "Требуется информация",
                        }
                    ],
                ],
                [("QR-1", "start_progress"), ("QR-1", "needInfo")],
            ),
            # chains through open
            (
                [
                    [{"id": "open", "display": "Открыть"}],
                    [{"id": "need_info", "display": "Need Info"}],
                ],
                [("QR-1", "open"), ("QR-1", "need_info")],
            ),
        ],
    )
    def test_chains_intermediate_transitions(self, chain_transitions, expected_calls) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(side_effect=chain_transitions)
        client.execute_transition = MagicMock()

        client.transition_to_needs_info("QR-1")
        assert client.execute_transition.call_count == len(expected_calls)
        for issue_key, tid in expected_calls:
            client.execute_transition.assert_any_call(issue_key, tid)

    def test_stops_after_max_depth(self) -> None:
        client = _make_client()
        client.get_transitions = MagicMock(return_value=[{"id": "open", "display": "Открыть"}])
        client.execute_transition = MagicMock()

        client.transition_to_needs_info("QR-1")
        assert client.execute_transition.call_count == 4


class TestGetMyselfLogin:
    @pytest.mark.parametrize(
        ("api_response", "expected"),
        [
            ({"login": "bot-user", "display": "Bot"}, "bot-user"),
            ({"display": "Bot"}, ""),
        ],
    )
    def test_returns_login_or_empty(self, api_response, expected) -> None:
        client = _make_client()
        client._request = MagicMock(return_value=api_response)
        assert client.get_myself_login() == expected


class TestGetComments:
    def test_get_comments(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value=[{"text": "hello"}])

        result = client.get_comments("QR-1")
        assert result == [{"text": "hello"}]
        client._request.assert_called_once_with("GET", "/issues/QR-1/comments")


class TestUpdateComment:
    def test_update_comment(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={"id": 42, "text": "updated"})

        result = client.update_comment("QR-1", 42, "updated")
        assert result == {"id": 42, "text": "updated"}
        client._request.assert_called_once_with(
            "PATCH",
            "/issues/QR-1/comments/42",
            json={"text": "updated"},
        )


class TestCreateIssue:
    @pytest.mark.parametrize(
        ("extra_kwargs", "expected_field", "expected_value"),
        [
            (
                {"tags": ["ai-task", "supervisor"]},
                "tags",
                ["ai-task", "supervisor"],
            ),
            ({"parent": "QR-50"}, "parent", "QR-50"),
        ],
    )
    def test_creates_with_optional_field(self, extra_kwargs, expected_field, expected_value) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={"key": "QR-99"})

        result = client.create_issue(
            queue="QR",
            summary="Test",
            description="Desc",
            **extra_kwargs,
        )
        assert result["key"] == "QR-99"
        body = client._request.call_args[1]["json"]
        assert body[expected_field] == expected_value

    @pytest.mark.parametrize("absent_field", ["tags", "parent"])
    def test_creates_without_optional_field(self, absent_field) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={"key": "QR-100"})

        client.create_issue(queue="QR", summary="Test", description="Desc")
        body = client._request.call_args[1]["json"]
        assert absent_field not in body


class TestGetChecklist:
    @pytest.mark.parametrize(
        ("api_return", "expected_len"),
        [
            ([{"text": "item 1", "checked": True}], 1),
            (None, 0),
        ],
    )
    def test_get_checklist(self, api_return, expected_len) -> None:
        client = _make_client()
        client._request = MagicMock(return_value=api_return)
        result = client.get_checklist("QR-1")
        assert len(result) == expected_len


class TestGetLinks:
    def test_get_links(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value=[{"id": "1"}])

        result = client.get_links("QR-1")
        assert result == [{"id": "1"}]
        client._request.assert_called_once_with("GET", "/issues/QR-1/links")


class TestUpdateIssueTags:
    def test_update_issue_tags(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={"key": "QR-1", "tags": ["ai-task"]})

        result = client.update_issue_tags("QR-1", ["ai-task"])
        assert result["key"] == "QR-1"
        client._request.assert_called_once_with("PATCH", "/issues/QR-1", json={"tags": ["ai-task"]})


class TestUpdateIssue:
    def test_patches_tags(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={})

        client.update_issue("QR-1", tags=["foo", "bar"])

        client._request.assert_called_once_with("PATCH", "/issues/QR-1", json={"tags": ["foo", "bar"]})

    def test_patches_summary(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={})

        client.update_issue("QR-1", summary="New title")

        client._request.assert_called_once_with("PATCH", "/issues/QR-1", json={"summary": "New title"})

    def test_patches_description(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={})

        client.update_issue("QR-1", description="new desc")

        client._request.assert_called_once_with("PATCH", "/issues/QR-1", json={"description": "new desc"})

    def test_patches_multiple_fields(self) -> None:
        client = _make_client()
        client._request = MagicMock(return_value={})

        client.update_issue("QR-1", summary="title", description="desc", tags=["t"])

        client._request.assert_called_once_with(
            "PATCH",
            "/issues/QR-1",
            json={"summary": "title", "description": "desc", "tags": ["t"]},
        )

    def test_no_fields_raises(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError, match="At least one field"):
            client.update_issue("QR-1")


class TestTransitionToCancelled:
    def test_uses_cancel_strategy(self) -> None:
        from orchestrator.tracker_client import STRATEGY_CANCELLED

        client = _make_client()
        client._execute_strategy = MagicMock()

        client.transition_to_cancelled("QR-1", comment="Cancelled by user")
        client._execute_strategy.assert_called_once_with(
            "QR-1",
            STRATEGY_CANCELLED,
            comment="Cancelled by user",
            _depth=0,
        )


class TestCreateSubtask:
    def test_creates_issue_with_parent(self) -> None:
        client = _make_client()
        client.create_issue = MagicMock(return_value={"key": "QR-101"})

        result = client.create_subtask(
            parent_key="QR-50",
            queue="QR",
            summary="Follow-up task",
            description="Remaining work",
            components=["Backend"],
            assignee="john",
            project_id=13,
            boards=[14],
            tags=["ai-task"],
        )

        assert result["key"] == "QR-101"
        assert "link_failed" not in result or result["link_failed"] is False
        client.create_issue.assert_called_once_with(
            queue="QR",
            summary="Follow-up task",
            description="Remaining work",
            issue_type=2,
            components=["Backend"],
            assignee="john",
            project_id=13,
            boards=[14],
            tags=["ai-task"],
            parent="QR-50",
        )

    def test_falls_back_to_manual_link_when_parent_param_fails(self) -> None:
        """When parent parameter fails, create issue without parent then manually link."""
        client = _make_client()
        # First call with parent fails, second call without parent succeeds
        client.create_issue = MagicMock(
            side_effect=[
                requests.HTTPError("422: Parent issue not found"),
                {"key": "QR-101"},
            ]
        )
        client.add_link = MagicMock(return_value={"id": "link-1"})

        result = client.create_subtask(
            parent_key="QR-50",
            queue="QR",
            summary="Follow-up task",
            description="Remaining work",
        )

        assert result["key"] == "QR-101"
        assert "link_failed" not in result or result["link_failed"] is False
        # First attempt with parent
        assert client.create_issue.call_count == 2
        # Second attempt without parent
        client.create_issue.assert_any_call(
            queue="QR",
            summary="Follow-up task",
            description="Remaining work",
            issue_type=2,
            components=None,
            assignee=None,
            project_id=None,
            boards=None,
            tags=None,
            parent=None,
        )
        # Manual link added with correct relationship type
        client.add_link.assert_called_once_with("QR-101", "QR-50", "is subtask for")

    def test_sets_link_failed_when_both_parent_and_manual_link_fail(self) -> None:
        """When both parent parameter and manual linking fail, set link_failed flag."""
        client = _make_client()
        # First call with parent fails, second call without parent succeeds, manual link fails
        client.create_issue = MagicMock(
            side_effect=[
                requests.HTTPError("422: Parent issue not found"),
                {"key": "QR-101"},
            ]
        )
        client.add_link = MagicMock(side_effect=requests.HTTPError("403: Permission denied"))

        result = client.create_subtask(
            parent_key="QR-50",
            queue="QR",
            summary="Follow-up task",
            description="Remaining work",
        )

        assert result["key"] == "QR-101"
        assert result["link_failed"] is True
        assert client.create_issue.call_count == 2
        client.add_link.assert_called_once()


class TestGetAttachments:
    @pytest.mark.parametrize(
        ("api_return", "expected_len"),
        [
            (
                [
                    {
                        "id": 71,
                        "name": "sitemap.xml",
                        "mimetype": "text/xml",
                        "size": 1024,
                    }
                ],
                1,
            ),
            ([], 0),
        ],
    )
    def test_get_attachments(self, api_return, expected_len) -> None:
        client = _make_client()
        client._request = MagicMock(return_value=api_return)

        result = client.get_attachments("QR-1")
        assert len(result) == expected_len


class TestDownloadAttachment:
    @patch("orchestrator.tracker_client.time.sleep")
    def test_download_text_attachment(self, mock_sleep) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<xml>test</xml>"
        mock_resp.headers = {
            "Content-Type": "text/xml",
            "Content-Length": "15",
        }
        mock_resp.iter_content = MagicMock(return_value=iter([b"<xml>test</xml>"]))

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = _make_client()
        client._session = mock_session

        content, content_type = client.download_attachment(71)
        assert content == b"<xml>test</xml>"
        assert content_type == "text/xml"

    @patch("orchestrator.tracker_client.time.sleep")
    def test_download_respects_size_limit(self, mock_sleep) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x" * 100
        mock_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": str(10 * 1024 * 1024),
        }

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = _make_client()
        client._session = mock_session

        with pytest.raises(ValueError, match="too large"):
            client.download_attachment(999)

    @patch("orchestrator.tracker_client.time.sleep")
    def test_size_limit_checked_before_loading_content(self, mock_sleep) -> None:
        """Content-Length checked BEFORE accessing resp.content."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": str(10 * 1024 * 1024),
        }

        def raise_if_accessed():
            raise AssertionError("resp.content accessed before size check!")

        type(mock_resp).content = PropertyMock(side_effect=raise_if_accessed)

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = _make_client()
        client._session = mock_session

        try:
            with pytest.raises(ValueError, match="too large"):
                client.download_attachment(999)
        except AssertionError as e:
            raise AssertionError("Content was accessed before size limit check!") from e

    @patch("orchestrator.tracker_client.time.sleep")
    def test_download_retries_on_429(self, mock_sleep) -> None:
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "1"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.content = b"content"
        ok_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": "7",
        }
        ok_resp.iter_content = MagicMock(return_value=iter([b"content"]))

        mock_session = MagicMock()
        mock_session.get.side_effect = [rate_resp, ok_resp]

        client = _make_client()
        client._session = mock_session

        content, _content_type = client.download_attachment(71)
        assert content == b"content"
        mock_sleep.assert_called_once_with(1)

    @patch("orchestrator.tracker_client.time.sleep")
    def test_download_handles_http_date_retry_after(self, mock_sleep) -> None:
        """HTTP-date Retry-After falls back to default."""
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.content = b"content"
        ok_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": "7",
        }
        ok_resp.iter_content = MagicMock(return_value=iter([b"content"]))

        mock_session = MagicMock()
        mock_session.get.side_effect = [rate_resp, ok_resp]

        client = _make_client()
        client._session = mock_session

        content, _content_type = client.download_attachment(71)
        assert content == b"content"
        mock_sleep.assert_called_once_with(5)

    @patch("orchestrator.tracker_client.time.sleep")
    def test_download_closes_response_on_429_retry(self, mock_sleep) -> None:
        """Response closed on 429 to prevent connection leaks."""
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "1"}
        rate_resp.close = MagicMock()

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.content = b"content"
        ok_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": "7",
        }

        mock_session = MagicMock()
        mock_session.get.side_effect = [rate_resp, ok_resp]

        client = _make_client()
        client._session = mock_session

        client.download_attachment(71)
        rate_resp.close.assert_called_once()

    def test_download_closes_response_on_size_limit_error(
        self,
    ) -> None:
        """Response closed when raising ValueError for oversized files."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": str(10 * 1024 * 1024),
        }
        mock_resp.close = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = _make_client()
        client._session = mock_session

        with pytest.raises(ValueError, match="too large"):
            client.download_attachment(999)

        mock_resp.close.assert_called_once()

    @patch("orchestrator.tracker_client.time.sleep")
    def test_content_length_bypass_with_incorrect_header(self, mock_sleep) -> None:
        """Streaming rejects content exceeding limit even if Content-Length lies."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "text/plain",
            "Content-Length": str(1 * 1024 * 1024),
        }

        chunk_size = 1024 * 1024
        chunks = [b"x" * chunk_size for _ in range(10)]
        mock_resp.iter_content = MagicMock(return_value=iter(chunks))

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = _make_client()
        client._session = mock_session

        with pytest.raises(ValueError, match="too large"):
            client.download_attachment(999)

    @patch("orchestrator.tracker_client.time.sleep")
    def test_missing_content_length_streams_safely(self, mock_sleep) -> None:
        """Missing Content-Length → stream with size enforcement."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/plain"}

        chunk_size = 1024 * 1024
        chunks = [b"x" * chunk_size for _ in range(10)]
        mock_resp.iter_content = MagicMock(return_value=iter(chunks))

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = _make_client()
        client._session = mock_session

        with pytest.raises(ValueError, match="too large"):
            client.download_attachment(999)
