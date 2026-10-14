import pytest


@pytest.fixture(autouse=True)
def clean_idempotency_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("QEFRO_IDEMPOTENCY_DIR", str(tmp_path / "inbox"))
