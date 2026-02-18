#!/bin/bash
# Launch OpenClaw TUI Dashboard
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv if it exists
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Ensure openclaw is in PATH
export PATH="$HOME/.npm-global/bin:$PATH"

python dashboard.py "$@"
