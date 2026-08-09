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

## Quick Start

```bash
# 1. Copy and fill in your secrets
cp .env.example .env

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Start services (Redis + Postgres)
docker-compose up redis postgres -d

# 4. Run the API server
uvicorn app.main:app --reload --port 8000

# 5. Expose locally for GitHub webhooks
ngrok http 8000
```

## Setup

See the [GitHub Integration Setup Guide](docs/github_setup.md) for full instructions on:
- Creating a GitHub PAT with correct permissions
- Generating a webhook secret
- Registering the webhook in your repo
- End-to-end smoke testing

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
