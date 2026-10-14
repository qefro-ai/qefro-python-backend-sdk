"""Durable idempotency inbox for SDK tool invocations.

Provides effectively-once execution semantics by recording incoming request_id
in a durable file-based ledger. Replayed requests for completed operations return
the cached response without re-invoking downstream handlers.
Concurrent invocations return a concurrent conflict status.
Abandoned executions exceeding the crash recovery window return UNCERTAIN_OUTCOME
to fail closed and prevent unsafe duplicate mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import tempfile
from typing import Any, Dict, Literal, Optional

DEFAULT_CRASH_WINDOW_SECS: int = 300
DEFAULT_RETENTION_TTL_DAYS: int = 7


def is_production_env() -> bool:
    env = (
        os.environ.get("ENV")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("QEFRO_ENV")
        or os.environ.get("PYTHON_ENV")
        or ""
    ).lower()
    return env in ("production", "prod")


class PreMutationError(Exception):
    """Raised when failure occurred before any external mutation began.
    Informs the inbox to record 'failed_retryable' instead of 'uncertain_outcome'.
    """

    pass


def default_inbox_dir(signing_secret: str) -> str:
    env_dir = os.environ.get("QEFRO_IDEMPOTENCY_DIR", "").strip()
    if env_dir:
        return env_dir
    if is_production_env():
        raise RuntimeError(
            "Persistent idempotency directory required in production. "
            "Specify QEFRO_IDEMPOTENCY_DIR or config.idempotency_dir. "
            "Ephemeral temp storage is not permitted in production."
        )
    shard = hashlib.sha256(signing_secret.encode("utf-8")).hexdigest()[:8]
    return os.path.join(tempfile.gettempdir(), "qefro-idempotency-inbox", shard)


def _file_name_for(tool: str, request_id: str) -> str:
    safe_tool = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in tool)[:60]
    safe_req = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in request_id)[:120]
    return f"{safe_tool}__{safe_req}.json"


def _sanitize_tenant(tenant_id: Optional[str]) -> str:
    if not tenant_id:
        return "default"
    safe = "".join(c for c in tenant_id if (c.isalnum() or c in "-_"))
    return safe if safe else "default"


@dataclass
class InboxRecord:
    request_id: str
    tool: str
    status: str  # "executing", "completed", "failed", "uncertain_crash"
    created_at: str  # ISO 8601
    updated_at: str
    expires_at: str
    response: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "request_id": self.request_id,
            "tool": self.tool,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }
        if self.response is not None:
            d["response"] = self.response
        return d


@dataclass
class ClaimResult:
    status: Literal["claimed", "cached", "concurrent_execution", "uncertain_crash"]
    response: Optional[Any] = None
    record: Optional[InboxRecord] = None


class IdempotencyInbox:
    def __init__(
        self,
        directory: str,
        *,
        crash_window_secs: int = DEFAULT_CRASH_WINDOW_SECS,
        retention_days: int = DEFAULT_RETENTION_TTL_DAYS,
    ):
        self.dir = directory
        self.crash_window_secs = crash_window_secs
        self.retention_ttl = timedelta(days=retention_days)
        os.makedirs(self.dir, exist_ok=True)

    def record_path(self, tenant_id: Optional[str], tool: str, request_id: str) -> str:
        return os.path.join(self.dir, _sanitize_tenant(tenant_id), _file_name_for(tool, request_id))

    def claim(self, tenant_id: Optional[str], tool: str, request_id: str) -> ClaimResult:
        path = self.record_path(tenant_id, tool, request_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + self.retention_ttl).isoformat()

        record = InboxRecord(
            request_id=request_id,
            tool=tool,
            status="executing",
            created_at=now_iso,
            updated_at=now_iso,
            expires_at=expires_iso,
        )
        body = json.dumps(record.to_dict(), indent=2)

        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, body.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            return ClaimResult(status="claimed")
        except FileExistsError:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as err:
                raise RuntimeError(f"Corrupt idempotency record at {path}: {err}") from err

            status = raw.get("status")
            if status == "completed":
                return ClaimResult(
                    status="cached",
                    response=raw.get("response", {"status": "success"}),
                )
            if status == "executing":
                try:
                    created = datetime.fromisoformat(raw.get("created_at"))
                except Exception:
                    created = now
                age = (now - created).total_seconds()
                if age < self.crash_window_secs:
                    return ClaimResult(status="concurrent_execution")
                else:
                    self._update_status(tenant_id, tool, request_id, "uncertain_outcome")
                    rec = InboxRecord(
                        request_id=raw.get("request_id", request_id),
                        tool=raw.get("tool", tool),
                        status="uncertain_outcome",
                        created_at=raw.get("created_at", now_iso),
                        updated_at=raw.get("updated_at", now_iso),
                        expires_at=raw.get("expires_at", expires_iso),
                    )
                    return ClaimResult(status="uncertain_crash", record=rec)
            if status in ("uncertain_outcome", "uncertain_crash"):
                rec = InboxRecord(
                    request_id=raw.get("request_id", request_id),
                    tool=raw.get("tool", tool),
                    status="uncertain_outcome",
                    created_at=raw.get("created_at", now_iso),
                    updated_at=raw.get("updated_at", now_iso),
                    expires_at=raw.get("expires_at", expires_iso),
                )
                return ClaimResult(status="uncertain_crash", record=rec)
            if status == "failed_retryable":
                self._update_status(tenant_id, tool, request_id, "executing")
                return ClaimResult(status="claimed")
            return ClaimResult(status="claimed")

    def complete(self, tenant_id: Optional[str], tool: str, request_id: str, response: Any) -> None:
        self._update_status(tenant_id, tool, request_id, "completed", response=response)

    def fail_retryable(self, tenant_id: Optional[str], tool: str, request_id: str) -> None:
        self._update_status(tenant_id, tool, request_id, "failed_retryable")

    def fail_uncertain(self, tenant_id: Optional[str], tool: str, request_id: str) -> None:
        self._update_status(tenant_id, tool, request_id, "uncertain_outcome")

    def fail(self, tenant_id: Optional[str], tool: str, request_id: str) -> None:
        self.fail_uncertain(tenant_id, tool, request_id)

    def _update_status(
        self,
        tenant_id: Optional[str],
        tool: str,
        request_id: str,
        status: str,
        response: Optional[Any] = None,
    ) -> None:
        path = self.record_path(tenant_id, tool, request_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + self.retention_ttl).isoformat()

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = {
                    "request_id": request_id,
                    "tool": tool,
                    "created_at": now_iso,
                    "expires_at": expires_iso,
                }
        else:
            raw = {
                "request_id": request_id,
                "tool": tool,
                "created_at": now_iso,
                "expires_at": expires_iso,
            }

        raw["status"] = status
        raw["updated_at"] = now_iso
        if response is not None:
            raw["response"] = response

        tmp = f"{path}.tmp"
        body = json.dumps(raw, indent=2)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def prune(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        self._prune_dir(self.dir, now_iso)

    def _prune_dir(self, directory: str, now_iso: str) -> None:
        try:
            entries = os.listdir(directory)
        except OSError:
            return
        for entry in entries:
            full_path = os.path.join(directory, entry)
            try:
                if os.path.isdir(full_path):
                    self._prune_dir(full_path, now_iso)
                elif entry.endswith(".json"):
                    with open(full_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict) and raw.get("expires_at", "") <= now_iso:
                        os.unlink(full_path)
            except OSError:
                pass
