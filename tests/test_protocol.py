import pytest

from meechat_matrix.protocol import ProtocolError, dumps, loads


def test_dumps_round_trips_message():
    encoded = dumps({"type": "message", "body": "hello"})

    assert encoded.endswith("\n")
    assert loads(encoded) == {"type": "message", "body": "hello"}


def test_loads_rejects_empty_line():
    with pytest.raises(ProtocolError, match="empty"):
        loads("\n")


def test_loads_rejects_non_object():
    with pytest.raises(ProtocolError, match="object"):
        loads("[]")


def test_loads_rejects_missing_type():
    with pytest.raises(ProtocolError, match="type"):
        loads("{}")


def test_dumps_rejects_missing_type():
    with pytest.raises(ProtocolError, match="type"):
        dumps({"body": "hello"})
