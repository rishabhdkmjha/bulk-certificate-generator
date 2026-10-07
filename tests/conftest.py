from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def make_client(tmp_path):
    """Factory: make_client(generator=None, **settings_overrides) -> TestClient.

    Defaults to synchronous processing so assertions are deterministic.
    """
    created = []

    def _make(generator=None, **overrides):
        base = Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=tmp_path / "certs",
            sync_processing=True,
            max_recipients_per_job=50,
        )
        app = create_app(replace(base, **overrides), generator=generator)
        client = TestClient(app)
        created.append(app)
        return client

    yield _make
    for app in created:  # release SQLite file handles (matters on Windows)
        app.state.dispatcher.shutdown()
        app.state.engine.dispose()


@pytest.fixture
def client(make_client):
    return make_client()


def job_payload(recipients, **extra):
    return {"event_name": "PyCon India 2026", "issuer": "Python Society",
            "issue_date": "2026-10-07", "recipients": recipients, **extra}


GOOD = [
    {"name": "Asha Verma", "email": "asha@example.com"},
    {"name": "Rahul Singh", "email": "rahul@example.com", "achievement": "Excellence"},
    {"name": "Meera Iyer", "email": "meera@example.com"},
]
