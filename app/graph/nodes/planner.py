"""
Planner Node
============
First node in the graph. Inspects the parsed diff and decides which specialist
agents to invoke — avoids wasting LLM tokens on irrelevant agents.

Decision logic:
  - Security agent  → if auth/crypto/injection patterns detected OR .env / config files changed
  - Style agent     → if Python/JS/TS files changed
  - Logic agent     → always (core bug detection)
  - Test agent      → if source files changed without corresponding test changes
"""
from __future__ import annotations

import time

import structlog

from app.github.diff_parser import DiffChunk
from app.graph.state import GraphState
from app.schemas import AgentPlan

log = structlog.get_logger(__name__)


def planner_node(state: GraphState) -> dict:
    """
    Analyse the diff metadata and emit an AgentPlan.
    This node runs zero LLM calls — it's pure heuristics.
    Pure routing: fast, deterministic, testable.
    """
    t0 = time.monotonic()
    diff_files: list[DiffChunk] = state["diff_files"]
    repo = state["repo_full_name"]
    pr = state["pr_number"]

    log.info("planner.start", repo=repo, pr=pr, files=len(diff_files))

    # ── Heuristics ─────────────────────────────────────────────────────────────────
    filenames = [f.file_path for f in diff_files]
    all_added_lines = "\n".join(
        content
        for f in diff_files
        for _, content in f.added_lines
    )

    has_python = any(fn.endswith(".py") for fn in filenames)
    has_js_ts  = any(fn.endswith((".js", ".ts", ".jsx", ".tsx")) for fn in filenames)
    has_config = any(
        fn.endswith((".env", ".yml", ".yaml", ".toml", ".json", ".cfg", ".ini"))
        or ".env" in fn or "config" in fn.lower()
        for fn in filenames
    )
    has_tests = any(
        "test" in fn.lower() or "spec" in fn.lower()
        for fn in filenames
    )
    has_source_without_tests = (has_python or has_js_ts) and not has_tests

    # Security triggers
    security_keywords = [
        "password", "secret", "token", "api_key", "auth", "jwt",
        "eval(", "exec(", "subprocess", "os.system", "shell=True",
        "sql", "query", "cursor.execute", "pickle", "yaml.load",
        "hashlib.md5", "hashlib.sha1", "random.random",
    ]
    has_security_signals = has_config or any(
        kw in all_added_lines.lower() for kw in security_keywords
    )

    # ── Build plan ────────────────────────────────────────────────────────────
    agents_to_run: list[str] = []
    rationale: list[str] = []

    if has_security_signals:
        agents_to_run.append("security")
        rationale.append(
            "Security patterns detected (auth/crypto/config/injection keywords)"
        )

    if has_python or has_js_ts:
        agents_to_run.append("style")
        rationale.append(f"Source files changed: python={has_python}, js/ts={has_js_ts}")

    # Logic agent runs on any code change
    agents_to_run.append("logic")
    rationale.append("Logic/bug analysis runs on all diffs")

    if has_source_without_tests:
        agents_to_run.append("test")
        rationale.append("Source changed without corresponding test file changes")

    elapsed = (time.monotonic() - t0) * 1000
    plan = AgentPlan(
        agents_to_run=agents_to_run,
        rationale="; ".join(rationale),
        diff_summary=f"{len(diff_files)} file(s) changed",
    )

    log.info(
        "planner.done",
        agents=agents_to_run,
        elapsed_ms=round(elapsed, 1),
    )

    return {
        "agent_plan": plan,
        "node_timings": {"planner": elapsed},
        "errors": [],
    }
