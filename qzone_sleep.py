import time


def sleep_seconds(sec: float):
    """Blocking sleep helper for tool_loop compatibility."""
    try:
        s = float(sec)
    except Exception:
        s = 0.0
    if s <= 0:
        return
    time.sleep(s)
