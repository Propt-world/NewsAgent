# NewsAgent

An AI-powered news article processing system built with LangGraph that automatically extracts, summarizes, categorizes, and translates news articles. The system uses a Redis-backed queue architecture for scalable, asynchronous processing of news articles with comprehensive validation and quality assurance.

## 🎯 Overview

NewsAgent is a production-ready system designed to process news articles through a sophisticated AI workflow. It extracts content from URLs, generates validated summaries, categorizes articles (with a focus on real estate news), generates SEO metadata, translates content to Arabic, and delivers results via webhook notifications.

### Key Features

- **Intelligent Content Extraction**: Uses `newspaper4k` and `requests-html` to extract clean article content, even from JavaScript-rendered pages
- **AI-Powered Summarization**: Generates concise summaries with a validation loop that ensures quality and accuracy
- **Smart Categorization**: Automatically categorizes articles using a comprehensive knowledge base (specialized for real estate news)
- **SEO Optimization**: Generates optimized metadata including titles, descriptions, slugs, and keywords
- **Multi-language Support**: Translates articles to Modern Standard Arabic (MSA) with professional journalistic tone
- **Related Sources Discovery**: Finds and validates related articles using Tavily search
- **Webhook Integration**: Delivers processed results to external systems via configurable webhooks
- **Observability**: Integrated with Opik for workflow tracing and monitoring
- **Scalable Architecture**: Redis-backed queue system allows horizontal scaling of workers

## 🏗️ Architecture

The system follows a producer-consumer pattern with three main components:

1. **API Server** (`src/main.py`): FastAPI application that accepts job submissions and queues them in Redis
2. **Worker** (`src/worker.py`): Background process that consumes jobs from Redis and executes the LangGraph workflow
3. **Redis**: Message broker that manages the job queue

### Workflow Graph

The processing pipeline consists of the following nodes:

1. **Load Agent Configuration**: Loads prompt templates from the database
2. **Fetch Content**: Extracts raw article content from the URL
3. **Extract Links**: Identifies and extracts embedded links from the article
4. **Generate Summary**: Creates a concise summary of the article
5. **Validate Summary**: Validates summary quality (semantic accuracy, tone, hallucinations)
6. **Select Best Summary**: Chooses the best summary from multiple attempts
7. **Check Embedded Links**: Validates relevance of embedded links
8. **Find Other Sources**: Discovers related articles using search queries
9. **Categorize Article**: Assigns categories and sub-categories
10. **Generate SEO**: Creates SEO-optimized metadata
11. **Translate Article**: Translates content to Arabic
12. **Notify Webhook**: Sends final results to configured webhook endpoint

## 📋 Prerequisites

Before you begin, ensure you have the following installed:

- **Python 3.11+** (recommended: Python 3.11)
- **Docker** and **Docker Compose** (for containerized deployment)
- **Redis** (if running locally without Docker)
- **PostgreSQL** or **SQLite** (for the database)

### API Keys Required

You'll need API keys for the following services:

- **OpenAI API Key**: For LLM operations (summarization, categorization, translation, etc.)
- **Tavily API Key**: For web search and finding related sources
- **Opik API Key** (optional): For observability and tracing
- **Opik Workspace** (optional): Your Opik workspace identifier

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

The easiest way to get started is using Docker Compose, which sets up all services automatically.

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd NewsAgent
   ```

2. **Create environment file**:
   ```bash
   cp sample.env .env
   ```

3. **Configure environment variables**:
   Edit `.env` and fill in your API keys:
   ```env
   OPENAI_API_KEY=your_openai_api_key
   TAVILY_API_KEY=your_tavily_api_key
   OPIK_API_KEY=your_opik_api_key
   OPIK_WORKSPACE=your_workspace
   WEBHOOK_URL=https://your-webhook-endpoint.com
   WEBHOOK_SECRET=your_webhook_secret
   DATABASE_URL=sqlite:///./newsagent.db
   REDIS_URL=redis://redis:6379/0
   REDIS_QUEUE_NAME=newsagent_jobs
   MODEL_NAME=gpt-4o-mini
   MODEL_TEMPERATURE=0.5
   ```

4. **Initialize the database**:
   ```bash
   docker-compose run --rm api python src/scripts/init_db.py
   ```

5. **Start all services**:
   ```bash
   docker-compose up -d
   ```

   This will start:
   - Redis on port 6379
   - API server on port 8000
   - Worker process (background)

6. **Verify the setup**:
   ```bash
   curl http://localhost:8000/health
   ```

### Option 2: Local Development Setup

For local development without Docker:

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd NewsAgent
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv venv

   # On Windows
   venv\Scripts\activate

   # On macOS/Linux
   source venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Install system dependencies** (for `newspaper4k` and `lxml`):

   **On Ubuntu/Debian**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y gcc g++ libxml2-dev libxslt-dev libjpeg-dev zlib1g-dev
   ```

   **On macOS**:
   ```bash
   brew install libxml2 libxslt libjpeg
   ```

   **On Windows**: These dependencies are typically included with Python, but you may need Visual C++ Build Tools.

5. **Set up Redis**:

   **Using Docker** (easiest):
   ```bash
   docker run -d -p 6379:6379 redis:alpine
   ```

   **Or install locally**:
   - Follow Redis installation instructions for your OS
   - Start Redis: `redis-server`

6. **Create environment file**:
   ```bash
   cp sample.env .env
   ```

7. **Configure environment variables**:
   Edit `.env` with your settings:
   ```env
   OPENAI_API_KEY=your_openai_api_key
   TAVILY_API_KEY=your_tavily_api_key
   OPIK_API_KEY=your_opik_api_key
   OPIK_WORKSPACE=your_workspace
   WEBHOOK_URL=https://your-webhook-endpoint.com
   WEBHOOK_SECRET=your_webhook_secret
   DATABASE_URL=sqlite:///./newsagent.db
   REDIS_URL=redis://localhost:6379/0
   REDIS_QUEUE_NAME=newsagent_jobs
   MODEL_NAME=gpt-4o-mini
   MODEL_TEMPERATURE=0.5
   OPENAI_URL=https://api.openai.com/v1/chat/completions
   ```

8. **Initialize the database**:
   ```bash
   python src/scripts/init_db.py
   ```

9. **Start the API server** (in one terminal):
   ```bash
   python src/main.py
   ```

   Or using uvicorn directly:
   ```bash
   uvicorn src.main:api --host 0.0.0.0 --port 8000 --reload
   ```

10. **Start the worker** (in another terminal):
    ```bash
    python src/worker.py
    ```

## 📖 Usage

### Submitting a Job

Submit a news article URL for processing:

```bash
curl -X POST "http://localhost:8000/submit-job" \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "https://example.com/news-article",
    "max_retries": 3
  }'
```

**Response**:
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "queue_position": 1,
  "message": "Job successfully sent to Redis worker."
}
```

### Checking Queue Status

```bash
curl http://localhost:8000/queue-status
```

**Response**:
```json
{
  "status": "operational",
  "queue_name": "newsagent_jobs",
  "pending_jobs": 0
}
```

### Health Check

```bash
curl http://localhost:8000/health
```

**Response**:
```json
{
  "status": "healthy",
  "redis": "connected",
  "graph_logic": "operational"
}
```

### Visualizing the Workflow Graph

Generate a visual representation of the workflow:

```bash
curl http://localhost:8000/debug/draw-graph --output workflow_graph.png
```

## 🔧 Configuration

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `OPENAI_API_KEY` | OpenAI API key for LLM operations | Yes | - |
| `OPENAI_URL` | OpenAI API endpoint URL | No | `https://api.openai.com/v1/chat/completions` |
| `TAVILY_API_KEY` | Tavily API key for web search | Yes | - |
| `OPIK_API_KEY` | Opik API key for observability | No | - |
| `OPIK_WORKSPACE` | Opik workspace identifier | No | - |
| `MODEL_NAME` | OpenAI model to use | No | `gpt-4o-mini` |
| `MODEL_TEMPERATURE` | Model temperature setting | No | `0.5` |
| `REDIS_URL` | Redis connection URL | No | `redis://localhost:6379/0` |
| `REDIS_QUEUE_NAME` | Redis queue name for jobs | No | `newsagent_jobs` |
| `DATABASE_URL` | Database connection string | Yes | - |
| `WEBHOOK_URL` | Webhook endpoint for results | No | - |
| `WEBHOOK_SECRET` | Secret for webhook authentication | No | - |
| `HOST` | API server host | No | `0.0.0.0` |
| `PORT` | API server port | No | `8000` |

### Database Configuration

The system uses SQLAlchemy for database operations. Supported databases:

- **SQLite** (development): `sqlite:///./newsagent.db`
- **PostgreSQL** (production): `postgresql://user:password@localhost/newsagent`
- **MySQL**: `mysql://user:password@localhost/newsagent`

### Prompt Templates

Prompt templates are stored in the database and can be managed through the `PromptTemplate` model. The initialization script (`src/scripts/init_db.py`) seeds the database with default prompts for:

- Content extraction
- Summary generation
- Summary validation
- Link relevance checking
- Search query generation
- Article categorization
- SEO metadata generation
- Arabic translation

## 🏛️ Project Structure

```
NewsAgent/
├── src/
│   ├── configs/
│   │   └── settings.py          # Application configuration
│   ├── db/
│   │   ├── models.py            # SQLAlchemy models
│   │   └── enums.py             # Database enums
│   ├── graph/
│   │   ├── graph.py             # Main workflow graph definition
│   │   └── nodes/               # Workflow node implementations
│   │       ├── load_agent_configuration.py
│   │       ├── raw_extraction.py
│   │       ├── extract_links.py
│   │       ├── summary_generator.py
│   │       ├── validate_summary.py
│   │       ├── select_best_summary.py
│   │       ├── check_embedded_links.py
│   │       ├── find_other_sources.py
│   │       ├── categorize_article.py
│   │       ├── generate_seo.py
│   │       ├── translate_article.py
│   │       ├── notify_webhook.py
│   │       └── conditional_edges.py
│   ├── models/                  # Pydantic models for data validation
│   ├── prompts/                 # Prompt template definitions
│   ├── scripts/
│   │   └── init_db.py           # Database initialization script
│   ├── main.py                  # FastAPI application
│   ├── worker.py                # Worker process
│   └── draw_workflow_graph.py   # Graph visualization utility
├── graphs/                      # Generated workflow graphs
├── docker-compose.yml           # Docker Compose configuration
├── Dockerfile                   # Docker image definition
├── requirements.txt             # Python dependencies
├── sample.env                   # Environment variables template
└── README.md                    # This file
```

## 🔍 Workflow Details

### Summary Generation & Validation Loop

The system implements a sophisticated validation loop for summary generation:

1. **Generate Summary**: Creates an initial summary using the LLM
2. **Validate Summary**: A "critic" LLM evaluates the summary on:
   - Semantic similarity (0-10 scale, requires ≥8.0)
   - Tone matching (0-10 scale, requires ≥7.0)
   - Hallucination detection (must be zero)
3. **Retry Logic**: If validation fails, the system regenerates the summary with feedback
4. **Best Selection**: After multiple attempts, the best summary is selected

### Categorization System

The categorization system uses a comprehensive knowledge base focused on real estate news, including:

- **Main Categories**: Market & Economy, Residential, Commercial, Hospitality, Development, Developers, Investment, Finance, PropTech, Construction, Architecture & Design, Sustainability, Policy & Regulations, People
- **Sub-categories**: Specific classifications within each main category
- **Developer Recognition**: Identifies major developers (Emaar, Aldar, Roshn, etc.)

### Translation

Articles are translated to Modern Standard Arabic (MSA) with:
- Professional journalistic tone (similar to Al Arabiya / Asharq Business)
- Accurate real estate terminology translation
- Full content translation (no summarization)
- Natural Arabic flow and grammar

## 🧪 Development

### Running Tests

(Add test instructions when tests are available)

### Code Style

The project follows PEP 8 Python style guidelines. Consider using:

- `black` for code formatting
- `flake8` or `pylint` for linting
- `mypy` for type checking

### Adding New Nodes

To add a new workflow node:

1. Create a new file in `src/graph/nodes/`
2. Implement a function that takes `MainWorkflowState` and returns `MainWorkflowState`
3. Add the node to `src/graph/graph.py`:
   ```python
   builder.add_node("your_node_name", your_node_function)
   ```
4. Connect it to the workflow graph with appropriate edges

### Modifying Prompts

Prompts are stored in the database. To modify them:

1. Update the `INITIAL_DATA` in `src/scripts/init_db.py`
2. Re-run the initialization script (it will skip existing active prompts)
3. Or update prompts directly in the database using SQLAlchemy

## 🐛 Troubleshooting

### Redis Connection Issues

**Problem**: Worker cannot connect to Redis

**Solutions**:
- Verify Redis is running: `redis-cli ping` (should return `PONG`)
- Check `REDIS_URL` in `.env` matches your Redis configuration
- For Docker: Ensure Redis service is healthy: `docker-compose ps`

### Database Connection Issues

**Problem**: Database initialization fails

**Solutions**:
- Verify `DATABASE_URL` is correct
- Ensure database exists (for PostgreSQL/MySQL)
- Check file permissions (for SQLite)
- Install required database drivers (e.g., `psycopg2` for PostgreSQL)

### API Key Errors

**Problem**: LLM operations fail with authentication errors

**Solutions**:
- Verify API keys are set in `.env`
- Check API key validity and remaining credits
- Ensure no extra spaces or quotes in `.env` file

### Content Extraction Failures

**Problem**: Articles fail to extract content

**Solutions**:
- Some sites may block automated access (check User-Agent)
- JavaScript-heavy sites may require longer render timeouts
- Consider adding custom extraction logic for specific sites

## 📝 API Documentation

When the API server is running, interactive API documentation is available at:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 🔐 Security Considerations

- **Never commit `.env` files**: They contain sensitive API keys
- **Use environment variables**: Never hardcode secrets in source code
- **Webhook Security**: Implement webhook secret validation in your receiving endpoint
- **Database Security**: Use strong passwords and encrypted connections in production
- **API Rate Limiting**: Consider implementing rate limiting for the `/submit-job` endpoint

## 🚢 Production Deployment

### Recommended Setup

1. **Use PostgreSQL** instead of SQLite for production
2. **Set up Redis persistence** for job queue reliability
3. **Use environment-specific `.env` files** (not committed to git)
4. **Enable HTTPS** for the API server (use a reverse proxy like Nginx)
5. **Set up monitoring** and logging (consider Opik for observability)
6. **Scale workers** horizontally based on queue depth
7. **Implement health checks** and auto-restart policies
8. **Set up backups** for the database

### Docker Production Example

```yaml
# docker-compose.prod.yml
version: '3.8'

services:
  redis:
    image: redis:alpine
    volumes:
      - redis_data:/data
    command: redis-server --appendonly yes

  api:
    build: .
    command: uvicorn src.main:api --host 0.0.0.0 --port 8000
    environment:
      - REDIS_URL=redis://redis:6379/0
    env_file:
      - .env.production
    restart: unless-stopped

  worker:
    build: .
    command: python src/worker.py
    environment:
      - REDIS_URL=redis://redis:6379/0
    env_file:
      - .env.production
    restart: unless-stopped
    deploy:
      replicas: 3  # Scale workers
```

## 📄 License

(Add your license information here)

## 🤝 Contributing

(Add contribution guidelines here)

## 📧 Support

(Add support/contact information here)

## 🙏 Acknowledgments

- **LangGraph**: For the workflow orchestration framework
- **LangChain**: For LLM integration and tooling
- **FastAPI**: For the high-performance API framework
- **Newspaper4k**: For article extraction
- **Redis**: For reliable job queue management

---

**Version**: 3.1
**Last Updated**: 2024
