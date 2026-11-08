"""Continuous keep-alive loop.

A background thread requests this service's own public /health URL every
PING_INTERVAL_SECONDS. Because the request goes out through Render's public
edge and comes back in, it counts as inbound traffic and resets the free-tier
idle timer (about 15 minutes).

Notes:
- The interval is clamped to a minimum of 1 second. Anything below ~14 minutes
  already keeps the service awake; very short intervals only burn bandwidth.
- If the service is already asleep, this loop is asleep too. Use the optional
  external pinger (GitHub Actions workflow or UptimeRobot) as a backup.
"""
import logging
import os
import threading
import time

import requests

log = logging.getLogger("jarvis.keepalive")
_started = False
_lock = threading.Lock()


def start_keepalive():
    global _started
    if os.getenv("KEEPALIVE_ENABLED", "true").lower() not in ("1", "true", "yes"):
        log.info("Keep-alive disabled")
        return None

    base = (os.getenv("SELF_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base:
        log.warning("Keep-alive off: set SELF_URL (RENDER_EXTERNAL_URL is automatic on Render)")
        return None

    with _lock:
        if _started:
            return None
        _started = True

    try:
        interval = max(1.0, float(os.getenv("PING_INTERVAL_SECONDS", "240")))
    except ValueError:
        interval = 240.0
    target = f"{base}/health"
    log_every = max(1, int(900 / interval))  # roughly one log line per 15 min

    def loop():
        http = requests.Session()
        count = 0
        failures = 0
        log.info("Keep-alive loop started: %s every %ss", target, interval)
        while True:
            ok = False
            try:
                r = http.get(target, timeout=20, headers={"User-Agent": "jarvis-keepalive"})
                ok = r.ok
            except Exception as exc:  # network hiccup, service restarting, etc.
                log.warning("Keep-alive ping failed: %s", exc)
            count += 1
            failures = 0 if ok else failures + 1
            if count % log_every == 0 or (not ok and failures == 1):
                log.info("Keep-alive ping #%d ok=%s", count, ok)
            time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="keepalive")
    t.start()
    return t
