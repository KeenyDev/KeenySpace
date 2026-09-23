from __future__ import annotations

import base64

import pytest


def test_encode_decode_roundtrip() -> None:
    from keenyspace_server.ws.cursor import decode_cursor, encode_cursor

    assert decode_cursor(encode_cursor({"o": 50})) == {"o": 50}


def test_encode_decode_mtime_cursor() -> None:
    from keenyspace_server.ws.cursor import decode_mtime_cursor, encode_mtime_cursor

    mtime_ns = 1234567890123456789
    path = "concepts/foo.md"
    result = decode_mtime_cursor(encode_mtime_cursor(mtime_ns, path))
    assert result == (mtime_ns, path)


def test_decode_cursor_invalid_base64_raises_value_error() -> None:
    from keenyspace_server.ws.cursor import decode_cursor

    with pytest.raises(ValueError):
        decode_cursor("not-base64-$$$")


def test_decode_cursor_invalid_json_raises_value_error() -> None:
    from keenyspace_server.ws.cursor import decode_cursor

    cursor = base64.urlsafe_b64encode(b"not json").decode()
    with pytest.raises(ValueError):
        decode_cursor(cursor)
