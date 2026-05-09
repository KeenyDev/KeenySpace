from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


async def test_healthz_returns_200(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readyz_returns_200_or_503(client):
    resp = await client.get("/readyz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "checks" in body


async def test_metrics_returns_200_with_prometheus_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    content_type = resp.headers.get("content-type", "")
    assert "text/plain" in content_type
    body = resp.text
    assert "http_requests_total" in body or "keenyspace_" in body or "python_gc" in body


async def test_healthz_twice(client, capsys):
    for _ in range(2):
        resp = await client.get("/healthz")
        assert resp.status_code == 200
