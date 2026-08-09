"""
Unified Diff Parser
====================
Parses the raw unified diff from a PR into structured DiffChunk objects.
These are passed to agents so they can reason about specific changed lines
rather than the entire file.

A unified diff looks like:
    diff --git a/src/auth.py b/src/auth.py
    index abc..def 100644
    --- a/src/auth.py
    +++ b/src/auth.py
    @@ -10,6 +10,8 @@
     context line
    -removed line
    +added line
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DiffChunk:
    """A single changed hunk within a file."""

    file_path: str
    """Repo-relative file path (new path for renames)."""

    start_line: int
    """First line number in the new file for this hunk."""

    end_line: int
    """Last line number in the new file for this hunk."""

    added_lines: list[tuple[int, str]]
    """(line_number, content) for lines added (+) in this hunk."""

    removed_lines: list[tuple[int, str]]
    """(line_number, content) for lines removed (-) in this hunk."""

    context_lines: list[tuple[int, str]]
    """(line_number, content) for context lines (unchanged, shown for context)."""

    raw_hunk: str = ""
    """The raw text of this hunk for LLM context."""

    @property
    def is_python(self) -> bool:
        return self.file_path.endswith(".py")

    @property
    def added_content(self) -> str:
        return "\n".join(line for _, line in self.added_lines)

    @property
    def full_diff_context(self) -> str:
        """Returns the hunk in a human-readable format for LLM prompts."""
        return (
            f"File: {self.file_path} (lines {self.start_line}–{self.end_line})\n"
            f"{self.raw_hunk}"
        )


@dataclass
class ParsedDiff:
    """Complete parsed diff for a PR."""

    chunks: list[DiffChunk] = field(default_factory=list)

    @property
    def changed_files(self) -> list[str]:
        """Unique list of files that have at least one changed chunk."""
        return list(dict.fromkeys(c.file_path for c in self.chunks))

    @property
    def python_files(self) -> list[str]:
        return [f for f in self.changed_files if f.endswith(".py")]

    @property
    def has_auth_patterns(self) -> bool:
        """Heuristic: any security-relevant keywords in added lines."""
        security_keywords = {
            "password", "token", "secret", "auth", "crypto", "jwt", "hash",
            "sql", "query", "execute", "eval", "exec", "subprocess", "os.system",
            "pickle", "deserializ", "marshal",
        }
        for chunk in self.chunks:
            combined = (chunk.added_content).lower()
            if any(kw in combined for kw in security_keywords):
                return True
        return False

    @property
    def has_test_files(self) -> bool:
        return any(
            "test_" in f or "_test.py" in f or f.startswith("tests/")
            for f in self.changed_files
        )

    def chunks_for_file(self, file_path: str) -> list[DiffChunk]:
        return [c for c in self.chunks if c.file_path == file_path]


# ── Parser ────────────────────────────────────────────────────────────────────

# Matches: diff --git a/path/to/file b/path/to/file
_FILE_HEADER_RE = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)
# Matches: @@ -old_start,old_count +new_start,new_count @@
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def parse_diff(raw_diff: str) -> ParsedDiff:
    """
    Parse a GitHub PR unified diff into structured DiffChunk objects.

    Args:
        raw_diff: The raw unified diff text (from GitHub API with Accept: diff)

    Returns:
        ParsedDiff with all changed hunks parsed into DiffChunk objects.
    """
    if not raw_diff or not raw_diff.strip():
        return ParsedDiff()

    parsed = ParsedDiff()

    # Split the diff into per-file sections
    file_sections = _FILE_HEADER_RE.split(raw_diff)
    # file_sections: ["preamble", "file1.py", "...file1 diff...", "file2.py", "..."]
    # The split captures group 1 (file path), so odd indices are paths, even are content

    # Pair up: (file_path, file_diff_text)
    it = iter(file_sections[1:])  # skip the preamble before the first "diff --git"
    for file_path, file_diff in zip(it, it):
        file_path = file_path.strip()
        _parse_file_hunks(file_path, file_diff, parsed)

    return parsed


def _parse_file_hunks(file_path: str, file_diff: str, parsed: ParsedDiff) -> None:
    """Parse all hunks within a single file's diff section."""
    hunk_parts = _HUNK_HEADER_RE.split(file_diff)
    # hunk_parts: ["header stuff", new_start1, new_count1, "hunk1 lines", new_start2, ...]

    # Walk in groups of 3: (new_start, new_count, hunk_lines)
    i = 1
    while i + 2 < len(hunk_parts):
        new_start = int(hunk_parts[i])
        new_count_str = hunk_parts[i + 1]
        new_count = int(new_count_str) if new_count_str else 1
        hunk_text = hunk_parts[i + 2]

        chunk = _parse_hunk(file_path, new_start, new_count, hunk_text)
        if chunk:
            parsed.chunks.append(chunk)
        i += 3


def _parse_hunk(
    file_path: str, new_start: int, new_count: int, hunk_text: str
) -> DiffChunk | None:
    """Parse the lines within a single hunk."""
    added: list[tuple[int, str]] = []
    removed: list[tuple[int, str]] = []
    context: list[tuple[int, str]] = []

    new_line = new_start
    raw_lines = []

    for raw_line in hunk_text.splitlines():
        if raw_line.startswith("+"):
            content = raw_line[1:]
            added.append((new_line, content))
            new_line += 1
            raw_lines.append(f"+{content}")
        elif raw_line.startswith("-"):
            content = raw_line[1:]
            removed.append((new_line, content))
            raw_lines.append(f"-{content}")
            # removed lines don't advance new_line
        elif raw_line.startswith("\\"):
            # "\ No newline at end of file" — skip
            continue
        else:
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            context.append((new_line, content))
            new_line += 1
            raw_lines.append(f" {content}")

    if not added and not removed:
        return None

    end_line = max(new_start + new_count - 1, new_start)

    return DiffChunk(
        file_path=file_path,
        start_line=new_start,
        end_line=end_line,
        added_lines=added,
        removed_lines=removed,
        context_lines=context,
        raw_hunk="\n".join(raw_lines),
    )


def format_diff_for_llm(parsed_diff: ParsedDiff, max_chars: int = 12_000) -> str:
    """
    Format the parsed diff into a compact string for LLM prompts.
    Truncates to max_chars to stay within context limits.
    """
    lines = []
    for chunk in parsed_diff.chunks:
        lines.append(chunk.full_diff_context)
        lines.append("")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n[... diff truncated for context limit ...]"
    return result
