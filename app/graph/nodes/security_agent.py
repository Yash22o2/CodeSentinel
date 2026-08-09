"""
Security Agent
==============
Specialist agent for security vulnerabilities in code diffs.
Focuses on: auth bypass, injection, secrets in code, crypto weaknesses,
insecure deserialization, path traversal, SSRF, and dependency issues.
"""
from __future__ import annotations

from app.graph.nodes.base_agent import BaseAgent


class SecurityAgent(BaseAgent):
    agent_name = "security"
    SYSTEM_PROMPT = """\
You are a senior application security engineer performing a focused security \
code review. You have deep expertise in OWASP Top 10, CWE, and real-world \
vulnerability patterns.

Your ONLY job is to find security vulnerabilities in the ADDED lines of this diff.

Focus on:
- Injection flaws: SQL injection, command injection, SSTI, XSS
- Authentication & authorization: broken auth, JWT weaknesses, missing access checks
- Secrets & credentials: hardcoded API keys, passwords, tokens in source code
- Cryptography: use of MD5/SHA1 for passwords, weak random, ECB mode
- Deserialization: pickle.loads, yaml.load without Loader, json.loads on untrusted input
- Path traversal: os.path.join with user input, open() without validation
- SSRF: requests.get(user_input), urllib with unvalidated URLs
- Dependency confusion / supply chain: suspicious package names or imports

Be precise. Only flag genuine vulnerabilities, not style issues.
Confidence > 0.7 means you are sure. Flag 0.4-0.7 as speculative.
Do NOT report low-confidence guesses as high-severity."""

    def _build_prompt(self, diff_context: str, repo: str, pr: int) -> str:
        return (
            f"Security review for PR #{pr} in {repo}.\n\n"
            f"Review ONLY the added lines (+) for security vulnerabilities:\n\n"
            f"{diff_context}"
        )


# Singleton — instantiated once, called as a LangGraph node
security_agent = SecurityAgent()
