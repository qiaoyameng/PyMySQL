"""Unit tests for Connection.reset_session() with a fake server."""

import io
import struct

import pytest

import pymysql
from pymysql.constants import COMMAND, ER, FIELD_TYPE, SERVER_STATUS
from pymysql.cursors import SSCursor

AUTOCOMMIT = SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT
MORE_RESULTS = SERVER_STATUS.SERVER_MORE_RESULTS_EXISTS


def _frame(payload, seq):
    return struct.pack("<I", len(payload))[:3] + bytes([seq]) + payload


def _ok(status=AUTOCOMMIT):
    return b"\x00\x00\x00" + struct.pack("<HH", status, 0)


def _err(errno=ER.UNKNOWN_ERROR, message=b"reset failed"):
    return b"\xff" + struct.pack("<H", errno) + b"#HY000" + message


def _eof(status=AUTOCOMMIT):
    return b"\xfe" + struct.pack("<HH", 0, status)


def _lenenc_str(s):
    return bytes([len(s)]) + s


def _field(name=b"col"):
    return (
        _lenenc_str(b"def")
        + _lenenc_str(b"")
        + _lenenc_str(b"")
        + _lenenc_str(b"")
        + _lenenc_str(name)
        + _lenenc_str(b"")
        + b"\x0c"
        + struct.pack("<HIBHB", 33, 1024, FIELD_TYPE.VAR_STRING, 0, 0)
        + b"\x00\x00"
    )


def _row(*values):
    return b"".join(_lenenc_str(v) for v in values)


def _result_set(rows, status=AUTOCOMMIT, seq_start=1):
    """Frame a one-column result set; returns (bytes, next_seq)."""
    data = b""
    seq = seq_start
    for payload in [b"\x01", _field(), _eof(status)] + list(rows) + [_eof(status)]:
        data += _frame(payload, seq)
        seq = (seq + 1) % 256
    return data, seq


class FakeSocket:
    """A socket stub replaying a canned response stream."""

    def __init__(self, incoming=b""):
        self._incoming = io.BytesIO(incoming)
        self.written = b""
        self.closed = False

    def settimeout(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.written += data

    def makefile(self, mode):
        return self._incoming

    def close(self):
        self.closed = True


def _make_connection(incoming=b"", **kwargs):
    kwargs.setdefault("user", "fake-user")
    conn = pymysql.connect(defer_connect=True, **kwargs)
    sock = FakeSocket(incoming)
    conn._sock = sock
    conn._rfile = sock.makefile("rb")
    conn._next_seq_id = 0
    conn._current_timeout = None
    conn._closed = False
    conn.server_status = AUTOCOMMIT
    return conn, sock


def _sent_packets(sock):
    """Split the written byte stream into (seq, command, sql) tuples."""
    packets = []
    data = sock.written
    while data:
        size = struct.unpack("<I", data[:3] + b"\x00")[0]
        seq = data[3]
        payload = data[4 : 4 + size]
        packets.append((seq, payload[0], payload[1:]))
        data = data[4 + size :]
    return packets


def _reset_ok_stream():
    """Responses for COM_RESET_CONNECTION + SET NAMES + init + SET AUTOCOMMIT."""
    return b"".join(_frame(_ok(), 1) for _ in range(4))


class TestResetSession:
    def test_reset_with_unread_sscursor_result(self):
        incoming, _ = _result_set([_row(b"1"), _row(b"2")])
        incoming += _reset_ok_stream()
        conn, sock = _make_connection(incoming, init_command="SET @x = 1")

        cur = conn.cursor(SSCursor)
        cur.execute("SELECT x FROM t")
        # The result is left unread; reset_session must drain it first.
        assert conn._result.unbuffered_active

        with pytest.warns(UserWarning, match="unbuffered result"):
            conn.reset_session()

        packets = _sent_packets(sock)
        commands = [cmd for _, cmd, _ in packets]
        assert commands == [
            COMMAND.COM_QUERY,
            COMMAND.COM_RESET_CONNECTION,
            COMMAND.COM_QUERY,  # SET NAMES
            COMMAND.COM_QUERY,  # init_command
            COMMAND.COM_QUERY,  # SET AUTOCOMMIT
        ]
        sqls = [sql for _, cmd, sql in packets if cmd == COMMAND.COM_QUERY]
        assert sqls == [
            b"SELECT x FROM t",
            b"SET NAMES utf8mb4",
            b"SET @x = 1",
            b"SET AUTOCOMMIT = 0",
        ]
        # The old cursor is invalidated, the connection stays usable.
        assert cur.connection is None
        with pytest.raises(pymysql.err.ProgrammingError, match="Cursor closed"):
            cur.execute("SELECT 1")
        assert conn.open
        assert conn._result is None or not conn._result.unbuffered_active

    def test_reset_drains_multiple_result_sets(self):
        incoming, seq = _result_set([_row(b"1")], status=AUTOCOMMIT | MORE_RESULTS)
        result2, _ = _result_set([_row(b"2")], seq_start=seq)
        incoming += result2
        incoming += _reset_ok_stream()
        conn, sock = _make_connection(incoming, init_command="SET @x = 1")

        cur = conn.cursor()
        cur.execute("SELECT 1; SELECT 2")
        assert cur.fetchall() == (("1",),)
        assert conn._result.has_next

        conn.reset_session()

        packets = _sent_packets(sock)
        commands = [cmd for _, cmd, _ in packets]
        assert commands == [
            COMMAND.COM_QUERY,
            COMMAND.COM_RESET_CONNECTION,
            COMMAND.COM_QUERY,
            COMMAND.COM_QUERY,
            COMMAND.COM_QUERY,
        ]
        # Every command starts a new packet sequence at 0.
        assert [seq for seq, _, _ in packets] == [0, 0, 0, 0, 0]
        assert cur.connection is None
        assert conn.open

    def test_reset_error_closes_connection(self):
        incoming = _frame(_err(message=b"reset rejected"), 1)
        conn, sock = _make_connection(incoming)

        cur = conn.cursor()
        with pytest.raises(pymysql.err.OperationalError, match="reset rejected"):
            conn.reset_session()

        # Nothing but COM_RESET_CONNECTION was sent; the connection is dead.
        assert _sent_packets(sock) == [(0, COMMAND.COM_RESET_CONNECTION, b"")]
        assert not conn.open
        assert sock.closed
        assert cur.connection is None

    def test_init_command_failure_closes_connection(self):
        incoming = (
            _frame(_ok(), 1)  # COM_RESET_CONNECTION
            + _frame(_ok(), 1)  # SET NAMES
            + _frame(_err(message=b"init failed"), 1)  # init_command
        )
        conn, sock = _make_connection(incoming, init_command="SET @x = 1")

        with pytest.raises(pymysql.err.OperationalError, match="init failed"):
            conn.reset_session()

        commands = [cmd for _, cmd, _ in _sent_packets(sock)]
        assert commands == [
            COMMAND.COM_RESET_CONNECTION,
            COMMAND.COM_QUERY,  # SET NAMES
            COMMAND.COM_QUERY,  # init_command
        ]
        assert not conn.open
        assert sock.closed

    def test_reset_on_closed_connection(self):
        conn, _ = _make_connection()
        conn.close()
        with pytest.raises(pymysql.err.Error, match="Already closed"):
            conn.reset_session()
