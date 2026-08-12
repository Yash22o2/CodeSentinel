from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import ReviewMetric
from app.db.session import get_session

router = APIRouter(prefix="/metrics", tags=["metrics"])

api_key_header = APIKeyHeader(name="X-API-Key")

def get_api_key(api_key: str = Security(api_key_header)) -> str:
    settings = get_settings()
    if api_key != settings.metrics_api_key:
        raise HTTPException(status_code=403, detail="Could not validate credentials")
    return api_key

@router.get("/")
def get_metrics(
    session: Session = Depends(get_session),
    api_key: str = Depends(get_api_key)
) -> Dict[str, Any]:
    metrics = session.exec(select(ReviewMetric)).all()
    
    total_prs = len(metrics)
    total_tokens = sum(m.total_tokens for m in metrics)
    total_cost = sum(m.estimated_cost_usd for m in metrics)
    total_latency = sum(m.total_latency_ms for m in metrics)
    
    security_findings = sum(m.security_findings_kept for m in metrics)
    style_findings = sum(m.style_findings_kept for m in metrics)
    logic_findings = sum(m.logic_findings_kept for m in metrics)
    test_findings = sum(m.test_findings_kept for m in metrics)
    
    avg_latency = (total_latency / total_prs) if total_prs > 0 else 0
    
    return {
        "total_prs_reviewed": total_prs,
        "total_tokens_used": total_tokens,
        "estimated_cost_usd": total_cost,
        "average_latency_ms": avg_latency,
        "findings_kept": {
            "security": security_findings,
            "style": style_findings,
            "logic": logic_findings,
            "test": test_findings,
        }
    }
