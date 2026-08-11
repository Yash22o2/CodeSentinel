# CodeSentinel Phases Summary

This document explains the architecture introduced in Phase 3 of CodeSentinel. Specifically, it breaks down the roles of the various components used to make the application reliable, resilient, and scalable.

---

## 1. FastAPI: The Web Server / "The Front Door"

**What it is:**
FastAPI is a modern, fast (high-performance) web framework for building APIs with Python. In CodeSentinel, it serves as the HTTP endpoint that receives incoming webhooks from GitHub.

**Why it is used here:**
GitHub needs an HTTP endpoint to send a `POST` request to whenever a Pull Request is opened or updated. FastAPI is exceptional at handling these HTTP requests quickly.

**The catch (and why we need queues):**
GitHub expects the webhook endpoint to respond with a success status (like `202 Accepted`) very quickly (usually within 10 seconds). If your server takes too long to respond, GitHub assumes the delivery failed and will retry or eventually disable the webhook. 
CodeSentinel's AI review process takes 30 to 120 seconds. Therefore, FastAPI **cannot** do the AI processing itself while GitHub is waiting on the line. It must accept the request, save the job somewhere, and hang up the phone (return `202`) instantly.

---

## 2. Redis: The Message Broker / "The Corkboard"

**What it is:**
Redis is an open-source, in-memory data structure store. It is extremely fast because it keeps all its data in RAM rather than on a slow hard drive.

**Why it is used here:**
Think of Redis as a shared "corkboard" in an office kitchen. 
When FastAPI receives a webhook from GitHub, it needs a place to drop off the details of the job (the PR number, repo name, etc.) so that it can immediately go back to listening for new webhooks. 
FastAPI writes a message (a "sticky note") to a queue in Redis and says, "Someone needs to review PR #42."

Because Redis runs as a completely separate process (often in its own Docker container), it provides a crucial layer of safety:
- **Persistence:** If the FastAPI server crashes or restarts, the jobs waiting in the Redis queue are not lost.
- **Decoupling:** It separates the "receivers" of work (FastAPI) from the "doers" of work (the background workers).

---

## 3. RQ (Redis Queue): The Background Worker / "The Receptionist & Staff"

**What it is:**
RQ is a simple Python library for queueing jobs and processing them in the background with workers. It uses Redis as its storage engine.

**Why it is used here:**
If Redis is the corkboard, RQ is the system of workers looking at that corkboard.
Instead of the FastAPI web server running the heavy LangGraph AI review, we spin up entirely separate Python processes called **RQ Workers** (e.g., running `rq worker codesentinel`). 
These workers sit completely outside the FastAPI web server. Their only job is to watch the Redis queue. When a new job appears on the queue, an RQ worker picks it up, runs the 30-120 second AI graph, posts the comment to GitHub, and then marks the job as finished.

**Benefits:**
- **Scalability:** If you start getting 100 PRs a minute, your single FastAPI server won't crash. You can just spin up 10 or 20 RQ worker processes on different machines to process the queue in parallel.
- **Stability:** If the AI process runs out of memory or crashes, it only kills that one isolated RQ worker process. The FastAPI web server remains completely unaffected and continues accepting new webhooks.

---

## 4. BackgroundTask Fallback: The Developer Convenience

**What it is:**
FastAPI has a built-in feature called `BackgroundTasks`. It allows you to tell FastAPI, "After you return the HTTP response to the user, run this function in the background *inside* the same web server process."

**Why we implemented a fallback:**
While RQ and Redis are the robust, production-ready way to handle background jobs, they require running extra infrastructure (a Redis server and a separate RQ worker process). 
During local development, you often just want to run `uvicorn app.main:app --reload` and have everything work without needing Docker or Redis installed.

In Phase 3, we wrote the webhook logic like this:
1. Try to connect to Redis.
2. If Redis is available, put the job in the RQ queue (Production mode).
3. **If Redis is NOT available, silently fall back to using FastAPI's built-in `BackgroundTasks` (Dev mode).**

This means you get the best of both worlds: robust queueing in production, and zero-configuration ease-of-use for local development.

---

## 5. Circuit Breaker: The "Fail Fast" Mechanism

**What it is:**
A Circuit Breaker is a design pattern used in modern software development to detect failures and encapsulate the logic of preventing a failure from constantly recurring, during maintenance, temporary external system failure or unexpected system difficulties.

**Why it is used here:**
CodeSentinel relies heavily on the Groq API for its LLM calls. External APIs can go down, become extremely slow, or rate-limit you.
If Groq goes down, and we have a queue of 50 PRs, our agents will pick up a PR, try to call Groq, wait for a timeout, fail, pick up the next PR, wait for a timeout, fail... wasting massive amounts of time and clogging the system.

The Circuit Breaker pattern prevents this:
1. **Closed (Normal):** Requests go through to Groq normally.
2. **Open (Failing):** If an agent fails to talk to Groq 5 times in a row, the circuit "opens." For the next 60 seconds, any attempt to use that agent will immediately return an error *without even trying to contact Groq*. This saves time and stops hammering a broken API.
3. **Half-Open (Testing):** After 60 seconds, the circuit lets exactly *one* request through. If it succeeds, the API is back online, and the circuit Closes. If it fails, the circuit Opens again for another 60 seconds.

This ensures that CodeSentinel fails fast and gracefully when its dependencies are unhealthy.

---

## 6. Graph and Node Timeouts

**What it is:**
Strict time limits placed on how long any single operation (or the entire operation) can take.

**Why it is used here:**
Even with circuit breakers, AI models can sometimes just "hang" or take an unusually long time to generate a response. 
- **Per-Node Timeout (`asyncio.wait_for`):** We give each specialist agent (e.g., Security Agent) a strict budget (e.g., 45 seconds). If it takes longer, we cancel that specific agent's work. The rest of the graph continues. We'd rather have a review missing the Security Agent's input than no review at all.
- **Total Graph Timeout:** We give the entire LangGraph process a hard limit of 120 seconds. If the deadline is reached, we cancel everything and post a "Review Timed Out" comment on GitHub, letting the developer know we tried but failed, rather than leaving them waiting forever.

---

## CodeSentinel Phase 4: Critic Memory / Chroma Vector Store

**What it is:**
This phase introduces a memory layer to the CodeSentinel critic node. By remembering past findings and the critic's decisions on them, we can suppress repeated false positives across reviews without incurring additional LLM costs.

**What we did:**
1. **ChromaDB Vector Store:** We integrated ChromaDB using chromadb.PersistentClient to create a local persistent vector database (.chroma_data).
2. **Schema Update:** Added a code_snippet field to the Finding schema.
3. **Memory Integration:** Updated the Critic node to query Chroma for highly similar findings that were previously dropped, and automatically drop them (pre-filter) before sending borderline findings to the LLM. 
4. **LLM Evaluation Storage:** After the LLM evaluates the remaining borderline findings, the decisions are stored back into Chroma so the system "learns" for next time.

**Challenges and Solutions:**
- **The "Similarity Threshold" Trap:** Tuning semantic similarity is difficult. If the threshold is too loose, we would accidentally auto-drop real bugs.
  - *Solution:* We started highly conservative. We configured Chroma to use cosine distance space and enforced a match condition of distance <= 0.05 (meaning >= 0.95 Cosine Similarity). It is better to pay the LLM a few extra cents to re-evaluate a finding than to let a critical security flaw slip through.
- **Context Loss with Naive Embeddings:** If we only embed the finding text (e.g., "Unused variable 'x'"), Chroma would think every unused variable named 'x' is a match, regardless of the file or context.
  - *Solution:* We adopted a composite embedding strategy. We format the embedding string as:
    ``
    Rule: <rule_id>
    Code: <code_snippet>
    Issue: <description>
    ``
    This ensures Chroma only auto-drops if the exact type of issue is happening in a highly similar block of code.
- **Repository Cross-Contamination:** CodeSentinel might review multiple repositories. A false positive in Repo A might be a valid security flaw in Repo B.
  - *Solution:* We passed the repository name in the Chroma metadata payload and used Chroma's where filter to isolate similarity searches to the same repository.
- **Model Selection:**
  - *Alternative Considered:* jina-embeddings-v2-base-code or 
omic-embed-text, which are specialized for code.
  - *What we chose:* ll-MiniLM-L6-v2. This is Chroma's default built-in model. It is incredibly fast and consumes almost zero RAM. While it isn't specifically trained for code, because we carefully formatted our composite embedding text to combine rule, issue, and snippet, it performs exceptionally well for duplicate detection with zero setup overhead.
