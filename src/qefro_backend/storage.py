"""Managed document storage via storage-service (ADR-002). Used only from app tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class StorageContext:
    def __init__(self, platform: Optional[Dict[str, Any]] = None):
        self._platform = platform if isinstance(platform, dict) else {}

    def _endpoint(self) -> tuple[str, str, Dict[str, Any]]:
        storage = self._platform.get("storage") if isinstance(self._platform, dict) else None
        if not isinstance(storage, dict):
            storage = {}
        from_env = (os.environ.get("QEFRO_STORAGE_URL") or "").rstrip("/")
        base_url = (storage.get("base_url") or from_env or "").rstrip("/")
        if not base_url:
            raise RuntimeError("ctx.storage requires platform.storage.base_url or QEFRO_STORAGE_URL")
        context = storage.get("context")
        if not isinstance(context, dict):
            raise RuntimeError("ctx.storage requires platform.storage.context on tool.invoke")
        token = (
            storage.get("token")
            or os.environ.get("QEFRO_SERVICE_TOKEN")
            or os.environ.get("QEFRO_INTERNAL_TOKEN")
            or ""
        )
        return base_url, token, context

    def _call(self, op: str, body: Dict[str, Any]) -> Dict[str, Any]:
        base_url, token, context = self._endpoint()
        payload = {**body, "context": context}
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"{base_url}/v1/internal/storage/{op}",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8")
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"storage.{op} failed ({err.code}): {text}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"storage.{op} failed: {err}") from err
        if status < 200 or status >= 300:
            raise RuntimeError(f"storage.{op} failed ({status}): {text}")
        if not text:
            return {}
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}

    async def insert(
        self,
        collection: str,
        document: Dict[str, Any],
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"collection": collection, "document": document}
        if options and options.get("allocate_code"):
            body["allocate_code"] = options["allocate_code"]
        return self._call("insert", body)

    async def find(
        self,
        collection: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        opts = options or {}
        body: Dict[str, Any] = {
            "collection": collection,
            "filter": opts.get("filter") or {},
        }
        if opts.get("limit") is not None:
            body["limit"] = opts["limit"]
        if opts.get("sort") is not None:
            body["sort"] = opts["sort"]
        out = self._call("find", body)
        items = out.get("items") if isinstance(out.get("items"), list) else []
        total = out.get("total") if isinstance(out.get("total"), int) else len(items)
        return {"items": items, "total": total}

    async def get(self, collection: str, id: str) -> Dict[str, Any]:
        return self._call("get", {"collection": collection, "id": id})

    async def update(self, collection: str, id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("update", {"collection": collection, "id": id, "patch": patch})

    async def delete(self, collection: str, id: str) -> Dict[str, Any]:
        return self._call("delete", {"collection": collection, "id": id})


def build_storage_context(platform: Optional[Dict[str, Any]] = None) -> StorageContext:
    return StorageContext(platform)


__all__ = ["StorageContext", "build_storage_context"]
