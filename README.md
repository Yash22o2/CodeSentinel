# 🛡️ CodeSentinel

**CodeSentinel is an autonomous AI code review bot that plugs directly into GitHub.** Open a Pull Request, and within seconds, multiple AI agents have already read the diff, hunted for bugs, security holes, and style problems, and posted a structured review comment — all without you lifting a finger.

No dashboards to check, no commands to run. The bot does the work.

---

## How It Works (The 30-Second Version)

1. A developer opens a Pull Request on GitHub.
2. GitHub fires a webhook to CodeSentinel's server (takes less than 1ms to acknowledge).
3. A background worker picks up the job and sends the code diff to **four specialist AI agents running in parallel**: Security, Style, Logic, and Test Coverage.
4. A Critic AI filters out false alarms by comparing new findings against a memory of past false positives (stored in a local vector database).
5. The bot posts a clean, formatted review comment directly on the Pull Request with a severity breakdown table.
6. All review metrics (cost, latency, findings count) are saved to a **Visual Analytics Dashboard** at `/dashboard`.

---

## Demo

> 🎬 *GIF recording coming soon — will show a PR being opened and the bot commenting in real-time.*

**Live Demo:** Open the dashboard at `/dashboard` and use the interactive "Try It" panel to run the agents on a custom code snippet without needing a real PR.

---

## Architecture

```
GitHub PR Opened
      │
      ▼  POST /webhook/github  (returns 202 in <1ms)
 ┌──────────────┐
 │  FastAPI     │  ← HMAC-SHA256 signature verified here
 └──────┬───────┘
        │  enqueue job
        ▼
 ┌──────────────┐
 │  Redis Queue │  ← Job sits here safely even if server restarts
 └──────┬───────┘
        │  poll
        ▼
 ┌──────────────┐
 │  RQ Worker   │  ← Separate process, no timeout risk
 └──────┬───────┘
        │  invoke graph
        ▼
 ┌─────────────────────────────────────────────────┐
 │            LangGraph StateGraph                 │
 │                                                 │
 │  planner ──→ [parallel fan-out]                 │
 │                ├── security_agent               │
 │                ├── style_agent                  │
 │                ├── logic_agent                  │
 │                └── test_agent                   │
 │                          │                      │
 │              [fan-in] critic ◄── ChromaDB Memory│
 │                          │                      │
 │                     aggregator                  │
 └──────────────────────┬──────────────────────────┘
                        │
                        ▼
              GitHub PR Review Comment
              + Dashboard DB update
```

---

## Key Engineering Decisions

### 1. Webhook Returns Immediately
Running AI agents inside a webhook request thread would cause GitHub to retry the delivery after 10 seconds (and eventually disable the webhook). CodeSentinel returns `HTTP 202 Accepted` in under 1ms. The heavy AI work happens in a completely separate background worker process.

### 2. Parallel Agent Fan-Out
Instead of sending the entire diff to a single "do everything" prompt (which gives vague, unfocused output), CodeSentinel splits the review across specialists. All four agents run at the same time, cutting total latency significantly.

### 3. False-Positive Memory (ChromaDB)
Every finding the Critic decides to drop is stored as a vector embedding. On the next PR review in the same repo, if a new finding is semantically very similar (cosine distance ≤ 0.05) to a past false positive, it is dropped automatically before the LLM even sees it. This gets smarter over time.

### 4. Fault Tolerance at Every Layer
- **Circuit Breakers:** If any agent fails 3 times in a row, it is bypassed for 60 seconds so one broken agent cannot block the entire review.
- **Per-Node Timeouts:** If an agent stalls, it yields empty findings and the pipeline continues.
- **Graph Deadline:** A total 120-second wall-clock deadline is shared across all nodes so the whole review can never hang indefinitely.
- **Redis Fallback:** If Redis is unavailable (e.g., during local dev without Docker), the webhook falls back to FastAPI's native `BackgroundTasks` automatically — so the system works with or without Redis.

---

## Tech Stack

| Layer | Technology |
| :--- | :--- |
| Web Framework | Python 3.11, FastAPI, Pydantic v2 |
| AI Orchestration | LangGraph (`StateGraph`), LangChain |
| LLM Provider | Groq (`llama-3.3-70b-versatile`) |
| Task Queue | Redis, RQ (Redis Queue) |
| Vector Memory | ChromaDB (Local Persistent + ONNX Runtime embeddings) |
| GitHub Integration | HTTPX, PyGithub, HMAC-SHA256 webhook verification |
| Analytics DB | SQLite via SQLModel |
| Frontend Dashboard | FastAPI + Jinja2, Vanilla CSS (Glassmorphism / Dark Bento UI) |
| Testing | Pytest, Pytest-Asyncio, Tenacity |

---

## Project Structure

```
CodeSentinel/
├── app/
│   ├── api/
│   │   ├── webhook.py       # Receives GitHub events, enqueues jobs
│   │   ├── dashboard.py     # Serves the analytics UI + metrics API
│   │   └── health.py        # Health check endpoint
│   ├── graph/
│   │   ├── builder.py       # Assembles the LangGraph StateGraph
│   │   ├── state.py         # Typed state shared across all nodes
│   │   └── nodes/
│   │       ├── base_agent.py    # Shared LLM + retry + circuit breaker logic
│   │       ├── planner.py       # Decides which agents to run per PR
│   │       ├── security_agent.py
│   │       ├── style_agent.py
│   │       ├── logic_agent.py
│   │       ├── test_agent.py
│   │       ├── critic.py        # Deduplicates + filters findings via ChromaDB
│   │       └── aggregator.py    # Formats + posts the GitHub review comment
│   ├── github/
│   │   ├── client.py            # GitHub API wrapper (fetch diffs, post comments)
│   │   ├── diff_parser.py       # Parses raw .diff text into structured objects
│   │   ├── comment_poster.py    # Formats and posts the Markdown review
│   │   └── webhook_validator.py # HMAC-SHA256 signature verification
│   ├── reliability/
│   │   ├── circuit_breaker.py   # Per-agent CLOSED/OPEN/HALF-OPEN state machine
│   │   └── job_store.py         # In-memory job status tracker
│   ├── memory/
│   │   └── chroma_store.py      # ChromaDB vector store for false-positive memory
│   ├── db/
│   │   ├── models.py            # SQLModel ReviewMetric table schema
│   │   └── session.py           # SQLite engine + session factory
│   ├── worker/
│   │   ├── processor.py         # Runs the full review pipeline end-to-end
│   │   └── rq_worker.py         # RQ entry point (sync wrapper for async code)
│   ├── templates/
│   │   └── dashboard.html       # Jinja2 template for the analytics dashboard
│   ├── static/
│   │   ├── css/style.css        # Dark glassmorphism UI styles
│   │   └── js/dashboard.js      # Chart.js charts + live SSE streaming
│   ├── config.py                # Pydantic settings (reads from .env)
│   ├── schemas.py               # Shared Pydantic models (Finding, PRReviewJob, etc.)
│   └── main.py                  # FastAPI app factory + router registration
├── tests/                       # Pytest test suite (8 test files, ~60 tests)
├── scripts/                     # Utility scripts (e.g., DB seeder)
├── docker-compose.yml           # Redis service for local development
├── Dockerfile                   # Production container definition
├── pyproject.toml               # Dependencies (uv / pip)
└── .env.example                 # Environment variable template
```

---

## Quick Start (Local Development)

### Prerequisites
- Python 3.11+
- Docker Desktop (for Redis)
- A Groq API key (free at [console.groq.com](https://console.groq.com))
- A GitHub repo with a configured webhook pointing to your ngrok URL

### 1. Clone and Install
```bash
git clone https://github.com/Yash22o2/CodeSentinel.git
cd CodeSentinel
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -e ".[dev]"
```

### 2. Configure Environment
```bash
cp .env.example .env
# Fill in your GROQ_API_KEY, GITHUB_TOKEN, and GITHUB_WEBHOOK_SECRET
```

### 3. Start Redis
```bash
docker-compose up -d redis
```

### 4. Run the Server
```bash
uvicorn app.main:app --reload
```

### 5. Run the Background Worker (new terminal)
```bash
# Windows (SimpleWorker avoids the os.fork() issue):
.venv\Scripts\rq worker codesentinel --worker-class rq.SimpleWorker
```

### 6. Expose Locally via Ngrok
```bash
ngrok http 8000
# Copy the https URL and set it as your GitHub webhook URL: https://xxx.ngrok.io/webhook/github
```

### 7. Open the Dashboard
Navigate to `http://localhost:8000/dashboard`

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Environment Variables

| Variable | Description |
| :--- | :--- |
| `GROQ_API_KEY` | Your Groq API key |
| `GITHUB_TOKEN` | GitHub Personal Access Token (to post review comments) |
| `GITHUB_WEBHOOK_SECRET` | Secret set in your GitHub webhook settings |
| `REDIS_URL` | Redis connection URL (default: `redis://localhost:6379`) |
| `ENV` | `development` or `production` |

---

## License

MIT
