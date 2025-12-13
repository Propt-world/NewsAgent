#!/bin/bash
set -e

echo "=== Fix and Start Script ==="

# 1. Ensure .env exists
if [ ! -f .env ]; then
    echo "Creating .env file with defaults..."
    cat > .env <<EOF
OPENAI_API_KEY=sk-dummy
OPIK_API_KEY=
TAVILY_API_KEY=
OPENAI_URL=https://api.openai.com/v1/chat/completions
MODEL_NAME=gpt-4o-mini
MODEL_TEMPERATURE=0.5
REDIS_URL=redis://redis:6379/0
REDIS_QUEUE_NAME=news_queue
REDIS_DLQ_NAME=news_dlq
DATABASE_URL=mongodb://mongo:27017
MONGO_DB_NAME=newsagent
SMTP_SERVER=smtp.example.com
SMTP_PORT=587
SMTP_EMAIL=dummy@example.com
SMTP_PASSWORD=dummy
MAIN_API_URL=http://api:8000
WEBHOOK_URL=http://localhost:8001/webhook
WEBHOOK_SECRET=dummy
SUBMISSION_SOURCE_ID=manual
EOF
fi

# 2. Remove --reload flag if present (causes issues in docker)
if grep -q "\-\-reload" docker-compose.yml; then
    echo "Removing --reload flag from docker-compose.yml..."
    # Should work on Linux (Ubuntu)
    sed -i 's/ --reload//g' docker-compose.yml
fi

# 3. Restart Containers
echo "Rebuilding and starting containers..."
# Try with sudo, failback to normal
if command -v sudo &> /dev/null; then
    sudo docker-compose down
    sudo docker-compose up -d --build
else
    docker-compose down
    docker-compose up -d --build
fi

# 4. Wait for startup
echo "Waiting 20 seconds for services to initialize..."
sleep 20

# 5. Test Connectivity
echo "=================================="
echo "Testing NewsAPI (Port 8000)"
echo "=================================="
curl -v http://localhost:8000/health || echo "Failed to connect to 8000"

echo ""
echo "=================================="
echo "Testing Scheduler (Port 8001)"
echo "=================================="
curl -v http://localhost:8001/health || echo "Failed to connect to 8001"

echo ""
echo "=== Logs (Last 20 lines) ==="
if command -v sudo &> /dev/null; then
    sudo docker logs newsagent_api --tail 20
else
    docker logs newsagent_api --tail 20
fi
