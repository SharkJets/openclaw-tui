#!/usr/bin/env python3
"""
OpenClaw TUI Dashboard
Real-time terminal monitoring for OpenClaw agents.
Uses Rich for rendering with minimal CPU overhead (~1%).

Features:
- System health (CPU, RAM, Disk, GPU with sparklines)
- OpenClaw status and channels
- 5-hour usage window with rate limits
- Cost tracking by model and day
- Session tracking
- Cron job status
- Live message feed
- Top processes
- Network traffic with sparklines
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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

# Refresh intervals (seconds)
FAST_INTERVAL = 2      # CPU, RAM, Network
MEDIUM_INTERVAL = 10   # Sessions, processes
SLOW_INTERVAL = 30     # Costs, usage, crons, health

# ASCII mode for Linux TTY (auto-detected)
_term = os.environ.get('TERM', '').lower()
ASCII_MODE = (_term == 'linux')

# State
console = Console()
last_net = {"sent": 0, "recv": 0, "time": 0}
cpu_history = []
ram_history = []
upload_history = []
download_history = []
MAX_HISTORY = 30

# Cached data (to avoid re-reading every loop)
cached_health = {"data": None, "time": 0}
cached_sessions = {"data": [], "time": 0}
cached_usage = {"data": {}, "time": 0}
cached_costs = {"data": {}, "time": 0}
cached_crons = {"data": [], "time": 0}
cached_messages = {"data": [], "time": 0}
cached_skills = {"data": [], "time": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

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
    return f"${c:.2f}" if c >= 1 else f"${c:.3f}"


def format_ago(ts_ms: int) -> str:
    if not ts_ms:
        return "never"
    diff_sec = (time.time() * 1000 - ts_ms) / 1000
    if diff_sec < 60:
        return "now"
    if diff_sec < 3600:
        return f"{int(diff_sec/60)}m"
    if diff_sec < 86400:
        return f"{int(diff_sec/3600)}h"
    return f"{int(diff_sec/86400)}d"


def get_color(pct: float) -> str:
    if pct < 50:
        return "green"
    if pct < 80:
        return "yellow"
    return "red"


def sparkline(values: list[float], width: int = 20, max_val: float = 100) -> str:
    if not values:
        return "▁" * width if not ASCII_MODE else "_" * width
    
    chars = "_.oO08@#" if ASCII_MODE else "▁▂▃▄▅▆▇█"
    recent = values[-width:]
    
    # Pad left
    if len(recent) < width:
        recent = [0] * (width - len(recent)) + recent
    
    result = ""
    for v in recent:
        idx = min(int((v / max_val) * 7.99), 7)
        result += chars[idx]
    return result


def bar(pct: float, width: int = 20) -> Text:
    filled = int(width * min(pct, 100) / 100)
    color = get_color(pct)
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="bright_black")
    t.append(f" {pct:.0f}%", style=f"bold {color}")
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Data fetchers (with caching)
# ─────────────────────────────────────────────────────────────────────────────

def get_health():
    now = time.time()
    if now - cached_health["time"] < SLOW_INTERVAL:
        return cached_health["data"]
    
    try:
        env = os.environ.copy()
        env["PATH"] = f"{HOME}/.npm-global/bin:{env.get('PATH', '')}"
        result = subprocess.run(
            ["openclaw", "health", "--json"],
            capture_output=True, text=True, timeout=5, env=env
        )
        if result.returncode == 0:
            cached_health["data"] = json.loads(result.stdout)
            cached_health["time"] = now
    except:
        pass
    return cached_health["data"]


def get_sessions():
    now = time.time()
    if now - cached_sessions["time"] < MEDIUM_INTERVAL:
        return cached_sessions["data"]
    
    try:
        sess_file = SESS_DIR / 'sessions.json'
        if sess_file.exists():
            data = json.loads(sess_file.read_text())
            sessions = []
            seen_labels = set()
            
            for key, s in data.items():
                # Determine label
                if ':main:main' in key:
                    label = 'main'
                elif 'cron:' in key:
                    label = s.get('label', s.get('name', 'cron'))
                    # Clean up cron labels
                    if label.startswith('Cron: '):
                        label = label[6:]
                elif 'subagent' in key:
                    label = s.get('label', 'subagent')
                else:
                    label = s.get('label', key.split(':')[-1])
                
                # Truncate and clean label
                label = label[:28]
                
                # Skip duplicates (keep most recent)
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                
                # Clean model name
                model = (s.get('model') or '-').split('/')[-1]
                # Shorten common model names
                model = model.replace('claude-', '').replace('-20250', '')[:10]
                
                sessions.append({
                    'label': label,
                    'model': model,
                    'tokens': s.get('totalTokens', 0),
                    'updated': s.get('updatedAt', 0),
                })
            
            sessions.sort(key=lambda x: x['updated'], reverse=True)
            cached_sessions["data"] = sessions[:10]
            cached_sessions["time"] = now
    except:
        pass
    return cached_sessions["data"]


def get_usage():
    now = time.time()
    if now - cached_usage["time"] < SLOW_INTERVAL:
        return cached_usage["data"]
    
    try:
        now_ms = now * 1000
        five_hours_ms = 5 * 3600 * 1000
        per_model = {}
        total_cost = 0
        total_calls = 0
        recent = []
        
        for f in SESS_DIR.glob('*.jsonl'):
            if f.stat().st_mtime * 1000 < now_ms - five_hours_ms:
                continue
            for line in f.read_text().splitlines()[-100:]:
                try:
                    d = json.loads(line)
                    if d.get('type') != 'message':
                        continue
                    msg = d.get('message', {})
                    ts = d.get('timestamp', '')
                    if not ts:
                        continue
                    ts_ms = datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000
                    if now_ms - ts_ms > five_hours_ms:
                        continue
                    
                    usage = msg.get('usage', {})
                    model = msg.get('model', 'unknown').split('/')[-1]
                    if 'delivery' in model:
                        continue
                    
                    out_tok = usage.get('output', 0)
                    cost = usage.get('cost', {}).get('total', 0)
                    
                    if model not in per_model:
                        per_model[model] = {'output': 0, 'cost': 0, 'calls': 0}
                    per_model[model]['output'] += out_tok
                    per_model[model]['cost'] += cost
                    per_model[model]['calls'] += 1
                    total_cost += cost
                    total_calls += 1
                    recent.append({'ts': ts_ms, 'output': out_tok, 'cost': cost})
                except:
                    continue
        
        # Burn rate (last 30 min)
        thirty_min_ago = now_ms - 30 * 60 * 1000
        recent_30 = [r for r in recent if r['ts'] >= thirty_min_ago]
        burn_tokens = burn_cost = 0
        if recent_30:
            span_ms = max(now_ms - min(r['ts'] for r in recent_30), 60000)
            burn_tokens = sum(r['output'] for r in recent_30) / (span_ms / 60000)
            burn_cost = sum(r['cost'] for r in recent_30) / (span_ms / 60000)
        
        # Rate limits
        opus_out = sum(v['output'] for k, v in per_model.items() if 'opus' in k.lower())
        sonnet_out = sum(v['output'] for k, v in per_model.items() if 'sonnet' in k.lower())
        
        cached_usage["data"] = {
            'per_model': per_model,
            'total_cost': total_cost,
            'total_calls': total_calls,
            'burn_tokens': burn_tokens,
            'burn_cost': burn_cost,
            'opus_out': opus_out,
            'opus_pct': (opus_out / 88000 * 100),
            'sonnet_out': sonnet_out,
            'sonnet_pct': (sonnet_out / 220000 * 100),
        }
        cached_usage["time"] = now
    except:
        pass
    return cached_usage["data"]


def get_costs():
    now = time.time()
    if now - cached_costs["time"] < SLOW_INTERVAL:
        return cached_costs["data"]
    
    try:
        per_day = {}
        per_model = {}
        total = 0
        today = datetime.now().strftime('%Y-%m-%d')
        week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        for f in SESS_DIR.glob('*.jsonl'):
            for line in f.read_text().splitlines():
                try:
                    d = json.loads(line)
                    if d.get('type') != 'message':
                        continue
                    msg = d.get('message', {})
                    cost = msg.get('usage', {}).get('cost', {}).get('total', 0)
                    if cost <= 0:
                        continue
                    day = d.get('timestamp', '')[:10]
                    model = msg.get('model', 'unknown').split('/')[-1]
                    if 'delivery' in model:
                        continue
                    per_day[day] = per_day.get(day, 0) + cost
                    per_model[model] = per_model.get(model, 0) + cost
                    total += cost
                except:
                    continue
        
        week_cost = sum(c for d, c in per_day.items() if d >= week_ago)
        
        cached_costs["data"] = {
            'total': total,
            'today': per_day.get(today, 0),
            'week': week_cost,
            'per_day': dict(sorted(per_day.items(), reverse=True)[:5]),
            'per_model': dict(sorted(per_model.items(), key=lambda x: -x[1])[:4]),
        }
        cached_costs["time"] = now
    except:
        pass
    return cached_costs["data"]


def get_crons():
    now = time.time()
    if now - cached_crons["time"] < SLOW_INTERVAL:
        return cached_crons["data"]
    
    try:
        if CRON_FILE.exists():
            data = json.loads(CRON_FILE.read_text())
            jobs = []
            for j in data.get('jobs', []):
                jobs.append({
                    'name': j.get('name', j.get('id', '')[:8])[:25],
                    'enabled': j.get('enabled', True),
                    'last_run': j.get('state', {}).get('lastRunAtMs', 0),
                })
            cached_crons["data"] = jobs[:6]
            cached_crons["time"] = now
    except:
        pass
    return cached_crons["data"]


def get_skills():
    now = time.time()
    if now - cached_skills["time"] < SLOW_INTERVAL:
        return cached_skills["data"]
    
    try:
        skills = []
        
        # Check workspace skills directory
        workspace_skills = WORKSPACE_DIR / 'skills'
        if workspace_skills.exists():
            for skill_dir in workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / 'SKILL.md'
                    if skill_file.exists():
                        skills.append({
                            'name': skill_dir.name,
                            'source': 'workspace',
                        })
        
        # Check ~/.openclaw/skills
        openclaw_skills = OPENCLAW_DIR / 'skills'
        if openclaw_skills.exists():
            for skill_dir in openclaw_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / 'SKILL.md'
                    if skill_file.exists():
                        skills.append({
                            'name': skill_dir.name,
                            'source': 'openclaw',
                        })
        
        # Check bundled skills (npm global)
        npm_skills = HOME / '.npm-global' / 'lib' / 'node_modules' / 'openclaw' / 'skills'
        if npm_skills.exists():
            for skill_dir in npm_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / 'SKILL.md'
                    if skill_file.exists():
                        # Don't duplicate if already found
                        if not any(s['name'] == skill_dir.name for s in skills):
                            skills.append({
                                'name': skill_dir.name,
                                'source': 'bundled',
                            })
        
        skills.sort(key=lambda x: x['name'])
        cached_skills["data"] = skills
        cached_skills["time"] = now
    except:
        pass
    return cached_skills["data"]


def get_messages():
    now = time.time()
    if now - cached_messages["time"] < MEDIUM_INTERVAL:
        return cached_messages["data"]
    
    try:
        messages = []
        now_ms = now * 1000
        
        for f in sorted(SESS_DIR.glob('*.jsonl'), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            for line in f.read_text().splitlines()[-20:]:
                try:
                    d = json.loads(line)
                    if d.get('type') != 'message':
                        continue
                    msg = d.get('message', {})
                    role = msg.get('role', '')
                    if role not in ('user', 'assistant'):
                        continue
                    
                    ts = d.get('timestamp', '')
                    ts_ms = datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000
                    if now_ms - ts_ms > 3600000:
                        continue
                    
                    content = msg.get('content', '')
                    if isinstance(content, list):
                        content = next((b.get('text', '') for b in content if b.get('type') == 'text'), '')
                    
                    messages.append({
                        'ts': ts_ms,
                        'role': role[0].upper(),
                        'text': str(content).replace('\n', ' ')[:120]
                    })
                except:
                    continue
        
        messages.sort(key=lambda x: x['ts'])  # Oldest first, newest at bottom
        cached_messages["data"] = messages[-8:]  # Keep last 8
        cached_messages["time"] = now
    except:
        pass
    return cached_messages["data"]


# ─────────────────────────────────────────────────────────────────────────────
# Panels
# ─────────────────────────────────────────────────────────────────────────────

def make_overview() -> Panel:
    lines = []
    
    # Health
    health = get_health()
    if health and health.get('ok'):
        channels = [n for n, i in health.get('channels', {}).items() 
                   if i.get('configured') and i.get('probe', {}).get('ok')]
        lines.append(Text("● OPENCLAW ONLINE", style="bold green"))
        if channels:
            lines.append(Text(f"  {', '.join(channels)}", style="cyan"))
    else:
        lines.append(Text("● OPENCLAW OFFLINE", style="bold red"))
    
    lines.append(Text())
    
    # System
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    cpu_history.append(cpu)
    ram_history.append(mem.percent)
    while len(cpu_history) > MAX_HISTORY:
        cpu_history.pop(0)
    while len(ram_history) > MAX_HISTORY:
        ram_history.pop(0)
    
    lines.append(Text(f"CPU  {sparkline(cpu_history, 12)} {cpu:3.0f}%", style=get_color(cpu)))
    lines.append(Text(f"RAM  {sparkline(ram_history, 12)} {mem.percent:3.0f}% {mem.used/1024**3:.1f}G", style=get_color(mem.percent)))
    lines.append(Text(f"DISK ") + bar(disk.percent, 10) + Text(f" {disk.free/1024**3:.0f}G free", style="dim"))
    
    if HAS_NVIDIA:
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            gpu = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            vram = pynvml.nvmlDeviceGetMemoryInfo(h)
            vram_pct = (vram.used / vram.total) * 100
            lines.append(Text(f"GPU  ") + bar(gpu, 10))
            lines.append(Text(f"VRAM ") + bar(vram_pct, 10) + Text(f" {vram.used/1024**3:.1f}G/{vram.total/1024**3:.0f}G", style="dim"))
        except:
            pass
    
    load = psutil.getloadavg()
    uptime_sec = int(time.time() - psutil.boot_time())
    days, rem = divmod(uptime_sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    lines.append(Text(f"\nLoad: {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}", style="dim"))
    lines.append(Text(f"Uptime: {days}d {hours}h {mins}m", style="dim"))
    
    return Panel("\n".join(str(l) for l in lines), title="Overview", border_style="cyan")


def make_usage() -> Panel:
    usage = get_usage()
    lines = []
    
    # Per model
    for model, data in sorted(usage.get('per_model', {}).items(), key=lambda x: -x[1]['output'])[:3]:
        lines.append(f"{model[:18]:<18} {format_tokens(data['output']):>6} {format_cost(data['cost']):>7}")
    
    if not usage.get('per_model'):
        lines.append("No activity")
    
    lines.append("")
    
    # Rate limits
    opus_pct = usage.get('opus_pct', 0)
    sonnet_pct = usage.get('sonnet_pct', 0)
    lines.append(f"Opus   {bar(opus_pct, 10)} {format_tokens(usage.get('opus_out', 0))}")
    lines.append(f"Sonnet {bar(sonnet_pct, 10)} {format_tokens(usage.get('sonnet_out', 0))}")
    
    lines.append("")
    lines.append(f"Cost: {format_cost(usage.get('total_cost', 0))}  Calls: {usage.get('total_calls', 0)}")
    
    # Burn rate
    burn_tok = usage.get('burn_tokens', 0)
    burn_cost = usage.get('burn_cost', 0)
    if burn_tok > 0:
        lines.append(f"Burn: {format_tokens(int(burn_tok))}/min ({format_cost(burn_cost)}/min)")
    
    return Panel("\n".join(str(l) for l in lines), title="Usage (5h)", border_style="yellow")


def make_costs() -> Panel:
    costs = get_costs()
    lines = [
        f"Today:     {format_cost(costs.get('today', 0))}",
        f"This Week: {format_cost(costs.get('week', 0))}",
        f"All Time:  {format_cost(costs.get('total', 0))}",
        ""
    ]
    
    # By model
    for model, cost in list(costs.get('per_model', {}).items())[:3]:
        lines.append(f"  {model[:14]:<14} {format_cost(cost):>8}")
    
    lines.append("")
    
    # Recent days
    for day, cost in list(costs.get('per_day', {}).items())[:3]:
        lines.append(f"  {day} {format_cost(cost):>8}")
    
    return Panel("\n".join(lines), title="Costs", border_style="green")


def make_sessions() -> Panel:
    sessions = get_sessions()
    lines = []
    
    # Header
    lines.append("[dim]SESSION                      MODEL      TOKENS  AGO[/]")
    lines.append("")
    
    for s in sessions:
        ago = format_ago(s['updated'])
        tokens = format_tokens(s['tokens'])
        
        # Color based on recency
        if ago == 'now' or 'm' in ago:
            style = "green"
        elif 'h' in ago and int(ago.replace('h', '')) < 6:
            style = "yellow"
        else:
            style = "dim"
        
        lines.append(f"[{style}]{s['label']:<28} {s['model']:<10} {tokens:>6}  {ago:>4}[/]")
    
    if not sessions:
        lines.append("  No sessions")
    
    return Panel("\n".join(lines), title="Sessions", border_style="magenta")


def make_crons() -> Panel:
    crons = get_crons()
    lines = []
    
    for c in crons:
        icon = "●" if c['enabled'] else "○"
        color = "green" if c['enabled'] else "dim"
        lines.append(f"  [{color}]{icon}[/] {c['name']:<20} {format_ago(c['last_run']):>4}")
    
    if not crons:
        lines.append("  No crons")
    
    return Panel("\n".join(lines), title="Crons", border_style="blue")


def make_processes() -> Panel:
    lines = []
    procs = []
    
    for p in psutil.process_iter(['name', 'cpu_percent', 'memory_percent']):
        try:
            cpu = p.info.get('cpu_percent', 0) or 0
            mem = p.info.get('memory_percent', 0) or 0
            if cpu > 0.1 or mem > 1:
                procs.append((p.info.get('name', '?')[:16], cpu, mem))
        except:
            pass
    
    for name, cpu, mem in sorted(procs, key=lambda x: -x[1])[:6]:
        color = "red" if cpu > 50 else ("yellow" if cpu > 20 else "white")
        lines.append(f"[{color}]{name:<16} {cpu:5.1f}% {mem:5.1f}%[/]")
    
    if not lines:
        lines.append("  Idle")
    
    lines.append("")
    lines.append("[dim]NAME             CPU   MEM[/]")
    
    return Panel("\n".join(lines), title="Processes", border_style="white")


def make_network() -> Panel:
    global last_net
    
    net = psutil.net_io_counters()
    now = time.time()
    
    if last_net["time"] > 0:
        dt = now - last_net["time"]
        up_rate = (net.bytes_sent - last_net["sent"]) / dt if dt > 0 else 0
        dn_rate = (net.bytes_recv - last_net["recv"]) / dt if dt > 0 else 0
    else:
        up_rate = dn_rate = 0
    
    last_net = {"sent": net.bytes_sent, "recv": net.bytes_recv, "time": now}
    
    upload_history.append(up_rate)
    download_history.append(dn_rate)
    while len(upload_history) > MAX_HISTORY:
        upload_history.pop(0)
    while len(download_history) > MAX_HISTORY:
        download_history.pop(0)
    
    peak_up = max(upload_history) if upload_history else 0
    peak_dn = max(download_history) if download_history else 0
    max_scale = max(peak_up, peak_dn, 1024*1024)
    
    lines = [
        f"[red]↑ {sparkline(upload_history, 18, max_scale)} {format_bytes(up_rate):>9}/s[/]",
        f"[green]↓ {sparkline(download_history, 18, max_scale)} {format_bytes(dn_rate):>9}/s[/]",
        "",
        f"[dim]Peak  ↑ {format_bytes(peak_up):>8}/s  ↓ {format_bytes(peak_dn):>8}/s[/]",
        f"[dim]Total ↑ {format_bytes(net.bytes_sent):>8}  ↓ {format_bytes(net.bytes_recv):>8}[/]",
        f"[dim]Scale: {format_bytes(max_scale)}/s[/]"
    ]
    
    return Panel("\n".join(lines), title="Network", border_style="yellow")


def make_feed() -> Panel:
    messages = get_messages()
    lines = []
    
    for m in messages:
        color = "cyan" if m['role'] == 'U' else "green"
        lines.append(f"[{color}][{m['role']}][/] {m['text'][:100]}")
    
    if not messages:
        lines.append("  No recent messages")
    
    return Panel("\n".join(lines), title="Live Feed", border_style="red")


def make_skills() -> Panel:
    skills = get_skills()
    lines = []
    
    # Group by source
    workspace = [s for s in skills if s['source'] == 'workspace']
    openclaw = [s for s in skills if s['source'] == 'openclaw']
    bundled = [s for s in skills if s['source'] == 'bundled']
    
    if workspace:
        lines.append("[yellow]workspace[/]")
        for s in workspace:
            lines.append(f"  [green]●[/] {s['name']}")
    
    if openclaw:
        if lines:
            lines.append("")
        lines.append("[cyan]~/.openclaw[/]")
        for s in openclaw:
            lines.append(f"  [green]●[/] {s['name']}")
    
    if bundled:
        if lines:
            lines.append("")
        lines.append("[dim]bundled[/]")
        for s in bundled[:6]:  # Limit bundled to 6
            lines.append(f"  [dim]●[/] {s['name']}")
        if len(bundled) > 6:
            lines.append(f"  [dim]... +{len(bundled) - 6} more[/]")
    
    if not skills:
        lines.append("  No skills loaded")
    
    return Panel("\n".join(lines), title=f"Skills ({len(skills)})", border_style="magenta")


def make_layout() -> Layout:
    layout = Layout()
    
    layout.split_column(
        Layout(name="row0", size=14),
        Layout(name="row1", size=12),
        Layout(name="row2", size=10),
        Layout(name="row3", size=10),
    )
    
    # Row 0: Overview | Usage | Costs
    layout["row0"].split_row(
        Layout(make_overview(), name="overview"),
        Layout(make_usage(), name="usage"),
        Layout(make_costs(), name="costs"),
    )
    
    # Row 1: Network | Crons | Processes
    layout["row1"].split_row(
        Layout(make_network(), name="network"),
        Layout(make_crons(), name="crons"),
        Layout(make_processes(), name="processes"),
    )
    
    # Row 2: Skills | Live Feed (2 cols)
    layout["row2"].split_row(
        Layout(make_skills(), name="skills", ratio=1),
        Layout(make_feed(), name="feed", ratio=2),
    )
    
    # Row 3: Empty | Sessions (2 cols)
    layout["row3"].split_row(
        Layout(Panel("", border_style="dim"), ratio=1),
        Layout(make_sessions(), name="sessions", ratio=2),
    )
    
    return layout


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Handle Ctrl+C gracefully
    def sigint_handler(sig, frame):
        sys.exit(0)
    signal.signal(signal.SIGINT, sigint_handler)
    
    console.clear()
    
    with Live(make_layout(), console=console, refresh_per_second=0.5, screen=True) as live:
        while True:
            time.sleep(FAST_INTERVAL)
            live.update(make_layout())


if __name__ == "__main__":
    main()
