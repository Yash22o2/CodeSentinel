# CodeSentinel

> Multi-agent, self-critiquing code review system powered by LangGraph + Groq.

CodeSentinel watches GitHub PRs, routes diffs to parallel specialist agents (Security, Style, Test Coverage, Logic/Bug), runs a self-critique loop to suppress false positives, and posts production-grade inline review comments.

## Architecture

```
GitHub Webhook → FastAPI → Redis Queue → LangGraph StateGraph
                                              │
                          ┌───────────────────┤ Planner (conditional routing)
                          │                   │
                     ┌────┼──────┬────────────┐
                     ▼    ▼      ▼            ▼
                 Security Style Test-Cov  Logic/Bug
                          │ (parallel)         │
                          └────────┬───────────┘
                                   ▼
                            Critic / Self-Reflection
                                   ▼
                            Aggregator → GitHub PR Comment
```

## Tech Stack

| Layer | Choice |
|-------|--------|
| Orchestration | LangGraph StateGraph |
| LLM | Groq (llama-3.3-70b-versatile) |
| Serving | FastAPI + Pydantic |
| Queue | Redis + RQ |
| Storage | PostgreSQL |
| Memory | Chroma (vector store) |
| Observability | structlog + LangSmith |
| Reliability | tenacity retries, circuit breakers |
| Deploy | Docker + Railway/Render |


## Project Structure

```
app/
├── api/           # FastAPI routes (webhook, health)
├── github/        # GitHub client, diff parser, comment poster
├── graph/         # LangGraph StateGraph + agent nodes
├── schemas/       # Pydantic models (Finding, PRReviewJob, etc.)
├── tools/         # Static analysis wrappers (bandit, ruff, ast)
├── llm/           # Groq client setup
├── worker/        # Background job processor
└── db/            # SQLAlchemy models + session
```
