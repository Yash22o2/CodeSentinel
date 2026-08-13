import json
import asyncio
from typing import Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import Session, select
from datetime import datetime, timedelta

from app.db.session import get_session
from app.db.models import ReviewMetric

router = APIRouter(tags=["dashboard"])

templates = Jinja2Templates(directory="app/templates")

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html")

@router.get("/api/v1/metrics/history")
async def get_metrics_history(session: Session = Depends(get_session)):
    metrics = session.exec(select(ReviewMetric)).all()
    
    total_prs = len(metrics)
    total_tokens = sum(m.total_tokens for m in metrics)
    total_cost = sum(m.estimated_cost_usd for m in metrics)
    total_latency = sum(m.total_latency_ms for m in metrics)
    
    security = sum(m.security_findings_kept for m in metrics)
    style = sum(m.style_findings_kept for m in metrics)
    logic = sum(m.logic_findings_kept for m in metrics)
    test = sum(m.test_findings_kept for m in metrics)
    
    avg_latency = (total_latency // total_prs) if total_prs > 0 else 0
    
    # Aggregate history for the last 30 days
    # (Group by date)
    history_dict = {}
    for m in metrics:
        date_str = m.created_at.strftime("%Y-%m-%d") if m.created_at else datetime.utcnow().strftime("%Y-%m-%d")
        if date_str not in history_dict:
            history_dict[date_str] = {"date": date_str, "prs": 0}
        history_dict[date_str]["prs"] += 1
        
    history_list = sorted(list(history_dict.values()), key=lambda x: x["date"])
    
    return {
        "totals": {
            "prs": total_prs,
            "tokens": total_tokens,
            "cost": total_cost,
            "latency": avg_latency
        },
        "history": history_list,
        "findings": {
            "security": security,
            "style": style,
            "logic": logic,
            "test": test
        }
    }

class DemoRequest(BaseModel):
    code: str

# In-memory store for demo task statuses (for demo purposes)
# In production, use Redis.
DEMO_TASKS = {}

@router.post("/api/v1/demo/start")
async def start_demo(req: DemoRequest):
    import uuid
    task_id = str(uuid.uuid4())
    DEMO_TASKS[task_id] = {"code": req.code, "status": "started"}
    return {"task_id": task_id}

@router.get("/api/v1/demo/stream")
async def stream_demo(task_id: str):
    async def event_generator():
        if task_id not in DEMO_TASKS:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Invalid task ID'})}\n\n"
            return
            
        code = DEMO_TASKS[task_id]["code"]
        
        # Simulate agent execution steps
        steps = [
            {"type": "step", "agent": "Planner", "message": "Analyzing code snippet and planning review strategy..."},
            {"type": "step", "agent": "SecurityAgent", "message": "Scanning for secrets and vulnerabilities..."},
            {"type": "finding", "message": "Found hardcoded secret 'api_key' on line 5."},
            {"type": "step", "agent": "StyleAgent", "message": "Checking adherence to PEP 8 style guide..."},
            {"type": "step", "agent": "LogicAgent", "message": "Evaluating algorithmic efficiency..."},
            {"type": "finding", "message": "Inefficient list comprehension and loop identified."},
            {"type": "step", "agent": "CriticAgent", "message": "Synthesizing findings and removing false positives..."},
            {"type": "step", "agent": "Reporter", "message": "Formatting final Markdown report."},
            {"type": "complete"}
        ]
        
        for step in steps:
            # Simulate processing time
            await asyncio.sleep(1.2)
            yield f"data: {json.dumps(step)}\n\n"
            if step["type"] == "complete":
                break
                
    return StreamingResponse(event_generator(), media_type="text/event-stream")
