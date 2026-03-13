"""GitHub GraphQL client for PR review thread and CI pipeline monitoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass
class ThreadComment:
    """A single comment within a review thread."""

    author: str
    body: str
    created_at: str


@dataclass
class ReviewThread:
    """An unresolved review thread on a PR."""

    id: str
    is_resolved: bool
    path: str | None
    line: int | None
    comments: list[ThreadComment]


@dataclass
class PRStatus:
    """PR state and review decision."""

    state: str  # "OPEN", "CLOSED", "MERGED"
    review_decision: str  # "APPROVED", "CHANGES_REQUESTED", ""
    mergeable: str = ""  # "MERGEABLE", "CONFLICTING", "UNKNOWN", ""
    head_sha: str = ""  # HEAD commit SHA for detecting new pushes


@dataclass
class FailedCheck:
    """A failed CI check run or status context on a PR."""

    name: str
    status: str  # "COMPLETED", "IN_PROGRESS", etc.
    conclusion: str  # "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"
    details_url: str | None
    summary: str | None


# CheckResult is structurally identical to FailedCheck — single dataclass, two names.
CheckResult = FailedCheck


@dataclass
class PRDetails:
    """Detailed PR information."""

    title: str
    body: str
    author: str
    base_branch: str
    head_branch: str
    state: str
    review_decision: str
    additions: int
    deletions: int
    changed_files: int


@dataclass
class PRFile:
    """A changed file in a PR."""

    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None


@dataclass
class MergeReadiness:
    """Combined merge readiness check result."""

    is_ready: bool
    mergeable: str
    review_decision: str
    has_failed_checks: bool
    has_pending_checks: bool
    has_unresolved_threads: bool
    reasons: list[str]


_PR_DETAILS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      title
      body
      author { login }
      baseRefName
      headRefName
      state
      reviewDecision
      additions
      deletions
      changedFiles
    }
  }
}
"""

_PR_REVIEW_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      state
      reviewDecision
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 20) {
            nodes {
              author { login }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
"""

_PR_STATUS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      state
      reviewDecision
      mergeable
      headRefOid
    }
  }
}
"""

_PR_NODE_ID_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
    }
  }
}
"""

_ENABLE_AUTO_MERGE_MUTATION = """
mutation($prId: ID!, $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {
    pullRequestId: $prId,
    mergeMethod: $mergeMethod
  }) {
    pullRequest { id }
  }
}
"""

_PR_MERGE_READINESS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      mergeable
      reviewDecision
      reviewThreads(first: 100) {
        nodes { isResolved }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  ... on CheckRun {
                    __typename
                    status
                    conclusion
                  }
                  ... on StatusContext {
                    __typename
                    state
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_PR_CHECKS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  ... on CheckRun {
                    __typename
                    name
                    status
                    conclusion
                    detailsUrl
                    summary
                  }
                  ... on StatusContext {
                    __typename
                    context
                    state
                    targetUrl
                    description
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Conclusions/states considered failures
_FAILED_CONCLUSIONS = frozenset(
    {
        "FAILURE",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "ERROR",
    }
)
_FAILED_STATES = frozenset({"ERROR", "FAILURE"})


_MAX_DIFF_LENGTH = 50_000
_MAX_PATCH_LENGTH = 10_000


class GitHubClient:
    """GitHub GraphQL API client for PR review monitoring."""

    BASE_URL = "https://api.github.com/graphql"
    REST_BASE_URL = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def get_unresolved_threads(self, owner: str, repo: str, pr_number: int) -> list[ReviewThread]:
        """Get only unresolved review threads via GraphQL."""
        return [thread for thread in self.get_review_threads(owner, repo, pr_number) if not thread.is_resolved]

    def get_pr_status(self, owner: str, repo: str, pr_number: int) -> PRStatus:
        """Get PR state (OPEN/CLOSED/MERGED) and review decision."""
        data = self._graphql(
            _PR_STATUS_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        pr = data["repository"]["pullRequest"]
        return PRStatus(
            state=pr["state"],
            review_decision=pr.get("reviewDecision") or "",
            mergeable=pr.get("mergeable") or "",
            head_sha=pr.get("headRefOid") or "",
        )

    def get_failed_checks(self, owner: str, repo: str, pr_number: int) -> list[FailedCheck]:
        """Get failed CI check runs and status contexts for the PR's head commit."""
        return [
            check
            for check in self.get_all_checks(owner, repo, pr_number)
            if check.conclusion in _FAILED_CONCLUSIONS or check.conclusion in _FAILED_STATES
        ]

    def get_pr_details(self, owner: str, repo: str, pr_number: int) -> PRDetails:
        """Get detailed PR information via GraphQL."""
        data = self._graphql(
            _PR_DETAILS_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        pr = data["repository"]["pullRequest"]
        author = pr.get("author")
        return PRDetails(
            title=pr["title"],
            body=pr.get("body") or "",
            author=author["login"] if author else "unknown",
            base_branch=pr["baseRefName"],
            head_branch=pr["headRefName"],
            state=pr["state"],
            review_decision=pr.get("reviewDecision") or "",
            additions=pr["additions"],
            deletions=pr["deletions"],
            changed_files=pr["changedFiles"],
        )

    def get_review_threads(self, owner: str, repo: str, pr_number: int) -> list[ReviewThread]:
        """Get ALL review threads (resolved + unresolved) via GraphQL."""
        data = self._graphql(
            _PR_REVIEW_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        pr = data["repository"]["pullRequest"]
        threads: list[ReviewThread] = []
        for node in pr["reviewThreads"]["nodes"]:
            comments = [
                ThreadComment(
                    author=c["author"]["login"] if c["author"] else "unknown",
                    body=c["body"],
                    created_at=c["createdAt"],
                )
                for c in node["comments"]["nodes"]
            ]
            threads.append(
                ReviewThread(
                    id=node["id"],
                    is_resolved=node["isResolved"],
                    path=node.get("path"),
                    line=node.get("line"),
                    comments=comments,
                )
            )
        return threads

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Get raw diff text for a PR via REST API. Truncated to ~50k chars."""
        resp = self._session.get(
            f"{self.REST_BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        resp.raise_for_status()
        text = resp.text
        if len(text) > _MAX_DIFF_LENGTH:
            text = text[:_MAX_DIFF_LENGTH] + "\n\n... [truncated]"
        return text

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[PRFile]:
        """Get list of changed files in a PR via REST API.

        Paginates through all pages (GitHub returns max 100 per page).
        """
        url = f"{self.REST_BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        per_page = 100
        page = 1
        files: list[PRFile] = []

        while True:
            resp = self._session.get(url, params={"per_page": str(per_page), "page": str(page)})
            resp.raise_for_status()
            batch = resp.json()

            for f in batch:
                patch = f.get("patch")
                if patch and len(patch) > _MAX_PATCH_LENGTH:
                    patch = patch[:_MAX_PATCH_LENGTH] + "\n... [truncated]"
                files.append(
                    PRFile(
                        filename=f["filename"],
                        status=f["status"],
                        additions=f["additions"],
                        deletions=f["deletions"],
                        patch=patch,
                    )
                )

            if len(batch) < per_page:
                break
            page += 1

        return files

    def get_merge_commit_sha(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> str:
        """Get the merge commit SHA for a merged PR via REST API.

        Returns:
            The merge commit SHA, or empty string on failure.
        """
        try:
            resp = self._session.get(
                f"{self.REST_BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}",
            )
            resp.raise_for_status()
            return resp.json().get("merge_commit_sha") or ""
        except requests.RequestException:
            logger.warning(
                "Failed to get merge_commit_sha for %s/%s#%d",
                owner,
                repo,
                pr_number,
            )
            return ""

    def list_prs(self, owner: str, repo: str, state: str = "open", limit: int = 10) -> list[dict]:
        """List PRs in a repo via REST API."""
        resp = self._session.get(
            f"{self.REST_BASE_URL}/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": str(limit)},
        )
        resp.raise_for_status()
        return [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "author": pr["user"]["login"] if pr.get("user") else "unknown",
                "head_branch": pr["head"]["ref"],
                "base_branch": pr["base"]["ref"],
            }
            for pr in resp.json()
        ]

    def search_prs(self, query: str, org: str | None = None, merged_only: bool = False) -> list[dict]:
        """Search for PRs across all repositories using GitHub Search API.

        Args:
            query: Search query (e.g., task key like "QR-194")
            org: Optional organization to restrict search to (e.g., "zvenoai")
            merged_only: If True, only return merged PRs (adds is:merged filter)

        Returns:
            List of PR dictionaries with keys: number, title, state, html_url, merged
        """
        # GitHub search API: https://docs.github.com/en/rest/search#search-issues-and-pull-requests
        # Note: PRs are treated as issues in the search API

        # BUGFIX QR-194: Build query with org restriction to avoid matching unrelated repos
        search_query_parts = [query, "type:pr"]
        if org:
            search_query_parts.append(f"org:{org}")
        # BUGFIX QR-194: Add is:merged filter to constrain results and avoid pagination issues
        if merged_only:
            search_query_parts.append("is:merged")

        search_query = " ".join(search_query_parts)

        resp = self._session.get(
            f"{self.REST_BASE_URL}/search/issues",
            params={
                "q": search_query,
                "per_page": "10",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("items", []):
            # Extract PR number from URL
            # URL format: https://api.github.com/repos/owner/repo/pulls/123
            pr_api_url = item.get("pull_request", {}).get("url", "")
            pr_number = None
            if pr_api_url:
                try:
                    pr_number = int(pr_api_url.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    pass

            # BUGFIX QR-194 review: search/issues API doesn't reliably provide merged_at.
            # When merged_only=True filter is used, trust that all results are merged,
            # even if merged_at field is missing from the response.
            merged_at_present = item.get("pull_request", {}).get("merged_at") is not None
            is_merged = merged_at_present or merged_only

            results.append(
                {
                    "number": pr_number or item.get("number"),
                    "title": item.get("title", ""),
                    "state": item.get("state", ""),
                    "html_url": item.get("html_url", ""),
                    "merged": is_merged,
                }
            )

        return results

    def get_all_checks(self, owner: str, repo: str, pr_number: int) -> list[CheckResult]:
        """Get ALL CI check runs and status contexts (not just failed) for a PR."""
        data = self._graphql(
            _PR_CHECKS_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        commits = data["repository"]["pullRequest"]["commits"]["nodes"]
        if not commits:
            return []

        rollup = commits[0]["commit"].get("statusCheckRollup")
        if not rollup:
            return []

        contexts = rollup.get("contexts", {}).get("nodes", [])
        checks: list[CheckResult] = []

        for ctx in contexts:
            typename = ctx.get("__typename")
            if typename == "CheckRun":
                checks.append(
                    CheckResult(
                        name=ctx.get("name", "unknown"),
                        status=ctx.get("status", ""),
                        conclusion=(ctx.get("conclusion") or "").upper(),
                        details_url=ctx.get("detailsUrl"),
                        summary=ctx.get("summary"),
                    )
                )
            elif typename == "StatusContext":
                checks.append(
                    CheckResult(
                        name=ctx.get("context", "unknown"),
                        status="COMPLETED",
                        conclusion=(ctx.get("state") or "").upper(),
                        details_url=ctx.get("targetUrl"),
                        summary=ctx.get("description"),
                    )
                )

        return checks

    def get_commit_check_runs(
        self,
        owner: str,
        repo: str,
        sha: str,
    ) -> list[CheckResult]:
        """Get CI check runs for a specific commit SHA.

        Uses the REST API ``/repos/{owner}/{repo}/commits/{ref}/
        check-runs`` endpoint.
        """
        url = f"{self.REST_BASE_URL}/repos/{owner}/{repo}/commits/{sha}/check-runs"
        resp = self._session.get(url)
        resp.raise_for_status()
        data = resp.json()
        checks: list[CheckResult] = []
        for run in data.get("check_runs", []):
            checks.append(
                CheckResult(
                    name=run.get("name", "unknown"),
                    status=(run.get("status", "").upper()),
                    conclusion=((run.get("conclusion") or "").upper()),
                    details_url=run.get("details_url"),
                    summary=run.get("output", {}).get("summary"),
                )
            )
        return checks

    def get_pr_node_id(self, owner: str, repo: str, pr_number: int) -> str:
        """Get the GraphQL node ID for a PR (required for mutations)."""
        data = self._graphql(
            _PR_NODE_ID_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        return data["repository"]["pullRequest"]["id"]

    def enable_auto_merge(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        method: str = "SQUASH",
    ) -> bool:
        """Enable GitHub auto-merge on a PR.

        Returns True on success, False on failure.
        """
        try:
            node_id = self.get_pr_node_id(owner, repo, pr_number)
            self._graphql(
                _ENABLE_AUTO_MERGE_MUTATION,
                {"prId": node_id, "mergeMethod": method},
            )
            return True
        except (RuntimeError, requests.RequestException) as e:
            logger.warning(
                "Failed to enable auto-merge for %s/%s#%d: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return False

    def merge_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        method: str = "squash",
    ) -> bool:
        """Merge a PR via REST API (fallback for repos without branch protection).

        Returns True on success, False on failure.
        """
        try:
            resp = self._session.put(
                f"{self.REST_BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                json={"merge_method": method},
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.warning(
                "Failed to merge PR %s/%s#%d: %s",
                owner,
                repo,
                pr_number,
                e,
            )
            return False

    def post_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict] | None = None,
    ) -> bool:
        """Post a review on a PR via REST API.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: PR number.
            body: Review body text.
            event: Review event type (COMMENT, REQUEST_CHANGES,
                APPROVE).
            comments: Optional inline comments (path, body, line).

        Returns:
            True on success, False on failure.
        """
        url = f"{self.REST_BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        payload: dict = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        try:
            resp = self._session.post(url, json=payload)
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            status_code = getattr(
                getattr(e, "response", None),
                "status_code",
                None,
            )
            if status_code in (401, 403):
                # False positive: logs diagnostic message about auth failure, not actual credentials
                logger.error(  # nosemgrep
                    "Auth error posting review on %s/%s#%d (HTTP %s) — check GitHub token scopes: %s",
                    owner,
                    repo,
                    pr_number,
                    status_code,
                    str(e),
                )
            else:
                logger.warning(
                    "Failed to post review on %s/%s#%d: %s",
                    owner,
                    repo,
                    pr_number,
                    e,
                )
            return False

    def check_merge_readiness(self, owner: str, repo: str, pr_number: int) -> MergeReadiness:
        """Combined check: mergeable + review + checks + threads."""
        data = self._graphql(
            _PR_MERGE_READINESS_QUERY,
            {"owner": owner, "repo": repo, "number": pr_number},
        )
        pr = data["repository"]["pullRequest"]
        mergeable = pr.get("mergeable") or "UNKNOWN"
        review_decision = pr.get("reviewDecision") or ""

        # Check for unresolved threads
        thread_nodes = pr.get("reviewThreads", {}).get("nodes", [])
        has_unresolved = any(not t.get("isResolved", True) for t in thread_nodes)

        # Check for failed or in-progress CI
        has_failed = False
        has_pending = False
        commits = pr.get("commits", {}).get("nodes", [])
        if commits:
            rollup = commits[0].get("commit", {}).get("statusCheckRollup")
            if rollup:
                for ctx in rollup.get("contexts", {}).get("nodes", []):
                    typename = ctx.get("__typename")
                    if typename == "CheckRun":
                        conclusion = (ctx.get("conclusion") or "").upper()
                        if conclusion in _FAILED_CONCLUSIONS:
                            has_failed = True
                        elif (ctx.get("status") or "").upper() != "COMPLETED":
                            # null conclusion + non-COMPLETED status
                            # means the check is still running/queued.
                            has_pending = True
                    elif typename == "StatusContext":
                        state = (ctx.get("state") or "").upper()
                        if state in _FAILED_STATES:
                            has_failed = True
                        elif state == "PENDING":
                            has_pending = True

        # review_decision is "" when no reviews are required or
        # none have been submitted yet.  Treat "" as non-blocking
        # so repos without mandatory review can still auto-merge.
        has_blocking_review = review_decision not in ("APPROVED", "")

        reasons: list[str] = []
        if mergeable == "CONFLICTING":
            reasons.append("merge conflicts")
        if has_unresolved:
            reasons.append("unresolved review threads")
        if has_failed:
            reasons.append("failed CI checks")
        if has_pending:
            reasons.append("in-progress CI checks")
        if has_blocking_review:
            reasons.append(f"review: {review_decision}")

        is_ready = (
            mergeable == "MERGEABLE"
            and not has_unresolved
            and not has_failed
            and not has_pending
            and not has_blocking_review
        )

        return MergeReadiness(
            is_ready=is_ready,
            mergeable=mergeable,
            review_decision=review_decision,
            has_failed_checks=has_failed,
            has_pending_checks=has_pending,
            has_unresolved_threads=has_unresolved,
            reasons=reasons,
        )

    def _graphql(self, query: str, variables: dict) -> dict:
        """Execute a GraphQL query."""
        resp = self._session.post(self.BASE_URL, json={"query": query, "variables": variables})
        resp.raise_for_status()
        result = resp.json()
        if "errors" in result:
            raise RuntimeError(f"GraphQL errors: {result['errors']}")
        return result["data"]
