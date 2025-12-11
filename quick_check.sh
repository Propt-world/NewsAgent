#!/bin/bash
# Quick diagnostic - run this on the server

echo "=== Testing API Health ==="
curl -v http://localhost:8000/health 2>&1 | head -20

echo ""
echo "=== Checking if API process is running ==="
docker exec newsagent_api ps aux | grep uvicorn

echo ""
echo "=== Last 20 lines of API logs ==="
docker logs newsagent_api --tail 20

echo ""
echo "=== Testing if port 8000 is listening ==="
docker exec newsagent_api netstat -tlnp 2>/dev/null | grep 8000 || docker exec newsagent_api ss -tlnp 2>/dev/null | grep 8000

echo ""
echo "=== Checking for Python errors ==="
docker logs newsagent_api 2>&1 | grep -i "error\|exception\|traceback" | tail -10
