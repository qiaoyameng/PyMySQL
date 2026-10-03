# Python implementation of the MySQL prepared statement (binary) protocol.
# https://dev.mysql.com/doc/dev/mysql-server/latest/page_protocol_com_stmt_prepare.html
# https://dev.mysql.com/doc/dev/mysql-server/latest/page_protocol_com_stmt_execute.html

import datetime
import struct
from decimal import Decimal

from . import converters, err
from .connections import MySQLResult, _lenenc_int
from .constants import COMMAND, CR, FIELD_TYPE, FLAG
from .protocol import FieldDescriptorPacket

#: Cursor type flag sent with COM_STMT_EXECUTE (no cursor).
CURSOR_TYPE_NO_CURSOR = 0

_INT_STRUCTS = {
    FIELD_TYPE.TINY: ("<b", "<B"),
    FIELD_TYPE.SHORT: ("<h", "<H"),
    FIELD_TYPE.YEAR: ("<h", "<H"),
    FIELD_TYPE.LONG: ("<i", "<I"),
    FIELD_TYPE.INT24: ("<i", "<I"),
    FIELD_TYPE.LONGLONG: ("<q", "<Q"),
}


def _encode_param(value, connection):
    """Encode a Python value for COM_STMT_EXECUTE.

    Returns a (type_code, unsigned, payload) tuple. The payload is empty
    for None (NULL is conveyed through the NULL bitmap instead).
    """
    if value is None:
        return FIELD_TYPE.NULL, False, b""
    if isinstance(value, bool):
        return FIELD_TYPE.TINY, False, struct.pack("<b", value)
    if isinstance(value, int):
        if -(1 << 63) <= value < (1 << 63):
            return FIELD_TYPE.LONGLONG, False, struct.pack("<q", value)
        if (1 << 63) <= value < (1 << 64):
            return FIELD_TYPE.LONGLONG, True, struct.pack("<Q", value)
        raise err.DataError(f"Integer {value} is out of range for BIGINT")
    if isinstance(value, float):
        return FIELD_TYPE.DOUBLE, False, struct.pack("<d", value)
    if isinstance(value, str):
        data = value.encode(connection.encoding)
        return FIELD_TYPE.VAR_STRING, False, _lenenc_int(len(data)) + data
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        return FIELD_TYPE.BLOB, False, _lenenc_int(len(data)) + data
    if isinstance(value, Decimal):
        data = converters.Decimal2Literal(value, None).encode("ascii")
        return FIELD_TYPE.NEWDECIMAL, False, _lenenc_int(len(data)) + data
    if isinstance(value, datetime.datetime):
        if value.microsecond:
            payload = struct.pack(
                "<BHBBBBBI",
                11,
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
            )
        elif value.hour or value.minute or value.second:
            payload = struct.pack(
                "<BHBBBBB",
                7,
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
            )
        else:
            payload = struct.pack("<BHBB", 4, value.year, value.month, value.day)
        return FIELD_TYPE.DATETIME, False, payload
    if isinstance(value, datetime.date):
        payload = struct.pack("<BHBB", 4, value.year, value.month, value.day)
        return FIELD_TYPE.DATE, False, payload
    if isinstance(value, datetime.timedelta):
        total_microseconds = (
            value.days * 86400 + value.seconds
        ) * 10**6 + value.microseconds
        negative = total_microseconds < 0
        total_microseconds = abs(total_microseconds)
        days, remainder = divmod(total_microseconds, 86400 * 10**6)
        hours, remainder = divmod(remainder, 3600 * 10**6)
        minutes, remainder = divmod(remainder, 60 * 10**6)
        seconds, microseconds = divmod(remainder, 10**6)
        if microseconds:
            payload = struct.pack(
                "<BBIBBBI",
                12,
                int(negative),
                days,
                hours,
                minutes,
                seconds,
                microseconds,
            )
        else:
            payload = struct.pack(
                "<BBIBBB", 8, int(negative), days, hours, minutes, seconds
            )
        return FIELD_TYPE.TIME, False, payload
    if isinstance(value, datetime.time):
        if value.microsecond:
            payload = struct.pack(
                "<BBIBBBI",
                12,
                0,
                0,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
            )
        else:
            payload = struct.pack(
                "<BBIBBB", 8, 0, 0, value.hour, value.minute, value.second
            )
        return FIELD_TYPE.TIME, False, payload
    raise err.ProgrammingError(
        f"Unsupported prepared statement parameter type: {type(value).__name__}"
    )


def _read_binary_datetime(packet, field_type):
    length = packet.read_uint8()
    if length == 0:
        # "Zero" date/datetime (0000-00-00 ...).
        return None
    year, month, day = packet.read_struct("<HBB")
    if field_type == FIELD_TYPE.DATE or length == 4:
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None
    hour, minute, second = packet.read_struct("<BBB")
    microsecond = packet.read_struct("<I")[0] if length >= 11 else 0
    try:
        return datetime.datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return None


def _read_binary_time(packet):
    length = packet.read_uint8()
    if length == 0:
        return datetime.timedelta(0)
    negative, days, hour, minute, second = packet.read_struct("<BIBBB")
    microsecond = packet.read_struct("<I")[0] if length >= 12 else 0
    delta = datetime.timedelta(
        days=days,
        hours=hour,
        minutes=minute,
        seconds=second,
        microseconds=microsecond,
    )
    return -delta if negative else delta


class BinaryResult(MySQLResult):
    """MySQLResult that decodes binary protocol rows (COM_STMT_EXECUTE)."""

    def _read_row_from_packet(self, packet):
        packet.advance(1)  # 0x00 packet header for binary rows
        # The binary row NULL bitmap is offset by 2 bits.
        null_bitmap = packet.read((self.field_count + 7 + 2) // 8)
        row = []
        for i, field in enumerate(self.fields):
            bit = i + 2
            if null_bitmap[bit >> 3] & (1 << (bit & 7)):
                row.append(None)
                continue
            row.append(self._read_binary_column(packet, field, self.converters[i]))
        return tuple(row)

    def _read_binary_column(self, packet, field, converter):
        field_type = field.type_code
        int_structs = _INT_STRUCTS.get(field_type)
        if int_structs is not None:
            fmt = int_structs[1] if field.flags & FLAG.UNSIGNED else int_structs[0]
            return packet.read_struct(fmt)[0]
        if field_type == FIELD_TYPE.FLOAT:
            return packet.read_struct("<f")[0]
        if field_type == FIELD_TYPE.DOUBLE:
            return packet.read_struct("<d")[0]
        if field_type in (FIELD_TYPE.DATE, FIELD_TYPE.DATETIME, FIELD_TYPE.TIMESTAMP):
            return _read_binary_datetime(packet, field_type)
        if field_type == FIELD_TYPE.TIME:
            return _read_binary_time(packet)
        if field_type == FIELD_TYPE.NULL:
            return None
        # String-ish types (VAR_STRING, STRING, VARCHAR, BLOBs, BIT, JSON,
        # DECIMAL/NEWDECIMAL, ENUM, SET, GEOMETRY) are length coded strings.
        data = packet.read_length_coded_string()
        if data is None:
            return None
        encoding, convert = converter
        if encoding is not None:
            data = data.decode(encoding)
        if convert is not None:
            data = convert(data)
        return data


class PreparedStatement:
    """A server-side prepared statement bound to a Connection.

    Instances are created with `Connection.prepare()`. A statement belongs
    to the connection generation that created it: once the connection is
    closed or re-established, the statement handle becomes invalid.
    """

    def __init__(
        self,
        connection,
        statement_id,
        param_count,
        fields,
        description,
        warning_count,
    ):
        self._connection = connection
        self._generation = connection._stmt_generation
        self.statement_id = statement_id
        self.param_count = param_count
        self.fields = fields
        self.description = description
        self.warning_count = warning_count
        self._closed = False

    @classmethod
    def prepare(cls, connection, sql):
        """Send COM_STMT_PREPARE and build a PreparedStatement."""
        if isinstance(sql, str):
            sql = sql.encode(connection.encoding)
        connection._execute_command(COMMAND.COM_STMT_PREPARE, sql)

        packet = connection._read_packet()
        if packet.read_uint8() != 0:
            raise err.OperationalError(
                CR.CR_MALFORMED_PACKET, "Malformed COM_STMT_PREPARE response"
            )
        statement_id = packet.read_uint32()
        column_count = packet.read_uint16()
        param_count = packet.read_uint16()
        packet.advance(1)  # reserved byte
        warning_count = packet.read_uint16()

        # Parameter metadata: param_count field descriptors + EOF.
        for _ in range(param_count):
            connection._read_packet()
        if param_count:
            cls._read_eof(connection)

        # Column metadata: column_count field descriptors + EOF.
        fields = []
        description = []
        for _ in range(column_count):
            field = connection._read_packet(FieldDescriptorPacket)
            fields.append(field)
            description.append(field.description())
        if column_count:
            cls._read_eof(connection)

        return cls(
            connection,
            statement_id,
            param_count,
            tuple(fields),
            tuple(description) if description else None,
            warning_count,
        )

    @staticmethod
    def _read_eof(connection):
        packet = connection._read_packet()
        if not packet.is_eof_packet():
            raise err.OperationalError(
                CR.CR_MALFORMED_PACKET, "Expected EOF packet in prepare response"
            )

    @property
    def closed(self):
        return self._closed

    def _check_valid(self):
        if self._closed or self._connection is None:
            raise err.ProgrammingError("Cannot use a closed prepared statement")
        if (
            self._connection._sock is None
            or self._connection._stmt_generation != self._generation
        ):
            raise err.ProgrammingError(
                "Prepared statement belongs to a previous connection generation"
            )

    def execute(self, params=None):
        """Execute the prepared statement with the given parameters.

        Parameter types are re-declared on every execution, so the types of
        the supplied values may change between calls.

        :raise ProgrammingError: If the number of parameters does not match
            the number of placeholders in the prepared SQL.
        """
        self._check_valid()
        connection = self._connection
        if params is None:
            params = ()
        if len(params) != self.param_count:
            raise err.ProgrammingError(
                f"Expected {self.param_count} parameters, got {len(params)}"
            )

        payload = bytearray()
        payload += struct.pack("<IBI", self.statement_id, CURSOR_TYPE_NO_CURSOR, 1)
        if self.param_count:
            # NULL bitmap for parameters: no offset (unlike result rows).
            null_bitmap = bytearray((self.param_count + 7) // 8)
            types = bytearray()
            values = bytearray()
            for i, value in enumerate(params):
                type_code, unsigned, data = _encode_param(value, connection)
                if value is None:
                    null_bitmap[i >> 3] |= 1 << (i & 7)
                types += struct.pack("<BB", type_code, 0x80 if unsigned else 0)
                values += data
            # new_params_bound_flag is always 1: types may change per call.
            payload += bytes(null_bitmap) + b"\x01" + bytes(types) + bytes(values)

        connection._execute_command(COMMAND.COM_STMT_EXECUTE, bytes(payload))
        result = BinaryResult(connection)
        result.read()
        connection._result = result
        connection._affected_rows = result.affected_rows
        if result.server_status is not None:
            connection.server_status = result.server_status
        return result

    def close(self):
        """Close the statement on the server.

        COM_STMT_CLOSE has no server response, so this method never waits
        for a reply. Closing is idempotent and silently does nothing if the
        connection is already gone or was re-established (the server has
        already forgotten the statement in those cases).
        """
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        self._connection = None
        if connection is None or connection._sock is None:
            return
        if connection._stmt_generation != self._generation:
            return
        connection._execute_command(
            COMMAND.COM_STMT_CLOSE, struct.pack("<I", self.statement_id)
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        del exc_info
        self.close()
