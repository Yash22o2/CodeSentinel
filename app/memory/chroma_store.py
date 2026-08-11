import hashlib
import structlog
import chromadb
from chromadb.utils import embedding_functions

from app.config import get_settings
from app.schemas import Finding

log = structlog.get_logger(__name__)

class ChromaStore:
    def __init__(self):
        self.settings = get_settings()
        self.client = chromadb.PersistentClient(path=self.settings.chroma_persist_dir)
        self.embedding_fn = embedding_functions.DefaultEmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name="critic_memory",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

    def _format_embedding_text(self, finding: Finding) -> str:
        code_str = finding.code_snippet or ""
        rule_str = finding.rule_id or ""
        msg_str = finding.message or ""
        return f"Rule: {rule_str}\nCode: {code_str}\nIssue: {msg_str}"

    def add_evaluations(self, repo: str, findings: list[Finding], decisions: list[str]):
        """Store findings and their keep/drop decisions in Chroma."""
        if not findings:
            return

        documents = []
        metadatas = []
        ids = []

        for finding, decision in zip(findings, decisions):
            doc_text = self._format_embedding_text(finding)
            id_str = f"{repo}:{finding.file}:{finding.line}:{doc_text}"
            doc_id = hashlib.sha256(id_str.encode()).hexdigest()

            documents.append(doc_text)
            metadatas.append({
                "repo": repo,
                "decision": decision,
                "severity": finding.severity.value,
                "file": finding.file,
                "line": finding.line
            })
            ids.append(doc_id)

        try:
            # upsert is used to overwrite if the exact same finding is evaluated again
            self.collection.upsert(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            log.info("chroma_store.add", count=len(findings), repo=repo)
        except Exception as e:
            log.error("chroma_store.add_failed", error=str(e), repo=repo)

    def find_similar_dropped(self, repo: str, finding: Finding) -> bool:
        """Return True if a highly similar finding in this repo was previously dropped."""
        doc_text = self._format_embedding_text(finding)
        try:
            results = self.collection.query(
                query_texts=[doc_text],
                n_results=1,
                where={
                    "$and": [
                        {"repo": repo},
                        {"decision": "drop"}
                    ]
                }
            )

            if results["distances"] and len(results["distances"][0]) > 0:
                min_distance = results["distances"][0][0]
                max_allowed_distance = 1.0 - self.settings.chroma_similarity_threshold
                if min_distance <= max_allowed_distance:
                    log.info("chroma_store.auto_drop", repo=repo, file=finding.file, distance=min_distance)
                    return True
            return False
        except Exception as e:
            log.error("chroma_store.query_failed", error=str(e), repo=repo)
            return False

_store = None

def get_chroma_store() -> ChromaStore:
    global _store
    if _store is None:
        _store = ChromaStore()
    return _store
