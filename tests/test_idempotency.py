import os
import shutil
import tempfile
import time
import uuid
import pytest

from qefro_connector import (
    IdempotencyInbox,
    PreMutationError,
    Qefro,
    default_inbox_dir,
    is_production_env,
)


def test_inbox_claim_complete_cached():
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-test-inbox-{uuid.uuid4()}")
    inbox = IdempotencyInbox(test_dir)
    req_id = "req-101"

    # 1. First claim succeeds
    c1 = inbox.claim("tenant-1", "order_cancel", req_id)
    assert c1.status == "claimed"

    # 2. Concurrent claim while executing returns concurrent_execution
    c2 = inbox.claim("tenant-1", "order_cancel", req_id)
    assert c2.status == "concurrent_execution"

    # 3. Complete with response
    resp = {"order_id": "ORD-1", "canceled": True}
    inbox.complete("tenant-1", "order_cancel", req_id, resp)

    # 4. Cached replay returns cached response
    c3 = inbox.claim("tenant-1", "order_cancel", req_id)
    assert c3.status == "cached"
    assert c3.response == resp

    shutil.rmtree(test_dir, ignore_errors=True)


def test_inbox_crash_window_uncertain_crash():
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-test-inbox-{uuid.uuid4()}")
    # 0s crash window for immediate timeout
    inbox = IdempotencyInbox(test_dir, crash_window_secs=0)
    req_id = "req-crashed"

    c1 = inbox.claim("tenant-1", "charge_card", req_id)
    assert c1.status == "claimed"

    time.sleep(0.015)

    c2 = inbox.claim("tenant-1", "charge_card", req_id)
    assert c2.status == "uncertain_crash"
    assert c2.record is not None
    assert c2.record.request_id == req_id
    assert c2.record.tool == "charge_card"

    shutil.rmtree(test_dir, ignore_errors=True)


def test_inbox_tenant_isolation():
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-test-inbox-{uuid.uuid4()}")
    inbox = IdempotencyInbox(test_dir)
    req_id = "req-shared-id"

    # Tenant A claims and completes
    inbox.claim("tenant-a", "action_a", req_id)
    inbox.complete("tenant-a", "action_a", req_id, {"tenant": "a"})

    # Tenant B with same request_id does NOT collide
    cB = inbox.claim("tenant-b", "action_b", req_id)
    assert cB.status == "claimed"

    shutil.rmtree(test_dir, ignore_errors=True)


def test_inbox_fail_retryable_vs_fail_uncertain():
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-test-inbox-{uuid.uuid4()}")
    inbox = IdempotencyInbox(test_dir)

    # 1. fail_retryable test
    req_retry = "req-retry-1"
    inbox.claim("tenant-1", "order_cancel", req_retry)
    inbox.fail_retryable("tenant-1", "order_cancel", req_retry)
    claim_after_retryable = inbox.claim("tenant-1", "order_cancel", req_retry)
    assert claim_after_retryable.status == "claimed", "fail_retryable must allow re-claim"

    # 2. fail_uncertain test
    req_uncertain = "req-uncertain-1"
    inbox.claim("tenant-1", "order_cancel", req_uncertain)
    inbox.fail_uncertain("tenant-1", "order_cancel", req_uncertain)
    claim_after_uncertain = inbox.claim("tenant-1", "order_cancel", req_uncertain)
    assert claim_after_uncertain.status == "uncertain_crash", "fail_uncertain must block re-claim"

    shutil.rmtree(test_dir, ignore_errors=True)


def test_production_env_requires_persistent_dir():
    old_env = os.environ.get("ENV")
    old_dir = os.environ.get("QEFRO_IDEMPOTENCY_DIR")
    try:
        os.environ["ENV"] = "production"
        if "QEFRO_IDEMPOTENCY_DIR" in os.environ:
            del os.environ["QEFRO_IDEMPOTENCY_DIR"]

        assert is_production_env() is True

        with pytest.raises(RuntimeError, match="Persistent idempotency directory required in production"):
            default_inbox_dir("some-secret")
    finally:
        if old_env is not None:
            os.environ["ENV"] = old_env
        elif "ENV" in os.environ:
            del os.environ["ENV"]

        if old_dir is not None:
            os.environ["QEFRO_IDEMPOTENCY_DIR"] = old_dir
        elif "QEFRO_IDEMPOTENCY_DIR" in os.environ:
            del os.environ["QEFRO_IDEMPOTENCY_DIR"]


@pytest.mark.asyncio
async def test_qefro_tool_idempotency_cached_on_replay():
    secret = "test-secret"
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-app-inbox-{uuid.uuid4()}")
    app = Qefro(secret, idempotency_dir=test_dir)

    call_count = 0
    captured_ctx = {}

    @app.tool("charge_customer", idempotency_supported=True)
    async def charge_customer(ctx):
        nonlocal call_count, captured_ctx
        call_count += 1
        captured_ctx["request_id"] = ctx.request_id
        captured_ctx["idempotency_key"] = ctx.idempotency_key
        return {"status": "charged", "amount": ctx.parameters.get("amount")}

    @app.tool("default_tool")
    async def default_tool(ctx):
        return {"ok": True}

    # 1. capabilities.list advertises idempotency_supported correctly
    caps = await app.handle({
        "protocol_version": "1",
        "request_id": "req-cap",
        "type": "capabilities.list",
    })
    charge_meta = next((t for t in caps["tools"] if t["name"] == "charge_customer"), None)
    assert charge_meta is not None
    assert charge_meta.get("idempotency_supported") is True

    default_meta = next((t for t in caps["tools"] if t["name"] == "default_tool"), None)
    assert default_meta is not None
    assert default_meta.get("idempotency_supported") is False, "idempotency_supported MUST default to False"

    # 2. First invocation executes handler
    resp1 = await app.handle({
        "protocol_version": "1",
        "request_id": "tx-req-888",
        "type": "tool.invoke",
        "tool": "charge_customer",
        "parameters": {"amount": 75},
    })
    assert resp1["type"] == "result"
    assert resp1["output"]["status"] == "charged"
    assert resp1["output"]["amount"] == 75
    assert call_count == 1
    assert captured_ctx["request_id"] == "tx-req-888"

    # 3. Replay with same request_id returns cached result WITHOUT re-invoking handler
    resp2 = await app.handle({
        "protocol_version": "1",
        "request_id": "tx-req-888",
        "type": "tool.invoke",
        "tool": "charge_customer",
        "parameters": {"amount": 75},
    })
    assert resp2["type"] == "result"
    assert resp2["output"]["status"] == "charged"
    assert resp2["output"]["amount"] == 75
    assert call_count == 1, "Handler must NOT be re-executed on idempotency replay"

    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_handler_failure_is_uncertain_blocks_retry():
    secret = "test-secret"
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-app-inbox-{uuid.uuid4()}")
    app = Qefro(secret, idempotency_dir=test_dir)

    executions = 0

    @app.tool("pay_invoice", idempotency_supported=True)
    async def pay_invoice(ctx):
        nonlocal executions
        executions += 1
        raise RuntimeError("timeout contacting bank API after sending payment")

    # 1. First attempt fails
    r1 = await app.handle({
        "protocol_version": "1",
        "request_id": "req-invoice-1",
        "type": "tool.invoke",
        "tool": "pay_invoice",
        "parameters": {"invoice_id": "INV-1"},
    })
    assert r1["type"] == "error"
    assert executions == 1

    # 2. Second attempt with SAME request_id: blocked fail-closed with UNCERTAIN_OUTCOME!
    r2 = await app.handle({
        "protocol_version": "1",
        "request_id": "req-invoice-1",
        "type": "tool.invoke",
        "tool": "pay_invoice",
        "parameters": {"invoice_id": "INV-1"},
    })
    assert r2["type"] == "error"
    assert r2["code"] == "UNCERTAIN_OUTCOME"
    assert executions == 1, "Handler must NOT be executed a second time after uncertain failure"

    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_pre_mutation_failure_allows_retry():
    secret = "test-secret"
    test_dir = os.path.join(tempfile.gettempdir(), f"qefro-py-app-inbox-{uuid.uuid4()}")
    app = Qefro(secret, idempotency_dir=test_dir)

    attempts = 0

    @app.tool("validate_and_pay", idempotency_supported=True)
    async def validate_and_pay(ctx):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PreMutationError("Input validation failed before payment dispatch")
        return {"status": "paid"}

    # 1. First attempt fails before mutation
    r1 = await app.handle({
        "protocol_version": "1",
        "request_id": "req-pre-1",
        "type": "tool.invoke",
        "tool": "validate_and_pay",
        "parameters": {},
    })
    assert r1["type"] == "error"
    assert attempts == 1

    # 2. Second attempt with SAME request_id: re-claim succeeds and executes again!
    r2 = await app.handle({
        "protocol_version": "1",
        "request_id": "req-pre-1",
        "type": "tool.invoke",
        "tool": "validate_and_pay",
        "parameters": {},
    })
    assert r2["type"] == "result"
    assert r2["output"]["status"] == "paid"
    assert attempts == 2

    shutil.rmtree(test_dir, ignore_errors=True)
