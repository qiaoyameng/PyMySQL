"""Unit tests for the LOAD DATA LOCAL INFILE source callback.

These tests use a fake socket that speaks just enough of the MySQL
protocol to run two consecutive commands (LOAD DATA LOCAL INFILE followed
by another command) without a real server.
"""

import struct

import pytest

import pymysql
from pymysql import err
from pymysql.constants import COMMAND

OK_PACKET = b"\x00\x00\x00\x02\x00\x00\x00"


def _packet(payload, seq_id):
    return struct.pack("<I", len(payload))[:3] + bytes([seq_id]) + payload


class FakeLoadLocalServer:
    """Fake socket acting as a MySQL server for LOAD DATA LOCAL INFILE.

    On a LOAD DATA LOCAL INFILE query it requests `filename`, collects the
    uploaded data packets until the empty terminator, and answers OK. Any
    other command is answered with an OK packet, which lets tests verify
    that the connection is usable for the next command.
    """

    def __init__(self, filename=b"in-memory-source"):
        self.filename = filename
        self.client_packets = []  # payloads received from the client
        self.received_data = b""
        self.closed = False
        self._recv = bytearray()
        self._send_buf = bytearray()
        self._phase = "command"
        self._last_client_seq = 0

    # socket interface used by Connection
    def settimeout(self, value):
        pass

    def close(self):
        self.closed = True

    def makefile(self, mode):
        return self

    def sendall(self, data):
        self._send_buf += data
        self._parse_client_packets()

    def read(self, n):
        if n == 0:
            return b""
        assert len(self._recv) >= n, "server response was not ready"
        data = bytes(self._recv[:n])
        del self._recv[:n]
        return data

    # server-side protocol state machine
    def _queue_response(self, payload):
        self._last_client_seq = (self._last_client_seq + 1) % 256
        self._recv += _packet(payload, self._last_client_seq)

    def _parse_client_packets(self):
        while True:
            if len(self._send_buf) < 4:
                return
            length = int.from_bytes(self._send_buf[:3], "little")
            if len(self._send_buf) < 4 + length:
                return
            self._last_client_seq = self._send_buf[3]
            payload = bytes(self._send_buf[4 : 4 + length])
            del self._send_buf[: 4 + length]
            self.client_packets.append(payload)
            self._handle_client_packet(payload)

    def _handle_client_packet(self, payload):
        if self._phase == "command":
            assert payload[0] == COMMAND.COM_QUERY
            if payload[1:].startswith(b"LOAD DATA LOCAL INFILE"):
                self._phase = "data"
                self._queue_response(b"\xfb" + self.filename)
            else:
                self._queue_response(OK_PACKET)
        else:
            if payload:
                self.received_data += payload
            else:
                # empty packet terminates the upload
                self._phase = "command"
                self._queue_response(OK_PACKET)


class TrackingSource:
    """Iterator of bytes chunks that records whether it was closed."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


def make_connection(sock, callback, **kwargs):
    options = {
        "user": "test",
        "local_infile": True,
        "local_infile_callback": callback,
        "defer_connect": True,
    }
    options.update(kwargs)
    conn = pymysql.connect(**options)
    conn._sock = sock
    conn._rfile = sock.makefile("rb")
    conn._next_seq_id = 0
    conn._current_timeout = None
    return conn


def test_callback_upload_succeeds():
    sock = FakeLoadLocalServer()
    requested_names = []
    source = TrackingSource([b"1,2\n", bytearray(b"3,4\n"), memoryview(b"5,6\n")])

    def callback(filename):
        requested_names.append(filename)
        return source

    conn = make_connection(sock, callback)
    conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")
    # the connection must be clean enough to run a second command
    conn.query("DO 0")

    assert requested_names == [b"in-memory-source"]
    assert sock.received_data == b"1,2\n3,4\n5,6\n"
    # the empty terminator packet was sent after the data packets
    assert sock.client_packets[1:4] == [b"1,2\n", b"3,4\n", b"5,6\n"]
    assert sock.client_packets[4] == b""
    assert source.closed
    assert not sock.closed


def test_callback_empty_source_sends_terminator_only():
    sock = FakeLoadLocalServer()
    source = TrackingSource([])

    conn = make_connection(sock, lambda filename: source)
    conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")
    conn.query("DO 0")

    assert sock.received_data == b""
    assert sock.client_packets[1] == b""
    assert source.closed
    assert not sock.closed


def test_callback_refused_source_keeps_connection_usable():
    sock = FakeLoadLocalServer()
    conn = make_connection(sock, lambda filename: None)

    with pytest.raises(err.OperationalError, match="refused"):
        conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")

    # the upload was aborted with the empty terminator packet, so the
    # connection is still usable for the next command
    assert sock.client_packets[-1] == b""
    assert sock.received_data == b""
    conn.query("DO 0")
    assert not sock.closed


def test_callback_failure_mid_upload_keeps_connection_usable():
    sock = FakeLoadLocalServer()

    def broken_chunks():
        yield b"partial-row\n"
        raise ValueError("source stream broke")

    class BrokenSource:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return broken_chunks()

        def close(self):
            self.closed = True

    source = BrokenSource()
    conn = make_connection(sock, lambda filename: source)

    with pytest.raises(ValueError, match="source stream broke"):
        conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")

    # the source was closed and the exchange was finished with the empty
    # terminator packet, so the next command sees a clean connection
    assert source.closed
    assert sock.received_data == b"partial-row\n"
    assert sock.client_packets[-1] == b""
    conn.query("DO 0")
    assert not sock.closed


@pytest.mark.parametrize("chunk", ["not-bytes", 123, None])
def test_callback_non_bytes_chunk_is_rejected(chunk):
    sock = FakeLoadLocalServer()
    source = TrackingSource([chunk])
    conn = make_connection(sock, lambda filename: source)

    with pytest.raises(err.OperationalError, match="must yield bytes"):
        conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")

    assert source.closed
    assert sock.client_packets[-1] == b""
    conn.query("DO 0")


@pytest.mark.parametrize(
    "chunk",
    [b"", b"x" * (16 * 1024 * 1024 + 1)],
    ids=["empty", "oversized"],
)
def test_callback_out_of_bounds_chunk_size_is_rejected(chunk):
    sock = FakeLoadLocalServer()
    source = TrackingSource([chunk])
    conn = make_connection(sock, lambda filename: source)

    with pytest.raises(err.OperationalError, match="chunk size"):
        conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")

    assert source.closed
    assert sock.client_packets[-1] == b""
    conn.query("DO 0")


def test_callback_mode_never_opens_server_path(tmp_path, monkeypatch):
    # A file exists at the requested path, but callback mode must not
    # fall back to opening it directly.
    sock = FakeLoadLocalServer(filename=str(tmp_path / "data.txt").encode())
    (tmp_path / "data.txt").write_bytes(b"from-disk\n")

    def fail_open(*args, **kwargs):
        raise AssertionError("open() must not be called in callback mode")

    monkeypatch.setattr("builtins.open", fail_open)
    conn = make_connection(sock, lambda filename: [b"from-memory\n"])
    conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")
    conn.query("DO 0")

    assert sock.received_data == b"from-memory\n"


def test_callback_requires_local_infile_enabled():
    sock = FakeLoadLocalServer()
    conn = make_connection(sock, lambda filename: [b"data\n"], local_infile=False)

    with pytest.raises(RuntimeError, match="local_infile option is false"):
        conn.query("LOAD DATA LOCAL INFILE 'anything' INTO TABLE t")
