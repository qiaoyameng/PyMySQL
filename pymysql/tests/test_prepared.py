"""Tests for server-side prepared statements.

These tests use a scripted socket that plays back fixed protocol bytes and
records everything the client writes, so no MySQL server is needed.
"""

import datetime
import io
import struct
from decimal import Decimal

import pytest

import pymysql
from pymysql import err
from pymysql.constants import COMMAND, FIELD_TYPE, FLAG


class ScriptedSocket:
    """A fake socket that serves scripted response bytes and records writes."""

    def __init__(self, response=b""):
        self._reader = io.BytesIO(response)
        self.written = bytearray()
        self.closed = False

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        pass

    def makefile(self, mode):
        return self._reader

    def sendall(self, data):
        self.written += data

    def close(self):
        self.closed = True


def make_connection(response=b""):
    """Return (connection, socket) wired to a ScriptedSocket."""
    conn = pymysql.connect(user="test", defer_connect=True, ssl_disabled=True)
    sock = ScriptedSocket(response)
    conn._sock = sock
    conn._rfile = sock.makefile("rb")
    conn._next_seq_id = 0
    conn._closed = False
    conn._generation += 1  # simulate a finished connect()
    conn._current_timeout = None
    conn.server_status = 0
    return conn, sock


def frame(*payloads):
    """Wrap payload bytes in MySQL packet headers, seq starting at 1."""
    out = bytearray()
    for i, payload in enumerate(payloads):
        out += len(payload).to_bytes(3, "little")
        out.append((1 + i) % 256)
        out += payload
    return bytes(out)


def sent_packets(sock):
    """Split the recorded client writes into (sequence_id, payload) pairs."""
    data = bytes(sock.written)
    packets = []
    pos = 0
    while pos < len(data):
        length = int.from_bytes(data[pos : pos + 3], "little")
        seq = data[pos + 3]
        packets.append((seq, data[pos + 4 : pos + 4 + length]))
        pos += 4 + length
    return packets


def lenenc_str(data):
    return bytes([len(data)]) + data


def field(name, type_code, flags=0, charsetnr=63, length=20, scale=0):
    if isinstance(name, str):
        name = name.encode()
    payload = b"".join(lenenc_str(part) for part in (b"def", b"", b"", b"", name, b""))
    return payload + struct.pack(
        "<xHIBHBxx", charsetnr, length, type_code, flags, scale
    )


def prepare_ok(stmt_id, num_columns, num_params):
    return b"\x00" + struct.pack("<IHHBH", stmt_id, num_columns, num_params, 0, 0)


def ok_packet(affected_rows=0, insert_id=0, status=2, warnings=0):
    return (
        b"\x00"
        + bytes([affected_rows])
        + bytes([insert_id])
        + struct.pack("<HH", status, warnings)
    )


def error_packet(errno, message=b"boom", sqlstate=b"HY000"):
    return b"\xff" + struct.pack("<H", errno) + b"#" + sqlstate + message


EOF_PACKET = b"\xfe\x00\x00\x02\x00"


def test_prepare_execute_twice_then_close():
    script = frame(
        # COM_STMT_PREPARE response: stmt 7, 1 column, 2 params
        prepare_ok(7, 1, 2),
        field("p0", FIELD_TYPE.LONGLONG),
        field("p1", FIELD_TYPE.VAR_STRING, charsetnr=33),
        EOF_PACKET,
        field("n", FIELD_TYPE.LONG),
        EOF_PACKET,
    )
    script += frame(
        # first COM_STMT_EXECUTE response: one row, n = 42
        b"\x01",
        field("n", FIELD_TYPE.LONG),
        EOF_PACKET,
        b"\x00\x00" + struct.pack("<i", 42),
        EOF_PACKET,
    )
    script += frame(
        # second COM_STMT_EXECUTE response: one row, n = 43
        b"\x01",
        field("n", FIELD_TYPE.LONG),
        EOF_PACKET,
        b"\x00\x00" + struct.pack("<i", 43),
        EOF_PACKET,
    )
    # COM_STMT_CLOSE has no response at all
    conn, sock = make_connection(script)

    stmt = conn.prepare("SELECT ? + ?")
    assert stmt.statement_id == 7
    assert stmt.num_params == 2

    result1 = stmt.execute((1, "x"))
    assert result1.rows == ((42,),)
    assert result1.affected_rows == 1
    assert result1.description[0][0] == "n"

    # Parameter types may change between executions.
    result2 = stmt.execute((None, 2.5))
    assert result2.rows == ((43,),)

    stmt.close()
    assert stmt.closed
    stmt.close()  # idempotent

    packets = sent_packets(sock)
    assert len(packets) == 4

    seq, payload = packets[0]
    assert seq == 0
    assert payload[0] == COMMAND.COM_STMT_PREPARE
    assert payload[1:] == b"SELECT ? + ?"

    seq, payload = packets[1]
    assert payload[0] == COMMAND.COM_STMT_EXECUTE
    assert payload[1:5] == struct.pack("<I", 7)
    assert payload[5] == 0  # flags: no cursor
    assert payload[6:10] == struct.pack("<I", 1)  # iteration count
    # parameter NULL bitmap starts at bit 0 (no offset): both set -> 0x00
    assert payload[10] == 0x00
    assert payload[11] == 1  # new-params-bound-flag
    # types: LONGLONG signed, VAR_STRING
    assert payload[12:16] == struct.pack(
        "<BBBB", FIELD_TYPE.LONGLONG, 0, FIELD_TYPE.VAR_STRING, 0
    )
    assert payload[16:] == struct.pack("<q", 1) + b"\x01x"

    seq, payload = packets[2]
    assert payload[0] == COMMAND.COM_STMT_EXECUTE
    # first parameter is NULL -> bit 0 of the parameter bitmap
    assert payload[10] == 0x01
    # types: NULL, DOUBLE
    assert payload[12:16] == struct.pack(
        "<BBBB", FIELD_TYPE.NULL, 0, FIELD_TYPE.DOUBLE, 0
    )
    assert payload[16:] == struct.pack("<d", 2.5)

    seq, payload = packets[3]
    assert payload[0] == COMMAND.COM_STMT_CLOSE
    assert payload[1:] == struct.pack("<I", 7)

    # close() must not wait for a response: the scripted buffer was fully
    # consumed by the two executes, so any read would have raised.
    with pytest.raises(err.ProgrammingError):
        stmt.execute((1, 2))


def test_binary_row_types():
    columns = [
        field("c_tiny", FIELD_TYPE.TINY),
        field("c_short", FIELD_TYPE.SHORT),
        field("c_long", FIELD_TYPE.LONG),
        field("c_uint", FIELD_TYPE.LONG, flags=FLAG.UNSIGNED),
        field("c_big", FIELD_TYPE.LONGLONG),
        field("c_ubig", FIELD_TYPE.LONGLONG, flags=FLAG.UNSIGNED),
        field("c_float", FIELD_TYPE.FLOAT),
        field("c_double", FIELD_TYPE.DOUBLE),
        field("c_text", FIELD_TYPE.VAR_STRING, charsetnr=33),
        field("c_blob", FIELD_TYPE.BLOB),
        field("c_null", FIELD_TYPE.VAR_STRING, charsetnr=33),
        field("c_dt", FIELD_TYPE.DATETIME),
        field("c_date", FIELD_TYPE.DATE),
        field("c_time", FIELD_TYPE.TIME),
        field("c_dec", FIELD_TYPE.NEWDECIMAL),
    ]
    values = (
        struct.pack("<b", -5)
        + struct.pack("<h", -300)
        + struct.pack("<i", -70000)
        + struct.pack("<I", 2**32 - 1)
        + struct.pack("<q", -(2**63))
        + struct.pack("<Q", 2**64 - 1)
        + struct.pack("<f", 1.5)
        + struct.pack("<d", -2.25)
        + lenenc_str("héllo".encode())
        + lenenc_str(b"\x00\x01\xff")
        # c_null: no bytes, bit set in the NULL bitmap
        + b"\x0b"
        + struct.pack("<HBBBBBI", 2026, 10, 3, 12, 34, 56, 789000)
        + b"\x04"
        + struct.pack("<HBB", 2026, 2, 28)
        + b"\x08"
        + struct.pack("<BIBBB", 1, 1, 1, 6, 17)  # -25:06:17
        + lenenc_str(b"1234.5678")
    )
    # 15 columns -> result-row NULL bitmap is 3 bytes with a 2-bit offset.
    # c_null is column index 10 -> bit 12 -> second byte, 0x10.
    row = b"\x00" + b"\x00\x10\x00" + values

    script = frame(
        prepare_ok(11, len(columns), 0),
        *columns,
        EOF_PACKET,
    )
    script += frame(
        bytes([len(columns)]),
        *columns,
        EOF_PACKET,
        row,
        EOF_PACKET,
    )
    conn, _sock = make_connection(script)

    stmt = conn.prepare("SELECT ...")
    result = stmt.execute()

    assert result.rows == (
        (
            -5,
            -300,
            -70000,
            2**32 - 1,
            -(2**63),
            2**64 - 1,
            1.5,
            -2.25,
            "héllo",
            b"\x00\x01\xff",
            None,
            datetime.datetime(2026, 10, 3, 12, 34, 56, 789000),
            datetime.date(2026, 2, 28),
            -datetime.timedelta(days=1, hours=1, minutes=6, seconds=17),
            Decimal("1234.5678"),
        ),
    )
    assert [d[0] for d in result.description] == [
        "c_tiny",
        "c_short",
        "c_long",
        "c_uint",
        "c_big",
        "c_ubig",
        "c_float",
        "c_double",
        "c_text",
        "c_blob",
        "c_null",
        "c_dt",
        "c_date",
        "c_time",
        "c_dec",
    ]


def test_execute_ok_packet_empty_result():
    script = frame(prepare_ok(3, 0, 0))
    script += frame(ok_packet(affected_rows=5, insert_id=9))
    conn, _sock = make_connection(script)

    stmt = conn.prepare("UPDATE t SET n = n + 1")
    result = stmt.execute(())
    assert result.rows == ()
    assert result.affected_rows == 5
    assert result.insert_id == 9
    assert result.description is None
    assert conn.affected_rows() == 5


def test_prepare_server_error():
    script = frame(error_packet(1064, b"syntax error", b"42000"))
    conn, _sock = make_connection(script)

    with pytest.raises(err.ProgrammingError):
        conn.prepare("SELEC 1")


def test_execute_server_error():
    script = frame(prepare_ok(4, 0, 0))
    script += frame(error_packet(1146, b"Table 'db.nope' doesn't exist", b"42S02"))
    conn, _sock = make_connection(script)

    stmt = conn.prepare("DELETE FROM nope")
    with pytest.raises(err.ProgrammingError):
        stmt.execute(())


def test_param_count_mismatch_rejected_before_sending():
    script = frame(
        prepare_ok(5, 0, 2),
        field("p0", FIELD_TYPE.LONGLONG),
        field("p1", FIELD_TYPE.LONGLONG),
        EOF_PACKET,
    )
    conn, sock = make_connection(script)

    stmt = conn.prepare("SELECT ? + ?")
    written_after_prepare = len(sock.written)

    with pytest.raises(err.ProgrammingError):
        stmt.execute((1,))
    with pytest.raises(err.ProgrammingError):
        stmt.execute((1, 2, 3))

    # Nothing may be sent for the rejected executions.
    assert len(sock.written) == written_after_prepare


def test_integer_param_boundaries():
    script = frame(
        prepare_ok(6, 0, 4),
        *(field(f"p{i}", FIELD_TYPE.LONGLONG) for i in range(4)),
        EOF_PACKET,
    )
    script += frame(ok_packet())
    conn, sock = make_connection(script)

    stmt = conn.prepare("SELECT ?, ?, ?, ?")
    stmt.execute((-(2**63), 2**63 - 1, 2**63, 2**64 - 1))

    payload = sent_packets(sock)[1][1]
    assert payload[0] == COMMAND.COM_STMT_EXECUTE
    types = payload[12:20]
    assert types == struct.pack(
        "<BBBBBBBB",
        FIELD_TYPE.LONGLONG,
        0x00,
        FIELD_TYPE.LONGLONG,
        0x00,
        FIELD_TYPE.LONGLONG,
        0x80,  # unsigned flag
        FIELD_TYPE.LONGLONG,
        0x80,
    )
    values = payload[20:]
    assert values == (
        struct.pack("<q", -(2**63))
        + struct.pack("<q", 2**63 - 1)
        + struct.pack("<Q", 2**63)
        + struct.pack("<Q", 2**64 - 1)
    )

    # Out of 64-bit range: rejected before anything is sent.
    written = len(sock.written)
    with pytest.raises(err.DataError):
        stmt.execute((2**64, 0, 0, 0))
    with pytest.raises(err.DataError):
        stmt.execute((-(2**63) - 1, 0, 0, 0))
    assert len(sock.written) == written


def test_param_type_encoding():
    script = frame(
        prepare_ok(8, 0, 9),
        *(field(f"p{i}", FIELD_TYPE.BLOB) for i in range(9)),
        EOF_PACKET,
    )
    script += frame(ok_packet())
    conn, sock = make_connection(script)

    stmt = conn.prepare("SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?")
    stmt.execute(
        (
            True,
            1.5,
            "hi",
            b"\x00\xff",
            Decimal("1.25"),
            datetime.datetime(2026, 1, 2, 3, 4, 5, 600),
            datetime.date(2026, 1, 2),
            datetime.timedelta(days=1, seconds=3661),
            datetime.time(3, 4, 5),
        )
    )

    payload = sent_packets(sock)[1][1]
    # layout: cmd(1) stmt_id(4) flags(1) iter(4) bitmap(2) bound(1) types(18)
    types = payload[13:31]
    assert types == struct.pack(
        "<BBBBBBBBBBBBBBBBBB",
        FIELD_TYPE.TINY,
        0,
        FIELD_TYPE.DOUBLE,
        0,
        FIELD_TYPE.VAR_STRING,
        0,
        FIELD_TYPE.BLOB,
        0,
        FIELD_TYPE.NEWDECIMAL,
        0,
        FIELD_TYPE.DATETIME,
        0,
        FIELD_TYPE.DATE,
        0,
        FIELD_TYPE.TIME,
        0,
        FIELD_TYPE.TIME,
        0,
    )
    values = payload[31:]
    assert values == (
        b"\x01"
        + struct.pack("<d", 1.5)
        + b"\x02hi"
        + b"\x02\x00\xff"
        + b"\x041.25"
        + b"\x0b"
        + struct.pack("<HBBBBBI", 2026, 1, 2, 3, 4, 5, 600)
        + b"\x04"
        + struct.pack("<HBB", 2026, 1, 2)
        + b"\x08"
        + struct.pack("<BIBBB", 0, 1, 1, 1, 1)
        + b"\x08"
        + struct.pack("<BIBBB", 0, 0, 3, 4, 5)
    )


def test_statement_invalidated_by_reconnect():
    script = frame(prepare_ok(9, 0, 0))
    conn, _sock = make_connection(script)

    stmt = conn.prepare("SELECT 1")
    generation = conn._generation

    # Simulate a reconnect: new socket, new generation.
    conn._force_close()
    new_sock = ScriptedSocket(frame(prepare_ok(10, 0, 0)))
    conn._sock = new_sock
    conn._rfile = new_sock.makefile("rb")
    conn._generation += 1
    conn._closed = False

    with pytest.raises(err.InterfaceError):
        stmt.execute(())

    # close() on a stale statement must not send anything on the new
    # connection generation.
    stmt.close()
    assert bytes(new_sock.written) == b""

    # The new generation can prepare and close normally.
    stmt2 = conn.prepare("SELECT 1")
    assert stmt2._generation == generation + 1
    stmt2.close()
    packets = sent_packets(new_sock)
    assert len(packets) == 2
    assert packets[0][1][0] == COMMAND.COM_STMT_PREPARE
    assert packets[1][1][0] == COMMAND.COM_STMT_CLOSE
    assert packets[1][1][1:] == struct.pack("<I", 10)


def test_execute_after_connection_lost():
    script = frame(prepare_ok(12, 0, 0))
    conn, _sock = make_connection(script)

    stmt = conn.prepare("SELECT 1")
    conn._force_close()

    with pytest.raises(err.InterfaceError):
        stmt.execute(())
    # close() is a harmless no-op once the connection is gone.
    stmt.close()
    assert stmt.closed
