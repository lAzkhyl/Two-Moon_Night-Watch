# =====
# MODULE: utils/time.py
# =====
# Architecture Overview:
# Handles timezone-aware clock operations, uptime tracking, and converts
# raw timestamps into human-readable relative strings.
# =====

from datetime import datetime, timezone

BOT_START_TIME: datetime | None = None


def set_start_time():
    global BOT_START_TIME
    BOT_START_TIME = datetime.now(timezone.utc)


def format_uptime() -> str:
    # -----
    # Calculates seconds elapsed since launch and condenses it into 
    # a readable 'Nd Nh Nm Ns' string notation.
    # -----
    if BOT_START_TIME is None:
        return "unknown"
        
    delta = datetime.now(timezone.utc) - BOT_START_TIME
    total = int(delta.total_seconds())
    
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    
    return " ".join(parts)


def format_relative(dt: datetime) -> str:
    # -----
    # Converts absolute ISO timestamps into dynamic "X mins ago" strings.
    # Prevents discord natively mangling the date formats.
    # -----
    now   = datetime.now(timezone.utc)
    delta = now - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else now - dt
    total = int(delta.total_seconds())
    
    if total < 0:
        ahead = abs(total)
        if ahead < 3600:
            return f"in {ahead // 60}m"
        if ahead < 86400:
            return f"in {ahead // 3600}h"
        return f"in {ahead // 86400}d"
        
    if total < 60:
        return "just now"
    if total < 3600:
        return f"{total // 60}m ago"
    if total < 86400:
        return f"{total // 3600}h ago"
        
    return f"{total // 86400}d ago"