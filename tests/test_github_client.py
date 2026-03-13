"""Tests for github_client module."""

from unittest.mock import MagicMock

import pytest

from orchestrator.github_client import (
    FailedCheck,
    GitHubClient,
    PRDetails,
    PRFile,
    ReviewThread,
    ThreadComment,
)


def _make_graphql_response(
    state: str = "OPEN",
    review_decision: str | None = None,
    threads: list | None = None,
) -> dict:
    """Build a fake GraphQL response."""
    if threads is None:
        threads = []
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": state,
                    "reviewDecision": review_decision,
                    "reviewThreads": {"nodes": threads},
                }
            }
        }
    }


def _make_thread_node(
    thread_id: str = "T_1",
    is_resolved: bool = False,
    path: str | None = "src/main.py",
    line: int | None = 42,
    comments: list | None = None,
) -> dict:
    if comments is None:
        comments = [
            {
                "author": {"login": "reviewer"},
                "body": "Please fix this",
                "createdAt": "2025-01-01T00:00:00Z",
            }
        ]
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "path": path,
        "line": line,
        "comments": {"nodes": comments},
    }


class TestGetUnresolvedThreads:
    def test_returns_unresolved_only(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _make_graphql_response(
            threads=[
                _make_thread_node("T_1", is_resolved=False),
                _make_thread_node("T_2", is_resolved=True),
                _make_thread_node("T_3", is_resolved=False),
            ]
        )

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        threads = client.get_unresolved_threads("owner", "repo", 1)
        assert len(threads) == 2
        assert threads[0].id == "T_1"
        assert threads[1].id == "T_3"
        assert all(not t.is_resolved for t in threads)

    def test_parses_comments(self) -> None:
        comments = [
            {"author": {"login": "alice"}, "body": "Fix this", "createdAt": "2025-01-01T00:00:00Z"},
            {"author": {"login": "bob"}, "body": "Agreed", "createdAt": "2025-01-01T01:00:00Z"},
        ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _make_graphql_response(threads=[_make_thread_node("T_1", comments=comments)])

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        threads = client.get_unresolved_threads("owner", "repo", 1)
        assert len(threads) == 1
        assert len(threads[0].comments) == 2
        assert threads[0].comments[0].author == "alice"
        assert threads[0].comments[0].body == "Fix this"
        assert threads[0].comments[1].author == "bob"

    def test_handles_null_author(self) -> None:
        comments = [
            {"author": None, "body": "Bot comment", "createdAt": "2025-01-01T00:00:00Z"},
        ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _make_graphql_response(threads=[_make_thread_node("T_1", comments=comments)])

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        threads = client.get_unresolved_threads("owner", "repo", 1)
        assert threads[0].comments[0].author == "unknown"

    def test_empty_threads(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _make_graphql_response(threads=[])

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        threads = client.get_unresolved_threads("owner", "repo", 1)
        assert threads == []

    def test_path_and_line(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _make_graphql_response(threads=[_make_thread_node("T_1", path="foo.py", line=10)])

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        threads = client.get_unresolved_threads("owner", "repo", 1)
        assert threads[0].path == "foo.py"
        assert threads[0].line == 10

    def test_graphql_error_raises(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"errors": [{"message": "Bad query"}]}

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        with pytest.raises(RuntimeError, match="GraphQL errors"):
            client.get_unresolved_threads("owner", "repo", 1)


class TestFilteredMethodsReuseUnfiltered:
    def test_get_unresolved_threads_filters_get_review_threads(self) -> None:
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(
            side_effect=AssertionError("get_unresolved_threads should not query GraphQL directly")
        )
        client.get_review_threads = MagicMock(
            return_value=[
                ReviewThread(
                    id="T_1",
                    is_resolved=False,
                    path="src/main.py",
                    line=10,
                    comments=[ThreadComment(author="alice", body="Needs fix", created_at="2025-01-01T00:00:00Z")],
                ),
                ReviewThread(
                    id="T_2",
                    is_resolved=True,
                    path="src/main.py",
                    line=11,
                    comments=[ThreadComment(author="bob", body="Resolved", created_at="2025-01-01T01:00:00Z")],
                ),
            ]
        )

        threads = client.get_unresolved_threads("owner", "repo", 1)

        assert [thread.id for thread in threads] == ["T_1"]
        client.get_review_threads.assert_called_once_with("owner", "repo", 1)

    def test_get_failed_checks_filters_get_all_checks(self) -> None:
        client = GitHubClient("fake-token")
        client._graphql = MagicMock(side_effect=AssertionError("get_failed_checks should not query GraphQL directly"))
        client.get_all_checks = MagicMock(
            return_value=[
                FailedCheck(
                    name="tests",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                    details_url="https://ci.example.com/tests",
                    summary="All passed",
                ),
                FailedCheck(
                    name="lint",
                    status="COMPLETED",
                    conclusion="FAILURE",
                    details_url="https://ci.example.com/lint",
                    summary="Lint failed",
                ),
                FailedCheck(
                    name="build",
                    status="COMPLETED",
                    conclusion="ERROR",
                    details_url="https://ci.example.com/build",
                    summary="Build errored",
                ),
                FailedCheck(
                    name="e2e",
                    status="COMPLETED",
                    conclusion="TIMED_OUT",
                    details_url="https://ci.example.com/e2e",
                    summary="E2E timed out",
                ),
            ]
        )

        failed_checks = client.get_failed_checks("owner", "repo", 1)

        assert [check.name for check in failed_checks] == ["lint", "build", "e2e"]
        client.get_all_checks.assert_called_once_with("owner", "repo", 1)


class TestGetPRStatus:
    def test_open_pr(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "state": "OPEN",
                        "reviewDecision": "CHANGES_REQUESTED",
                    }
                }
            }
        }

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        status = client.get_pr_status("owner", "repo", 1)
        assert status.state == "OPEN"
        assert status.review_decision == "CHANGES_REQUESTED"

    def test_merged_pr(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "state": "MERGED",
                        "reviewDecision": "APPROVED",
                    }
                }
            }
        }

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        status = client.get_pr_status("owner", "repo", 1)
        assert status.state == "MERGED"
        assert status.review_decision == "APPROVED"

    def test_null_review_decision(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "state": "OPEN",
                        "reviewDecision": None,
                    }
                }
            }
        }

        client = GitHubClient("fake-token")
        client._session = MagicMock()
        client._session.post.return_value = resp

        status = client.get_pr_status("owner", "repo", 1)
        assert status.review_decision == ""


def _make_client_with_session() -> tuple[GitHubClient, MagicMock]:
    """Create a GitHubClient with a mocked session."""
    client = GitHubClient("fake-token")
    client._session = MagicMock()
    return client, client._session


class TestGetPRDetails:
    def test_returns_pr_details(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Add feature",
                        "body": "Description here",
                        "author": {"login": "dev1"},
                        "baseRefName": "main",
                        "headRefName": "feat/foo",
                        "state": "OPEN",
                        "reviewDecision": "APPROVED",
                        "additions": 50,
                        "deletions": 10,
                        "changedFiles": 3,
                    }
                }
            }
        }
        session.post.return_value = resp

        details = client.get_pr_details("owner", "repo", 1)
        assert isinstance(details, PRDetails)
        assert details.title == "Add feature"
        assert details.body == "Description here"
        assert details.author == "dev1"
        assert details.base_branch == "main"
        assert details.head_branch == "feat/foo"
        assert details.state == "OPEN"
        assert details.review_decision == "APPROVED"
        assert details.additions == 50
        assert details.deletions == 10
        assert details.changed_files == 3

    def test_null_author(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Bot PR",
                        "body": "",
                        "author": None,
                        "baseRefName": "main",
                        "headRefName": "bot/fix",
                        "state": "OPEN",
                        "reviewDecision": None,
                        "additions": 1,
                        "deletions": 0,
                        "changedFiles": 1,
                    }
                }
            }
        }
        session.post.return_value = resp

        details = client.get_pr_details("owner", "repo", 2)
        assert details.author == "unknown"
        assert details.review_decision == ""


class TestGetReviewThreads:
    def test_returns_all_threads(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = _make_graphql_response(
            threads=[
                _make_thread_node("T_1", is_resolved=False),
                _make_thread_node("T_2", is_resolved=True),
            ]
        )
        session.post.return_value = resp

        threads = client.get_review_threads("owner", "repo", 1)
        assert len(threads) == 2
        assert threads[0].id == "T_1"
        assert not threads[0].is_resolved
        assert threads[1].id == "T_2"
        assert threads[1].is_resolved

    def test_empty_threads(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = _make_graphql_response(threads=[])
        session.post.return_value = resp

        threads = client.get_review_threads("owner", "repo", 1)
        assert threads == []


class TestGetPRDiff:
    def test_returns_diff_text(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.text = "diff --git a/file.py b/file.py\n+new line"
        session.get.return_value = resp

        diff = client.get_pr_diff("owner", "repo", 1)
        assert "diff --git" in diff
        assert "+new line" in diff

    def test_truncates_long_diff(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.text = "x" * 100_000
        session.get.return_value = resp

        diff = client.get_pr_diff("owner", "repo", 1)
        assert len(diff) <= 50_000 + 100  # some room for truncation message

    def test_empty_diff(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.text = ""
        session.get.return_value = resp

        diff = client.get_pr_diff("owner", "repo", 1)
        assert diff == ""


class TestGetPRFiles:
    def test_returns_files(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = [
            {
                "filename": "src/main.py",
                "status": "modified",
                "additions": 10,
                "deletions": 2,
                "patch": "@@ -1,3 +1,5 @@\n+new code",
            },
            {
                "filename": "README.md",
                "status": "added",
                "additions": 5,
                "deletions": 0,
                "patch": "@@ +1,5 @@\n+content",
            },
        ]
        session.get.return_value = resp

        files = client.get_pr_files("owner", "repo", 1)
        assert len(files) == 2
        assert isinstance(files[0], PRFile)
        assert files[0].filename == "src/main.py"
        assert files[0].status == "modified"
        assert files[0].additions == 10
        assert files[0].deletions == 2
        assert files[0].patch == "@@ -1,3 +1,5 @@\n+new code"
        assert files[1].filename == "README.md"

    def test_paginates_all_pages(self) -> None:
        """get_pr_files must paginate when a page returns per_page (100) items."""
        client, session = _make_client_with_session()

        # Page 1: exactly 100 items (full page → must request page 2)
        page1_files = [
            {"filename": f"file_{i}.py", "status": "modified", "additions": 1, "deletions": 0} for i in range(100)
        ]
        # Page 2: 1 item (< 100 → last page)
        page2_files = [
            {"filename": "extra.py", "status": "added", "additions": 1, "deletions": 0},
        ]

        resp_page1 = MagicMock()
        resp_page1.json.return_value = page1_files
        resp_page2 = MagicMock()
        resp_page2.json.return_value = page2_files
        session.get.side_effect = [resp_page1, resp_page2]

        files = client.get_pr_files("owner", "repo", 1)

        assert len(files) == 101
        assert files[0].filename == "file_0.py"
        assert files[100].filename == "extra.py"
        # Should have made 2 requests (full page 1 + partial page 2)
        assert session.get.call_count == 2
        # Verify page params
        call1_params = session.get.call_args_list[0].kwargs.get("params", {})
        assert call1_params.get("per_page") == "100"
        assert call1_params.get("page") == "1"
        call2_params = session.get.call_args_list[1].kwargs.get("params", {})
        assert call2_params.get("page") == "2"

    def test_single_page_no_extra_request(self) -> None:
        """When first page returns fewer than per_page items, no second request."""
        client, session = _make_client_with_session()

        # Return fewer than 100 items → single page
        resp = MagicMock()
        resp.json.return_value = [
            {"filename": "x.py", "status": "modified", "additions": 1, "deletions": 0},
        ]
        session.get.return_value = resp

        files = client.get_pr_files("owner", "repo", 1)

        assert len(files) == 1
        assert session.get.call_count == 1

    def test_empty_files(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = []
        session.get.return_value = resp

        files = client.get_pr_files("owner", "repo", 1)
        assert files == []

    def test_missing_patch(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = [
            {
                "filename": "binary.bin",
                "status": "added",
                "additions": 0,
                "deletions": 0,
            },
        ]
        session.get.return_value = resp

        files = client.get_pr_files("owner", "repo", 1)
        assert files[0].patch is None


class TestListPRs:
    def test_returns_prs(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = [
            {
                "number": 1,
                "title": "First PR",
                "state": "open",
                "user": {"login": "dev1"},
                "head": {"ref": "feat/one"},
                "base": {"ref": "main"},
            },
            {
                "number": 2,
                "title": "Second PR",
                "state": "open",
                "user": {"login": "dev2"},
                "head": {"ref": "feat/two"},
                "base": {"ref": "main"},
            },
        ]
        session.get.return_value = resp

        prs = client.list_prs("owner", "repo", state="open", limit=10)
        assert len(prs) == 2
        assert prs[0]["number"] == 1
        assert prs[0]["title"] == "First PR"
        assert prs[1]["number"] == 2

    def test_empty_prs(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = []
        session.get.return_value = resp

        prs = client.list_prs("owner", "repo")
        assert prs == []

    def test_calls_correct_url(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = []
        session.get.return_value = resp

        client.list_prs("myowner", "myrepo", state="closed", limit=5)
        session.get.assert_called_once()
        call_url = session.get.call_args[0][0]
        assert "myowner" in call_url
        assert "myrepo" in call_url


class TestSearchPRs:
    def test_returns_merged_pr(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "items": [
                {
                    "number": 123,
                    "title": "[QR-194] Add preflight checks",
                    "state": "closed",
                    "html_url": "https://github.com/org/repo/pull/123",
                    "pull_request": {"merged_at": "2025-01-15T10:00:00Z"},
                },
            ]
        }
        session.get.return_value = resp

        prs = client.search_prs("QR-194")
        assert len(prs) == 1
        assert prs[0]["number"] == 123
        assert prs[0]["title"] == "[QR-194] Add preflight checks"
        assert prs[0]["state"] == "closed"
        assert prs[0]["merged"] is True
        assert prs[0]["html_url"] == "https://github.com/org/repo/pull/123"

    def test_merged_pr_without_merged_at_field(self) -> None:
        """Bug QR-194 review: search/issues API doesn't reliably return merged_at.

        When using is:merged filter, the API may return merged PRs without merged_at
        timestamp in the response. We should derive merged status from state=closed
        combined with is:merged filter usage.
        """
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "items": [
                {
                    "number": 456,
                    "title": "[QR-147] Fix issue",
                    "state": "closed",
                    "html_url": "https://github.com/org/repo/pull/456",
                    "pull_request": {},  # No merged_at field
                },
            ]
        }
        session.get.return_value = resp

        # When using merged_only=True, results should be marked as merged
        # even without merged_at field, because the API filter guarantees it
        prs = client.search_prs("QR-147", merged_only=True)
        assert len(prs) == 1
        assert prs[0]["merged"] is True  # Should be True despite missing merged_at

    def test_search_prs_filters_by_org(self) -> None:
        """Bug QR-194 conversation 2: search_prs must restrict query to org to avoid matching unrelated PRs."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"items": []}
        session.get.return_value = resp

        client.search_prs("QR-194", org="zvenoai")

        # Verify the query includes org restriction
        call_params = session.get.call_args.kwargs.get("params", {})
        query = call_params.get("q", "")
        assert "org:zvenoai" in query
        assert "QR-194" in query
        assert "type:pr" in query

    def test_search_prs_filters_merged_when_requested(self) -> None:
        """Bug QR-194 conversation 3: search_prs must support is:merged filter to avoid pagination issues."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"items": []}
        session.get.return_value = resp

        client.search_prs("QR-194", merged_only=True)

        # Verify the query includes is:merged filter
        call_params = session.get.call_args.kwargs.get("params", {})
        query = call_params.get("q", "")
        assert "is:merged" in query
        assert "QR-194" in query
        assert "type:pr" in query

    def test_returns_open_pr(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "items": [
                {
                    "number": 456,
                    "title": "QR-147: Fix issue",
                    "state": "open",
                    "html_url": "https://github.com/org/repo/pull/456",
                    "pull_request": {},
                },
            ]
        }
        session.get.return_value = resp

        prs = client.search_prs("QR-147")
        assert len(prs) == 1
        assert prs[0]["merged"] is False

    def test_no_results(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"items": []}
        session.get.return_value = resp

        prs = client.search_prs("QR-999")
        assert prs == []

    def test_api_error_raises(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("API error")
        session.get.return_value = resp

        with pytest.raises(Exception, match="API error"):
            client.search_prs("QR-123")

    def test_calls_correct_endpoint(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"items": []}
        session.get.return_value = resp

        client.search_prs("QR-194")
        session.get.assert_called_once()
        call_url = session.get.call_args[0][0]
        assert call_url.startswith("https://api.github.com/search/issues")
        call_params = session.get.call_args.kwargs.get("params", {})
        assert "QR-194" in call_params.get("q", "")
        assert "type:pr" in call_params.get("q", "")


class TestGetAllChecks:
    def test_returns_all_checks(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "oid": "abc123",
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    {
                                                        "__typename": "CheckRun",
                                                        "name": "tests",
                                                        "status": "COMPLETED",
                                                        "conclusion": "SUCCESS",
                                                        "detailsUrl": "https://ci.example.com/1",
                                                        "summary": "All passed",
                                                    },
                                                    {
                                                        "__typename": "CheckRun",
                                                        "name": "lint",
                                                        "status": "COMPLETED",
                                                        "conclusion": "FAILURE",
                                                        "detailsUrl": "https://ci.example.com/2",
                                                        "summary": "Lint failed",
                                                    },
                                                ]
                                            }
                                        },
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }
        session.post.return_value = resp

        checks = client.get_all_checks("owner", "repo", 1)
        assert len(checks) == 2
        assert checks[0].name == "tests"
        assert checks[0].conclusion == "SUCCESS"
        assert checks[1].name == "lint"
        assert checks[1].conclusion == "FAILURE"

    def test_empty_commits(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"data": {"repository": {"pullRequest": {"commits": {"nodes": []}}}}}
        session.post.return_value = resp

        checks = client.get_all_checks("owner", "repo", 1)
        assert checks == []

    def test_no_rollup(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {"commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": None}}]}}
                }
            }
        }
        session.post.return_value = resp

        checks = client.get_all_checks("owner", "repo", 1)
        assert checks == []

    def test_status_context(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "oid": "abc123",
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    {
                                                        "__typename": "StatusContext",
                                                        "context": "deploy/preview",
                                                        "state": "SUCCESS",
                                                        "targetUrl": "https://preview.example.com",
                                                        "description": "Deploy succeeded",
                                                    },
                                                ]
                                            }
                                        },
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }
        session.post.return_value = resp

        checks = client.get_all_checks("owner", "repo", 1)
        assert len(checks) == 1
        assert checks[0].name == "deploy/preview"
        assert checks[0].conclusion == "SUCCESS"


class TestGetCommitCheckRuns:
    """Tests for get_commit_check_runs REST endpoint."""

    def test_uses_rest_base_url(self) -> None:
        """URL should use self.REST_BASE_URL, not hardcoded github.com."""
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {"check_runs": []}
        session.get.return_value = resp

        client.get_commit_check_runs("owner", "repo", "sha123")

        called_url = session.get.call_args[0][0]
        assert called_url.startswith(client.REST_BASE_URL)
        assert "/repos/owner/repo/commits/sha123/check-runs" in called_url

    def test_parses_check_runs(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "check_runs": [
                {
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "https://ci/1",
                    "output": {"summary": "ok"},
                },
            ],
        }
        session.get.return_value = resp

        checks = client.get_commit_check_runs("o", "r", "sha")
        assert len(checks) == 1
        assert checks[0].name == "tests"
        assert checks[0].conclusion == "SUCCESS"


class TestGetMergeCommitSha:
    """Tests for get_merge_commit_sha REST endpoint."""

    def test_returns_sha(self) -> None:
        client, session = _make_client_with_session()
        resp = MagicMock()
        resp.json.return_value = {
            "merge_commit_sha": "abc123merge",
        }
        session.get.return_value = resp

        sha = client.get_merge_commit_sha("owner", "repo", 42)
        assert sha == "abc123merge"

    def test_returns_empty_on_failure(self) -> None:
        import requests as req

        client, session = _make_client_with_session()
        session.get.side_effect = req.ConnectionError("boom")

        sha = client.get_merge_commit_sha("owner", "repo", 42)
        assert sha == ""
