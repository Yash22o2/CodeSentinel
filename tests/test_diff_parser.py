"""Tests for unified diff parser."""
import pytest
from app.github.diff_parser import parse_diff


SAMPLE_DIFF = """\
diff --git a/src/auth.py b/src/auth.py
index abc123..def456 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,6 +10,8 @@
 def authenticate(username, password):
-    query = f"SELECT * FROM users WHERE name = '{username}'"
+    query = "SELECT * FROM users WHERE name = ?"
+    params = (username,)
     return db.execute(query)
diff --git a/README.md b/README.md
index 111..222 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # My Project
+Added login feature
 Some description
"""


def test_parse_diff_finds_files():
    parsed = parse_diff(SAMPLE_DIFF)
    assert "src/auth.py" in parsed.changed_files
    assert "README.md" in parsed.changed_files


def test_parse_diff_finds_python_files():
    parsed = parse_diff(SAMPLE_DIFF)
    assert "src/auth.py" in parsed.python_files
    assert "README.md" not in parsed.python_files


def test_parse_diff_detects_auth_patterns():
    parsed = parse_diff(SAMPLE_DIFF)
    assert parsed.has_auth_patterns  # "SELECT" / "query" in added lines


def test_parse_diff_added_lines():
    parsed = parse_diff(SAMPLE_DIFF)
    auth_chunks = parsed.chunks_for_file("src/auth.py")
    assert len(auth_chunks) > 0
    added_contents = [line for _, line in auth_chunks[0].added_lines]
    assert any("?" in line for line in added_contents)


def test_parse_empty_diff():
    parsed = parse_diff("")
    assert parsed.chunks == []
    assert parsed.changed_files == []
