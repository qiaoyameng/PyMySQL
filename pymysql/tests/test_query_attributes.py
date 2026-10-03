"""Tests for CLIENT_QUERY_ATTRIBUTES support.

The wire-level tests use a tiny in-process fake server that speaks just enough
of the MySQL handshake to let a real Connection connect, then records the raw
COM_QUERY packets the client sends.
"""

import os
import socket
import struct
import threading

import pytest

import pymysql
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.connections import (
    _encode_query_attributes_block,
    _merge_query_attributes,
    _normalize_query_attributes,
)


# ---------------------------------------------------------------------------
# Encoder unit tests
# ---------------------------------------------------------------------------


class TestEncoder:
    def test_empty_block(self):
        # With the capability negotiated, even a plain query carries the two
        # length-encoded counters (0 parameters, 0 new parameter sets).
        assert _encode_query_attributes_block([], "utf8") == b"\x00\x00"

    def test_string_attribute(self):
        block = _encode_query_attributes_block([("name", "ab")], "utf8")
        assert block == (
            b"\x00"  # 0 parameters
            b"\x01"  # new_params_bound
            b"\x01"  # 1 attribute
            b"\x00"  # null bitmap
            + bytes([FIELD_TYPE.VAR_STRING, 0x00])
            + b"\x04name"
            + b"\x02ab"
        )

    def test_bytes_attribute(self):
        block = _encode_query_attributes_block([("b", b"\x00\xff")], "utf8")
        assert block == (
            b"\x00\x01\x01\x00"
            + bytes([FIELD_TYPE.BLOB, 0x00])
            + b"\x01b"
            + b"\x02\x00\xff"
        )

    def test_integer_attribute(self):
        block = _encode_query_attributes_block([("i", 42)], "utf8")
        assert block == (
            b"\x00\x01\x01\x00"
            + bytes([FIELD_TYPE.LONGLONG, 0x00])
            + b"\x01i"
            + struct.pack("<q", 42)
        )

    def test_unsigned_large_integer_attribute(self):
        value = 1 << 63
        block = _encode_query_attributes_block([("u", value)], "utf8")
        assert block[4:6] == bytes([FIELD_TYPE.LONGLONG, 0x80])
        assert block.endswith(struct.pack("<Q", value))

    def test_float_attribute(self):
        block = _encode_query_attributes_block([("f", 1.5)], "utf8")
        assert block == (
            b"\x00\x01\x01\x00"
            + bytes([FIELD_TYPE.DOUBLE, 0x00])
            + b"\x01f"
            + struct.pack("<d", 1.5)
        )

    def test_null_attribute(self):
        block = _encode_query_attributes_block([("n", None)], "utf8")
        assert block == (
            b"\x00"  # 0 parameters
            b"\x01"  # new_params_bound
            b"\x01"  # 1 attribute
            + b"\x01"  # null bitmap: bit 0 set
            + bytes([FIELD_TYPE.NULL, 0x00])
            + b"\x01n"
            # no value follows
        )

    def test_null_bitmap_multiple_attributes(self):
        block = _encode_query_attributes_block(
            [("a", 1), ("b", None), ("c", None), ("d", 2)], "utf8"
        )
        # params, new_params_bound, count
        assert block[:3] == b"\x00\x01\x04"
        # bits 1 and 2 set
        assert block[3] == 0b00000110

    def test_boolean_attribute(self):
        block = _encode_query_attributes_block([("flag", True)], "utf8")
        assert block[4] == FIELD_TYPE.TINY
        assert block.endswith(b"\x01")

    def test_unsupported_type(self):
        with pytest.raises(TypeError):
            _encode_query_attributes_block([("x", [1, 2])], "utf8")

    def test_integer_out_of_range(self):
        with pytest.raises(TypeError):
            _encode_query_attributes_block([("x", 1 << 64)], "utf8")


class TestNormalization:
    def test_mapping(self):
        assert _normalize_query_attributes({"a": 1}) == [("a", 1)]

    def test_sequence_of_pairs(self):
        assert _normalize_query_attributes([("a", 1), ("b", 2)]) == [
            ("a", 1),
            ("b", 2),
        ]

    def test_none(self):
        assert _normalize_query_attributes(None) is None

    def test_duplicate_name_in_mapping_is_impossible(self):
        assert _normalize_query_attributes({"a": 1}) == [("a", 1)]

    def test_duplicate_name_in_sequence(self):
        with pytest.raises(ValueError, match="duplicate"):
            _normalize_query_attributes([("a", 1), ("a", 2)])

    def test_non_string_name(self):
        with pytest.raises(TypeError, match="name"):
            _normalize_query_attributes([(1, 2)])

    def test_not_mapping_or_sequence(self):
        with pytest.raises(TypeError):
            _normalize_query_attributes(42)

    def test_merge_per_query_overrides_default(self):
        merged = _merge_query_attributes(
            [("a", 1), ("b", 2)], [("b", 3), ("c", 4)]
        )
        assert dict(merged) == {"a": 1, "b": 3, "c": 4}

    def test_empty_per_query_cancels_defaults(self):
        assert _merge_query_attributes([("a", 1)], []) == []

    def test_merge_combines_without_duplicates(self):
        merged = _merge_query_attributes([("a", 1)], [("b", 2)])
        assert dict(merged) == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Minimal fake MySQL server
# ---------------------------------------------------------------------------

import contextlib

from pymysql.constants import COMMAND

OK_PACKET = b"\x00\x00\x00\x02\x00\x00\x00"


def _read_packet(rfile):
    header = rfile.read(4)
    if len(header) < 4:
        return None, b""
    length = header[0] | (header[1] << 8) | (header[2] << 16)
    seq = header[3]
    payload = rfile.read(length)
    while length == 0xFFFFFF:
        header = rfile.read(4)
        length = header[0] | (header[1] << 8) | (header[2] << 16)
        payload += rfile.read(length)
    return seq, payload


def _write_packet(sock, seq, payload):
    sock.sendall(struct.pack("<I", len(payload))[:3] + bytes([seq]) + payload)


def _handshake_payload(capabilities, salt=b"0123456789abcdefghi"):
    return (
        b"\x0a"  # protocol version
        + b"8.0.23-fake\0"
        + struct.pack("<I", 7)  # thread id
        + salt[:8]
        + b"\0"
        + struct.pack("<H", capabilities & 0xFFFF)
        + b"\x2d"  # charset: utf8mb4
        + struct.pack("<H", 0x0002)  # status: autocommit
        + struct.pack("<H", capabilities >> 16)
        + bytes([21])  # auth plugin data length
        + b"\0" * 10  # reserved
        + salt[8:]
        + b"\0"
        + b"mysql_native_password\0"
    )


BASE_CAPABILITIES = (
    CLIENT.LONG_PASSWORD
    | CLIENT.PROTOCOL_41
    | CLIENT.TRANSACTIONS
    | CLIENT.SECURE_CONNECTION
    | CLIENT.PLUGIN_AUTH
    | CLIENT.CONNECT_ATTRS
    | CLIENT.PLUGIN_AUTH_LENENC_CLIENT_DATA
)


class _WriteShim:
    """Route sendall() through os.write().

    Some sandboxes block the send() syscall on sockets while still allowing
    write(); the data is identical for a connected socket.
    """

    def __init__(self, sock):
        self._sock = sock

    def sendall(self, data):
        view = memoryview(data)
        while view:
            written = os.write(self._sock.fileno(), view)
            view = view[written:]

    def __getattr__(self, name):
        return getattr(self._sock, name)


class FakeMySQLServer(threading.Thread):
    """Minimal handshake server over an in-process socket pair."""

    def __init__(self, support_query_attributes):
        super().__init__(daemon=True)
        self.support_query_attributes = support_query_attributes
        capabilities = BASE_CAPABILITIES
        if support_query_attributes:
            capabilities |= CLIENT.QUERY_ATTRIBUTES
        self.capabilities = capabilities
        server_raw, client_raw = socket.socketpair()
        self._server_sock = _WriteShim(server_raw)
        self.client_sock = _WriteShim(client_raw)
        # Application-level COM_QUERY payloads and the connection's own
        # "SET NAMES" query are tracked separately.
        self.queries = []
        self.setup_queries = []
        self.client_capabilities = None
        self.error = None
        self.handshake_done = threading.Event()

    def run(self):
        conn = self._server_sock
        rfile = conn.makefile("rb")
        try:
            _write_packet(conn, 0, _handshake_payload(self.capabilities))

            seq, response = _read_packet(rfile)
            self.client_capabilities = struct.unpack_from("<I", response, 0)[0]
            _write_packet(conn, seq + 1, OK_PACKET)
            self.handshake_done.set()

            while True:
                seq, payload = _read_packet(rfile)
                if not payload:
                    return
                command = payload[0]
                if command == COMMAND.COM_QUIT:
                    return
                if command == COMMAND.COM_QUERY:
                    body = payload[1:]
                    # Negotiated capability prefixes SET NAMES with the two
                    # parameter counters; strip them before classifying.
                    stripped = body
                    if stripped.startswith(b"\x00\x00SET NAMES"):
                        stripped = stripped[2:]
                    if stripped.startswith(b"SET NAMES"):
                        self.setup_queries.append(body)
                    else:
                        self.queries.append(body)
                _write_packet(conn, 1, OK_PACKET)
        except OSError:
            # Socket closed during teardown (including skipped tests).
            self.handshake_done.set()
        except Exception as exc:  # pragma: no cover - surfaced via self.error
            self.error = exc
            self.handshake_done.set()

    def connect_client(self, **kwargs):
        params = dict(
            unix_socket="",
            user="user",
            password="password",
            ssl_disabled=True,
            autocommit=None,
            defer_connect=True,
        )
        params.update(kwargs)
        conn = pymysql.connect(**params)
        conn.connect(sock=self.client_sock)
        return conn

    def require_capability(self, negated=False):
        if negated == self.support_query_attributes:
            reason = (
                "only meaningful without the capability"
                if negated
                else "server does not advertise CLIENT_QUERY_ATTRIBUTES"
            )
            pytest.skip(reason)

    def stop(self):
        with contextlib.suppress(Exception):
            self._server_sock._sock.close()
        with contextlib.suppress(Exception):
            self.client_sock._sock.close()


@pytest.fixture(params=[True, False], ids=["with-cap", "without-cap"])
def fake_server(request):
    server = FakeMySQLServer(request.param)
    server.start()
    try:
        yield server
    finally:
        if not server.handshake_done.is_set():
            # Skipped before connecting: the thread is still blocked in
            # accept(); close the socket instead of waiting for it.
            server.stop()
            return
        with contextlib.suppress(Exception):
            server._server_sock._sock.shutdown(socket.SHUT_RDWR)
        server.join(timeout=2)
        server.stop()
        assert server.error is None, server.error


def _attributes_prefix(count):
    """Return the raw bytes of the counters and count for a query."""
    if count:
        return b"\x00\x01" + bytes([count])
    # Negotiated capability, no attributes: two counters only.
    return b"\x00\x00"


# ---------------------------------------------------------------------------
# Wire-level tests against the fake server
# ---------------------------------------------------------------------------


class TestQueryAttributesWire:
    def test_capability_is_negotiated(self, fake_server):
        conn = fake_server.connect_client()
        fake_server.handshake_done.wait(5)
        advertised = bool(
            fake_server.client_capabilities & CLIENT.QUERY_ATTRIBUTES
        )
        assert advertised is fake_server.support_query_attributes
        conn.close()

    def test_handshake_sends_minimal_flag_set(self, fake_server):
        # The handshake must still carry the baseline capabilities.
        conn = fake_server.connect_client()
        fake_server.handshake_done.wait(5)
        for flag in (
            CLIENT.PROTOCOL_41,
            CLIENT.SECURE_CONNECTION,
            CLIENT.PLUGIN_AUTH,
        ):
            assert fake_server.client_capabilities & flag
        conn.close()

    def test_query_without_attributes_encoding(self, fake_server):
        conn = fake_server.connect_client()
        conn.query("SELECT 1")
        if fake_server.support_query_attributes:
            # Negotiated capability: two length-encoded counters are sent
            # before the query text even with no attributes.
            assert fake_server.queries[-1] == b"\x00\x00SELECT 1"
        else:
            # No capability: original plain packet byte for byte.
            assert fake_server.queries[-1] == b"SELECT 1"
        conn.close()

    def test_internal_setup_query_uses_counters(self, fake_server):
        # SET NAMES issued during connect() must also follow the negotiated
        # format; a bare query would confuse a real 8.0.23+ server.
        conn = fake_server.connect_client()
        setup = fake_server.setup_queries
        assert setup, "expected a SET NAMES query during connect()"
        if fake_server.support_query_attributes:
            assert all(p.startswith(b"\x00\x00SET NAMES") for p in setup)
        else:
            assert all(p.startswith(b"SET NAMES") for p in setup)
        conn.close()

    def test_query_with_string_attribute(self, fake_server):
        fake_server.require_capability()
        conn = fake_server.connect_client()
        conn.query("SELECT 1", query_attributes={"trace_id": "abc"})
        expected = (
            _attributes_prefix(1)
            + b"\x00"  # null bitmap
            + bytes([FIELD_TYPE.VAR_STRING, 0x00])
            + b"\x08trace_id"
            + b"\x03abc"
            + b"SELECT 1"
        )
        assert fake_server.queries[-1] == expected
        conn.close()

    def test_all_supported_types(self, fake_server):
        fake_server.require_capability()
        conn = fake_server.connect_client()
        conn.query(
            "SELECT 1",
            query_attributes={
                "s": "x",
                "b": b"y",
                "i": 42,
                "f": 1.5,
                "n": None,
            },
        )
        payload = fake_server.queries[-1]
        assert payload[:3] == b"\x00\x01\x05"
        assert payload[3] == 0b00010000  # only "n" (index 4) is NULL
        assert payload.endswith(b"SELECT 1")
        conn.close()

    def test_cursor_execute_passes_attributes(self, fake_server):
        fake_server.require_capability()
        conn = fake_server.connect_client()
        cur = conn.cursor()
        cur.execute(
            "SELECT %s", (9,), query_attributes=[("trace_id", "z")]
        )
        payload = fake_server.queries[-1]
        assert payload.startswith(
            _attributes_prefix(1)
            + b"\x00"
            + bytes([FIELD_TYPE.VAR_STRING, 0x00])
            + b"\x08trace_id"
            + b"\x01z"
        )
        # Interpolated args end up in the SQL, never in the attribute block.
        assert payload.endswith(b"SELECT 9")
        cur.close()
        conn.close()

    def test_successive_queries_no_residue_after_cancel(self, fake_server):
        fake_server.require_capability()
        conn = fake_server.connect_client(query_attributes={"trace_id": "abc"})
        # First query carries the default attribute.
        conn.query("SELECT 1")
        first = fake_server.queries[-1]
        assert first.startswith(_attributes_prefix(1))
        assert b"trace_id" in first

        # Cancel defaults for one query: counters still present, no attrs.
        conn.query("SELECT 2", query_attributes={})
        assert fake_server.queries[-1] == b"\x00\x00SELECT 2"

        # Defaults apply again on the next request; the cancel left no residue.
        conn.query("SELECT 3")
        third = fake_server.queries[-1]
        assert third == first.replace(b"SELECT 1", b"SELECT 3")
        conn.close()

    def test_per_query_override_does_not_mutate_defaults(self, fake_server):
        fake_server.require_capability()
        conn = fake_server.connect_client(query_attributes={"trace_id": "abc"})
        conn.query("SELECT 1", query_attributes={"trace_id": "zzz"})
        assert b"\x03zzz" in fake_server.queries[-1]
        conn.query("SELECT 2")
        assert b"\x03abc" in fake_server.queries[-1]
        conn.close()

    def test_caller_dict_is_not_mutated_or_retained(self, fake_server):
        fake_server.require_capability()
        attrs = {"trace_id": "abc"}
        conn = fake_server.connect_client(query_attributes=attrs)
        attrs["trace_id"] = "changed"
        attrs["extra"] = 1
        conn.query("SELECT 1")
        payload = fake_server.queries[-1]
        assert b"\x03abc" in payload
        assert b"extra" not in payload
        conn.close()

    def test_defaults_do_not_leak_between_connections(self):
        server1 = FakeMySQLServer(True)
        server1.start()
        server2 = FakeMySQLServer(True)
        server2.start()
        try:
            conn1 = server1.connect_client(query_attributes={"trace_id": "abc"})
            conn2 = server2.connect_client()
            conn1.query("SELECT 1")
            conn2.query("SELECT 2")
            assert server1.queries[-1] != b"SELECT 1"
            assert b"trace_id" in server1.queries[-1]
            assert server2.queries[-1] == b"\x00\x00SELECT 2"
            conn1.close()
            conn2.close()
        finally:
            server1.join(timeout=5)
            server2.join(timeout=5)
            server1.stop()
            server2.stop()
            assert server1.error is None
            assert server2.error is None

    def test_duplicate_names_rejected_before_send(self, fake_server):
        conn = fake_server.connect_client()
        with pytest.raises(ValueError, match="duplicate"):
            conn.query(
                "SELECT 1",
                query_attributes=[("a", 1), ("a", 2)],
            )
        assert fake_server.queries == []
        conn.close()

    def test_unsupported_type_rejected_before_send(self, fake_server):
        conn = fake_server.connect_client()
        with pytest.raises(TypeError):
            conn.query("SELECT 1", query_attributes={"a": object()})
        assert fake_server.queries == []
        conn.close()

    def test_plain_query_encoding(self, fake_server):
        # No attributes supplied at either level.
        conn = fake_server.connect_client()
        conn.query("SELECT plain")
        if fake_server.support_query_attributes:
            assert fake_server.queries[-1] == b"\x00\x00SELECT plain"
        else:
            # No capability: original packet byte for byte.
            assert fake_server.queries[-1] == b"SELECT plain"
        conn.close()

    def test_attributes_fail_without_capability(self, fake_server):
        fake_server.require_capability(negated=True)
        # Connection-level defaults are also explicit attributes: the first
        # query that would use them fails, having sent nothing.
        conn = fake_server.connect_client(query_attributes={"a": 1})
        with pytest.raises(pymysql.err.NotSupportedError):
            conn.query("SELECT 1")
        assert fake_server.queries == []
        # Cancelling the defaults makes the same connection usable again with
        # the original plain packet.
        conn.query("SELECT 2", query_attributes={})
        assert fake_server.queries[-1] == b"SELECT 2"
        conn.close()


class TestConnectionDefaults:
    def test_duplicate_default_names_rejected_at_connect(self):
        with pytest.raises(ValueError, match="duplicate"):
            pymysql.connect(
                host="127.0.0.1",
                defer_connect=True,
                ssl_disabled=True,
                query_attributes=[("a", 1), ("a", 2)],
            )

    def test_unsupported_default_type_rejected_at_connect(self):
        with pytest.raises(TypeError):
            pymysql.connect(
                host="127.0.0.1",
                defer_connect=True,
                ssl_disabled=True,
                query_attributes={"a": object()},
            )
