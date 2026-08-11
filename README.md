# 🛡️ CodeSentinel

> An asynchronous, fault-tolerant multi-agent code review system built with LangGraph, FastAPI, Redis/RQ, and ChromaDB.

CodeSentinel connects to GitHub via webhooks to automatically review Pull Requests. Instead of dumping raw diffs into a single prompt, it fans out the code diff to **parallel specialist agents** (Security, Style, Logic, Test Coverage), filters out false alarms using **vector memory**, and posts structured inline review comments back to GitHub.

---

## 📐 System Architecture

```text
                           ┌───────────────────────────┐
                           │    GitHub Webhook Event   │
                           └─────────────┬─────────────┘
                                         │ POST /webhook/github (<1ms)
                                         ▼
                           ┌───────────────────────────┐
                           │      FastAPI Router       │
                           └─────────────┬─────────────┘
                                         │ Enqueue Job
                                         ▼
                           ┌───────────────────────────┐
                           │     Redis Job Queue       │
                           └─────────────┬─────────────┘
                                         │ Poll Task
                                         ▼
                           ┌───────────────────────────┐
                           │     RQ Worker Process     │
                           └─────────────┬─────────────┘
                                         │ Run Graph
                                         ▼
                           ┌───────────────────────────┐
                           │   LangGraph Workflow      │
                           └─────────────┬─────────────┘
                                         │
      ┌──────────────────┬───────────────┴───────────────┬──────────────────┬──────────────────┐
      ▼                  ▼                               ▼                  ▼                  ▼
┌───────────┐      ┌───────────┐                   ┌───────────┐      ┌───────────┐      ┌─────────────────┐
│ Security  │      │   Style   │                   │   Logic   │      │   Test    │      │   Reliability   │
│  Agent    │      │   Agent   │                   │   Agent   │      │   Agent   │      │ (Timeouts/Fuses)│
└─────┬─────┘      └─────┬─────┘                   └─────┬─────┘      └─────┬─────┘      └─────────────────┘
      │                  │                               │                  │
      └──────────────────┴───────────────┬───────────────┴──────────────────┘
                                         │ Raw Findings
                                         ▼
                           ┌───────────────────────────┐
                           │        Critic Node        │ ◄─── Auto-Drop ─── ┌─────────────────┐
                           └─────────────┬─────────────┘ (Distance <= 0.05) │ ChromaDB Memory │
                                         │ Filtered Findings                └─────────────────┘
                                         ▼
                           ┌───────────────────────────┐
                           │      Aggregator Node      │
                           └─────────────┬─────────────┘
                                         │ Post Review Comment
                                         ▼
                           ┌───────────────────────────┐
                           │    GitHub Pull Request    │
                           └───────────────────────────┘
```

## ⚙️ Core Engineering Design

### 1. Decoupled Webhook Ingestion (`FastAPI` + `Redis/RQ`)
Running multi-agent LLM pipelines directly inside a Webhook request thread leads to timeouts and process memory spikes. 
* **Producer:** FastAPI validates the GitHub HMAC signature, enqueues the review job into **Redis**, and returns an immediate `200 OK` (`<1ms` latency).
* **Consumer:** An independent background **RQ Worker** polls Redis and executes the heavy multi-agent workflow without blocking the web server.

### 2. Parallel Multi-Agent Fan-out (`LangGraph`)
Reviewing large diffs in a single prompt dilutes model focus. CodeSentinel routes diff chunks in parallel across four dedicated agents:
* **Security Agent:** Scans for injection vulnerabilities, secret leaks, and insecure data handling.
* **Style Agent:** Checks for formatting, dead code, and naming consistency.
* **Logic Agent:** Inspects control flow, edge cases, and off-by-one errors.
* **Test Agent:** Identifies missing assertion paths or untested functions.

### 3. Reliability & Fault Tolerance Layer
* **Per-Node Timeouts:** Wraps agent execution in `asyncio.wait_for`. If a specialist stalls, it yields empty findings and gracefully degrades without breaking the pipeline.
* **Circuit Breakers:** Uses an in-memory breaker per agent (`CLOSED` $\rightarrow$ `OPEN` $\rightarrow$ `HALF-OPEN`). If an agent fails 3 consecutive times, it is bypassed for 60 seconds.

### 4. False-Positive Suppression Memory (`ChromaDB`)
To prevent the Critic node from re-evaluating recurring false alarms across PRs:
* **Composite Context Embeddings:** Stores dropped findings as `Rule ID + Code Snippet + Issue Message`.
* **Local ONNX Embeddings:** Uses Chroma's lightweight `all-MiniLM-L6-v2` model locally via ONNX Runtime (zero extra API cost).
* **Repository Isolation:** Queries use metadata filters (`where={"repo": repo, "decision": "drop"}`) to isolate memories per project.
* **Vector Match Thresholding:** Uses Cosine Distance ($\le 0.05$, corresponding to $\ge 0.95$ Cosine Similarity) to auto-drop known false positives before hitting the LLM.

---

## 🧰 Tech Stack

| Domain | Technology |
| :--- | :--- |
| **Frameworks** | Python 3.11, FastAPI, Pydantic v2 |
| **Orchestration** | LangGraph (`StateGraph`), LangChain |
| **Task Queue** | Redis, RQ (Redis Queue) |
| **Vector Store** | ChromaDB (Local Persistent Mode + ONNX Runtime) |
| **Inference API** | Groq (`llama-3.3-70b-versatile`) |
| **Integrations** | PyGithub, HTTPX, GitHub Webhooks (HMAC SHA-256) |
| **Testing** | Pytest, Pytest-Asyncio, Tenacity |
