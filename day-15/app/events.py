"""
A tiny event emitter that appends JSON lines to a file (demo only).
"""
import os
import json
from concurrent.futures import ThreadPoolExecutor

EVENTS_FILE = os.getenv("EVENTS_FILE", "/app/events/events.log")
_executor = ThreadPoolExecutor(max_workers=2)


def emit_event(event: dict):
    # write asynchronously to a file for demo inspection
    def _write(e):
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")
    _executor.submit(_write, event)
