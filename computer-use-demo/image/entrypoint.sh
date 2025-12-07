#!/bin/bash
set -e

./start_all.sh
./novnc_startup.sh

# python http_server.py > /tmp/server_logs.txt 2>&1 &

uvicorn computer_use_demo.api:app --host 0.0.0.0 --port 8080 > /tmp/uvicorn_stdout.log &

echo "✨ Computer Use Demo is ready!"
echo "➡️  Open http://localhost:8080 in your browser to begin"

# Keep the container running
tail -f /dev/null
