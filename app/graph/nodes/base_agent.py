"""
Base Agent
==========
Shared logic for all specialist LLM agents.
Each agent subclasses BaseAgent and only overrides:
  - SYSTEM_PROMPT  — persona and focus area
  - finding_type   — the Severity/category tag

The base handles:
  - Groq API call (via langchain-groq)
  - Tenacity retries (3x with exp backoff)
  - Structured output parsing (Finding list)
  - Timing instrumentation
  - Non-fatal error capture into state["errors"]
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog
from langchain_groq import ChatGroq
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.github.diff_parser import DiffChunk
from app.schemas import Finding, Severity

log = structlog.get_logger(__name__)

# ── Shared Groq client (one per process, reused across all agents) ────────────
_settings = get_settings()
_llm = ChatGroq(
    model=_settings.groq_model,
    api_key=_settings.groq_api_key,
    temperature=_settings.groq_temperature,
    max_tokens=_settings.groq_max_tokens,
)


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
  "filename": "<file path>",
  "line_number": <integer or null>,
  "severity": "<critical|high|medium|low|info>",
  "title": "<short one-line title>",
  "description": "<detailed explanation, 1-3 sentences>",
  "suggestion": "<concrete fix or improvement>",
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
        diff_files: list[DiffFile] = state["diff_files"]
        repo = state["repo_full_name"]
        pr = state["pr_number"]

        log.info(f"{self.agent_name}.start", repo=repo, pr=pr)

        try:
            findings = self._run_with_retry(diff_files, repo, pr)
        except Exception as exc:
            log.error(f"{self.agent_name}.failed", error=str(exc))
            findings = []
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
        response = _llm.invoke(
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
