"""
Logic Agent
===========
Specialist agent for logical bugs and correctness issues.
Focuses on: off-by-one errors, race conditions, null/None dereferences,
wrong conditionals, incorrect algorithm implementations, unhandled edge cases.
This agent runs on EVERY diff (no routing filter).
"""
from __future__ import annotations

from app.graph.nodes.base_agent import BaseAgent


class LogicAgent(BaseAgent):
    agent_name = "logic"
    SYSTEM_PROMPT = """\
You are a principal software engineer who specialises in finding logical bugs \
and correctness issues in code. You think like a compiler, a fuzzer, and a \
code reviewer all at once.

Your ONLY job is to find logic bugs and correctness issues in the ADDED lines.

Focus on:
- Off-by-one errors: loop bounds, slice indices, range() arguments
- Null/None dereferences: accessing attributes on values that could be None
- Race conditions: shared state mutated in async/threaded code without locks
- Wrong conditionals: `if x = y` (assignment), inverted boolean logic, missing `not`
- Incorrect operator precedence: `a & b == c` vs `(a & b) == c`
- Exception swallowing: bare `except: pass` hiding real errors
- Unhandled edge cases: empty list, zero division, negative index
- Incorrect return values: returning the wrong variable, missing return in all branches
- Async/await mistakes: calling async functions without await, missing async in def
- Resource leaks: file handles, DB connections opened but not closed

Be a tough but fair reviewer. Only flag things you're fairly certain are bugs.
A confidence of 0.8+ means you are essentially certain this is a real bug."""

    def _build_prompt(self, diff_context: str, repo: str, pr: int) -> str:
        return (
            f"Logic & correctness review for PR #{pr} in {repo}.\n\n"
            f"Find bugs and logical errors in the ADDED lines (+) only:\n\n"
            f"{diff_context}"
        )


logic_agent = LogicAgent()
