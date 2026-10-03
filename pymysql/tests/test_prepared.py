"""Tests for server-side prepared statements (COM_STMT_PREPARE/EXECUTE/CLOSE).

These tests use a scripted fake socket and do not require a MySQL server.
"""

import datetime
import io
import struct
from decimal import Decimal

import pytest

from pymysql import err
from pymysql.connections import Connection, _pack_int24
from pymysql.constants import COMMAND, FIELD_TYPE, FLAG


class FakeSocket:
    """A socket that plays back a scripted byte stream and records writes."""

    def __init__(self, script=b""):
        self._rfile = io.BytesIO(script)
        self.written = bytearray()
        self.closed = False

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.written += data

    def makefile(self, mode):
        return self._rfile

    def close(self):
        self.closed = True


def make_connection(script=b""):
    conn = Connection(defer_connect=True)
    sock = FakeSocket(script)
    conn._sock = sock
    conn._rfile = sock.makefile("rb")
    conn._current_timeout = None
    return conn, sock


def packet(payload, seq=0):
    return _pack_int24(len(payload)) + bytes([seq]) + payload


class Script:
    """Builds a server response script with automatic sequence numbers."""

    def __init__(self):
        self.data = bytearray()
        self.seq = 1

    def add(self, payload):
        self.data += packet(payload, self.seq)
        self.seq += 1
        return self

    def __bytes__(self):
        return bytes(self.data)


EOF_PACKET = b"\xfe\x00\x00\x02\x00"


def prepare_ok_payload(stmt_id, num_columns, num_params, warnings=0):
    return (
        b"\x00"
        + struct.pack("<IHH", stmt_id, num_columns, num_params)
        + b"\x00"
        + struct.pack("<H", warnings)
    )


def field_payload(name, type_code, flags=0, charsetnr=45, length=255, scale=0):
    def lc(value):
        return bytes([len(value)]) + value

    return (
        lc(b"def")
        + lc(b"")
        + lc(b"")
        + lc(b"")
        + lc(name)
        + lc(name)
        + b"\x0c"
        + struct.pack("<HIBHB", charsetnr, length, type_code, flags, scale)
        + b"\x00\x00"
    )


def ok_payload(affected_rows=1, insert_id=0):
    return bytes([0, affected_rows, insert_id]) + struct.pack("<HH", 2, 0)


def error_payload(errno=1064, message=b"You have an error in your SQL syntax"):
    return b"\xff" + struct.pack("<H", errno) + b"#42000" + message


def resultset_payload(fields, rows):
    """Column count + field descriptors + EOF + binary rows + EOF."""
    script = Script()
    script.add(bytes([len(fields)]))
    for field in fields:
        script.add(field)
    script.add(EOF_PACKET)
    for row in rows:
        script.add(row)
    script.add(EOF_PACKET)
    return script


def prepare_script(stmt_id, column_fields=(), param_count=0):
    script = Script()
    script.add(prepare_ok_payload(stmt_id, len(column_fields), param_count))
    for _ in range(param_count):
        script.add(field_payload(b"?", FIELD_TYPE.VAR_STRING))
    if param_count:
        script.add(EOF_PACKET)
    for field in column_fields:
        script.add(field)
    if column_fields:
        script.add(EOF_PACKET)
    return script


def execute_payload(stmt_id, params, encoded):
    """Expected COM_STMT_EXECUTE payload for already-encoded parameters."""
    payload = bytearray(b"\x17")
    payload += struct.pack("<IBI", stmt_id, 0, 1)
    if params is not None:
        null_bitmap = bytearray((len(params) + 7) // 8)
        types = bytearray()
        values = bytearray()
        for i, (type_code, unsigned, data) in enumerate(encoded):
            if params[i] is None:
                null_bitmap[i >> 3] |= 1 << (i & 7)
            types += struct.pack("<BB", type_code, 0x80 if unsigned else 0)
            values += data
        payload += bytes(null_bitmap) + b"\x01" + bytes(types) + bytes(values)
    return bytes(payload)


def test_prepare_parses_metadata():
    script = prepare_script(
        7,
        [
            field_payload(b"id", FIELD_TYPE.LONG, flags=FLAG.UNSIGNED),
            field_payload(b"name", FIELD_TYPE.VAR_STRING),
        ],
        param_count=2,
    )
    conn, sock = make_connection(bytes(script))

    stmt = conn.prepare("SELECT id, name FROM t WHERE a=? AND b=?")

    assert stmt.statement_id == 7
    assert stmt.param_count == 2
    assert not stmt.closed
    assert [d[0] for d in stmt.description] == ["id", "name"]
    assert stmt.description[0][1] == FIELD_TYPE.LONG
    assert bytes(sock.written) == packet(
        b"\x16SELECT id, name FROM t WHERE a=? AND b=?"
    )


def test_prepare_server_error():
    script = Script()
    script.add(error_payload())
    conn, _ = make_connection(bytes(script))

    with pytest.raises(err.ProgrammingError):
        conn.prepare("SELECT ?")


def test_execute_ok_result():
    script = bytes(prepare_script(3)) + bytes(Script().add(ok_payload(affected_rows=5)))
    conn, sock = make_connection(script)

    stmt = conn.prepare("UPDATE t SET a=1")
    result = stmt.execute()

    assert result.affected_rows == 5
    assert result.rows is None
    expected = packet(b"\x16UPDATE t SET a=1") + packet(
        b"\x17" + struct.pack("<IBI", 3, 0, 1)
    )
    assert bytes(sock.written) == expected


def test_execute_empty_resultset():
    columns = [field_payload(b"n", FIELD_TYPE.LONGLONG)]
    script = bytes(prepare_script(4, columns)) + bytes(resultset_payload(columns, []))
    conn, _ = make_connection(script)

    stmt = conn.prepare("SELECT n FROM t")
    result = stmt.execute()

    assert result.rows == ()
    assert result.affected_rows == 0


def test_execute_server_error():
    script = bytes(prepare_script(5)) + bytes(Script().add(error_payload()))
    conn, _ = make_connection(script)

    stmt = conn.prepare("SELECT 1")
    with pytest.raises(err.ProgrammingError):
        stmt.execute()


def test_two_executes_with_changing_param_types():
    columns = [field_payload(b"n", FIELD_TYPE.LONGLONG)]
    script = (
        bytes(prepare_script(9, columns, param_count=1))
        # First execution: integer parameter, returns 42.
        + bytes(resultset_payload(columns, [b"\x00\x00" + struct.pack("<q", 42)]))
        # Second execution: string parameter, returns 7.
        + bytes(resultset_payload(columns, [b"\x00\x00" + struct.pack("<q", 7)]))
    )
    conn, sock = make_connection(script)

    stmt = conn.prepare("SELECT ?")
    result1 = stmt.execute((42,))
    result2 = stmt.execute(("hello",))

    assert result1.rows == ((42,),)
    assert result2.rows == ((7,),)

    expected = packet(b"\x16SELECT ?")
    expected += packet(
        execute_payload(9, (42,), [(FIELD_TYPE.LONGLONG, False, struct.pack("<q", 42))])
    )
    expected += packet(
        execute_payload(
            9, ("hello",), [(FIELD_TYPE.VAR_STRING, False, b"\x05hello")]
        )
    )
    assert bytes(sock.written) == expected


def test_row_null_bitmap_has_two_bit_offset():
    columns = [
        field_payload(b"a", FIELD_TYPE.LONGLONG),
        field_payload(b"b", FIELD_TYPE.LONGLONG),
        field_payload(b"c", FIELD_TYPE.LONGLONG),
    ]
    # Columns 0 and 2 are NULL: bits 2 and 4 of the row NULL bitmap.
    row = b"\x00" + bytes([(1 << 2) | (1 << 4)]) + struct.pack("<q", 5)
    script = bytes(prepare_script(2, columns)) + bytes(
        resultset_payload(columns, [row])
    )
    conn, _ = make_connection(script)

    stmt = conn.prepare("SELECT a, b, c FROM t")
    result = stmt.execute()

    assert result.rows == ((None, 5, None),)


def test_param_count_mismatch_rejected_before_sending():
    script = prepare_script(6, param_count=2)
    conn, sock = make_connection(bytes(script))
    stmt = conn.prepare("SELECT ?, ?")
    written_after_prepare = bytes(sock.written)

    with pytest.raises(err.ProgrammingError):
        stmt.execute((1,))
    with pytest.raises(err.ProgrammingError):
        stmt.execute((1, 2, 3))

    assert bytes(sock.written) == written_after_prepare


def test_param_type_encoding():
    dt = datetime.datetime(2024, 3, 1, 12, 30, 45, 123456)
    params = (
        None,
        True,
        -1,
        2**63 - 1,
        -(2**63),
        2**63,
        2**64 - 1,
        1.5,
        "hé",
        b"\x00\x01",
        Decimal("3.14"),
        dt,
        datetime.date(2024, 3, 1),
        datetime.time(12, 30, 45),
        datetime.timedelta(days=1, seconds=2),
    )
    script = bytes(prepare_script(8, param_count=len(params))) + bytes(
        Script().add(ok_payload())
    )
    conn, sock = make_connection(script)
    stmt = conn.prepare("SELECT " + ", ".join("?" * len(params)))
    written_after_prepare = bytes(sock.written)

    stmt.execute(params)

    encoded = [
        (FIELD_TYPE.NULL, False, b""),
        (FIELD_TYPE.TINY, False, struct.pack("<b", 1)),
        (FIELD_TYPE.LONGLONG, False, struct.pack("<q", -1)),
        (FIELD_TYPE.LONGLONG, False, struct.pack("<q", 2**63 - 1)),
        (FIELD_TYPE.LONGLONG, False, struct.pack("<q", -(2**63))),
        (FIELD_TYPE.LONGLONG, True, struct.pack("<Q", 2**63)),
        (FIELD_TYPE.LONGLONG, True, struct.pack("<Q", 2**64 - 1)),
        (FIELD_TYPE.DOUBLE, False, struct.pack("<d", 1.5)),
        (FIELD_TYPE.VAR_STRING, False, b"\x03h\xc3\xa9"),
        (FIELD_TYPE.BLOB, False, b"\x02\x00\x01"),
        (FIELD_TYPE.NEWDECIMAL, False, b"\x043.14"),
        (
            FIELD_TYPE.DATETIME,
            False,
            struct.pack("<BHBBBBBI", 11, 2024, 3, 1, 12, 30, 45, 123456),
        ),
        (FIELD_TYPE.DATE, False, struct.pack("<BHBB", 4, 2024, 3, 1)),
        (FIELD_TYPE.TIME, False, struct.pack("<BBIBBB", 8, 0, 0, 12, 30, 45)),
        (FIELD_TYPE.TIME, False, struct.pack("<BBIBBB", 8, 0, 1, 0, 0, 2)),
    ]
    expected = written_after_prepare + packet(execute_payload(8, params, encoded))
    assert bytes(sock.written) == expected


def test_int_param_out_of_range():
    script = prepare_script(10, param_count=1)
    conn, sock = make_connection(bytes(script))
    stmt = conn.prepare("SELECT ?")
    written_after_prepare = bytes(sock.written)

    with pytest.raises(err.DataError):
        stmt.execute((2**64,))
    with pytest.raises(err.DataError):
        stmt.execute((-(2**63) - 1,))

    assert bytes(sock.written) == written_after_prepare


def test_binary_result_type_decoding():
    columns = [
        field_payload(b"c_tiny", FIELD_TYPE.TINY, flags=FLAG.UNSIGNED),
        field_payload(b"c_short", FIELD_TYPE.SHORT),
        field_payload(b"c_long", FIELD_TYPE.LONG, flags=FLAG.UNSIGNED),
        field_payload(b"c_bigint", FIELD_TYPE.LONGLONG),
        field_payload(b"c_ubigint", FIELD_TYPE.LONGLONG, flags=FLAG.UNSIGNED),
        field_payload(b"c_float", FIELD_TYPE.FLOAT),
        field_payload(b"c_double", FIELD_TYPE.DOUBLE),
        field_payload(b"c_text", FIELD_TYPE.VAR_STRING),
        field_payload(b"c_blob", FIELD_TYPE.BLOB, charsetnr=63),
        field_payload(b"c_date", FIELD_TYPE.DATE),
        field_payload(b"c_datetime", FIELD_TYPE.DATETIME),
        field_payload(b"c_time", FIELD_TYPE.TIME),
        field_payload(b"c_decimal", FIELD_TYPE.NEWDECIMAL),
        field_payload(b"c_null", FIELD_TYPE.LONGLONG),
    ]
    values = (
        struct.pack("<B", 255)
        + struct.pack("<h", -2)
        + struct.pack("<I", 2**32 - 1)
        + struct.pack("<q", -(2**63))
        + struct.pack("<Q", 2**64 - 1)
        + struct.pack("<f", 1.5)
        + struct.pack("<d", -2.25)
        + b"\x06h\xc3\xa9llo"
        + b"\x02\x00\xff"
        + b"\x04"
        + struct.pack("<HBB", 2024, 2, 29)
        + b"\x0b"
        + struct.pack("<HBBBBBI", 2024, 2, 29, 23, 59, 58, 999999)
        + b"\x0c"
        + struct.pack("<BIBBBI", 0, 1, 2, 3, 4, 500000)
        + b"\x091234.5678"
    )
    # 14 columns: NULL bitmap is 2 bytes; last column (bit 15) is NULL.
    row = b"\x00" + b"\x00\x80" + values
    script = bytes(prepare_script(11, columns)) + bytes(
        resultset_payload(columns, [row])
    )
    conn, _ = make_connection(script)

    stmt = conn.prepare("SELECT * FROM t")
    result = stmt.execute()

    assert result.rows == (
        (
            255,
            -2,
            2**32 - 1,
            -(2**63),
            2**64 - 1,
            1.5,
            -2.25,
            "héllo",
            b"\x00\xff",
            datetime.date(2024, 2, 29),
            datetime.datetime(2024, 2, 29, 23, 59, 58, 999999),
            datetime.timedelta(
                days=1, hours=2, minutes=3, seconds=4, microseconds=500000
            ),
            Decimal("1234.5678"),
            None,
        ),
    )


def test_close_sends_no_response_expected():
    script = prepare_script(12)
    conn, sock = make_connection(bytes(script))
    stmt = conn.prepare("SELECT 1")

    # The script has no more bytes: any read attempt would raise.
    stmt.close()

    assert stmt.closed
    expected = packet(b"\x16SELECT 1") + packet(
        b"\x19" + struct.pack("<I", 12)
    )
    assert bytes(sock.written) == expected

    # Closing again is a no-op.
    stmt.close()
    assert bytes(sock.written) == expected

    with pytest.raises(err.ProgrammingError):
        stmt.execute()


def test_statement_invalid_after_reconnect():
    script = prepare_script(13)
    conn, sock = make_connection(bytes(script))
    stmt = conn.prepare("SELECT 1")
    written_after_prepare = bytes(sock.written)

    # Simulate the connection being re-established.
    conn._stmt_generation += 1

    with pytest.raises(err.ProgrammingError):
        stmt.execute()
    # Closing a stale statement does not send anything to the new connection.
    stmt.close()
    assert bytes(sock.written) == written_after_prepare


def test_statement_invalid_after_connection_close():
    script = prepare_script(14)
    conn, _ = make_connection(bytes(script))
    stmt = conn.prepare("SELECT 1")

    conn._force_close()

    with pytest.raises(err.ProgrammingError):
        stmt.execute()
    stmt.close()  # must not raise
