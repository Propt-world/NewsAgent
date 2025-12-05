
# NewsAgent
An AI-powered news article processing system built with LangGraph that automatically discovers, extracts, summarizes, categorizes, and translates news articles. The system uses a scalable, microservices-based architecture orchestrated with Docker Compose, featuring Redis for job queuing and MongoDB for persistence.

## 🎯 Overview
NewsAgent is a production-ready system designed to process news articles through a sophisticated AI workflow. It extracts content from URLs, generates validated summaries, categorizes articles (with a focus on real estate news), generates SEO metadata, translates content to Arabic, and delivers results via webhook notifications.

### Key Features
-   **Automated Link Discovery**: A dedicated Scheduler service crawls configured news sources at defined intervals to find new articles using intelligent ad/noise filtering.
-   **Intelligent Deduplication**: Prevents processing the same article twice using a robust URL tracking system stored in MongoDB.
-   **Intelligent Content Extraction**: Uses `newspaper4k` and `requests-html` to extract clean article content, even from JavaScript-rendered pages.
-   **AI-Powered Summarization**: Generates concise summaries with a validation loop that ensures quality and accuracy.
-   **Smart Categorization**: Automatically categorizes articles using a comprehensive knowledge base (specialized for real estate news).
-   **SEO Optimization**: Generates optimized metadata including titles, descriptions, slugs, and keywords.
-   **Multi-language Support**: Translates articles to Modern Standard Arabic (MSA) with professional journalistic tone.
-   **Related Sources Discovery**: Finds and validates related articles using Tavily search.
-   **Resilient Queue System**: Features a Dead Letter Queue (DLQ) for failed jobs and full status tracking (`queued` -> `processing` -> `completed`/`failed`).
-   **Error Notifications**: Automatically sends email alerts with error details and tracebacks to administrators when jobs or link discovery fails.
-   **Observability**: Integrated with Opik for workflow tracing and monitoring.
-   **Scalable Architecture**: Dockerized services allow for horizontal scaling of workers.

## 🏗️ Architecture

The system follows a producer-consumer pattern with four main components:

1.  **Scheduler Service** (`src/scheduler/main.py`):
    -   **Role**: The "Pulse" of the system.
    -   **Function**: Crawls news sources, filters ads/duplicates, creates job records in MongoDB, and submits jobs to the API. It also acts as the **Archive**, receiving final results via webhook.
2.  **API Server** (`src/main.py`):
    -   **Role**: The "Gatekeeper".
    -   **Function**: Accepts job submissions, pushes them to Redis, and provides endpoints to monitor queues and manage the Dead Letter Queue (DLQ).
3.  **Worker** (`src/worker.py`):
    -   **Role**: The "Brain".
    -   **Function**: Consumes jobs from Redis, executes the LangGraph AI workflow, handles errors (retries/DLQ), and sends results back to the Scheduler.
4.  **Infrastructure**:
    -   **Redis**: Message broker for the Main Queue (`newsagent_jobs`) and Dead Letter Queue (`newsagent_dlq`).
    -   **MongoDB**: Persistent storage for Prompts, Source Configurations, Processed Articles, and Email Recipients.

### Workflow Graph

The processing pipeline executed by the Worker consists of the following nodes:
1.  **Load Agent Configuration**: Loads prompt templates from MongoDB.
2.  **Fetch Content**: Extracts raw article content (HTML/Text) from the URL.
3.  **Extract Links**: Identifies and extracts embedded links from the article.
4.  **Generate Summary**: Creates a concise summary of the article.
5.  **Validate Summary**: Validates summary quality (semantic accuracy, tone, hallucinations).
6.  **Select Best Summary**: Chooses the best summary from multiple attempts.
7.  **Check Embedded Links**: Validates relevance of embedded links.
8.  **Find Other Sources**: Discovers related articles using search queries.
9.  **Categorize Article**: Assigns categories and sub-categories.
10.  **Generate SEO**: Creates SEO-optimized metadata.
11.  **Translate Article**: Translates content to Arabic.
12.  **Notify Webhook**: Sends final results to the Scheduler's storage endpoint.

## 📋 Prerequisites

Before you begin, ensure you have the following installed:
-   **Docker** and **Docker Compose** (Recommended for running the full stack)
-   **Python 3.11+** (If running locally)
-   **MongoDB** (If running locally without Docker)
-   **Redis** (If running locally without Docker)

### API Keys Required

You'll need API keys for the following services:
-   **OpenAI API Key**: For LLM operations (summarization, categorization, translation, etc.)
-   **Tavily API Key**: For web search and finding related sources
-   **Opik API Key** (optional): For observability and tracing
-   **Opik Workspace** (optional): Your Opik workspace identifier
-   **SMTP Credentials**: For email notifications (Gmail, SendGrid, etc.)


## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

The easiest way to get started is using Docker Compose, which sets up all services automatically.
1.  **Clone the repository**:
    ```
    git clone <repository-url>
    cd NewsAgent
    ```
2.  **Create environment file**:
    ```
    cp sample.env .env
    ```
3.  **Configure environment variables**: Edit `.env` and fill in your API keys and SMTP settings:
    ```
    OPENAI_API_KEY=your_openai_api_key
    TAVILY_API_KEY=your_tavily_api_key
    OPIK_API_KEY=your_opik_api_key
    OPIK_WORKSPACE=your_workspace

    # Internal Webhook (Scheduler Service)
    WEBHOOK_URL=http://scheduler:8001/webhook/store-result
    WEBHOOK_SECRET=your_webhook_secret

    # Database (Docker Service Names)
    DATABASE_URL=mongodb://mongo:27017
    MONGO_DB_NAME=newsagent
    REDIS_URL=redis://redis:6379/0
    REDIS_QUEUE_NAME=newsagent_jobs
    REDIS_DLQ_NAME=newsagent_dlq

    # Email Alerts
    SMTP_SERVER=smtp.gmail.com
    SMTP_PORT=587
    SMTP_EMAIL=your-system-email@gmail.com
    SMTP_PASSWORD=your-app-password
    ```
4.  **Initialize the database**: Run the initialization script inside the API container. This seeds Prompts, Email Recipients, and a Sample Source.
    ```
    docker-compose run --rm api python src/scripts/init_db.py
    ```
5.  **Start all services**:
    ```
    docker-compose up -d --build
    ```
    This will start:
    -   Redis (6379)
    -   MongoDB (27017)
    -   API server (8000)
    -   Scheduler (8001)
    -   Worker process (background)
6.  **Verify the setup**:
    ```
    curl http://localhost:8000/health
    ```


### Option 2: Local Development Setup

For local development without Docker:
1.  **Create a virtual environment**:
    ```
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```
2.  **Install dependencies**:
    ```
    pip install -r requirements.txt
    ```
3.  **Install system dependencies** (for `newspaper4k` and `requests-html`):
    -   **Linux**: `sudo apt-get install -y libxml2-dev libxslt-dev libjpeg-dev zlib1g-dev libx11-6 libx11-xcb1`
    -   **macOS**: `brew install libxml2 libxslt libjpeg`
4.  **Set up Redis and MongoDB**:
    ```
    docker run -d -p 6379:6379 redis:alpine
    docker run -d -p 27017:27017 mongo:latest
    ```
5.  **Configure `.env`**: Ensure `DATABASE_URL=mongodb://localhost:27017` (localhost instead of mongo).
6.  **Initialize DB**:
    ```
    python src/scripts/init_db.py

    ```
7.  **Run Services**: You will need 3 separate terminals:
    -   **Terminal 1 (API)**: `uvicorn src.main:api --port 8000`
    -   **Terminal 2 (Scheduler)**: `uvicorn src.scheduler.main:app --port 8001`
    -   **Terminal 3 (Worker)**: `python src/worker.py`


## 📖 Usage

### 1. Managing News Sources (Scheduler)

To add a news site for automatic crawling via the Scheduler API (Port 8001):
```
curl -X POST "http://localhost:8001/sources" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CNN Business",
    "listing_url": "[https://edition.cnn.com/business](https://edition.cnn.com/business)",
    "url_pattern": "/business/",
    "fetch_interval_minutes": 60
  }'
```

### 2. Manual Job Submission
You can manually trigger processing for a specific URL via the Main API (Port 8000):
```
curl -X POST "http://localhost:8000/submit-job" \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "[https://example.com/news-article](https://example.com/news-article)",
    "max_retries": 3
  }'

```

### 3. Viewing Processed Articles & Status

Retrieve the archive of processed articles from the Scheduler service. You can filter by status (`processed`, `approved`, `rejected`, `duplicated`).
```
# Get the latest 10 processed articles
curl "http://localhost:8001/articles?limit=10&status=processed"
```

To update an article's status (e.g., after human review):

```
curl -X PATCH "http://localhost:8001/articles/{article_id}/status" \
  -H "Content-Type: application/json" \
  -d '{ "status": "approved" }'

```

### 4. Queue Management

Monitor the Redis queues via the Main API:

-   **Queue Status**: `GET /queue/status`
-   **Main Queue Items**: `GET /queue/main/items`
-   **DLQ Items**: `GET /queue/dlq/items`
-   **Requeue Failed Job**: `POST /queue/dlq/requeue/{job_id}`
-   **Requeue All Failed Jobs**: `POST /queue/dlq/requeue-all`


## 🔧 Configuration

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `OPENAI_API_KEY` | OpenAI API key for LLM operations | Yes | - |
| `TAVILY_API_KEY` | Tavily API key for web search | Yes | - |
| `DATABASE_URL` | MongoDB connection string | Yes | `mongodb://localhost:27017` |
| `MONGO_DB_NAME` | MongoDB database name | No | `newsagent` |
| `REDIS_URL` | Redis connection URL | No | `redis://localhost:6379/0` |
| `REDIS_QUEUE_NAME` | Main queue name | No | `newsagent_jobs` |
| `REDIS_DLQ_NAME` | Dead Letter Queue name | No | `newsagent_dlq` |
| `WEBHOOK_URL` | Endpoint for worker to send results | Yes | - |
| `SMTP_SERVER` | SMTP Server (e.g., smtp.gmail.com) | Yes | - |
| `SMTP_EMAIL` | Email address sending alerts | Yes | - |
| `SMTP_PASSWORD` | App password/SMTP password | Yes | - |

### Database Configuration

The system uses **MongoDB** for storage.
-   **Prompts**: Stores AI prompts (`src/models/AgentPromptsModel.py`).
-   **Sources**: Configuration for the Scheduler crawler.
-   **Processed Articles**: The archive of all discovered and processed content.
-   **Email Recipients**: List of users who receive error alerts.


## 🏛️ Project Structure

```
NewsAgent/
├── src/
│   ├── configs/             # Configuration & Settings
│   ├── db/                  # MongoDB Models & Enums
│   ├── graph/               # LangGraph Workflow Definition
│   │   ├── nodes/           # AI Nodes (Summary, Extraction, etc.)
│   ├── models/              # Pydantic Data Models (State, Article, etc.)
│   ├── prompts/             # AI Prompt Templates
│   ├── scheduler/           # Scheduler Service Code
│   │   ├── link_discovery.py # Crawler & Ad Filtering Logic
│   │   ├── main.py          # Scheduler Service Entrypoint
│   │   └── models.py        # Scheduler DB Models
│   ├── scripts/             # Initialization Scripts (init_db.py)
│   ├── utils/               # Utilities (Email, etc.)
│   ├── main.py              # Main API Server Entrypoint
│   ├── worker.py            # Background Worker Entrypoint
│   └── draw_workflow_graph.py
├── graphs/                  # Generated Workflow Diagrams
├── docker-compose.yml       # Docker Orchestration
├── Dockerfile               # Unified Docker Image
├── requirements.txt         # Python Dependencies
├── sample.env               # Environment Template
└── README.md                # Documentation
```

## 🔍 Workflow Details

### Summary Generation & Validation Loop

The system implements a sophisticated validation loop for summary generation:
1.  **Generate Summary**: Creates an initial summary using the LLM.
2.  **Validate Summary**: A "critic" LLM evaluates the summary on:
    -   **Semantic similarity** (0-10 scale, requires ≥8.0).
    -   **Tone matching** (0-10 scale, requires ≥7.0).
    -   **Hallucination detection** (must be zero).
3.  **Retry Logic**: If validation fails, the system regenerates the summary with specific feedback.
4.  **Best Selection**: After multiple attempts, the best summary is selected.


### Categorization System

The categorization system uses a comprehensive knowledge base focused on real estate news, including:
-   **Main Categories**: Market & Economy, Residential, Commercial, Hospitality, Development, Developers, Investment, Finance, PropTech, Construction, Architecture & Design, Sustainability, Policy & Regulations, People.
-   **Sub-categories**: Specific classifications within each main category.
-   **Developer Recognition**: Identifies major developers (Emaar, Aldar, Roshn, etc.).

### Translation

Articles are translated to Modern Standard Arabic (MSA) with:
-   Professional journalistic tone (similar to Al Arabiya / Asharq Business).
-   Accurate real estate terminology translation (e.g., "Off-plan", "Freehold").
-   Full content translation (no summarization during translation).

## 🧪 Development

### Code Style

The project follows PEP 8 Python style guidelines. Consider using:
-   `black` for code formatting.
-   `flake8` or `pylint` for linting.
-   `mypy` for type checking.

### Adding New Nodes

To add a new workflow node:
1.  Create a new file in `src/graph/nodes/`.
2.  Implement a function that takes `MainWorkflowState` and returns `MainWorkflowState`.
3.  Add the node to `src/graph/graph.py`: `builder.add_node("your_node_name", your_node_function)`.
4.  Connect it to the workflow graph with appropriate edges.


### Modifying Prompts

Prompts are stored in the MongoDB `prompts` collection. To modify them:
1.  Update `INITIAL_DATA` in `src/scripts/init_db.py`.
2.  Re-run the initialization script (it skips existing active prompts).
3.  OR manually update the documents in MongoDB using a GUI like Compass.


## 🐛 Troubleshooting

### Common Issues

1.  **"Browser closed unexpectedly"**:
    -   This usually happens in the Scheduler or Worker when `requests-html` tries to render JS.
    -   **Fix**: Ensure you are using the provided `Dockerfile`, which installs all necessary Chromium dependencies (`libxml2`, `libx11`, etc.).

2.  **Redis/Mongo Connection Refused**:
    -   Ensure all containers are running: `docker-compose ps`.
    -   Check networking in `.env`. Within Docker, use service names (`redis`, `mongo`) as hostnames, not `localhost`.

3.  **Emails not sending**:
    -   Verify `SMTP_EMAIL` and `SMTP_PASSWORD` in `.env`.
    -   For Gmail, you must use an **App Password**, not your login password.

4.  **Scheduler not picking up sources**:
    -   Check if the source is active via `GET /sources/{id}`.
    -   Check `fetch_interval_minutes`. The scheduler main loop runs every 1 minute.


## 📝 API Documentation

When the servers are running, interactive API documentation is available at:
-   **Main API**: [http://localhost:8000/docs](https://www.google.com/search?q=http://localhost:8000/docs "null")
-   **Scheduler API**: [http://localhost:8001/docs](https://www.google.com/search?q=http://localhost:8001/docs "null")


## 🔐 Security Considerations

-   **Never commit `.env` files**: They contain sensitive API keys.
-   **Webhook Security**: Implement webhook secret validation in the Scheduler's `store_result` endpoint (currently checks for payload validity).
-   **Database Security**: Use strong passwords and encrypted connections in production.
-   **API Rate Limiting**: Consider implementing rate limiting for the `/submit-job` endpoint to prevent abuse.


## 🚢 Production Deployment

### Recommended Setup

1.  **Use Persistent Databases**: Use MongoDB Atlas (or a mounted volume) and a managed Redis instance instead of ephemeral Docker containers.
2.  **Set up Redis Persistence**: Enable AOF (Append Only File) for job queue reliability.
3.  **Use Environment-Specific Files**: Use `.env.production` (not committed to git).
4.  **Enable HTTPS**: Run the API and Scheduler behind a reverse proxy like Nginx or Traefik with SSL.
5.  **Scale Workers**: In `docker-compose.yml`, you can scale the worker service: `deploy: replicas: 3`.
6.  **Monitoring**: Use Opik for AI logic tracing and a tool like Prometheus/Grafana for system metrics.


### Docker Production Example

```
# docker-compose.prod.yml
version: '3.8'

services:
  redis:
    image: redis:alpine
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data

  mongo:
    image: mongo:latest
    volumes:
      - mongo_data:/data/db

  api:
    build: .
    command: uvicorn src.main:api --host 0.0.0.0 --port 8000
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATABASE_URL=mongodb://mongo:27017
    env_file: .env.production
    restart: always

  scheduler:
    build: .
    command: uvicorn src.scheduler.main:app --host 0.0.0.0 --port 8001
    env_file: .env.production
    restart: always

  worker:
    build: .
    command: python src/worker.py
    deploy:
      replicas: 3 # Scale to 3 workers
    env_file: .env.production
    restart: always
```

## 📄 License

(Add your license information here)

## 🤝 Contributing

(Add contribution guidelines here)

## 📧 Support

(Add support/contact information here)

## 🙏 Acknowledgments

-   **LangGraph**: For the workflow orchestration framework
-   **LangChain**: For LLM integration and tooling
-   **FastAPI**: For the high-performance API framework
-   **Newspaper4k**: For article extraction
-   **Redis**: For reliable job queue management


**Version**: 3.4 **Last Updated**: 2025