import io
import struct

import pytest

import pymysql
from pymysql.constants import COMMAND, FIELD_TYPE, SERVER_STATUS
from pymysql.cursors import Cursor, SSCursor


def frame(payload, sequence_id):
    return struct.pack("<I", len(payload))[:3] + bytes([sequence_id]) + payload


def ok_packet(server_status=0):
    return b"\x00\x00\x00" + struct.pack("<HH", server_status, 0)


def error_packet(code=1820, message=b"reset failed"):
    return b"\xff" + struct.pack("<H", code) + b"#HY000" + message


def length_encoded_string(value):
    if isinstance(value, str):
        value = value.encode()
    return bytes([len(value)]) + value


def field_packet():
    payload = b"".join(
        length_encoded_string(value)
        for value in ("def", "database", "table", "table", "column", "column")
    )
    payload += struct.pack(
        "<xHIBHBxx",
        63,
        1,
        FIELD_TYPE.LONG,
        0,
        0,
    )
    return payload


def result_set_frames(start_sequence_id, server_status, rows=()):
    sequence_id = start_sequence_id
    packets = [
        frame(b"\x01", sequence_id),
        frame(field_packet(), sequence_id + 1),
        frame(b"\xfe\x00\x00\x00\x00", sequence_id + 2),
    ]
    sequence_id += 3

    for row in rows:
        packets.append(frame(length_encoded_string(row), sequence_id))
        sequence_id += 1

    packets.append(
        frame(
            b"\xfe" + struct.pack("<HH", 0, server_status),
            sequence_id,
        )
    )
    return packets, sequence_id + 1


def restoration_frames(init_error=False):
    packets = [
        frame(ok_packet(0), 1),
        frame(ok_packet(0), 1),
    ]
    if init_error:
        packets.append(frame(error_packet(message=b"init failed"), 1))
    else:
        packets.append(frame(ok_packet(0), 1))
        packets.append(
            frame(ok_packet(SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT), 1)
        )
    return packets


class FakeSocket:
    def __init__(self):
        self.writes = []
        self.closed = False

    def sendall(self, data):
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        pass


def make_connection(frames):
    connection = pymysql.connect(
        user="user",
        password="",
        ssl_disabled=True,
        defer_connect=True,
        charset="latin1",
        autocommit=True,
        init_command="SELECT 1",
    )
    socket = FakeSocket()
    connection._sock = socket
    connection._rfile = io.BytesIO(b"".join(frames))
    connection._next_seq_id = 0
    connection._current_timeout = None
    connection.server_status = 0
    return connection, socket


def commands(socket):
    return [(packet[4], packet[5:]) for packet in socket.writes]


def test_reset_session_drains_unread_sscursor_before_reset():
    result_frames, _ = result_set_frames(1, 0, rows=(b"1",))
    frames = result_frames + restoration_frames()
    connection, socket = make_connection(frames)
    cursor = connection.cursor(SSCursor)

    cursor.execute("SELECT 1")
    assert cursor._result.unbuffered_active

    with pytest.warns(
        UserWarning, match="Previous unbuffered result was left incomplete"
    ):
        connection.reset_session()

    assert commands(socket) == [
        (COMMAND.COM_QUERY, b"SELECT 1"),
        (COMMAND.COM_RESET_CONNECTION, b""),
        (COMMAND.COM_QUERY, b"SET NAMES latin1"),
        (COMMAND.COM_QUERY, b"SELECT 1"),
        (COMMAND.COM_QUERY, b"SET AUTOCOMMIT = 1"),
    ]
    assert connection._rfile.read() == b""
    assert connection.open
    assert connection._result is None
    assert cursor.connection is None
    assert cursor._result is None
    with pytest.raises(pymysql.ProgrammingError, match="Cursor closed"):
        cursor.fetchone()
    with pytest.raises(pymysql.ProgrammingError, match="Cursor closed"):
        cursor.execute("SELECT 2")

    new_cursor = connection.cursor(SSCursor)
    assert new_cursor.connection is connection
    assert new_cursor is not cursor


def test_reset_session_drains_following_result_sets():
    first_result, _ = result_set_frames(
        1, SERVER_STATUS.SERVER_MORE_RESULTS_EXISTS
    )
    second_result, _ = result_set_frames(5, 0)
    frames = first_result + second_result + restoration_frames()
    connection, socket = make_connection(frames)
    cursor = connection.cursor(Cursor)

    cursor.execute("SELECT 1")
    assert connection._result.has_next

    connection.reset_session()

    assert commands(socket) == [
        (COMMAND.COM_QUERY, b"SELECT 1"),
        (COMMAND.COM_RESET_CONNECTION, b""),
        (COMMAND.COM_QUERY, b"SET NAMES latin1"),
        (COMMAND.COM_QUERY, b"SELECT 1"),
        (COMMAND.COM_QUERY, b"SET AUTOCOMMIT = 1"),
    ]
    assert connection._rfile.read() == b""
    assert connection.open
    assert cursor.connection is None
    assert cursor.description is None
    with pytest.raises(pymysql.ProgrammingError, match="Cursor closed"):
        cursor.execute("SELECT 2")

    assert connection.cursor().connection is connection


def test_reset_session_drains_unbuffered_following_result_sets():
    first_result, _ = result_set_frames(
        1, SERVER_STATUS.SERVER_MORE_RESULTS_EXISTS
    )
    second_result, _ = result_set_frames(5, 0)
    frames = first_result + second_result + restoration_frames()
    connection, socket = make_connection(frames)
    cursor = connection.cursor(SSCursor)

    cursor.execute("SELECT 1")
    with pytest.warns(
        UserWarning, match="Previous unbuffered result was left incomplete"
    ):
        connection.reset_session()

    assert commands(socket) == [
        (COMMAND.COM_QUERY, b"SELECT 1"),
        (COMMAND.COM_RESET_CONNECTION, b""),
        (COMMAND.COM_QUERY, b"SET NAMES latin1"),
        (COMMAND.COM_QUERY, b"SELECT 1"),
        (COMMAND.COM_QUERY, b"SET AUTOCOMMIT = 1"),
    ]
    assert connection._rfile.read() == b""
    assert connection.open
    assert cursor.connection is None


def test_reset_session_error_closes_connection_and_invalidates_cursors():
    frames = [frame(error_packet(), 1)]
    connection, socket = make_connection(frames)
    cursor = connection.cursor()

    with pytest.raises(pymysql.err.OperationalError, match="reset failed"):
        connection.reset_session()

    assert not connection.open
    assert socket.closed
    assert cursor.connection is None
    assert commands(socket) == [(COMMAND.COM_RESET_CONNECTION, b"")]


def test_reset_session_init_failure_closes_connection():
    frames = restoration_frames(init_error=True)
    connection, socket = make_connection(frames)
    cursor = connection.cursor()

    with pytest.raises(pymysql.err.OperationalError, match="init failed"):
        connection.reset_session()

    assert not connection.open
    assert socket.closed
    assert cursor.connection is None
    assert commands(socket) == [
        (COMMAND.COM_RESET_CONNECTION, b""),
        (COMMAND.COM_QUERY, b"SET NAMES latin1"),
        (COMMAND.COM_QUERY, b"SELECT 1"),
    ]
