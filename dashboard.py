#!/usr/bin/env python3
"""
OpenClaw TUI Dashboard
Real-time terminal monitoring for OpenClaw agents.

Features:
- System health (CPU, RAM, Disk, GPU)
- OpenClaw status and channels
- Session tracking with token counts
- 5-hour usage window and rate limits
- Cost tracking by model and day
- Live message feed
- Cron job status
- Top processes
- Network traffic with sparkline graphs
"""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static
from rich.text import Text
from rich.panel import Panel
from rich.console import Group

try:
    import pynvml
    pynvml.nvmlInit()
    HAS_NVIDIA = True
except Exception:
    HAS_NVIDIA = False

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

HOME = Path.home()
OPENCLAW_DIR = Path(os.environ.get('OPENCLAW_DIR', HOME / '.openclaw'))
WORKSPACE_DIR = Path(os.environ.get('WORKSPACE_DIR', os.environ.get('OPENCLAW_WORKSPACE', Path.cwd())))
AGENT_ID = os.environ.get('OPENCLAW_AGENT', 'main')
SESS_DIR = OPENCLAW_DIR / 'agents' / AGENT_ID / 'sessions'
CRON_FILE = OPENCLAW_DIR / 'cron' / 'jobs.json'

# ASCII sparklines for bare Linux TTY, Unicode for graphical terminals
_term = os.environ.get('TERM', '').lower()
ASCII_SPARKLINES = (_term == 'linux')


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def run_cmd(cmd: list[str], timeout: int = 5) -> tuple[bool, str]:
    try:
        env = os.environ.copy()
        npm_bin = os.path.expanduser("~/.npm-global/bin")
        if npm_bin not in env.get("PATH", ""):
            env["PATH"] = f"{npm_bin}:{env.get('PATH', '')}"
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return result.returncode == 0, result.stdout + result.stderr
    except:
        return False, ""


def format_bytes(b: float) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(b) < 1024.0:
            return f"{b:.1f}{unit}"
        b /= 1024.0
    return f"{b:.1f}PB"


def format_tokens(t: int) -> str:
    if t >= 1_000_000:
        return f"{t/1_000_000:.1f}M"
    if t >= 1_000:
        return f"{t/1_000:.1f}K"
    return str(t)


def format_cost(c: float) -> str:
    if c >= 1:
        return f"${c:.2f}"
    return f"${c:.3f}"


def format_ago(ts_ms: int) -> str:
    if not ts_ms:
        return "never"
    now = datetime.now().timestamp() * 1000
    diff_sec = (now - ts_ms) / 1000
    if diff_sec < 60:
        return "just now"
    if diff_sec < 3600:
        return f"{int(diff_sec/60)}m ago"
    if diff_sec < 86400:
        return f"{int(diff_sec/3600)}h ago"
    return f"{int(diff_sec/86400)}d ago"


def get_color(percent: float) -> str:
    if percent < 50:
        return "green"
    elif percent < 80:
        return "yellow"
    return "red"


def make_bar(percent: float, width: int = 30, label: str = "") -> Text:
    filled = int(width * min(percent, 100) / 100)
    empty = width - filled
    color = get_color(percent)
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * empty, style="bright_black")
    bar.append(f" {percent:.0f}%", style="bold " + color)
    if label:
        bar.append(f" {label}", style="dim")
    return bar


def make_sparkline(values: list[float], width: int = 30, color: str = "green", max_cap: float = 100, ascii_mode: bool = False) -> Text:
    """Create a sparkline from values. Newest on right, scrolls left."""
    if not values:
        char = "_" if ascii_mode else "▁"
        return Text(char * width, style="dim")
    
    # Sparkline characters (8 levels) - Unicode blocks or ASCII fallback
    if ascii_mode:
        chars = "_.oO08@#"  # ASCII-safe, all visible
    else:
        chars = "▁▂▃▄▅▆▇█"  # Unicode blocks
    
    # Take the most recent `width` values
    recent = values[-width:]
    
    # Normalize using absolute scale
    normalized = [min(v / max_cap, 1.0) for v in recent]
    
    # Pad on the LEFT with empty bars if we don't have enough history yet
    # This makes new values appear on the right and scroll left over time
    if len(normalized) < width:
        normalized = [0] * (width - len(normalized)) + normalized
    
    result = Text()
    for v in normalized:
        idx = min(int(v * 7.99), 7)  # 0-7 index into 8 chars
        result.append(chars[idx], style=color)
    
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Data Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_sessions() -> list[dict]:
    """Load sessions from sessions.json with cost/token data."""
    try:
        sess_file = SESS_DIR / 'sessions.json'
        if not sess_file.exists():
            return []
        data = json.loads(sess_file.read_text())
        sessions = []
        for key, s in data.items():
            # Resolve friendly name
            if ':main:main' in key:
                label = 'main'
            elif 'cron:' in key:
                label = s.get('label', 'cron-' + key.split('cron:')[1][:8])
            elif 'subagent' in key:
                label = 'sub-' + key.split(':')[-1][:8]
            else:
                label = s.get('label', key.split(':')[-1][:12])
            
            sessions.append({
                'key': key,
                'label': label,
                'model': (s.get('modelOverride') or s.get('model') or '-').split('/')[-1],
                'tokens': s.get('totalTokens', 0),
                'context': s.get('contextTokens', 0),
                'updated': s.get('updatedAt', 0),
                'channel': s.get('channel', '-'),
                'sessionId': s.get('sessionId', key),
            })
        sessions.sort(key=lambda x: x['updated'], reverse=True)
        return sessions
    except Exception:
        return []


def get_usage_5h() -> dict:
    """Get 5-hour rolling window usage stats."""
    try:
        now = datetime.now().timestamp() * 1000
        five_hours_ms = 5 * 3600 * 1000
        
        per_model = {}
        total_cost = 0
        total_calls = 0
        recent = []
        
        for f in SESS_DIR.glob('*.jsonl'):
            try:
                # Skip old files
                if f.stat().st_mtime * 1000 < now - five_hours_ms:
                    continue
                for line in f.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        if d.get('type') != 'message':
                            continue
                        msg = d.get('message', {})
                        ts_str = d.get('timestamp', '')
                        if not ts_str:
                            continue
                        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp() * 1000
                        if now - ts > five_hours_ms:
                            continue
                        
                        usage = msg.get('usage', {})
                        model = (msg.get('model', 'unknown')).split('/')[-1]
                        if 'delivery-mirror' in model:
                            continue
                        
                        in_tok = usage.get('input', 0) + usage.get('cacheRead', 0) + usage.get('cacheWrite', 0)
                        out_tok = usage.get('output', 0)
                        cost = usage.get('cost', {}).get('total', 0)
                        
                        if model not in per_model:
                            per_model[model] = {'input': 0, 'output': 0, 'cost': 0, 'calls': 0}
                        per_model[model]['input'] += in_tok
                        per_model[model]['output'] += out_tok
                        per_model[model]['cost'] += cost
                        per_model[model]['calls'] += 1
                        
                        total_cost += cost
                        total_calls += 1
                        
                        # Track recent for burn rate
                        recent.append({'ts': ts, 'output': out_tok, 'cost': cost})
                    except:
                        continue
            except:
                continue
        
        # Calculate burn rate (last 30 min)
        thirty_min_ago = now - 30 * 60 * 1000
        recent_30 = [r for r in recent if r['ts'] >= thirty_min_ago]
        burn_tokens = 0
        burn_cost = 0
        if recent_30:
            total_out = sum(r['output'] for r in recent_30)
            total_cost_30 = sum(r['cost'] for r in recent_30)
            span_ms = max(now - min(r['ts'] for r in recent_30), 60000)
            burn_tokens = total_out / (span_ms / 60000)
            burn_cost = total_cost_30 / (span_ms / 60000)
        
        # Estimated limits
        opus_limit = 88000
        sonnet_limit = 220000
        opus_out = sum(v['output'] for k, v in per_model.items() if 'opus' in k.lower())
        sonnet_out = sum(v['output'] for k, v in per_model.items() if 'sonnet' in k.lower())
        
        return {
            'per_model': per_model,
            'total_cost': total_cost,
            'total_calls': total_calls,
            'burn_tokens_per_min': burn_tokens,
            'burn_cost_per_min': burn_cost,
            'opus_out': opus_out,
            'opus_pct': (opus_out / opus_limit * 100) if opus_limit else 0,
            'sonnet_out': sonnet_out,
            'sonnet_pct': (sonnet_out / sonnet_limit * 100) if sonnet_limit else 0,
        }
    except Exception:
        return {'per_model': {}, 'total_cost': 0, 'total_calls': 0}


def get_costs() -> dict:
    """Get cost breakdown by day and model."""
    try:
        per_day = {}
        per_model = {}
        total = 0
        
        for f in SESS_DIR.glob('*.jsonl'):
            try:
                for line in f.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        if d.get('type') != 'message':
                            continue
                        msg = d.get('message', {})
                        cost = msg.get('usage', {}).get('cost', {}).get('total', 0)
                        if cost <= 0:
                            continue
                        
                        model = (msg.get('model', 'unknown')).split('/')[-1]
                        if 'delivery-mirror' in model:
                            continue
                        ts_str = d.get('timestamp', '')[:10]
                        
                        per_model[model] = per_model.get(model, 0) + cost
                        per_day[ts_str] = per_day.get(ts_str, 0) + cost
                        total += cost
                    except:
                        continue
            except:
                continue
        
        today = datetime.now().strftime('%Y-%m-%d')
        week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        week_cost = sum(c for d, c in per_day.items() if d >= week_ago)
        
        return {
            'total': total,
            'today': per_day.get(today, 0),
            'week': week_cost,
            'per_model': dict(sorted(per_model.items(), key=lambda x: -x[1])[:5]),
            'per_day': dict(sorted(per_day.items(), reverse=True)[:7]),
        }
    except:
        return {'total': 0, 'today': 0, 'week': 0, 'per_model': {}, 'per_day': {}}


def get_crons() -> list[dict]:
    """Load cron jobs."""
    try:
        if not CRON_FILE.exists():
            return []
        data = json.loads(CRON_FILE.read_text())
        jobs = []
        for j in data.get('jobs', []):
            schedule = j.get('schedule', {})
            sched_str = schedule.get('expr', schedule.get('every', '?'))
            jobs.append({
                'id': j.get('id', '')[:8],
                'name': j.get('name', j.get('id', '')[:8]),
                'schedule': sched_str,
                'enabled': j.get('enabled', True),
                'last_run': j.get('state', {}).get('lastRunAtMs', 0),
                'last_status': j.get('state', {}).get('lastStatus', 'unknown'),
            })
        return jobs
    except:
        return []


def get_live_messages(limit: int = 15) -> list[dict]:
    """Get recent messages from all sessions."""
    messages = []
    try:
        now = datetime.now().timestamp() * 1000
        one_hour_ms = 3600 * 1000
        
        for f in sorted(SESS_DIR.glob('*.jsonl'), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
            try:
                lines = f.read_text().splitlines()[-50:]  # Last 50 lines
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        if d.get('type') != 'message':
                            continue
                        msg = d.get('message', {})
                        role = msg.get('role', '')
                        if role not in ('user', 'assistant'):
                            continue
                        
                        ts_str = d.get('timestamp', '')
                        if not ts_str:
                            continue
                        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp() * 1000
                        if now - ts > one_hour_ms:
                            continue
                        
                        content = msg.get('content', '')
                        if isinstance(content, list):
                            for block in content:
                                if block.get('type') == 'text':
                                    content = block.get('text', '')
                                    break
                            else:
                                content = str(content[0]) if content else ''
                        
                        if isinstance(content, str):
                            content = content.replace('\n', ' ')[:150]
                        
                        messages.append({
                            'ts': ts,
                            'role': role,
                            'content': content,
                            'session': f.stem[:8],
                        })
                    except:
                        continue
            except:
                continue
        
        messages.sort(key=lambda x: x['ts'], reverse=True)
        return messages[:limit]
    except:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Panels
# ─────────────────────────────────────────────────────────────────────────────

class OverviewPanel(Static):
    """Main overview with system + OpenClaw health."""
    
    def on_mount(self) -> None:
        self.cpu_history: list[float] = []
        self.ram_history: list[float] = []
        self.max_history = 60  # 2 min at 2s intervals
        self.set_interval(2, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        lines = []
        
        # OpenClaw status
        ok, output = run_cmd(["openclaw", "health", "--json"], timeout=5)
        if ok:
            try:
                data = json.loads(output)
                if data.get("ok"):
                    lines.append(Text("● OPENCLAW ", style="bold green") + Text("ONLINE", style="green"))
                    channels = []
                    for name, info in data.get("channels", {}).items():
                        if info.get("configured") and info.get("probe", {}).get("ok"):
                            channels.append(name)
                    if channels:
                        lines.append(Text(f"  Channels: {', '.join(channels)}", style="cyan"))
                else:
                    lines.append(Text("● OPENCLAW DEGRADED", style="bold yellow"))
            except:
                lines.append(Text("● OPENCLAW ???", style="dim"))
        else:
            lines.append(Text("● OPENCLAW OFFLINE", style="bold red"))
        
        lines.append(Text())
        
        # System stats
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Track history
        self.cpu_history.append(cpu)
        self.ram_history.append(mem.percent)
        if len(self.cpu_history) > self.max_history:
            self.cpu_history = self.cpu_history[-self.max_history:]
        if len(self.ram_history) > self.max_history:
            self.ram_history = self.ram_history[-self.max_history:]
        
        lines.append(Text("SYSTEM", style="bold white"))
        
        # CPU with inline sparkline
        cpu_line = Text("  CPU ")
        cpu_line.append_text(make_sparkline(self.cpu_history, width=16, color=get_color(cpu), ascii_mode=ASCII_SPARKLINES))
        cpu_line.append(f" {cpu:3.0f}%", style="bold " + get_color(cpu))
        lines.append(cpu_line)
        
        # RAM with inline sparkline
        ram_line = Text("  RAM ")
        ram_line.append_text(make_sparkline(self.ram_history, width=16, color=get_color(mem.percent), ascii_mode=ASCII_SPARKLINES))
        ram_line.append(f" {mem.percent:3.0f}% {mem.used/1024**3:.1f}G", style="bold " + get_color(mem.percent))
        lines.append(ram_line)
        
        # Disk bar
        lines.append(Text("  DISK ") + make_bar(disk.percent, width=16, label=f"{disk.free/1024**3:.0f}G free"))
        
        # GPU if available
        if HAS_NVIDIA:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                mem_pct = (mem_info.used / mem_info.total) * 100
                lines.append(Text("  GPU  ") + make_bar(util.gpu, width=16))
                lines.append(Text("  VRAM ") + make_bar(mem_pct, width=16, label=f"{mem_info.used/1024**3:.1f}G/{mem_info.total/1024**3:.0f}G"))
            except:
                pass
        
        lines.append(Text())
        
        # Load average
        load1, load5, load15 = psutil.getloadavg()
        lines.append(Text(f"  Load: {load1:.2f} / {load5:.2f} / {load15:.2f}", style="dim"))
        
        # Uptime
        uptime_sec = int(datetime.now().timestamp() - psutil.boot_time())
        days, rem = divmod(uptime_sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        lines.append(Text(f"  Uptime: {days}d {hours}h {mins}m", style="dim"))
        
        return Panel(Group(*lines), title="[bold cyan]Overview[/bold cyan]", border_style="cyan")


class UsagePanel(Static):
    """5-hour rolling window usage."""
    
    def on_mount(self) -> None:
        self.set_interval(2, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        usage = get_usage_5h()
        lines = []
        
        lines.append(Text("5-HOUR WINDOW", style="bold yellow"))
        lines.append(Text())
        
        # Model breakdown
        for model, data in sorted(usage.get('per_model', {}).items(), key=lambda x: -x[1]['output'])[:4]:
            out = format_tokens(data['output'])
            cost = format_cost(data['cost'])
            lines.append(Text(f"  {model[:20]:<20} {out:>8} out  {cost:>8}", style="white"))
        
        if not usage.get('per_model'):
            lines.append(Text("  No activity", style="dim"))
        
        lines.append(Text())
        
        # Limits
        lines.append(Text("RATE LIMITS", style="bold"))
        lines.append(Text("  Opus   ") + make_bar(usage.get('opus_pct', 0), width=20, label=format_tokens(usage.get('opus_out', 0))))
        lines.append(Text("  Sonnet ") + make_bar(usage.get('sonnet_pct', 0), width=20, label=format_tokens(usage.get('sonnet_out', 0))))
        
        lines.append(Text())
        
        # Totals
        lines.append(Text(f"  Total Cost: {format_cost(usage.get('total_cost', 0))}", style="cyan"))
        lines.append(Text(f"  Total Calls: {usage.get('total_calls', 0)}", style="dim"))
        
        # Burn rate
        burn_tok = usage.get('burn_tokens_per_min', 0)
        burn_cost = usage.get('burn_cost_per_min', 0)
        if burn_tok > 0:
            lines.append(Text(f"  Burn: {format_tokens(int(burn_tok))}/min ({format_cost(burn_cost)}/min)", style="yellow"))
        
        return Panel(Group(*lines), title="[bold yellow]Usage[/bold yellow]", border_style="yellow")


class CostsPanel(Static):
    """Cost tracking."""
    
    def on_mount(self) -> None:
        self.set_interval(2, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        costs = get_costs()
        lines = []
        
        # Summary
        lines.append(Text("SPENDING", style="bold green"))
        lines.append(Text(f"  Today:    {format_cost(costs.get('today', 0))}", style="white"))
        lines.append(Text(f"  This Week: {format_cost(costs.get('week', 0))}", style="white"))
        lines.append(Text(f"  All Time:  {format_cost(costs.get('total', 0))}", style="cyan bold"))
        
        lines.append(Text())
        
        # By model
        lines.append(Text("BY MODEL", style="bold"))
        for model, cost in list(costs.get('per_model', {}).items())[:4]:
            lines.append(Text(f"  {model[:18]:<18} {format_cost(cost):>10}", style="white"))
        
        lines.append(Text())
        
        # By day (last 5)
        lines.append(Text("RECENT DAYS", style="bold"))
        for day, cost in list(costs.get('per_day', {}).items())[:5]:
            lines.append(Text(f"  {day}  {format_cost(cost):>10}", style="dim"))
        
        return Panel(Group(*lines), title="[bold green]Costs[/bold green]", border_style="green")


class SessionsPanel(Static):
    """Active sessions list."""
    
    def on_mount(self) -> None:
        self.set_interval(10, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        sessions = get_sessions()[:12]
        lines = []
        
        lines.append(Text("SESSIONS", style="bold magenta"))
        lines.append(Text())
        
        for s in sessions:
            ago = format_ago(s['updated'])
            tokens = format_tokens(s['tokens'])
            model = s['model'][:12]
            label = s['label'][:15]
            
            # Color based on recency
            if 'just now' in ago or 'm ago' in ago:
                style = "green"
            elif 'h ago' in ago:
                style = "yellow"
            else:
                style = "dim"
            
            lines.append(Text(f"  {label:<15} {model:<12} {tokens:>8} {ago:>10}", style=style))
        
        if not sessions:
            lines.append(Text("  No sessions", style="dim"))
        
        return Panel(Group(*lines), title="[bold magenta]Sessions[/bold magenta]", border_style="magenta")


class CronsPanel(Static):
    """Cron jobs status."""
    
    def on_mount(self) -> None:
        self.set_interval(30, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        crons = get_crons()[:8]
        lines = []
        
        lines.append(Text("CRON JOBS", style="bold blue"))
        lines.append(Text())
        
        for c in crons:
            status_icon = "●" if c['enabled'] else "○"
            status_color = "green" if c['enabled'] else "dim"
            last = format_ago(c['last_run'])
            name = c['name'][:28]
            
            line = Text()
            line.append(f"  {status_icon} ", style=status_color)
            line.append(f"{name:<28} ", style="white" if c['enabled'] else "dim")
            line.append(f"{last}", style="bright_black")
            lines.append(line)
        
        if not crons:
            lines.append(Text("  No cron jobs", style="dim"))
        
        return Panel(Group(*lines), title="[bold blue]Crons[/bold blue]", border_style="blue")


class LiveFeedPanel(Static):
    """Recent messages feed."""
    
    def on_mount(self) -> None:
        self.set_interval(5, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        messages = get_live_messages(10)
        lines = []
        
        lines.append(Text("LIVE FEED", style="bold red"))
        lines.append(Text())
        
        for m in messages:
            role = m['role'][:1].upper()
            content = m['content'][:120]  # More space now with 2 columns
            
            role_color = "cyan" if role == 'U' else "green"
            line = Text()
            line.append(f"  [{role}] ", style=role_color)
            line.append(f"{content}", style="white")
            lines.append(line)
        
        if not messages:
            lines.append(Text("  No recent messages", style="dim"))
        
        return Panel(Group(*lines), title="[bold red]Live Feed[/bold red]", border_style="red")


class TopProcessesPanel(Static):
    """Top processes by CPU usage."""
    
    def on_mount(self) -> None:
        self.set_interval(2, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        lines = []
        lines.append(Text("TOP PROCESSES", style="bold white"))
        lines.append(Text())
        
        # Get top processes by CPU
        procs = []
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                info = proc.info
                cpu = info.get('cpu_percent', 0) or 0
                mem = info.get('memory_percent', 0) or 0
                if cpu > 0 or mem > 1:
                    procs.append({
                        'name': info.get('name', '?')[:20],
                        'cpu': cpu,
                        'mem': mem,
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # Sort by CPU, take top 8
        procs.sort(key=lambda x: x['cpu'], reverse=True)
        
        for p in procs[:8]:
            cpu_color = "red" if p['cpu'] > 50 else ("yellow" if p['cpu'] > 20 else "white")
            line = Text()
            line.append(f"  {p['name']:<20} ", style="white")
            line.append(f"{p['cpu']:5.1f}%", style=cpu_color)
            line.append(f" {p['mem']:5.1f}%", style="dim")
            lines.append(line)
        
        if not procs:
            lines.append(Text("  No processes", style="dim"))
        
        # Header hint
        lines.append(Text())
        lines.append(Text("  NAME                  CPU   MEM", style="bright_black"))
        
        return Panel(Group(*lines), title="[bold white]Processes[/bold white]", border_style="white")


class NetworkPanel(Static):
    """Network stats with sparkline history."""
    
    def on_mount(self) -> None:
        self.net_last = {"sent": 0, "recv": 0, "time": 0}
        self.upload_history: list[float] = []
        self.download_history: list[float] = []
        self.max_history = 60  # 2 min at 2s intervals
        self.set_interval(2, self.refresh_stats)
    
    def refresh_stats(self) -> None:
        self.update(self.render_stats())
    
    def render_stats(self) -> Panel:
        net = psutil.net_io_counters()
        now = datetime.now().timestamp()
        
        if self.net_last["time"] > 0:
            dt = now - self.net_last["time"]
            if dt > 0:
                sent_rate = (net.bytes_sent - self.net_last["sent"]) / dt
                recv_rate = (net.bytes_recv - self.net_last["recv"]) / dt
            else:
                sent_rate = recv_rate = 0
        else:
            sent_rate = recv_rate = 0
        
        self.net_last = {"sent": net.bytes_sent, "recv": net.bytes_recv, "time": now}
        
        # Track history
        self.upload_history.append(sent_rate)
        self.download_history.append(recv_rate)
        if len(self.upload_history) > self.max_history:
            self.upload_history = self.upload_history[-self.max_history:]
        if len(self.download_history) > self.max_history:
            self.download_history = self.download_history[-self.max_history:]
        
        # Peak values
        peak_up = max(self.upload_history) if self.upload_history else 0
        peak_down = max(self.download_history) if self.download_history else 0
        
        lines = []
        lines.append(Text("NETWORK", style="bold yellow"))
        lines.append(Text())
        
        # Calculate scale - at least 1MB/s so small traffic shows as small
        min_scale = 1024 * 1024  # 1 MB/s minimum
        max_up = max(max(self.upload_history) if self.upload_history else 0, min_scale)
        max_down = max(max(self.download_history) if self.download_history else 0, min_scale)
        
        # Upload with inline sparkline
        up_line = Text("  ↑ ")
        up_line.append_text(make_sparkline(self.upload_history, width=20, color="red", max_cap=max_up, ascii_mode=ASCII_SPARKLINES))
        up_line.append(f" {format_bytes(sent_rate):>9}/s", style="bold red")
        lines.append(up_line)
        
        # Download with inline sparkline  
        down_line = Text("  ↓ ")
        down_line.append_text(make_sparkline(self.download_history, width=20, color="green", max_cap=max_down, ascii_mode=ASCII_SPARKLINES))
        down_line.append(f" {format_bytes(recv_rate):>9}/s", style="bold green")
        lines.append(down_line)
        
        lines.append(Text())
        lines.append(Text(f"  Peak  ↑ {format_bytes(peak_up)}/s  ↓ {format_bytes(peak_down)}/s", style="dim"))
        lines.append(Text(f"  Total ↑ {format_bytes(net.bytes_sent)}  ↓ {format_bytes(net.bytes_recv)}", style="dim"))
        lines.append(Text(f"  Scale: {format_bytes(max(max_up, max_down))}/s", style="bright_black"))
        
        return Panel(Group(*lines), title="[bold yellow]Network[/bold yellow]", border_style="yellow")


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

class OpenClawDashboard(App):
    """OpenClaw TUI Dashboard."""
    
    CSS = """
    Screen {
        layout: grid;
        grid-size: 3 3;
        grid-gutter: 1;
        padding: 1;
    }
    
    LiveFeedPanel {
        column-span: 2;
    }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]
    
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Row 1
        yield OverviewPanel()
        yield UsagePanel()
        yield CostsPanel()
        # Row 2
        yield SessionsPanel()
        yield CronsPanel()
        yield TopProcessesPanel()
        # Row 3
        yield NetworkPanel()
        yield LiveFeedPanel()  # spans 2 columns
        yield Footer()
    
    def action_refresh(self) -> None:
        for widget in self.query("OverviewPanel, UsagePanel, CostsPanel, SessionsPanel, CronsPanel, LiveFeedPanel, TopProcessesPanel, NetworkPanel"):
            if hasattr(widget, 'refresh_stats'):
                widget.refresh_stats()


def main():
    app = OpenClawDashboard()
    app.title = "OpenClaw Dashboard"
    app.run()


if __name__ == "__main__":
    main()
