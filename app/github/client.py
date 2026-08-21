"""
GitHub API Client
==================
Wraps PyGithub + raw httpx calls for operations PyGithub doesn't handle well
(e.g. fetching the raw unified diff).

Retry logic is baked in via @retry from tenacity — every API call
automatically retries up to 3 times with exponential backoff + jitter.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import httpx
from github import Auth, Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

# Retry decorator for all GitHub API calls
_github_retry = retry(
    retry=retry_if_exception_type((GithubException, httpx.HTTPStatusError, httpx.TimeoutException)),
    wait=wait_random_exponential(min=1, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)


class GitHubClient:
    """
    Async-friendly GitHub API client.

    Usage:
        client = GitHubClient()
        pr = client.get_pull_request("owner/repo", 42)
        diff = await client.get_pr_diff("owner/repo", 42)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.github_token
        self._gh = Github(auth=Auth.Token(self._token), per_page=100)
        self._http_client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github.v3.diff",  # unified diff format
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    # ── Repository & PR ───────────────────────────────────────────────────────

    @_github_retry
    def get_repository(self, repo_full_name: str) -> Repository:
        """Fetch repo object. Cached by PyGithub."""
        return self._gh.get_repo(repo_full_name)

    @_github_retry
    def get_pull_request(self, repo_full_name: str, pr_number: int) -> PullRequest:
        """Fetch PR object with all metadata."""
        repo = self.get_repository(repo_full_name)
        return repo.get_pull(pr_number)

    @_github_retry
    def get_changed_files(self, repo_full_name: str, pr_number: int) -> list[str]:
        """Return list of file paths changed in the PR."""
        pr = self.get_pull_request(repo_full_name, pr_number)
        return [f.filename for f in pr.get_files()]

    # ── Diff Fetching ─────────────────────────────────────────────────────────

    async def get_pr_diff(self, repo_full_name: str, pr_number: int) -> str:
        """
        Fetch the unified diff of a PR via the GitHub REST API.

        Returns the raw unified diff text (what you'd see in `git diff`).
        Uses httpx directly because PyGithub doesn't expose this cleanly.
        """
        url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
            diff = response.text
            logger.info(
                "Fetched PR diff",
                extra={"repo": repo_full_name, "pr": pr_number, "diff_bytes": len(diff)},
            )
            return diff
        except httpx.HTTPStatusError as e:
            logger.error(
                "Failed to fetch PR diff: %s %s", e.response.status_code, e.response.text[:200]
            )
            raise

    async def get_file_content(
        self, repo_full_name: str, file_path: str, ref: str
    ) -> str | None:
        """
        Fetch full file content at a specific git ref (commit SHA).
        Returns None if the file doesn't exist at that ref (e.g. newly added file).
        """
        url = (
            f"https://api.github.com/repos/{repo_full_name}/contents/{file_path}"
            f"?ref={ref}"
        )
        try:
            response = await self._http_client.get(
                url,
                headers={**self._http_client.headers, "Accept": "application/vnd.github.v3.raw"},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            logger.warning("Could not fetch file %s at %s: %s", file_path, ref, e)
            return None

    # ── Comment Posting ───────────────────────────────────────────────────────

    @_github_retry
    def post_review(
        self,
        repo_full_name: str,
        pr_number: int,
        body: str,
        inline_comments: list[dict] | None = None,
        commit_sha: str | None = None,
        event: str = "COMMENT",
    ) -> None:
        """
        Post a PR review with an optional summary body and inline comments.

        Args:
            repo_full_name: "owner/repo"
            pr_number: PR number
            body: Markdown summary comment (the overall review)
            inline_comments: List of dicts:
                {
                    "path": "src/auth.py",
                    "line": 42,
                    "body": "This is vulnerable to SQL injection"
                }
            commit_sha: The head commit SHA to attach the review to
            event: "COMMENT" | "APPROVE" | "REQUEST_CHANGES"
        """
        repo = self.get_repository(repo_full_name)
        pr = repo.get_pull(pr_number)

        if not commit_sha:
            commit_sha = pr.head.sha

        # Build the list of ReviewComment objects for inline annotations
        review_comments = []
        if inline_comments:
            commit = repo.get_commit(commit_sha)
            for c in inline_comments:
                try:
                    review_comments.append(
                        pr.create_review_comment(
                            body=c["body"],
                            commit=commit,
                            path=c["path"],
                            line=c["line"],
                        )
                    )
                except GithubException as e:
                    # Line may not be in the diff — skip gracefully
                    logger.warning(
                        "Could not post inline comment on %s:%s — %s",
                        c["path"],
                        c["line"],
                        e,
                    )

        # Post the main review body as a PR review
        pr.create_review(body=body, event=event)
        logger.info(
            "Posted review on %s#%s with %d inline comments",
            repo_full_name,
            pr_number,
            len(review_comments),
        )

    @_github_retry
    def post_issue_comment(
        self, repo_full_name: str, pr_number: int, body: str
    ) -> None:
        """Post a plain issue comment on the PR (simpler than a full review)."""
        repo = self.get_repository(repo_full_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(body)

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._http_client.aclose()


def get_github_client() -> GitHubClient:
    """GitHub client factory."""
    return GitHubClient()
