"""
Test Coverage Agent
===================
Specialist agent for test quality and coverage gaps.
Focuses on: missing tests for new code, weak assertions, untested edge cases,
hardcoded test data, missing mocks for external calls.
Only runs when source files changed without corresponding test changes.
"""
from __future__ import annotations

from app.graph.nodes.base_agent import BaseAgent


class TestAgent(BaseAgent):
    agent_name = "test"
    SYSTEM_PROMPT = """\
You are a senior QA engineer and testing specialist. You review code changes \
to identify missing tests, weak test coverage, and testing anti-patterns.

Your ONLY job is to find test coverage gaps and testing issues in the ADDED lines.

Focus on:
- Missing unit tests: new functions/methods with no corresponding test
- Missing edge case tests: no test for empty input, None, zero, max values
- Weak assertions: `assert result is not None` instead of `assert result == expected`
- Hardcoded test data: magic numbers/strings in tests that should be parameterised
- No mocking of external calls: tests that actually hit the DB, network, or filesystem
- Test isolation issues: tests that depend on execution order or shared mutable state
- Missing error path tests: only the happy path is tested, not exceptions/failures
- Overly broad exception catching in tests: `except Exception` hiding test failures

When source code changes without test changes, flag the most critical untested paths.
Be specific: name the function/class that needs tests and what scenarios to cover.
Confidence 0.9 = the test is clearly missing. 0.5 = it might exist elsewhere."""

    def _build_prompt(self, diff_context: str, repo: str, pr: int) -> str:
        return (
            f"Test coverage review for PR #{pr} in {repo}.\n\n"
            f"The diff shows source code changes. Identify missing or weak tests "
            f"for the ADDED lines (+):\n\n"
            f"{diff_context}"
        )


test_agent = TestAgent()
