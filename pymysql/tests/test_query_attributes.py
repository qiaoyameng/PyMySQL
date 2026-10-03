"""Tests for CLIENT_QUERY_ATTRIBUTES support in COM_QUERY."""

import struct
from unittest import mock

import pytest

import pymysql
from pymysql.constants import CLIENT, COMMAND, FIELD_TYPE

from . import base


def _lenenc_decode(data, pos):
    first = data[pos]
    pos += 1
    if first < 0xFB:
        return first, pos
    if first == 0xFC:
        return struct.unpack_from("<H", data, pos)[0], pos + 2
    if first == 0xFD:
        return struct.unpack_from("<I", data, pos)[0] & 0xFFFFFF, pos + 3
    return struct.unpack_from("<Q", data, pos)[0], pos + 8


def parse_attribute_block(body, attr_count):
    """Parse the query-attribute prefix of a COM_QUERY payload.

    The attribute count is not sent on the wire; callers pass the expected
    count.  Returns (attrs, query) where attrs is a list of
    (name, field_type, unsigned, raw_value_or_None).
    """
    pos = 0
    parameter_set_count, pos = _lenenc_decode(body, pos)
    assert parameter_set_count == 1

    bitmap_len = (attr_count + 7) // 8
    null_bitmap = body[pos : pos + bitmap_len]
    pos += bitmap_len

    new_params_bound = body[pos]
    pos += 1
    assert new_params_bound == 1

    types = []
    for _ in range(attr_count):
        field_type, unsigned = struct.unpack_from("<HB", body, pos)
        pos += 3
        types.append((field_type, unsigned))

    names = []
    for _ in range(attr_count):
        length, pos = _lenenc_decode(body, pos)
        names.append(body[pos : pos + length])
        pos += length

    attrs = []
    for index, (name, (field_type, unsigned)) in enumerate(zip(names, types)):
        is_null = bool(null_bitmap[index // 8] & (1 << (index % 8)))
        if is_null:
            raw = None
        elif field_type == FIELD_TYPE.TINY:
            raw = body[pos]
            pos += 1
        elif field_type == FIELD_TYPE.SHORT:
            raw = struct.unpack_from("<H" if unsigned else "<h", body, pos)[0]
            pos += 2
        elif field_type == FIELD_TYPE.LONG:
            raw = struct.unpack_from("<I" if unsigned else "<i", body, pos)[0]
            pos += 4
        elif field_type == FIELD_TYPE.LONGLONG:
            raw = struct.unpack_from("<Q" if unsigned else "<q", body, pos)[0]
            pos += 8
        elif field_type == FIELD_TYPE.FLOAT:
            raw = struct.unpack_from("<f", body, pos)[0]
            pos += 4
        elif field_type == FIELD_TYPE.DOUBLE:
            raw = struct.unpack_from("<d", body, pos)[0]
            pos += 8
        else:
            length, pos = _lenenc_decode(body, pos)
            raw = body[pos : pos + length]
            pos += length
        attrs.append((name, field_type, unsigned, raw))

    return attrs, body[pos:]


class FakeConnection:
    """Minimal harness capturing the raw bytes of COM_QUERY packets."""

    def __init__(self, capabilities=None, query_attributes=None):
        kwargs = {"user": "user", "ssl_disabled": True, "defer_connect": True}
        if query_attributes is not None:
            kwargs["query_attributes"] = query_attributes
        self.conn = pymysql.connect(**kwargs)
        self.conn.server_capabilities = (
            CLIENT.CAPABILITIES if capabilities is None else capabilities
        )
        self.conn._sock = object()
        self.sent = []

    def run(self, sql, **kwargs):
        sent = []

        def capture(data):
            sent.append(bytes(data))

        with (
            mock.patch.object(self.conn, "_write_bytes", side_effect=capture),
            mock.patch.object(self.conn, "_read_query_result", return_value=0),
        ):
            self.conn.query(sql.encode(self.conn.encoding), **kwargs)

        packet = sent[0]
        assert len(packet) >= 5
        size = struct.unpack_from("<i", packet, 0)[0]
        command = packet[4]
        assert command == COMMAND.COM_QUERY
        payload = packet[5 : 4 + size]
        self.sent.append(payload)
        return payload


class TestQueryAttributesEncoding:
    def test_capability_is_negotiated(self):
        conn = pymysql.connect(user="u", ssl_disabled=True, defer_connect=True)
        assert conn.client_flag & CLIENT.QUERY_ATTRIBUTES

    def test_plain_query_keeps_original_packet(self):
        harness = FakeConnection()
        payload = harness.run("SELECT 1")
        # No attribute block: the query text follows the command byte directly.
        assert payload == b"SELECT 1"

    def test_all_supported_types(self):
        harness = FakeConnection()
        payload = harness.run(
            "SELECT 1",
            query_attributes={
                "s": "hi",
                "b": b"\x00\xff",
                "i": 5,
                "neg": -7,
                "f": 1.5,
                "n": None,
                "flag": True,
            },
        )
        attrs, query = parse_attribute_block(payload, 7)
        assert query == b"SELECT 1"
        assert attrs == [
            (b"s", FIELD_TYPE.STRING, 0, b"hi"),
            (b"b", FIELD_TYPE.BLOB, 0, b"\x00\xff"),
            (b"i", FIELD_TYPE.LONGLONG, 1, 5),
            (b"neg", FIELD_TYPE.LONGLONG, 0, -7),
            (b"f", FIELD_TYPE.DOUBLE, 0, 1.5),
            (b"n", FIELD_TYPE.NULL, 0, None),
            (b"flag", FIELD_TYPE.TINY, 0, 1),
        ]

    def test_null_bitmap_covers_whole_bytes(self):
        harness = FakeConnection()
        payload = harness.run(
            "SELECT 1",
            query_attributes=[(f"a{i}", None) for i in range(9)],
        )
        # 9 attributes need a 2-byte bitmap; every bit must be set.
        assert payload[1:3] == b"\xff\x01"  # after parameter_set_count byte

    def test_attribute_block_layout_bytes(self):
        harness = FakeConnection()
        payload = harness.run("SELECT 1", query_attributes={"a": 1})
        # parameter_set_count(1) bitmap(0x00) new-bound(0x01)
        # type LONGLONG unsigned(08 00 01) name len(1) name(a) value(8 bytes)
        expected = (
            b"\x01\x00\x01"
            + struct.pack("<HB", FIELD_TYPE.LONGLONG, 1)
            + b"\x01a"
            + struct.pack("<Q", 1)
            + b"SELECT 1"
        )
        assert payload == expected

    def test_consecutive_queries_do_not_leak_attributes(self):
        harness = FakeConnection()
        first = harness.run("SELECT 1", query_attributes={"a": 1})
        attrs, _ = parse_attribute_block(first, 1)
        assert [name for name, *_ in attrs] == [b"a"]

        second = harness.run("SELECT 2")
        assert second == b"SELECT 2"

    def test_connection_defaults_are_sent(self):
        harness = FakeConnection(query_attributes={"a": 1})
        payload = harness.run("SELECT 1")
        attrs, _ = parse_attribute_block(payload, 1)
        assert attrs == [(b"a", FIELD_TYPE.LONGLONG, 1, 1)]

    def test_per_query_attributes_replace_defaults(self):
        harness = FakeConnection(query_attributes={"a": 1, "b": 2})
        payload = harness.run("SELECT 1", query_attributes={"a": 9})
        attrs, _ = parse_attribute_block(payload, 1)
        assert attrs == [(b"a", FIELD_TYPE.LONGLONG, 1, 9)]

    def test_empty_mapping_cancels_defaults_without_residue(self):
        harness = FakeConnection(query_attributes={"a": 1})

        canceled = harness.run("SELECT 1", query_attributes={})
        assert canceled == b"SELECT 1"

        restored = harness.run("SELECT 2")
        attrs, query = parse_attribute_block(restored, 1)
        assert attrs == [(b"a", FIELD_TYPE.LONGLONG, 1, 1)]
        assert query == b"SELECT 2"

    def test_caller_mapping_is_not_retained_or_mutated(self):
        defaults = {"a": 1}
        harness = FakeConnection(query_attributes=defaults)
        defaults["a"] = 2
        defaults["b"] = 3

        payload = harness.run("SELECT 1")
        attrs, _ = parse_attribute_block(payload, 1)
        assert attrs == [(b"a", FIELD_TYPE.LONGLONG, 1, 1)]

        per_query = {"c": 4}
        harness.run("SELECT 2", query_attributes=per_query)
        assert per_query == {"c": 4}

    def test_attributes_do_not_cross_connections(self):
        first = FakeConnection(query_attributes={"a": 1})
        second = FakeConnection()
        assert second.run("SELECT 1") == b"SELECT 1"
        attrs, _ = parse_attribute_block(first.run("SELECT 1"), 1)
        assert [name for name, *_ in attrs] == [b"a"]

    def test_explicit_attributes_fail_without_capability(self):
        harness = FakeConnection(
            capabilities=CLIENT.CAPABILITIES & ~CLIENT.QUERY_ATTRIBUTES
        )
        with (
            mock.patch.object(harness.conn, "_write_bytes") as write_bytes,
            pytest.raises(pymysql.err.NotSupportedError),
        ):
            harness.conn.query(b"SELECT 1", query_attributes={"a": 1})
        write_bytes.assert_not_called()

    def test_default_attributes_fail_connect_without_capability(self):
        conn = pymysql.connect(
            user="u",
            ssl_disabled=True,
            defer_connect=True,
            query_attributes={"a": 1},
        )
        conn.server_capabilities = CLIENT.CAPABILITIES & ~CLIENT.QUERY_ATTRIBUTES
        with (
            mock.patch.object(conn, "_get_server_information"),
            mock.patch.object(conn, "_request_authentication"),
            pytest.raises(pymysql.err.NotSupportedError),
        ):
            conn.connect(sock=mock.Mock())

    def test_duplicate_names_rejected(self):
        harness = FakeConnection()
        with pytest.raises(ValueError, match="duplicate"):
            harness.run("SELECT 1", query_attributes=[("a", 1), ("a", 2)])

    def test_duplicate_encoded_names_rejected(self):
        harness = FakeConnection()
        with pytest.raises(ValueError, match="duplicate"):
            harness.run("SELECT 1", query_attributes=[("a", 1), (b"a", 2)])

    @pytest.mark.parametrize("value", [object(), [1], {"x": 1}, 1 + 2j])
    def test_unsupported_value_type_rejected(self, value):
        harness = FakeConnection()
        with pytest.raises(TypeError, match="unsupported query attribute"):
            harness.run("SELECT 1", query_attributes={"a": value})

    def test_unsupported_name_type_rejected(self):
        harness = FakeConnection()
        with pytest.raises(TypeError, match="name must be str or bytes"):
            harness.run("SELECT 1", query_attributes=[(1, 1)])

    def test_non_mapping_non_pairs_rejected(self):
        harness = FakeConnection()
        with pytest.raises(TypeError):
            harness.run("SELECT 1", query_attributes=[1, 2])

    def test_integer_out_of_range_rejected(self):
        harness = FakeConnection()
        with pytest.raises(ValueError, match="out of 64-bit range"):
            harness.run("SELECT 1", query_attributes={"a": 2**64})

    def test_non_finite_float_rejected(self):
        harness = FakeConnection()
        with pytest.raises(ValueError, match="finite"):
            harness.run("SELECT 1", query_attributes={"a": float("nan")})

    def test_cursor_passes_attributes_separately_from_args(self):
        harness = FakeConnection()
        cursor = harness.conn.cursor()

        def fake_read_result(unbuffered=False):
            harness.conn._result = mock.Mock(
                affected_rows=0,
                warning_count=0,
                description=None,
                insert_id=None,
                rows=(),
            )
            return 0

        try:
            with (
                mock.patch.object(
                    harness.conn,
                    "_read_query_result",
                    side_effect=fake_read_result,
                ),
                mock.patch.object(
                    harness.conn, "_write_bytes", side_effect=harness.sent.append
                ),
            ):
                cursor.execute("SELECT %s", (42,), query_attributes={"trace": "abc"})
        finally:
            cursor.connection = None
        packet = harness.sent[0]
        size = struct.unpack_from("<i", packet, 0)[0]
        attrs, query = parse_attribute_block(packet[5 : 4 + size], 1)
        assert attrs == [(b"trace", FIELD_TYPE.STRING, 0, b"abc")]
        assert query == b"SELECT 42"


class TestQueryAttributesServer(base.PyMySQLTestCase):
    """End-to-end checks against a real MySQL 8.0.23+ server."""

    def setUp(self):
        super().setUp()
        try:
            conn = self.connect()
        except Exception:
            self.skipTest("no MySQL server available")
        self.addCleanup(conn.close)
        self.capable = (
            bool(conn.server_capabilities & CLIENT.QUERY_ATTRIBUTES)
            and self.get_mysql_vendor(conn) == "mysql"
        )

    def test_attribute_values_round_trip(self):
        if not self.capable:
            self.skipTest("server does not negotiate CLIENT_QUERY_ATTRIBUTES")
        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT "
                "mysql_query_attribute_string('s'), "
                "CAST(mysql_query_attribute_string('i') AS SIGNED), "
                "HEX(mysql_query_attribute_string('b')), "
                "mysql_query_attribute_string('n')",
                query_attributes={
                    "s": "trace-value",
                    "i": -12345,
                    "b": b"\x00\xff\x10",
                    "n": None,
                },
            )
            self.assertEqual(cursor.fetchone(), ("trace-value", -12345, "00FF10", None))

    def test_connection_defaults_and_cancellation(self):
        if not self.capable:
            self.skipTest("server does not negotiate CLIENT_QUERY_ATTRIBUTES")
        conn = self.connect(query_attributes={"trace_id": "default"})
        with conn.cursor() as cursor:
            cursor.execute("SELECT mysql_query_attribute_string('trace_id')")
            self.assertEqual(cursor.fetchone(), ("default",))

            cursor.execute(
                "SELECT mysql_query_attribute_string('trace_id')",
                query_attributes={},
            )
            self.assertEqual(cursor.fetchone(), (None,))

            cursor.execute("SELECT mysql_query_attribute_string('trace_id')")
            self.assertEqual(cursor.fetchone(), ("default",))
