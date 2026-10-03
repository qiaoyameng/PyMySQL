"""Tests for the local_infile_handler callback using a fake socket.

Each test runs two consecutive commands to prove that the connection is
left in a clean state after the LOAD DATA LOCAL INFILE exchange.
"""

import io
import struct
import unittest

from pymysql import OperationalError, connections

__all__ = ["TestLoadLocalHandler"]

OK_PAYLOAD = b"\x00\x00\x00\x02\x00\x00\x00"


def _packet(payload, seq_id):
    return struct.pack("<I", len(payload))[:3] + bytes([seq_id]) + payload


def _ok_packet(seq_id):
    return _packet(OK_PAYLOAD, seq_id)


def _load_local_packet(filename, seq_id=1):
    return _packet(b"\xfb" + filename, seq_id)


def _parse_packets(data):
    packets = []
    pos = 0
    while pos < len(data):
        (length,) = struct.unpack("<I", data[pos : pos + 3] + b"\x00")
        packets.append(data[pos + 4 : pos + 4 + length])
        pos += 4 + length
    return packets


class FakeSocket:
    def __init__(self, read_data):
        self._rfile = io.BytesIO(read_data)
        self.written = bytearray()
        self.closed = False

    def settimeout(self, value):
        pass

    def makefile(self, mode):
        return self._rfile

    def sendall(self, data):
        self.written += data

    def close(self):
        self.closed = True


class ChunkSource:
    """Bytes-chunk iterator with a close() method, like a stream."""

    def __init__(self, chunks=(), error=None):
        self._chunks = chunks
        self._error = error
        self.closed = False

    def __iter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    def close(self):
        self.closed = True


class TestLoadLocalHandler(unittest.TestCase):
    def _make_connection(self, handler, read_data, **kwargs):
        conn = connections.Connection(
            defer_connect=True,
            local_infile=True,
            local_infile_handler=handler,
            **kwargs,
        )
        sock = FakeSocket(read_data)
        conn._sock = sock
        conn._rfile = sock.makefile("rb")
        conn._next_seq_id = 0
        conn._closed = False
        conn._current_timeout = None
        self.addCleanup(conn._force_close)
        return conn, sock

    def test_upload(self):
        source = ChunkSource([b"1,alice\n", b"2,bob\n"])
        names = []

        def handler(filename):
            names.append(filename)
            return source

        # load local request (seq 1), OK after 2 chunks + empty packet
        # (seq 5), OK for the second command (seq 1)
        read_data = (
            _load_local_packet(b"virtual/data.csv") + _ok_packet(5) + _ok_packet(1)
        )
        conn, sock = self._make_connection(handler, read_data)

        conn.query("LOAD DATA LOCAL INFILE 'virtual/data.csv' INTO TABLE t")
        conn.query("DO 0")

        self.assertEqual(names, ["virtual/data.csv"])
        self.assertTrue(source.closed)
        payloads = _parse_packets(bytes(sock.written))
        self.assertEqual(len(payloads), 5)
        self.assertEqual(payloads[1], b"1,alice\n")
        self.assertEqual(payloads[2], b"2,bob\n")
        self.assertEqual(payloads[3], b"")  # empty terminator packet

    def test_refused_source(self):
        def handler(filename):
            return None

        # empty terminator (seq 2), OK (seq 3), OK for second command
        read_data = _load_local_packet(b"secret.txt") + _ok_packet(3) + _ok_packet(1)
        conn, sock = self._make_connection(handler, read_data)

        with self.assertRaises(OperationalError):
            conn.query("LOAD DATA LOCAL INFILE 'secret.txt' INTO TABLE t")
        conn.query("DO 0")

        payloads = _parse_packets(bytes(sock.written))
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[1], b"")  # empty terminator packet

    def test_source_fails_mid_upload(self):
        source = ChunkSource([b"1,alice\n"], error=ValueError("stream broke"))

        def handler(filename):
            return source

        # one chunk (seq 2), empty terminator (seq 3), OK (seq 4),
        # OK for the second command (seq 1)
        read_data = _load_local_packet(b"virtual.csv") + _ok_packet(4) + _ok_packet(1)
        conn, sock = self._make_connection(handler, read_data)

        with self.assertRaisesRegex(ValueError, "stream broke"):
            conn.query("LOAD DATA LOCAL INFILE 'virtual.csv' INTO TABLE t")
        conn.query("DO 0")

        self.assertTrue(source.closed)
        payloads = _parse_packets(bytes(sock.written))
        self.assertEqual(len(payloads), 4)
        self.assertEqual(payloads[1], b"1,alice\n")
        self.assertEqual(payloads[2], b"")  # empty terminator packet

    def test_empty_source(self):
        source = ChunkSource([])

        def handler(filename):
            return source

        read_data = _load_local_packet(b"empty.csv") + _ok_packet(3) + _ok_packet(1)
        conn, sock = self._make_connection(handler, read_data)

        conn.query("LOAD DATA LOCAL INFILE 'empty.csv' INTO TABLE t")
        conn.query("DO 0")

        self.assertTrue(source.closed)
        payloads = _parse_packets(bytes(sock.written))
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[1], b"")  # only the empty terminator packet

    def test_non_bytes_chunk(self):
        source = ChunkSource(["not bytes"])

        def handler(filename):
            return source

        read_data = _load_local_packet(b"bad.csv") + _ok_packet(3) + _ok_packet(1)
        conn, sock = self._make_connection(handler, read_data)

        with self.assertRaises(TypeError):
            conn.query("LOAD DATA LOCAL INFILE 'bad.csv' INTO TABLE t")
        conn.query("DO 0")

        self.assertTrue(source.closed)
        payloads = _parse_packets(bytes(sock.written))
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[1], b"")  # empty terminator packet

    def test_oversized_chunk(self):
        source = ChunkSource([b"x" * 32])

        def handler(filename):
            return source

        read_data = _load_local_packet(b"big.csv") + _ok_packet(3) + _ok_packet(1)
        conn, sock = self._make_connection(handler, read_data, max_allowed_packet=16)

        with self.assertRaises(OperationalError):
            conn.query("LOAD DATA LOCAL INFILE 'big.csv' INTO TABLE t")
        conn.query("DO 0")

        self.assertTrue(source.closed)
        payloads = _parse_packets(bytes(sock.written))
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[1], b"")  # empty terminator packet


if __name__ == "__main__":
    unittest.main()
