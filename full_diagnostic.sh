#!/bin/bash
# Comprehensive diagnostic script

echo "=========================================="
echo "1. Testing if ports are actually open"
echo "=========================================="
netstat -tlnp 2>/dev/null | grep -E ':(8000|8001)' || ss -tlnp 2>/dev/null | grep -E ':(8000|8001)'

echo ""
echo "=========================================="
echo "2. Testing connection to port 8000"
echo "=========================================="
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/8000' && echo "✓ Port 8000 is open" || echo "✗ Port 8000 is closed or refusing connections"

echo ""
echo "=========================================="
echo "3. Testing connection to port 8001"
echo "=========================================="
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/8001' && echo "✓ Port 8001 is open" || echo "✗ Port 8001 is closed or refusing connections"

echo ""
echo "=========================================="
echo "4. Checking Docker container status"
echo "=========================================="
docker ps | grep -E 'newsagent_(api|scheduler)'

echo ""
echo "=========================================="
echo "5. Checking API logs for errors"
echo "=========================================="
docker logs newsagent_api 2>&1 | tail -30

echo ""
echo "=========================================="
echo "6. Checking Scheduler logs for errors"
echo "=========================================="
docker logs newsagent_scheduler 2>&1 | tail -30

echo ""
echo "=========================================="
echo "7. Testing Python import inside container"
echo "=========================================="
docker exec newsagent_api python3 -c "
try:
    from src.main import api
    print('✓ API module imports successfully')
except Exception as e:
    print(f'✗ API import failed: {e}')
    import traceback
    traceback.print_exc()
" 2>&1

echo ""
echo "=========================================="
echo "8. Checking if uvicorn process is running"
echo "=========================================="
docker exec newsagent_api ps aux | grep -v grep | grep uvicorn || echo "✗ Uvicorn process not found"

echo ""
echo "=========================================="
echo "9. Checking container network settings"
echo "=========================================="
docker inspect newsagent_api | grep -A 10 "NetworkSettings"
