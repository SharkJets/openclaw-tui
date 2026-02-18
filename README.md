# OpenClaw TUI Dashboard

A lightweight terminal dashboard for monitoring [OpenClaw](https://openclaw.dev) AI agents in real-time.

![Dashboard Screenshot](docs/screenshot.png)

## Features

- 🖥️ **System Health** — CPU, RAM, Disk with sparkline graphs
- 🎮 **GPU Monitoring** — NVIDIA GPU and VRAM usage (if available)
- 🤖 **OpenClaw Status** — Gateway health and connected channels
- 📊 **5-Hour Usage Window** — Token usage and rate limit tracking
- 💰 **Cost Tracking** — Spending by day, model, and all-time totals
- 📝 **Sessions** — Active sessions with model and token counts
- ⏰ **Cron Jobs** — Scheduled task status
- 📡 **Live Feed** — Real-time message stream
- 🔝 **Top Processes** — CPU/memory usage by process
- 🌐 **Network** — Upload/download with traffic sparklines

## Why This Dashboard?

- **~1% CPU** — Uses Rich's simple Live display instead of reactive frameworks
- **Minimal deps** — Just `rich` and `psutil`
- **TTY compatible** — Auto-detects Linux console and uses ASCII sparklines
- **Cached data** — Expensive operations only run every 10-30s

## Requirements

- Python 3.10+
- OpenClaw installed and running
- Linux, macOS, or WSL

## Quick Start

```bash
# Clone the repo
git clone https://github.com/SharkJets/openclaw-tui.git

cd openclaw-tui

# Install dependencies
pip install -r requirements.txt

# Run the dashboard
python dashboard.py
```

## Installation

### Option 1: Virtual Environment (Recommended)

```bash
git clone https://github.com/SharkJets/openclaw-tui.git

cd openclaw-tui

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

./run.sh
```

### Option 2: System Install

```bash
pip install rich psutil pynvml
python dashboard.py
```

## Configuration

The dashboard auto-detects OpenClaw paths. Override with environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_DIR` | `~/.openclaw` | OpenClaw config directory |
| `WORKSPACE_DIR` | Current directory | Agent workspace |
| `OPENCLAW_AGENT` | `main` | Agent ID to monitor |

Example:
```bash
OPENCLAW_DIR=~/.openclaw OPENCLAW_AGENT=main ./run.sh
```

## Auto-Start on Boot (Linux)

To auto-start the dashboard on tty1:

```bash
# Set up auto-login on tty1
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/override.conf << 'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin YOUR_USERNAME --noclear %I $TERM
EOF

# Add to .bashrc
cat >> ~/.bashrc << 'EOF'

# Auto-start OpenClaw Dashboard on tty1
if [ "$(tty)" = "/dev/tty1" ]; then
    /path/to/openclaw-tui/run.sh
fi
EOF
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Quit |

## Terminal Compatibility

- **Graphical terminals** (xterm, GNOME Terminal, iTerm2, etc.): Full Unicode sparklines (▁▂▃▄▅▆▇█)
- **Linux TTY**: ASCII-safe sparklines (_.oO08@#) — auto-detected

## Layout

```
┌─────────────┬─────────────┬─────────────┐
│   Overview  │  Usage (5h) │    Costs    │
├─────────────┼─────────────┼─────────────┤
│  Sessions   │    Crons    │  Processes  │
├─────────────┼─────────────────────────────┤
│   Network   │         Live Feed          │
└─────────────┴─────────────────────────────┘
```

## Refresh Rates

| Data | Interval |
|------|----------|
| CPU, RAM, Network | 2s |
| Sessions, Processes, Live Feed | 10s |
| Usage, Costs, Crons, Health | 30s |

Data is cached to minimize file I/O and subprocess calls.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Credits

- Built for [OpenClaw](https://openclaw.dev)
- Uses [Rich](https://rich.readthedocs.io/) for terminal rendering
- Inspired by [openclaw-dashboard](https://github.com/tugcantopaloglu/openclaw-dashboard)

## Contributing

PRs welcome! Please open an issue first to discuss changes.
