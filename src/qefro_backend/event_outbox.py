"""Durable local outbox for Business Events (``ctx.emit()`` = persist, then deliver)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from typing import Any, Dict, List

MAX_FILES = 200


def default_outbox_dir(signing_secret: str) -> str:
    shard = hashlib.sha256(signing_secret.encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), "qefro-event-outbox", shard)


def _file_name_for(event_id: str) -> str:
    encoded = base64.urlsafe_b64encode(event_id.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded[:180]}.json"


class EventOutbox:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)

    def put(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("outbox.put requires event_id")
        os.makedirs(self.dir, exist_ok=True)
        dest = os.path.join(self.dir, _file_name_for(event_id))
        tmp = f"{dest}.tmp"
        from datetime import datetime, timezone

        body = json.dumps({**event, "queued_at": datetime.now(timezone.utc).isoformat()})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dest)
        self._prune()
        return event

    def pending(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict) and isinstance(raw.get("event_type"), str):
                    raw.pop("queued_at", None)
                    out.append(raw)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return out

    def ack(self, event_id: str) -> None:
        id_ = (event_id or "").strip()
        if not id_:
            return
        try:
            os.unlink(os.path.join(self.dir, _file_name_for(id_)))
        except OSError:
            pass

    def _prune(self) -> None:
        try:
            names = [n for n in os.listdir(self.dir) if n.endswith(".json")]
        except OSError:
            return
        if len(names) <= MAX_FILES:
            return
        names.sort(key=lambda n: os.path.getmtime(os.path.join(self.dir, n)))
        for name in names[: len(names) - MAX_FILES]:
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
