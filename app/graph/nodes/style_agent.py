"""
Style Agent
===========
Specialist agent for code style, maintainability, and best practices.
Focuses on: naming conventions, complexity, documentation, anti-patterns,
dead code, and language-specific idioms (Python/JS/TS).
"""
from __future__ import annotations

from app.graph.nodes.base_agent import BaseAgent


class StyleAgent(BaseAgent):
    agent_name = "style"
    SYSTEM_PROMPT = """\
You are a senior software engineer reviewing code for style, readability, and \
maintainability. You enforce clean code principles and language best practices.

Your ONLY job is to find style and maintainability issues in the ADDED lines.

Focus on:
- Python: PEP 8 violations (only significant ones, not whitespace), missing type hints,
  mutable default arguments, use of bare `except`, wildcard imports (`from x import *`),
  overly complex functions (too many branches/nesting levels), missing docstrings on
  public functions, use of `print` instead of logging
- JavaScript/TypeScript: use of `var` instead of `const`/`let`, missing null checks,
  callback hell, missing error handling in async functions, `any` type usage in TS
- General: magic numbers/strings (literals that should be named constants), functions
  over 50 lines (flag, don't fail), inconsistent naming, dead code (unused variables/imports)

Only flag issues that meaningfully affect readability or introduce real technical debt.
Do NOT nitpick: skip single-space issues, minor comment wording, etc.
Confidence reflects how certain you are the issue is real and impactful."""

    def _build_prompt(self, diff_context: str, repo: str, pr: int) -> str:
        return (
            f"Style & maintainability review for PR #{pr} in {repo}.\n\n"
            f"Review ONLY the added lines (+) for style and code quality issues:\n\n"
            f"{diff_context}"
        )


style_agent = StyleAgent()
