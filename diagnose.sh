#!/bin/bash
# Diagnostic script to check why containers are failing

echo "======================================"
echo "Checking NewsAgent API Container"
echo "======================================"
echo ""
echo "Container Status:"
docker ps -a | grep newsagent_api
echo ""
echo "Last 50 log lines:"
docker logs newsagent_api --tail 50 2>&1
echo ""
echo ""

echo "======================================"
echo "Checking Scheduler Container"
echo "======================================"
echo ""
echo "Container Status:"
docker ps -a | grep newsagent_scheduler
echo ""
echo "Last 50 log lines:"
docker logs newsagent_scheduler --tail 50 2>&1
echo ""
echo ""

echo "======================================"
echo "Testing Direct Python Import"
echo "======================================"
docker exec newsagent_api python3 -c "from src.middleware.request_logger import RequestLoggingMiddleware; print('✓ Middleware import successful')" 2>&1 || echo "✗ Import failed"
echo ""

echo "======================================"
echo "Testing API Startup"
echo "======================================"
docker exec newsagent_api python3 -c "from src.main import api; print('✓ API import successful')" 2>&1 || echo "✗ API import failed"
