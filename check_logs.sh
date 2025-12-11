#!/bin/bash
# Test script to check container logs

echo "=== NewsAgent API Logs ==="
docker logs newsagent_api --tail 100

echo ""
echo "=== NewsAgent Scheduler Logs ==="
docker logs newsagent_scheduler --tail 100
