# NewsAgent

A production-grade, microservices-based AI news processing system. NewsAgent automatically discovers, extracts, summarizes, validates, categorizes, and translates news articles using a robust **LangGraph** workflow orchestrated via **Redis** and **MongoDB**.

## 🎯 System Overview

NewsAgent operates as a distributed system designed for resilience and observability.

* **Discovery**: A dedicated **Scheduler Service** crawls target news sources (handling "infinite scroll" and ad filtering) and submits new links to the pipeline.
* **Orchestration**: A **Main API** acts as the gateway/producer, pushing jobs to a Redis Queue.
* **Processing**: Background **Workers** consume jobs and execute a sophisticated AI workflow (LangGraph) that includes summary validation loops (Actor-Critic), link relevance checking, and hallucination detection.
* **Delivery**: Final processed articles are sent via **Webhooks** to downstream services or archives.

### Key Features
* **LangGraph Workflow**: Cyclic graphs for self-correcting summarization and validation.
* **Headless Browsing**: Uses **Browserless (Chromium)** and **Playwright** for high-fidelity content extraction.
* **Resilience**: Implements Dead Letter Queues (DLQ) and automatic retries for failed jobs.
* **Governance**: Distributed rate limiting and `robots.txt` compliance to respect source policies.
* **Observability**: Integrated with **Opik** for tracing AI logic and **SMTP** for critical error alerting.
* **Search & SEO**: Auto-generates SEO metadata and finds corroborating sources via **Tavily**.

---

## 🏗️ Architecture

The system is composed of six Dockerized services:

| Service | Port | Description |
| :--- | :--- | :--- |
| **API** (`api`) | `8000` | The entry point. Accepts job submissions, manages configuration (Prompts/Categories), and provides queue metrics. |
| **Scheduler** (`scheduler`) | `8001` | The "Pulse". Runs background cron jobs to crawl listing pages and submits unique URLs to the API. |
| **Worker** (`worker`) | *N/A* | The consumer. Pulls jobs from Redis, executes the LangGraph workflow, and posts results. |
| **Redis** (`redis`) | `6379` | Message broker for job queues (`newsagent_jobs`) and distributed rate-limiting locks. |
| **MongoDB** (`mongo`) | `27017` | Persistent storage for prompts, source configs, processed article archives, and email recipients. |
| **Browserless** (`browserless`) | `3000` | Headless Chromium instance used by workers and the scheduler for rendering JavaScript. |

---

## 📋 Prerequisites

* **Docker** & **Docker Compose**
* **API Keys**:
    * **OpenAI API Key** (LLM operations)
    * **Tavily API Key** (Web search)
    * **Opik** (Optional - for observability)
    * **SMTP Credentials** (For error alerting)

---

## ⚙️ Configuration

1.  **Clone the repository**:
    ```bash
    git clone <your-repo-url>
    cd NewsAgent-workingState
    ```

2.  **Environment Setup**:
    Copy `sample.env` to `.env` and fill in your credentials.
    ```bash
    cp sample.env .env
    ```

    **Critical Variables:**
    * `OPENAI_API_KEY`: Required for all AI nodes.
    * `TAVILY_API_KEY`: Required for the "Find Other Sources" node.
    * `NEWSAGENT_API_KEY`: A secure key you generate to protect your internal API endpoints.
    * `BROWSERLESS_TOKEN`: Secure token for the browserless service.
    * `WEBHOOK_SECRET`: Shared secret for verifying webhook payloads.

---

## 🚀 Installation & Execution

### Method 1: Docker Compose (Recommended)

This brings up the entire stack, including the database and browser services.

1.  **Build and Start**:
    ```bash
    docker-compose up --build -d
    ```

2.  **Initialize Database**:
    The `db-init` container runs automatically on the first start to seed Prompts, Categories, and default configurations. You can check its logs to ensure success:
    ```bash
    docker-compose logs db-init
    ```

3.  **Verify Services**:
    * **Main API**: Visit `http://localhost:8000/docs`
    * **Scheduler API**: Visit `http://localhost:8001/docs`
    * **Browserless Debugger**: Visit `http://localhost:3000`

### Method 2: Local Development (Python)

If you need to run the Python code locally while keeping infrastructure (Redis/Mongo) in Docker:

1.  **Start Infrastructure**:
    ```bash
    docker-compose up -d redis mongo browserless
    ```
2.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Run Services** (in separate terminals):
    * **API**: `uvicorn src.main:api --port 8000 --reload`
    * **Scheduler**: `uvicorn src.scheduler.main:app --port 8001 --reload`
    * **Worker**: `python src/worker.py`

---

## 📖 Usage Guide

### 1. Managing News Sources (Scheduler)
Configure which sites to crawl via the Scheduler API (Port 8001).

**Add a Source:**
```bash
curl -X POST "http://localhost:8001/sources" \
  -H "X-API-Key: <YOUR_NEWSAGENT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CNN Business",
    "listing_url": "https://edition.cnn.com/business",
    "url_pattern": "/business/",
    "fetch_interval_minutes": 60,
    "delay_seconds": 5,
    "is_active": true
  }'
```

The scheduler will now check this URL every 60 minutes, filter links matching /business/, and submit them to the processing pipeline.

### 2. Manual Job Submission
You can manually trigger processing for a specific URL via the Main API (Port 8000).

```bash
curl -X POST "http://localhost:8000/submit-job" \
  -H "X-API-Key: <YOUR_NEWSAGENT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "https://example.com/some-news-article"
  }'
```

### 3. Monitoring & Debugging
Queue Status: Check pending jobs and Dead Letter Queue counts.  
`GET http://localhost:8000/queue/status`

Visualizing the Graph: Download the current LangGraph workflow diagram.  
`GET http://localhost:8000/debug/draw-graph`

Job Status: Check the realtime status of a specific job ID.  
`GET http://localhost:8000/jobs/{job_id}`

### 4. Handling Failures (Dead Letter Queue)
If a job crashes (e.g., parsing error), it moves to the DLQ. You can inspect and requeue it via the API.

List Failed Jobs: `GET /queue/dlq/items`

Retry a Job: `POST /queue/dlq/requeue/{job_id}`

## 📂 Project Structure

```text
src/
├── configs/           # Pydantic settings & .env loading
├── db/                # MongoDB models & enums
├── graph/             # LangGraph definitions
│   ├── nodes/         # Individual workflow steps (Summary, SEO, etc.)
│   └── graph.py       # Main graph topology builder
├── models/            # Pydantic data models (State, Articles)
├── prompts/           # Text prompts for LLMs
├── scheduler/         # Scheduler service logic & link discovery
├── utils/             # Helpers (Browser, Email, Security)
├── main.py            # Main API entry point
└── worker.py          # Background worker entry point
```

## 🐛 Troubleshooting

* **"Browser closed unexpectedly"**: Increase Docker memory allocation. Browserless requires significant RAM.
* **Redis Connection Error**: Ensure REDIS_URL in .env matches the docker service name (e.g., `redis://redis:6379/0`) when running inside Docker, or localhost when running locally.
* **MongoTimeoutError**: On first boot, the Python services might start before Mongo is ready. The services are configured to fail fast; simply run `docker-compose restart api scheduler worker`.