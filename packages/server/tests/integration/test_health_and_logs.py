from __future__ import annotations

import json

import pytest
import structlog
from keenyspace_server.observability.logging import configure_logging

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

    configure_logging("INFO")
    structlog.get_logger().info("test_marker_event", source="t-024")

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]

    parsed_records: list[dict[str, object]] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            parsed_records.append(rec)

    if not parsed_records:
        preview = "\n".join(lines[:5])
        raise AssertionError(
            f"No JSON-parsable records on stdout. First 5 captured lines:\n{preview}"
        )

    marker_records = [r for r in parsed_records if r.get("event") == "test_marker_event"]
    assert marker_records, (
        f"No record with event='test_marker_event' found. "
        f"Parsed events: {[r.get('event') for r in parsed_records]}"
    )

    rec = marker_records[0]
    missing = [k for k in ("event", "timestamp", "level") if k not in rec]
    assert not missing, (
        f"Structlog JSON record missing required keys: {missing}. "
        f"Record was: {rec}"
    )

    for key in ("event", "timestamp", "level"):
        value = rec[key]
        assert isinstance(value, str) and value, (
            f"Structlog key {key!r} must be non-empty str, got {value!r}"
        )
