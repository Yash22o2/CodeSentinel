"""
CodeSentinel — Pydantic Schemas
All inter-agent messages use validated schemas — never raw dicts or free-text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator


# ── Enums ────────────────────────────────────────────────────────────────────


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(str, Enum):
    SECURITY = "security"
    STYLE = "style"
    TEST_COVERAGE = "test_coverage"
    LOGIC = "logic"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"   # circuit-broken: ran but failed
    SKIPPED = "skipped"     # planner decided not to invoke


# ── Core Finding ─────────────────────────────────────────────────────────────


class Finding(BaseModel):
    """A single issue identified by a specialist agent.

    This is the atomic unit of the review system. Every agent returns
    a list of these. Never pass free-text between agents.
    """

    file: str = Field(..., description="Repo-relative file path, e.g. 'src/auth/login.py'")
    line: int = Field(..., ge=1, description="Line number in the file (1-indexed)")
    end_line: Optional[int] = Field(
        None, ge=1, description="End line for multi-line findings"
    )
    severity: Severity
    category: FindingCategory
    message: str = Field(..., min_length=10, max_length=1000)
    suggested_fix: Optional[str] = Field(
        None, max_length=2000, description="LLM-generated fix suggestion"
    )
    rule_id: Optional[str] = Field(
        None, description="Static analysis rule ID (e.g. 'B307', 'E501')"
    )
    tool: Optional[str] = Field(
        None, description="Tool that produced this finding: 'bandit', 'ruff', 'llm', 'ast'"
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Set by Critic node; 1.0 = certain"
    )
    false_positive_reason: Optional[str] = Field(
        None, description="Critic's reasoning if confidence < threshold"
    )

    @field_validator("end_line", mode="after")
    @classmethod
    def end_line_gte_line(cls, v: Optional[int], info) -> Optional[int]:
        if v is not None and "line" in info.data and v < info.data["line"]:
            raise ValueError("end_line must be >= line")
        return v


# ── GitHub Webhook Schemas ───────────────────────────────────────────────────


class GitHubUser(BaseModel):
    login: str
    id: int


class GitHubRepo(BaseModel):
    id: int
    name: str
    full_name: str
    private: bool
    html_url: str
    default_branch: str


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    state: str
    html_url: str
    head: dict  # head.sha, head.ref
    base: dict  # base.sha, base.ref
    user: GitHubUser
    body: Optional[str] = None
    draft: bool = False
    changed_files: Optional[int] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None


class GitHubWebhookPayload(BaseModel):
    """Validates the incoming GitHub webhook payload for pull_request events."""

    action: str  # opened, synchronize, reopened, closed, ...
    number: int
    pull_request: GitHubPullRequest
    repository: GitHubRepo
    sender: GitHubUser
    installation: Optional[dict] = None  # for GitHub App installs

    @property
    def should_review(self) -> bool:
        """Only trigger a review for these PR actions."""
        return self.action in {"opened", "synchronize", "reopened"} and not self.pull_request.draft


# ── Job / Agent Communication ─────────────────────────────────────────────────


class PRReviewJob(BaseModel):
    """Enqueued job passed from the webhook handler to the review worker."""

    request_id: str = Field(..., description="UUID threaded through all logs/traces")
    repo_full_name: str = Field(..., description="e.g. 'owner/repo'")
    pr_number: int
    head_sha: str
    base_sha: str
    pr_title: str
    pr_url: str
    diff: str = Field(..., description="Full unified diff of the PR")
    changed_files: list[str] = Field(
        default_factory=list, description="List of changed file paths"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentPlan(BaseModel):
    """Planner node output — which agents to invoke and why."""

    invoke_security: bool = True
    invoke_style: bool = True
    invoke_test_coverage: bool = True
    invoke_logic: bool = True
    skip_reasons: dict[str, str] = Field(
        default_factory=dict,
        description="Map of agent_name -> reason it was skipped",
    )
    reasoning: str = ""


class AgentResult(BaseModel):
    """Result from a single specialist agent execution."""

    agent: str
    status: AgentStatus
    findings: list[Finding] = Field(default_factory=list)
    error: Optional[str] = None
    tokens_used: int = 0
    latency_ms: float = 0.0


class ReviewMetadata(BaseModel):
    """Audit trail for cost/latency tracking stored in Postgres."""

    request_id: str
    repo: str
    pr_number: int
    total_findings_raw: int = 0
    total_findings_after_critic: int = 0
    agents_invoked: list[str] = Field(default_factory=list)
    agents_degraded: list[str] = Field(default_factory=list)
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    completed_at: Optional[datetime] = None
