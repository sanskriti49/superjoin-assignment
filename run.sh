#!/usr/bin/env bash
# Starts the API and the web interface together.
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v npm >/dev/null || { echo "node and npm are required"; exit 1; }

echo "Installing backend dependencies"
python3 -m pip install -q -r backend/requirements.txt

if [ ! -d frontend/node_modules ]; then
  echo "Installing frontend dependencies"
  (cd frontend && npm install --silent)
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd backend && python3 -m uvicorn app.main:app --port 8000) &
(cd frontend && npm run dev) &

echo
echo "Web interface  http://localhost:5173"
echo "API reference  http://localhost:8000/docs"
echo "Press Ctrl+C to stop both."
wait
