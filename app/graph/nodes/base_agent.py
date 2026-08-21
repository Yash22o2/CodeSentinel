"""
Base Agent
==========
Shared logic for all specialist LLM agents.
Each agent subclasses BaseAgent and only overrides:
  - SYSTEM_PROMPT  — persona and focus area
  - finding_type   — the Severity/category tag

The base handles:
  - LLM API call (via langchain-openai → OpenRouter or any OpenAI-compatible endpoint)
  - Tenacity retries (3x with exp backoff)
  - Structured output parsing (Finding list)
  - Timing instrumentation
  - Non-fatal error capture into state["errors"]
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog
from langchain_openai import ChatOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.github.diff_parser import DiffChunk
from app.reliability.circuit_breaker import CircuitBreakerOpenError, get_breaker
from app.schemas import Finding, Severity

log = structlog.get_logger(__name__)

# ── LLM client — built lazily so .env changes take effect without restarting worker ──
_llm_cache: ChatOpenAI | None = None
_llm_cache_model: str = ""


def _get_llm() -> ChatOpenAI:
    """Return a cached ChatOpenAI client, rebuilding it if the model has changed."""
    global _llm_cache, _llm_cache_model
    s = get_settings()
    if _llm_cache is None or _llm_cache_model != s.llm_model:
        _llm_cache = ChatOpenAI(
            model=s.llm_model,
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
            default_headers={
                "HTTP-Referer": "https://github.com/Yash22o2/CodeSentinel",
                "X-Title": "CodeSentinel",
            },
        )
        _llm_cache_model = s.llm_model
        log.info("LLM client initialised", model=s.llm_model, base_url=s.llm_base_url)
    return _llm_cache


def _build_diff_context(diff_files: list[DiffChunk], max_chars: int = 12_000) -> str:
    """Render the diff chunks into a compact text block for the prompt."""
    parts: list[str] = []
    total = 0
    for chunk in diff_files:
        header = f"### {chunk.file_path} (lines {chunk.start_line}-{chunk.end_line})\n"
        body = chunk.raw_hunk
        section = header + body + "\n\n"
        if total + len(section) > max_chars:
            parts.append(f"### {chunk.file_path} [TRUNCATED]\n\n")
            break
        parts.append(section)
        total += len(section)
    return "".join(parts)


_FINDING_SCHEMA = """
Return a JSON array of findings. Each finding must be:
{
  "file": "<file path>",
  "line": <integer or null>,
  "severity": "<critical|high|medium|low|info>",
  "category": "<security|style|test_coverage|logic>",
  "message": "<detailed explanation, 1-3 sentences>",
  "suggested_fix": "<concrete fix or improvement>",
  "confidence": <float 0.0-1.0>
}

Rules:
- Return [] if you find nothing significant.
- Do NOT invent issues that aren't clearly present in the diff.
- Only report lines that are ADDED (+ lines), not removed lines.
- confidence: 1.0 = certain bug/issue, 0.5 = possible issue, <0.4 = speculative
- Output ONLY the raw JSON array, no markdown, no explanation.
"""


class BaseAgent(ABC):
    """Abstract base for all specialist review agents."""

    SYSTEM_PROMPT: str = ""  # Override in subclass
    agent_name: str = "base"

    def __call__(self, state: dict) -> dict:
        """LangGraph node entrypoint — called with current graph state."""
        t0 = time.monotonic()
        diff_files: list[DiffChunk] = state["diff_files"]
        repo = state["repo_full_name"]
        pr = state["pr_number"]

        log.info(f"{self.agent_name}.start", repo=repo, pr=pr)

        # ── Phase 3: Check graph deadline ───────────────────────────────────────────────
        graph_deadline: float | None = state.get("graph_deadline")
        if graph_deadline is not None:
            remaining = graph_deadline - time.monotonic()
            if remaining <= 2.0:  # Less than 2 seconds left, skip this agent
                log.warning(
                    f"{self.agent_name}.skipped_deadline",
                    remaining_s=round(remaining, 1),
                )
                elapsed = (time.monotonic() - t0) * 1000
                return {
                    f"{self.agent_name}_findings": [],
                    "node_timings": {self.agent_name: elapsed},
                    "errors": [f"{self.agent_name}: skipped (graph deadline exceeded)"],
                }

        # ── Phase 3: Circuit breaker ────────────────────────────────────────────────────
        cb = get_breaker(
            self.agent_name,
            failure_threshold=get_settings().cb_failure_threshold,
            recovery_timeout=get_settings().cb_recovery_timeout_s,
        )

        try:
            findings = cb.call(
                lambda: self._run_with_timeout(diff_files, repo, pr, graph_deadline)
            )
        except CircuitBreakerOpenError as exc:
            elapsed = (time.monotonic() - t0) * 1000
            log.warning(f"{self.agent_name}.circuit_open", retry_after=exc.retry_after)
            return {
                f"{self.agent_name}_findings": [],
                "node_timings": {self.agent_name: elapsed},
                "errors": [f"{self.agent_name}: circuit breaker OPEN (retry in {exc.retry_after:.0f}s)"],
            }
        except Exception as exc:
            log.error(f"{self.agent_name}.failed", error=str(exc))
            elapsed = (time.monotonic() - t0) * 1000
            return {
                f"{self.agent_name}_findings": [],
                "node_timings": {self.agent_name: elapsed},
                "errors": [f"{self.agent_name}: {exc}"],
            }

        elapsed = (time.monotonic() - t0) * 1000
        log.info(
            f"{self.agent_name}.done",
            findings=len(findings),
            elapsed_ms=round(elapsed, 1),
        )
        return {
            f"{self.agent_name}_findings": findings,
            "node_timings": {self.agent_name: elapsed},
            "errors": [],
        }

    def _run_with_timeout(
        self,
        diff_files: list[DiffChunk],
        repo: str,
        pr: int,
        graph_deadline: float | None,
    ) -> list[Finding]:
        """
        Run the agent with a per-node timeout.
        Uses asyncio.wait_for if an event loop is running; otherwise falls back
        to a direct call (sync context in RQ worker).
        """
        timeout = get_settings().agent_timeout_s

        # Honour graph deadline: don't wait longer than remaining time
        if graph_deadline is not None:
            remaining = graph_deadline - time.monotonic()
            timeout = min(timeout, max(1.0, remaining - 1.0))

        # Try async path (if called from an async context)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    self._run_async(diff_files, repo, pr, timeout), loop
                )
                return future.result(timeout=timeout + 1)
        except RuntimeError:
            pass  # No event loop — sync fallback below

        # Sync path (RQ worker, tests): just call directly with tenacity retries
        # Timeout enforcement in sync mode happens via threading.Timer signal
        return self._run_with_retry(diff_files, repo, pr)

    async def _run_async(
        self,
        diff_files: list[DiffChunk],
        repo: str,
        pr: int,
        timeout: float,
    ) -> list[Finding]:
        """Async wrapper so asyncio.wait_for can apply a timeout."""
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: self._run_with_retry(diff_files, repo, pr)),
            timeout=timeout,
        )

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _run_with_retry(
        self, diff_files: list[DiffChunk], repo: str, pr: int
    ) -> list[Finding]:
        diff_context = _build_diff_context(diff_files)
        prompt = self._build_prompt(diff_context, repo, pr)
        response = _get_llm().invoke(
            [
                {"role": "system", "content": self.SYSTEM_PROMPT + "\n\n" + _FINDING_SCHEMA},
                {"role": "user", "content": prompt},
            ]
        )
        return self._parse_findings(response.content, diff_files)

    @abstractmethod
    def _build_prompt(self, diff_context: str, repo: str, pr: int) -> str:
        """Build the user-turn prompt. Override in subclass."""
        ...

    def _parse_findings(
        self, raw: str, diff_files: list[DiffChunk]
    ) -> list[Finding]:
        """Parse LLM JSON output into Finding objects, tolerating partial failures."""
        # Strip <think>...</think> blocks emitted by reasoning models (e.g. Qwen)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # Strip markdown code fences if present
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract the first JSON array we can find
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                log.warning(f"{self.agent_name}.parse_failed", raw=raw[:200])
                return []
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return []

        if not isinstance(data, list):
            return []

        filenames = {f.file_path for f in diff_files}
        findings: list[Finding] = []
        for item in data:
            try:
                severity_str = item.get("severity", "medium").lower()
                try:
                    severity = Severity(severity_str)
                except ValueError:
                    severity = Severity.MEDIUM

                category_str = item.get("category", "logic").lower()
                try:
                    from app.schemas import FindingCategory
                    category = FindingCategory(category_str)
                except (ValueError, ImportError):
                    from app.schemas import FindingCategory
                    category = FindingCategory.LOGIC

                # Support both old-style LLM outputs (filename/title) and
                # new-style (file/message) for forward-compat during migration
                file_path = (
                    item.get("file") or item.get("filename") or "unknown"
                )
                line_num = item.get("line") or item.get("line_number") or 1
                if line_num is None:
                    line_num = 1

                message_text = (
                    item.get("message")
                    or item.get("description")
                    or item.get("title")
                    or "No description provided by agent"
                )

                finding = Finding(
                    file=file_path,
                    line=int(line_num),
                    severity=severity,
                    category=category,
                    message=message_text,
                    suggested_fix=item.get("suggestion") or item.get("suggested_fix"),
                    rule_id=item.get("rule_id"),
                    tool=item.get("tool") or self.agent_name,
                    confidence=float(item.get("confidence", 0.7)),
                )
                findings.append(finding)
            except Exception as e:
                log.warning(f"{self.agent_name}.finding_parse_error", error=str(e))
                continue

        return findings
