from typing import Optional
from sqlmodel import SQLModel, Field

class ReviewMetric(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    pr_id: str
    total_latency_ms: int
    total_tokens: int
    estimated_cost_usd: float
    
    # Track effectiveness per agent
    security_findings_kept: int = 0
    style_findings_kept: int = 0
    logic_findings_kept: int = 0
    test_findings_kept: int = 0
