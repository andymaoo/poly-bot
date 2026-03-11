import asyncio
import json
import os
from typing import AsyncIterator, Dict, Any, List


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_PATH = os.path.join(PROJECT_ROOT, "events.jsonl")


async def tail_events(start_from_end: bool = True, poll_interval: float = 0.5) -> AsyncIterator[Dict[str, Any]]:
    """
    Async generator that tails events.jsonl and yields parsed JSON events as they arrive.

    This uses simple polling with seek to end on startup by default to avoid replaying history.
    """
    # Ensure file exists
    if not os.path.exists(EVENTS_PATH):
        open(EVENTS_PATH, "a", encoding="utf-8").close()

    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
        if start_from_end:
            f.seek(0, os.SEEK_END)

        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                f.seek(pos)
                await asyncio.sleep(poll_interval)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield event


def load_events(limit: int = 200, types: List[str] | None = None) -> List[Dict[str, Any]]:
    """
    Load the last `limit` events from events.jsonl, optionally filtered by type.
    """
    if not os.path.exists(EVENTS_PATH):
        return []

    events: List[Dict[str, Any]] = []
    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if types and event.get("type") not in types:
                continue
            events.append(event)

    return events[-limit:]

