#!/usr/bin/env bash
set -e

echo "Setting up AI Voice Assistant..."

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in real values before running."
fi

echo "Setup complete."
echo "Next: fill in .env, then run:"
echo "  source venv/bin/activate && python -m agent.main console   # fastest local test"
echo "  source venv/bin/activate && python -m agent.main dev        # full LiveKit room mode"
echo "  source venv/bin/activate && python frontend/serve.py        # browser frontend + API"
echo "  source venv/bin/activate && pytest -v                       # run automated test suite"
echo ""
echo "NOTE: use 'python frontend/serve.py', not 'python -m http.server' —"
echo "the plain http.server has no way to issue the LiveKit token the"
echo "frontend needs at /api/token, and connecting from the browser will fail."