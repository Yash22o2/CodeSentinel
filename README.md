# CodeSentinel 🛡️

> **Multi-agent, self-critiquing automated code review system** — built to demonstrate production-grade LLM orchestration with LangGraph.

CodeSentinel watches a GitHub repository for new Pull Requests via webhook, routes the PR diff to **parallel specialist AI agents** (Security, Style, Logic, Test Coverage), runs a **self-critique loop** to suppress false positives, and posts a production-quality inline review comment directly on the PR.

---

## Why This Project

Most AI code-review demos are "send diff to GPT and print the reply". This is not that.

CodeSentinel is designed to show:

- **Multi-agent orchestration** — four specialist agents run in parallel via LangGraph's conditional fan-out. Each has a narrow, focused system prompt.
- **Self-reflection / Critic loop** — a second independent LLM pass evaluates each finding. Low-confidence findings get challenged; high-confidence ones pass through immediately.
- **Verified correctness** — 69 automated tests covering routing logic, dedup, critic filtering, aggregator, and full end-to-end graph invocation (all with mocked Groq so tests are deterministic and free).
- **Real bugs caught** — testable against any GitHub repo with a registered webhook. You can point it at your own test PRs and see inline comments.

---

## Architecture

```
GitHub Webhook → FastAPI (HMAC validated) → BackgroundTask
                                                │
                                    LangGraph StateGraph
                                                │
                          ┌─────────────────────┤ Planner (heuristic routing, 0 LLM calls)
                          │                     │
              ┌───────────┼────────┬────────────┐
              ▼           ▼        ▼            ▼
          Security      Style    Test-Cov    Logic/Bug
           Agent        Agent     Agent       Agent
              │     (parallel asyncio fan-out)  │
              └───────────────┬─────────────────┘
                              ▼
                    Critic Node (LLM second-pass)
                      - hard-drop threshold (<0.35)
                      - deduplication (file+line+message)
                      - LLM filter for borderline findings
                      - fail-open if critic crashes
                              ▼
                    Aggregator Node (ReviewMetadata)
                              ▼
                    GitHub PR Inline Comment
```

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| **Orchestration** | LangGraph `StateGraph` with conditional parallel fan-out |
| **LLM** | Groq API — `llama-3.3-70b-versatile` (fast, cheap inference) |
| **API Server** | FastAPI + Pydantic v2 |
| **GitHub** | PyGithub + raw `httpx` diff fetching, HMAC-SHA256 webhook validation |
| **Retries** | Tenacity (3× exponential backoff on every LLM call) |
| **Logging** | structlog (structured JSON logs, threaded through all nodes) |
| **Testing** | pytest + pytest-asyncio (69 tests, 0 external calls) |
| **Queue** | Redis + RQ (Phase 3) |
| **Memory** | Chroma vector store (Phase 4 — past findings for FP suppression) |
| **Deploy** | Docker Compose + Railway/Render (Phase 6) |

---

## Current Status

| Phase | Status | Details |
|-------|--------|---------|
| 1 — Foundation | ✅ Done | FastAPI, HMAC webhook, diff parser, GitHub client, PR comment poster |
| 2 — LangGraph Graph | ✅ Done | Planner, 4 specialist agents, Critic, Aggregator, full StateGraph |
| 2 — Phase 2 Tests | ✅ Done | 58 tests: planner routing, critic dedup/filter/sort, graph integration |
| 3 — Reliability | 🔲 Next | Circuit breakers, per-node timeouts, total graph budget (120 s) |
| 4 — Critic Memory | 🔲 Planned | Chroma vector store, embed past findings, suppress repeated FPs |
| 5 — Evaluation | 🔲 Planned | Labeled PR dataset, precision/recall/F1 pipeline |
| 6 — Docker Deploy | 🔲 Planned | Dockerfile + docker-compose, deploy to Railway |

---

## Production Bugs Caught by Tests

The test suite was written against the live code and caught **two real bugs** before any production traffic:

1. **Schema field mismatch** — `critic.py` and `base_agent.py` referenced `Finding.filename / .title / .agent` but the canonical Pydantic schema uses `.file / .message / .tool`. Found and fixed.

2. **Concurrent fan-out crash** — `node_timings` in `GraphState` was a plain `dict`. When 3 agents ran in parallel, each tried to write it simultaneously → LangGraph raised `InvalidUpdateError`. Fixed by annotating with a `_merge_dicts` reducer: `Annotated[dict[str, float], _merge_dicts]`.

---

## Project Structure

```
app/
├── api/           # FastAPI routes (webhook, health)
├── github/        # GitHub client, diff parser, comment poster
├── graph/
│   ├── state.py   # GraphState TypedDict (fan-out reducers)
│   ├── builder.py # StateGraph assembly + conditional routing
│   └── nodes/     # planner, security, style, logic, test, critic, aggregator
├── schemas/       # Pydantic models (Finding, AgentPlan, ReviewMetadata, …)
└── worker/        # Background job processor

tests/
├── test_diff_parser.py  # Diff parsing
├── test_webhook.py      # HMAC validation, FastAPI routing
├── test_planner.py      # Routing heuristics (24 tests)
├── test_critic.py       # Critic node (17 tests)
└── test_graph.py        # Full graph integration (17 tests, mocked Groq)
```

---

## Quick Start

```bash
# 1. Clone & set up
git clone https://github.com/Yash22o2/CodeSentinel
cd CodeSentinel
python -m venv .venv && .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Set secrets
cp .env.example .env   # fill in GROQ_API_KEY and GITHUB_TOKEN

# 3. Verify everything works
python -m pytest tests/ -v                                  # 69 tests
python -c "from app.graph.builder import review_graph; print(type(review_graph).__name__)"

# 4. Run the server
uvicorn app.main:app --reload --port 8000
```

---

## Hiring / Resume Context

- **Hard ground truth**: unlike RAG chatbots, this produces verifiable outputs — inline GitHub PR comments that reviewers can accept, reject, or argue with.
- **Measurable accuracy**: Phase 5 will compute precision/recall/F1 on a labeled set of real PRs with known bugs.
- **Real engineering**: the two bugs caught by tests (schema mismatch, concurrent dict write) were real defects, not planted. This is what a proper test-first workflow looks like.
