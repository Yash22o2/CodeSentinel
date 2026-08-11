import pytest
import os
from app.memory.chroma_store import ChromaStore
from app.schemas import Finding, FindingCategory, Severity

@pytest.fixture
def temp_chroma_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_path / ".chroma_data"))
    monkeypatch.setenv("CHROMA_SIMILARITY_THRESHOLD", "0.95")
    
    import app.config
    app.config.get_settings.cache_clear()
    
    import app.memory.chroma_store
    app.memory.chroma_store._store = None
    
    store = app.memory.chroma_store.get_chroma_store()
    yield store
    
    # cleanup singleton
    app.memory.chroma_store._store = None

def test_chroma_store_add_and_find(temp_chroma_store):
    f1 = Finding(
        file="src/main.py",
        line=10,
        severity=Severity.MEDIUM,
        category=FindingCategory.LOGIC,
        message="Unused variable 'x'",
        code_snippet="x = 10",
        rule_id="F841"
    )
    repo = "test/repo"
    
    # Store should initially not find it
    assert not temp_chroma_store.find_similar_dropped(repo, f1)
    
    # Add evaluations: one dropped
    temp_chroma_store.add_evaluations(repo, [f1], ["drop"])
    
    # Now it should be found as dropped
    assert temp_chroma_store.find_similar_dropped(repo, f1)
    
    # Different repo shouldn't find it
    assert not temp_chroma_store.find_similar_dropped("other/repo", f1)
    
def test_chroma_store_keep_not_auto_dropped(temp_chroma_store):
    f1 = Finding(
        file="src/main.py",
        line=10,
        severity=Severity.HIGH,
        category=FindingCategory.SECURITY,
        message="SQL Injection",
        code_snippet="query = f'SELECT * FROM users WHERE id={id}'",
        rule_id="B608"
    )
    repo = "test/repo"
    
    # Add as 'keep'
    temp_chroma_store.add_evaluations(repo, [f1], ["keep"])
    
    # Should NOT be found as dropped
    assert not temp_chroma_store.find_similar_dropped(repo, f1)

def test_chroma_store_empty_fields_safe(temp_chroma_store):
    f1 = Finding(
        file="src/main.py",
        line=10,
        severity=Severity.LOW,
        category=FindingCategory.STYLE,
        message="Line too long"
        # code_snippet and rule_id are None
    )
    repo = "test/repo"
    
    temp_chroma_store.add_evaluations(repo, [f1], ["drop"])
    assert temp_chroma_store.find_similar_dropped(repo, f1)
