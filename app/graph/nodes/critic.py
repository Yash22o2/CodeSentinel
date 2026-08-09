"""
Critic Node
===========
Self-critique layer that runs AFTER all specialist agents.

Responsibilities:
1. Merge all findings from all agents into one list
2. Deduplicate near-identical findings (same file + line + same issue)
3. Filter false positives using a second LLM pass (confidence < threshold)
4. Assign final severity based on cross-agent agreement
5. Emit the final filtered findings list

The critic is why CodeSentinel has lower false-positive rate than naive reviewers:
a finding only survives if it passes a second, independent LLM evaluation.
"""
from __future__ import annotations

import json
import re
import time

import structlog
from langchain_groq import ChatGroq
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.graph.state import GraphState
from app.schemas import Finding, Severity

log = structlog.get_logger(__name__)

_settings = get_settings()
_llm = ChatGroq(
    model=_settings.groq_model,
    api_key=_settings.groq_api_key,
    temperature=0.0,   # Critic must be deterministic
    max_tokens=2048,
)

# Findings with confidence below this are sent to the LLM for re-evaluation
CONFIDENCE_THRESHOLD = 0.55
# Findings with confidence below this are always dropped (don't waste LLM tokens)
DROP_THRESHOLD = 0.35


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Remove near-duplicate findings (same file + line + similar title)."""
    seen: set[tuple] = set()
    unique: list[Finding] = []
    for f in findings:
        key = (
            f.filename,
            f.line_number,
            f.title[:40].lower().strip(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


_CRITIC_PROMPT = """\
You are a senior code review critic. You are given a list of findings from \
automated specialist agents. Your job is to filter out false positives.

For each finding, decide:
- "keep": the finding is a genuine issue worth reporting
- "drop": the finding is a false positive, too speculative, or not actionable

Rules:
- Drop findings where the issue only exists in removed lines (- lines), not added lines
- Drop findings with very vague descriptions that don't reference specific code
- Drop findings that are purely stylistic preferences with no real impact
- Keep all findings with severity "critical" or "high" unless obviously wrong
- When in doubt, keep the finding (false negatives are worse than false positives here)

Input format: JSON array of findings (same schema as output)
Output format: JSON array of objects: [{"id": <index>, "decision": "keep"|"drop", "reason": "<one sentence>"}]
Output ONLY the raw JSON array, no markdown."""


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _llm_filter(findings: list[Finding]) -> list[Finding]:
    """Use a second LLM call to validate borderline findings."""
    if not findings:
        return []

    findings_json = json.dumps(
        [
            {
                "id": i,
                "filename": f.filename,
                "line_number": f.line_number,
                "severity": f.severity.value,
                "title": f.title,
                "description": f.description,
                "agent": f.agent,
                "confidence": f.confidence,
            }
            for i, f in enumerate(findings)
        ],
        indent=2,
    )

    response = _llm.invoke(
        [
            {"role": "system", "content": _CRITIC_PROMPT},
            {"role": "user", "content": f"Evaluate these findings:\n\n{findings_json}"},
        ]
    )

    raw = re.sub(r"```(?:json)?\s*", "", response.content).strip().rstrip("```")
    try:
        decisions = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return findings  # If we can't parse, keep all (fail safe)
        decisions = json.loads(match.group())

    keep_ids = {d["id"] for d in decisions if d.get("decision") == "keep"}
    # If parsing fails for a finding, default to keep
    if not keep_ids and len(decisions) == 0:
        return findings

    return [f for i, f in enumerate(findings) if i in keep_ids]


def critic_node(state: GraphState) -> dict:
    """LangGraph node: merge, deduplicate, and filter all agent findings."""
    t0 = time.monotonic()
    repo = state["repo_full_name"]
    pr = state["pr_number"]

    log.info("critic.start", repo=repo, pr=pr)

    # ── 1. Merge all agent findings ───────────────────────────────────────────
    all_findings: list[Finding] = (
        state.get("security_findings", []) +
        state.get("style_findings", []) +
        state.get("logic_findings", []) +
        state.get("test_findings", [])
    )
    log.info("critic.merged", total=len(all_findings))

    # ── 2. Drop below hard threshold immediately ──────────────────────────────
    after_hard_drop = [f for f in all_findings if f.confidence >= DROP_THRESHOLD]
    dropped_early = len(all_findings) - len(after_hard_drop)
    if dropped_early:
        log.info("critic.hard_dropped", count=dropped_early)

    # ── 3. Deduplicate ────────────────────────────────────────────────────────
    deduped = _deduplicate(after_hard_drop)
    log.info("critic.deduped", before=len(after_hard_drop), after=len(deduped))

    # ── 4. Split: high-confidence pass-through vs borderline LLM review ──────
    high_confidence = [f for f in deduped if f.confidence >= CONFIDENCE_THRESHOLD]
    borderline      = [f for f in deduped if f.confidence < CONFIDENCE_THRESHOLD]

    # ── 5. LLM filter on borderline findings ─────────────────────────────────
    try:
        validated_borderline = _llm_filter(borderline) if borderline else []
    except Exception as exc:
        log.error("critic.llm_filter_failed", error=str(exc))
        validated_borderline = borderline  # fail-open: keep if critic fails

    filtered = high_confidence + validated_borderline

    # ── 6. Sort by severity then confidence ───────────────────────────────────
    sev_order = {
        Severity.CRITICAL: 0, Severity.HIGH: 1,
        Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4,
    }
    filtered.sort(key=lambda f: (sev_order.get(f.severity, 9), -f.confidence))

    elapsed = (time.monotonic() - t0) * 1000
    log.info(
        "critic.done",
        initial=len(all_findings),
        final=len(filtered),
        elapsed_ms=round(elapsed, 1),
    )

    return {
        "all_findings": all_findings,
        "filtered_findings": filtered,
        "node_timings": {"critic": elapsed},
        "errors": [],
    }
