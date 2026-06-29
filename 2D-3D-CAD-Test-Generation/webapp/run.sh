#!/usr/bin/env bash
# Launch the MTI 2D->3D pipeline web UI.
#   ./run.sh           # http://127.0.0.1:8092
#   PORT=9000 ./run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$HERE/.." && pwd)"
PORT="${PORT:-8092}"
VENV="$HERE/.venv"

if [ ! -d "$VENV" ]; then
  echo "Creating venv at $VENV ..."
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "Installing dependencies (pipeline + UI) ..."
pip install -q --upgrade pip
pip install -q -r "$PROJECT_DIR/requirements.txt"
pip install -q -r "$HERE/requirements-ui.txt"

echo
echo "  MTI 2D->3D Pipeline UI  ->  http://127.0.0.1:$PORT"
echo "  (Ctrl+C to stop)"
echo
exec uvicorn app:app --app-dir "$HERE" --host 127.0.0.1 --port "$PORT"
