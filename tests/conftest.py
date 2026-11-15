from contextlib import contextmanager

import pytest

from app.db import _analyze, get_db, init_db
from app.main import app

app.config.TOUCHUP = False


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    init_db(str(path))
    return str(path)


@pytest.fixture()
def client(db):
    return app.test_client


@pytest.fixture()
def headers(client):
    _req, resp = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    assert cookie
    return {"Cookie": f"flow_session={cookie}"}
