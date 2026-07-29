# Basic Python SDK example

Minimal `qefro-backend` handler for Sync Tools / Test Connection.

## Install

```bash
pip install qefro-backend
```

This example imports the package directly; run it from a venv where the SDK is
installed (`pip install -e .` from the repo root during development).

## Run

```bash
export QEFRO_SIGNING_SECRET=dev-secret
python examples/basic/server.py   # listens on :8088/qefro
```

Then create an SDK Connection pointing at your public `/qefro` webhook (or
tunnel) with the same secret. `capabilities.list` advertises the `get_orders`
tool (`lookup: { required: ["email"] }`) and the `order_lookup` flow.
