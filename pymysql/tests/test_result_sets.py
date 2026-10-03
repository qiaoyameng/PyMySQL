"""Tests for Cursor.iter_result_sets().

The ``TestResultSetProtocol`` cases drive the result-set state machine with
canned protocol packets, so they run without a MySQL server.  The
``TestResultSets`` cases need a live server (stored procedures and multiple
result sets).
"""

import struct

import pytest

import pymysql
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.protocol import MysqlPacket
from pymysql.tests import base

SERVER_MORE_RESULTS = 8


def _lenenc_int(value):
    if value < 251:
        return bytes([value])
    raise AssertionError("test helper only supports small integers")


def _lenenc_str(data):
    return _lenenc_int(len(data)) + data


def column_count_packet(count):
    return MysqlPacket(bytes([count]), "utf8")


def field_packet(name, type_code=FIELD_TYPE.LONGLONG):
    body = b"".join(
        _lenenc_str(part.encode())
        for part in ("def", "test1", "tbl", "tbl", name, name)
    )
    body += struct.pack("<xHIBHBxx", 63, 11, type_code, 0, 0)
    return MysqlPacket(body, "utf8")


def row_packet(values):
    return MysqlPacket(
        b"".join(_lenenc_str(str(value).encode()) for value in values),
        "utf8",
    )


def eof_packet(warning_count=0, server_status=0):
    return MysqlPacket(
        b"\xfe" + struct.pack("<hh", warning_count, server_status),
        "utf8",
    )


def ok_packet(affected_rows=0, warning_count=0, server_status=0, insert_id=0):
    return MysqlPacket(
        b"\x00"
        + _lenenc_int(affected_rows)
        + _lenenc_int(insert_id)
        + struct.pack("<HH", server_status, warning_count),
        "utf8",
    )


def error_packet(errno=1064, message=b"test error"):
    return MysqlPacket(
        b"\xff" + errno.to_bytes(2, "little") + b"#42000" + message,
        "utf8",
    )


def select_set(rows, names=("a",), status=SERVER_MORE_RESULTS, warning_count=0):
    """Build packets for one SELECT result set ending with EOF(status)."""
    packets = [column_count_packet(len(names))]
    packets.extend(field_packet(name) for name in names)
    packets.append(eof_packet(server_status=status))
    packets.extend(row_packet(row) for row in rows)
    packets.append(eof_packet(warning_count=warning_count, server_status=status))
    return packets


def mock_connection(packets):
    """Return an unconnected Connection whose packets come from *packets*."""
    conn = pymysql.connect(user="user", defer_connect=True, ssl_disabled=True)
    conn._sock = object()
    conn._closed = False
    conn.server_status = 2

    queue = list(packets)

    def read_packet(packet_type=MysqlPacket):
        packet = packet_type(queue.pop(0).get_all_data(), "utf8")
        if packet.is_error_packet():
            packet.raise_for_error()
        return packet

    conn._read_packet = read_packet
    conn._execute_command = lambda command, sql: None
    return conn, queue


class TestResultSetProtocol:
    def test_three_sets_skip_second_then_independent_query(self):
        """The scenario from the feature request, driven by canned packets."""
        packets = []
        packets += select_set([(1,)], status=SERVER_MORE_RESULTS)
        packets += select_set([(20,), (21,), (22,)], names=("b",))
        # Third set: empty SELECT, no more results.
        packets += select_set([], names=("c",), status=0)

        conn, leftover = mock_connection(packets)
        cursor = conn.cursor()

        with cursor.iter_result_sets("SELECT 1; SELECT 20,21,22; SELECT 30") as sets:
            first = next(sets)
            assert first.description[0][0] == "a"
            assert first.more_results is True
            assert first.rowcount == -1
            assert list(first) == [(1,)]
            assert first.rowcount == 1

            second = next(sets)
            assert second.description[0][0] == "b"
            assert second.fetchone() == (20,)
            # Skip after reading one row; the rest is drained on advance.
            third = next(sets)
            assert second.rowcount == 3

            assert third.description[0][0] == "c"
            assert list(third) == []
            assert third.rowcount == 0
            assert third.more_results is False

            with pytest.raises(StopIteration):
                next(sets)

        assert leftover == []
        # The connection is usable for an independent query afterwards.
        conn._read_packet = lambda packet_type=MysqlPacket: ok_packet(server_status=2)
        cursor.execute("SELECT 99")

    def test_empty_ok_sets_are_preserved(self):
        packets = select_set([(1,)], status=SERVER_MORE_RESULTS)
        # DML OK set (affected rows, warning count, more results).
        packets.append(
            ok_packet(
                affected_rows=3,
                warning_count=2,
                server_status=SERVER_MORE_RESULTS,
            )
        )
        # Trailing empty CALL OK set, without SERVER_MORE_RESULTS_EXISTS.
        packets.append(ok_packet(affected_rows=0, warning_count=0, server_status=2))

        conn, leftover = mock_connection(packets)
        cursor = conn.cursor()
        seen = []
        with cursor.iter_result_sets("CALL p()") as sets:
            for result_set in sets:
                seen.append(
                    (
                        result_set.description,
                        result_set.affected_rows,
                        result_set.warning_count,
                        result_set.more_results,
                    )
                )

        assert seen[0][0] is not None
        assert seen[1] == (None, 3, 2, True)
        assert seen[2] == (None, 0, 0, False)
        assert leftover == []

    def test_stale_iterator_is_rejected(self):
        packets = select_set([(1,)]) + select_set([(2,)], status=0)
        conn, _ = mock_connection(packets)
        cursor = conn.cursor()

        sets = cursor.iter_result_sets("SELECT 1; SELECT 2")
        first = next(sets)
        assert first.fetchone() == (1,)
        second = next(sets)
        # No two inner iterators may interleave reads on the socket.
        with pytest.raises(pymysql.ProgrammingError):
            first.fetchone()
        with pytest.raises(pymysql.ProgrammingError):
            next(iter(first))
        assert list(second) == [(2,)]

    def test_error_is_attributed_to_its_result_set(self):
        packets = select_set([(1,)])
        packets += select_set([(2,)])
        packets.append(error_packet())

        conn, _ = mock_connection(packets)
        cursor = conn.cursor()
        sets = cursor.iter_result_sets("SELECT 1; SELECT 2; bad")
        first = next(sets)
        assert list(first) == [(1,)]
        second = next(sets)
        assert list(second) == [(2,)]
        with pytest.raises(pymysql.ProgrammingError) as info:
            next(sets)
        assert info.value.args[0] == 1064
        # An error packet leaves the connection usable.
        assert conn.open
        conn._read_packet = lambda packet_type=MysqlPacket: ok_packet(server_status=2)
        cursor.execute("SELECT 3")

    def test_transport_failure_closes_connection(self):
        packets = select_set([(1,)])
        conn, _ = mock_connection(packets)
        cursor = conn.cursor()

        def failing_read(packet_type=MysqlPacket):
            conn._force_close()
            raise pymysql.OperationalError(2013, "Lost connection")

        sets = cursor.iter_result_sets("SELECT big")
        result_set = next(sets)
        conn._read_packet = failing_read
        with pytest.raises(pymysql.OperationalError):
            result_set.fetchone()
        assert not conn.open

    def test_server_error_mid_rows_keeps_connection_usable(self):
        # An error packet while reading rows ends the statement, but the
        # server has no pending data, so the connection stays usable.
        packets = [column_count_packet(1), field_packet("a"), eof_packet()]
        packets.append(row_packet((1,)))
        packets.append(error_packet())
        conn, _ = mock_connection(packets)
        cursor = conn.cursor()
        sets = cursor.iter_result_sets("SELECT maybe_error()")
        result_set = next(sets)
        assert result_set.fetchone() == (1,)
        with pytest.raises(pymysql.ProgrammingError):
            result_set.fetchone()
        assert conn.open
        conn._read_packet = lambda packet_type=MysqlPacket: ok_packet(server_status=2)
        cursor.execute("SELECT 2")

    def test_rows_are_not_buffered(self):
        # Reading one row must consume exactly one row packet from the wire;
        # the rest of a large result set stays unread.
        packets = select_set([(i,) for i in range(100)], status=0)
        conn, leftover = mock_connection(packets)
        cursor = conn.cursor()
        sets = cursor.iter_result_sets("SELECT i FROM big")
        result_set = next(sets)
        assert result_set.fetchone() == (0,)
        assert len(leftover) == 100  # 99 remaining rows + final EOF
        sets.close()
        assert leftover == []

    def test_dict_cursor_converts_rows_lazily(self):
        packets = select_set([(1,)]) + select_set([(2,)], status=0)
        conn, _ = mock_connection(packets)
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        with cursor.iter_result_sets("SELECT 1; SELECT 2") as sets:
            first = next(sets)
            assert first.fetchone() == {"a": 1}
            second = next(sets)
            assert list(second) == [{"a": 2}]


class TestResultSets(base.PyMySQLTestCase):
    """Integration tests requiring a live MySQL server."""

    procedure_name = "pymysql_test_result_sets"

    def _create_procedure(self, cursor):
        cursor.execute(f"DROP PROCEDURE IF EXISTS `{self.procedure_name}`")
        cursor.execute(
            f"""
            CREATE PROCEDURE `{self.procedure_name}`()
            BEGIN
                SELECT 1 AS a;
                SELECT 20 AS b UNION ALL SELECT 21 UNION ALL SELECT 22;
                SELECT 30 AS c WHERE 0;
            END
            """
        )
        self.addCleanup(self._drop_procedure, cursor)

    def _drop_procedure(self, cursor):
        try:
            cursor.execute(f"DROP PROCEDURE IF EXISTS `{self.procedure_name}`")
        except pymysql.MySQLError:
            pass

    def test_callproc_three_sets_and_reuse_connection(self):
        conn = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        cursor = conn.cursor()
        self._create_procedure(cursor)

        with cursor.iter_result_sets(f"CALL `{self.procedure_name}`()") as sets:
            first = next(sets)
            assert first.description[0][0] == "a"
            assert list(first) == [(1,)]

            second = next(sets)
            assert second.description[0][0] == "b"
            assert second.fetchone() == (20,)
            # Only one row read; remaining rows drain on advance.

            third = next(sets)
            assert third.description[0][0] == "c"
            # The empty result set is preserved.
            assert list(third) == []
            assert third.rowcount == 0

            # The trailing empty result set created by CALL is also visible.
            trailing = next(sets)
            assert trailing.description is None
            assert trailing.rowcount == 0
            assert trailing.more_results is False

            with pytest.raises(StopIteration):
                next(sets)

        # Normal end: the connection runs the next query immediately.
        cursor.execute("SELECT 42")
        assert cursor.fetchall() == [(42,)]

    def test_context_exit_drains_everything(self):
        conn = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        cursor = conn.cursor()
        self._create_procedure(cursor)

        with cursor.iter_result_sets(f"CALL `{self.procedure_name}`()") as sets:
            first = next(sets)
            assert first.fetchone() == (1,)
            # Leave every other set (and the CALL trailing set) unread.

        cursor.execute("SELECT 'after'")
        assert cursor.fetchall() == [("after",)]

    def test_multi_statement_error_attribution(self):
        conn = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        cursor = conn.cursor()
        sets = cursor.iter_result_sets("SELECT 1; this is not sql")
        first = next(sets)
        assert list(first) == [(1,)]
        with pytest.raises(pymysql.ProgrammingError):
            next(sets)
        cursor.execute("SELECT 2")
        assert cursor.fetchall() == [(2,)]

    def test_affected_rows_warning_count_and_more_results(self):
        conn = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        cursor = conn.cursor()
        cursor.execute("CREATE TEMPORARY TABLE rs_meta (id INT)")

        sets = cursor.iter_result_sets(
            "INSERT INTO rs_meta VALUES (1),(2); "
            "DROP TABLE IF EXISTS rs_no_such_table; "
            "SELECT 1"
        )
        insert_set = next(sets)
        assert insert_set.description is None
        assert insert_set.rowcount == 2
        assert insert_set.more_results is True

        warning_set = next(sets)
        assert warning_set.warning_count >= 1

        select_set = next(sets)
        assert list(select_set) == [(1,)]
        assert select_set.more_results is False
        sets.close()

        cursor.execute("SELECT COUNT(*) FROM rs_meta")
        assert cursor.fetchone() == (2,)
