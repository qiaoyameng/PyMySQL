# Server-side prepared statements (COM_STMT_PREPARE / COM_STMT_EXECUTE /
# COM_STMT_CLOSE) and the binary row protocol they return.
# https://dev.mysql.com/doc/dev/mysql-server/latest/page_protocol_com_stmt_prepare.html

import datetime
import struct
from decimal import Decimal

from . import err
from .connections import TEXT_TYPES, _lenenc_int
from .constants import COMMAND, CR, FIELD_TYPE, FLAG
from .protocol import (
    EOFPacketWrapper,
    FieldDescriptorPacket,
    OKPacketWrapper,
)

_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_UINT64_MAX = (1 << 64) - 1


def _encode_param(value, connection):
    """Encode one parameter for COM_STMT_EXECUTE.

    Returns ``(type_code, unsigned, payload)`` where payload is the value in
    the binary protocol, or ``None`` when the value is SQL NULL.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return FIELD_TYPE.TINY, False, bytes([value])
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _UINT64_MAX:
            raise err.DataError(f"integer parameter {value} does not fit in 64 bits")
        if value > _INT64_MAX:
            return FIELD_TYPE.LONGLONG, True, struct.pack("<Q", value)
        return FIELD_TYPE.LONGLONG, False, struct.pack("<q", value)
    if isinstance(value, float):
        return FIELD_TYPE.DOUBLE, False, struct.pack("<d", value)
    if isinstance(value, str):
        data = value.encode(connection.encoding)
        return FIELD_TYPE.VAR_STRING, False, _lenenc_int(len(data)) + data
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        return FIELD_TYPE.BLOB, False, _lenenc_int(len(data)) + data
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise err.ProgrammingError(
                f"{str(value).lower()} can not be used with MySQL"
            )
        data = format(value, "f").encode("ascii")
        return FIELD_TYPE.NEWDECIMAL, False, _lenenc_int(len(data)) + data
    if isinstance(value, datetime.datetime):
        payload = struct.pack(
            "<HBBBBB",
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
        )
        if value.microsecond:
            return (
                FIELD_TYPE.DATETIME,
                False,
                b"\x0b" + payload + struct.pack("<I", value.microsecond),
            )
        return FIELD_TYPE.DATETIME, False, b"\x07" + payload
    if isinstance(value, datetime.date):
        payload = struct.pack("<HBB", value.year, value.month, value.day)
        return FIELD_TYPE.DATE, False, b"\x04" + payload
    if isinstance(value, datetime.timedelta):
        negative = value.days < 0
        if negative:
            value = -value
        minutes, seconds = divmod(value.seconds, 60)
        hours, minutes = divmod(minutes, 60)
        payload = struct.pack(
            "<BIBBB", 1 if negative else 0, value.days, hours, minutes, seconds
        )
        if value.microseconds:
            return (
                FIELD_TYPE.TIME,
                False,
                b"\x0c" + payload + struct.pack("<I", value.microseconds),
            )
        return FIELD_TYPE.TIME, False, b"\x08" + payload
    if isinstance(value, datetime.time):
        payload = struct.pack("<BIBBB", 0, 0, value.hour, value.minute, value.second)
        if value.microsecond:
            return (
                FIELD_TYPE.TIME,
                False,
                b"\x0c" + payload + struct.pack("<I", value.microsecond),
            )
        return FIELD_TYPE.TIME, False, b"\x08" + payload
    raise TypeError(
        f"{type(value).__name__} can not be used as a prepared statement parameter"
    )


def _read_binary_datetime(packet, field_type):
    """Read a binary-protocol DATE/DATETIME/TIMESTAMP value."""
    length = packet.read_uint8()
    if length == 0:
        # "zero" date (0000-00-00); there is no Python representation.
        return None
    year, month, day = packet.read_struct("<HBB")
    if field_type in (FIELD_TYPE.DATE, FIELD_TYPE.NEWDATE) or length == 4:
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None
    hour, minute, second = packet.read_struct("<BBB")
    microsecond = packet.read_uint32() if length == 11 else 0
    try:
        return datetime.datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return None


def _read_binary_time(packet):
    """Read a binary-protocol TIME value as datetime.timedelta."""
    length = packet.read_uint8()
    if length == 0:
        return datetime.timedelta(0)
    negative, days, hours, minutes, seconds = packet.read_struct("<BIBBB")
    microseconds = packet.read_uint32() if length == 12 else 0
    delta = datetime.timedelta(
        days=days,
        hours=hours,
        minutes=minutes,
        seconds=seconds,
        microseconds=microseconds,
    )
    return -delta if negative else delta


def _read_binary_value(packet, field, connection):
    """Read one column value of a binary-protocol row."""
    field_type = field.type_code
    unsigned = bool(field.flags & FLAG.UNSIGNED)
    if field_type == FIELD_TYPE.TINY:
        return packet.read_struct("<B" if unsigned else "<b")[0]
    if field_type in (FIELD_TYPE.SHORT, FIELD_TYPE.YEAR):
        return packet.read_struct("<H" if unsigned else "<h")[0]
    if field_type in (FIELD_TYPE.LONG, FIELD_TYPE.INT24):
        return packet.read_struct("<I" if unsigned else "<i")[0]
    if field_type == FIELD_TYPE.LONGLONG:
        return packet.read_struct("<Q" if unsigned else "<q")[0]
    if field_type == FIELD_TYPE.FLOAT:
        return packet.read_struct("<f")[0]
    if field_type == FIELD_TYPE.DOUBLE:
        return packet.read_struct("<d")[0]
    if field_type in (
        FIELD_TYPE.DATE,
        FIELD_TYPE.NEWDATE,
        FIELD_TYPE.DATETIME,
        FIELD_TYPE.TIMESTAMP,
    ):
        return _read_binary_datetime(packet, field_type)
    if field_type == FIELD_TYPE.TIME:
        return _read_binary_time(packet)
    # Everything else arrives as a length-encoded string.
    data = packet.read_length_coded_string()
    if data is None:
        return None
    if field_type in (FIELD_TYPE.DECIMAL, FIELD_TYPE.NEWDECIMAL):
        converter = connection.decoders.get(field_type, Decimal)
        return converter(data.decode("ascii"))
    if field_type in TEXT_TYPES and field.charsetnr != 63:
        if connection.use_unicode:
            return data.decode(connection.encoding)
        return data
    if field_type == FIELD_TYPE.JSON and connection.use_unicode:
        return data.decode(connection.encoding)
    return data


class StmtResult:
    """Buffered result of one PreparedStatement.execute() call."""

    def __init__(self, connection, statement):
        self.connection = connection
        self.statement = statement
        self.affected_rows = None
        self.insert_id = None
        self.server_status = None
        self.warning_count = 0
        self.message = None
        self.description = None
        self.rows = ()
        self.fields = None

    def read(self):
        conn = self.connection
        first_packet = conn._read_packet()
        if first_packet.is_ok_packet():
            ok = OKPacketWrapper(first_packet)
            self.affected_rows = ok.affected_rows
            self.insert_id = ok.insert_id
            self.server_status = ok.server_status
            self.warning_count = ok.warning_count
            self.message = ok.message
            conn.server_status = ok.server_status
            return
        field_count = first_packet.read_length_encoded_integer()
        self.fields = []
        description = []
        for _ in range(field_count):
            field = conn._read_packet(FieldDescriptorPacket)
            self.fields.append(field)
            description.append(field.description())
        eof_packet = conn._read_packet()
        if not eof_packet.is_eof_packet():
            raise err.OperationalError(
                CR.CR_MALFORMED_PACKET,
                "Protocol error, expecting EOF after column definitions",
            )
        self.description = tuple(description)
        rows = []
        while True:
            packet = conn._read_packet()
            if packet.is_eof_packet():
                eof = EOFPacketWrapper(packet)
                self.warning_count = eof.warning_count
                self.server_status = eof.server_status
                conn.server_status = eof.server_status
                break
            rows.append(self._read_row(packet))
        self.affected_rows = len(rows)
        self.rows = tuple(rows)

    def _read_row(self, packet):
        """Parse one binary-protocol resultset row.

        The row header byte (0x00) is followed by a NULL bitmap whose bits
        are offset by 2 -- unlike the parameter NULL bitmap in
        COM_STMT_EXECUTE, which starts at bit 0.
        """
        packet.advance(1)  # row header, always 0x00
        num_fields = len(self.fields)
        null_bitmap = packet.read((num_fields + 7 + 2) // 8)
        row = []
        for i, field in enumerate(self.fields):
            if null_bitmap[(i + 2) // 8] & (1 << ((i + 2) % 8)):
                row.append(None)
            else:
                row.append(_read_binary_value(packet, field, self.connection))
        return tuple(row)


class PreparedStatement:
    """A server-side prepared statement bound to one connection generation.

    Created by ``Connection.prepare()``. The statement becomes invalid when
    the connection is closed or re-established; call ``close()`` to release
    the server-side handle explicitly.
    """

    def __init__(
        self,
        connection,
        statement_id,
        num_params,
        param_fields,
        column_fields,
        generation,
    ):
        self._connection = connection
        self.statement_id = statement_id
        self.num_params = num_params
        self.param_fields = param_fields
        self.column_fields = column_fields
        self._generation = generation
        self._closed = False

    @classmethod
    def _read_prepare_response(cls, connection):
        """Read the COM_STMT_PREPARE response and build a statement."""
        packet = connection._read_packet()
        status = packet.read_uint8()
        if status != 0:
            raise err.OperationalError(
                CR.CR_MALFORMED_PACKET,
                "Protocol error, expecting COM_STMT_PREPARE OK",
            )
        statement_id = packet.read_uint32()
        num_columns = packet.read_uint16()
        num_params = packet.read_uint16()
        packet.advance(1)  # reserved filler
        packet.read_uint16()  # warning count

        param_fields = []
        if num_params:
            for _ in range(num_params):
                param_fields.append(connection._read_packet(FieldDescriptorPacket))
            cls._read_expected_eof(connection)
        column_fields = []
        if num_columns:
            for _ in range(num_columns):
                column_fields.append(connection._read_packet(FieldDescriptorPacket))
            cls._read_expected_eof(connection)
        return cls(
            connection,
            statement_id,
            num_params,
            param_fields,
            column_fields,
            connection._generation,
        )

    @staticmethod
    def _read_expected_eof(connection):
        packet = connection._read_packet()
        if not packet.is_eof_packet():
            raise err.OperationalError(
                CR.CR_MALFORMED_PACKET,
                "Protocol error, expecting EOF packet",
            )

    @property
    def closed(self):
        return self._closed

    def _check_valid(self):
        if self._closed:
            raise err.ProgrammingError("Prepared statement is already closed")
        conn = self._connection
        if conn is None or conn._generation != self._generation or conn._sock is None:
            raise err.InterfaceError(
                "Prepared statement belongs to a previous connection generation"
            )

    def execute(self, params=None):
        """Execute the statement with the given parameters.

        Parameter types are re-declared on every call, so the types may
        change between executions of the same statement.
        """
        self._check_valid()
        conn = self._connection
        if params is None:
            params = ()
        params = tuple(params)
        if len(params) != self.num_params:
            raise err.ProgrammingError(
                f"expected {self.num_params} parameters, got {len(params)}"
            )

        payload = struct.pack("<IBI", self.statement_id, 0, 1)
        if self.num_params:
            # Parameter NULL bitmap: bit i corresponds to parameter i,
            # with NO 2-bit offset (unlike binary result rows).
            null_bitmap = bytearray((self.num_params + 7) // 8)
            types = bytearray()
            values = []
            for i, value in enumerate(params):
                encoded = _encode_param(value, conn)
                if encoded is None:
                    null_bitmap[i // 8] |= 1 << (i % 8)
                    types += struct.pack("<BB", FIELD_TYPE.NULL, 0)
                else:
                    type_code, unsigned, data = encoded
                    types += struct.pack("<BB", type_code, 0x80 if unsigned else 0)
                    values.append(data)
            payload += bytes(null_bitmap)
            payload += b"\x01"  # new-params-bound-flag
            payload += bytes(types)
            payload += b"".join(values)

        conn._execute_command(COMMAND.COM_STMT_EXECUTE, payload)
        result = StmtResult(conn, self)
        result.read()
        conn._affected_rows = result.affected_rows
        return result

    def close(self):
        """Close the server-side statement handle.

        COM_STMT_CLOSE has no server response, so this never waits for one.
        Closing is idempotent and a no-op if the connection is gone.
        """
        if self._closed:
            return
        self._closed = True
        conn = self._connection
        self._connection = None
        if conn is None or conn._generation != self._generation:
            return
        if conn._sock is None:
            return
        conn._execute_command(
            COMMAND.COM_STMT_CLOSE, struct.pack("<I", self.statement_id)
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        del exc_info
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: S110 - __del__ must never raise
            pass
