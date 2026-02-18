#!/bin/bash
# Install OpenClaw TUI Dashboard
set -e

INSTALL_DIR="${INSTALL_DIR:-$HOME/openclaw-tui}"

echo "🖥️  Installing OpenClaw TUI Dashboard..."

# Clone or update
if [ -d "$INSTALL_DIR" ]; then
    echo "→ Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull
else
    echo "→ Cloning repository..."
    git clone https://github.com/YOUR_USERNAME/openclaw-tui.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Create venv and install deps
echo "→ Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

# Make run.sh executable
chmod +x run.sh

echo ""
echo "✅ Installation complete!"
echo ""
echo "Run the dashboard:"
echo "  $INSTALL_DIR/run.sh"
echo ""
echo "Or add to your PATH:"
echo "  export PATH=\"\$PATH:$INSTALL_DIR\""
echo "  openclaw-tui"
