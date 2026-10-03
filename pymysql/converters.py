from __future__ import annotations

import datetime
import re
import time
import warnings
from decimal import Decimal
from typing import NamedTuple

from .constants import FIELD_TYPE
from .err import ProgrammingError

# This silly alias is needed for aiomysql compatibility for now. It is scheduled for removal in 1.3.0.
# https://github.com/PyMySQL/PyMySQL/issues/1274
escape_dict = escape_bytes_prefixed = "DO NOT IMPORT THIS!!!"


def escape_item(val, charset=None, mapping=None):
    if mapping is None:
        mapping = encoders
    encoder = mapping.get(type(val))

    # Fallback to default when no encoder found
    if not encoder:
        try:
            encoder = mapping[str]
        except KeyError:
            raise TypeError("no default type converter defined")

    if encoder == escape_sequence:
        val = encoder(val, charset, mapping)
    else:
        val = encoder(val, mapping)
    return val


def escape_not_supported(val, charset=None, mapping=None):
    raise TypeError(f"{type(val).__name__} can not be used as parameter")


def escape_sequence(val, charset=None, mapping=None):
    n = []
    for item in val:
        quoted = escape_item(item, charset, mapping)
        n.append(quoted)
    return "(" + ",".join(n) + ")"


def escape_set(val, charset=None, mapping=None):
    warnings.warn("escape_set is deprecated", DeprecationWarning, stacklevel=2)
    return ",".join([escape_item(x, charset, mapping) for x in val])


def escape_bool(value, mapping=None):
    return str(int(value))


def escape_int(value, mapping=None):
    return str(value)


def escape_float(value, mapping=None):
    s = repr(value)
    if s in ("inf", "-inf", "nan"):
        raise ProgrammingError(f"{s} can not be used with MySQL")
    if "e" not in s:
        s += "e0"
    return s


_escape_table = [chr(x) for x in range(128)]
_escape_table[0] = "\\0"
_escape_table[ord("\\")] = "\\\\"
_escape_table[ord("\n")] = "\\n"
_escape_table[ord("\r")] = "\\r"
_escape_table[ord("\032")] = "\\Z"
_escape_table[ord('"')] = '\\"'
_escape_table[ord("'")] = "\\'"


def escape_string(value, mapping=None):
    """escapes *value* without adding quote.

    Value should be unicode
    """
    return value.translate(_escape_table)


def escape_bytes(value, mapping=None) -> str:
    return "_binary X'%s'" % value.hex()


def escape_str(value, mapping=None) -> str:
    return "'" + escape_string(str(value)) + "'"


def escape_None(value, mapping=None):
    return "NULL"


def escape_timedelta(obj, mapping=None):
    # timedelta normalizes a negative value so that seconds/microseconds are
    # non-negative and the sign lives entirely in days, so the signed magnitude
    # must be reconstructed before it is split into hours/minutes/seconds --
    # otherwise a negative TIME comes out with complemented sub-hour fields.
    sign = ""
    if obj.days < 0:
        sign = "-"
        obj = abs(obj)

    micros = obj.microseconds
    minutes, seconds = divmod(obj.seconds, 60)
    hours, minutes = divmod(minutes, 60)
    hours += obj.days * 24

    if micros:
        return f"'{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{micros:06d}'"
    return f"'{sign}{hours:02d}:{minutes:02d}:{seconds:02d}'"


def escape_time(obj, mapping=None):
    if obj.microsecond:
        fmt = "'{0.hour:02}:{0.minute:02}:{0.second:02}.{0.microsecond:06}'"
    else:
        fmt = "'{0.hour:02}:{0.minute:02}:{0.second:02}'"
    return fmt.format(obj)


def escape_datetime(obj, mapping=None):
    if obj.microsecond:
        fmt = (
            "'{0.year:04}-{0.month:02}-{0.day:02}"
            + " {0.hour:02}:{0.minute:02}:{0.second:02}.{0.microsecond:06}'"
        )
    else:
        fmt = "'{0.year:04}-{0.month:02}-{0.day:02} {0.hour:02}:{0.minute:02}:{0.second:02}'"
    return fmt.format(obj)


def escape_date(obj, mapping=None):
    fmt = "'{0.year:04}-{0.month:02}-{0.day:02}'"
    return fmt.format(obj)


def escape_struct_time(obj, mapping=None):
    return escape_datetime(datetime.datetime(*obj[:6]))


def Decimal2Literal(o, d):
    if not o.is_finite():
        raise ProgrammingError("%s can not be used with MySQL" % str(o).lower())
    return format(o, "f")


def _convert_second_fraction(s):
    if not s:
        return 0
    # Pad zeros to ensure the fraction length in microseconds
    s = s.ljust(6, "0")
    return int(s[:6])


DATETIME_RE = re.compile(
    r"(\d{1,4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2}):(\d{1,2})(?:.(\d{1,6}))?"
)


def convert_datetime(obj):
    """Returns a DATETIME or TIMESTAMP column value as a datetime object:

      >>> convert_datetime('2007-02-25 23:06:20')
      datetime.datetime(2007, 2, 25, 23, 6, 20)
      >>> convert_datetime('2007-02-25T23:06:20')
      datetime.datetime(2007, 2, 25, 23, 6, 20)

    Illegal values are returned as str:

      >>> convert_datetime('2007-02-31T23:06:20')
      '2007-02-31T23:06:20'
      >>> convert_datetime('0000-00-00 00:00:00')
      '0000-00-00 00:00:00'
    """
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("ascii")

    m = DATETIME_RE.match(obj)
    if not m:
        return convert_date(obj)

    try:
        groups = list(m.groups())
        groups[-1] = _convert_second_fraction(groups[-1])
        return datetime.datetime(*[int(x) for x in groups])
    except ValueError:
        return convert_date(obj)


TIMEDELTA_RE = re.compile(r"(-)?(\d{1,3}):(\d{1,2}):(\d{1,2})(?:.(\d{1,6}))?")


def convert_timedelta(obj):
    """Returns a TIME column as a timedelta object:

      >>> convert_timedelta('25:06:17')
      datetime.timedelta(days=1, seconds=3977)
      >>> convert_timedelta('-25:06:17')
      datetime.timedelta(days=-2, seconds=82423)

    Illegal values are returned as string:

      >>> convert_timedelta('random crap')
      'random crap'

    Note that MySQL always returns TIME columns as (+|-)HH:MM:SS, but
    can accept values as (+|-)DD HH:MM:SS. The latter format will not
    be parsed correctly by this function.
    """
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("ascii")

    m = TIMEDELTA_RE.match(obj)
    if not m:
        return obj

    try:
        groups = list(m.groups())
        groups[-1] = _convert_second_fraction(groups[-1])
        negate = -1 if groups[0] else 1
        hours, minutes, seconds, microseconds = groups[1:]

        tdelta = (
            datetime.timedelta(
                hours=int(hours),
                minutes=int(minutes),
                seconds=int(seconds),
                microseconds=int(microseconds),
            )
            * negate
        )
        return tdelta
    except ValueError:
        return obj


TIME_RE = re.compile(r"(\d{1,2}):(\d{1,2}):(\d{1,2})(?:.(\d{1,6}))?")


def convert_time(obj):
    """Returns a TIME column as a time object:

      >>> convert_time('15:06:17')
      datetime.time(15, 6, 17)

    Illegal values are returned as str:

      >>> convert_time('-25:06:17')
      '-25:06:17'
      >>> convert_time('random crap')
      'random crap'

    Note that MySQL always returns TIME columns as (+|-)HH:MM:SS, but
    can accept values as (+|-)DD HH:MM:SS. The latter format will not
    be parsed correctly by this function.

    Also note that MySQL's TIME column corresponds more closely to
    Python's timedelta and not time. However if you want TIME columns
    to be treated as time-of-day and not a time offset, then you can
    use set this function as the converter for FIELD_TYPE.TIME.
    """
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("ascii")

    m = TIME_RE.match(obj)
    if not m:
        return obj

    try:
        groups = list(m.groups())
        groups[-1] = _convert_second_fraction(groups[-1])
        hours, minutes, seconds, microseconds = groups
        return datetime.time(
            hour=int(hours),
            minute=int(minutes),
            second=int(seconds),
            microsecond=int(microseconds),
        )
    except ValueError:
        return obj


def convert_date(obj):
    """Returns a DATE column as a date object:

      >>> convert_date('2007-02-26')
      datetime.date(2007, 2, 26)

    Illegal values are returned as str:

      >>> convert_date('2007-02-31')
      '2007-02-31'
      >>> convert_date('0000-00-00')
      '0000-00-00'
    """
    if isinstance(obj, (bytes, bytearray)):
        obj = obj.decode("ascii")
    try:
        return datetime.date(*[int(x) for x in obj.split("-", 2)])
    except ValueError:
        return obj


def through(x):
    return x


# def convert_bit(b):
#    b = "\x00" * (8 - len(b)) + b # pad w/ zeroes
#    return struct.unpack(">Q", b)[0]
#
#     the snippet above is right, but MySQLdb doesn't process bits,
#     so we shouldn't either
convert_bit = through


encoders = {
    bool: escape_bool,
    int: escape_int,
    float: escape_float,
    str: escape_str,
    bytes: escape_bytes,
    tuple: escape_sequence,
    list: escape_sequence,
    set: escape_sequence,
    frozenset: escape_sequence,
    dict: escape_not_supported,
    type(None): escape_None,
    datetime.date: escape_date,
    datetime.datetime: escape_datetime,
    datetime.timedelta: escape_timedelta,
    datetime.time: escape_time,
    time.struct_time: escape_struct_time,
    Decimal: Decimal2Literal,
}


decoders = {
    FIELD_TYPE.BIT: convert_bit,
    FIELD_TYPE.TINY: int,
    FIELD_TYPE.SHORT: int,
    FIELD_TYPE.LONG: int,
    FIELD_TYPE.FLOAT: float,
    FIELD_TYPE.DOUBLE: float,
    FIELD_TYPE.LONGLONG: int,
    FIELD_TYPE.INT24: int,
    FIELD_TYPE.YEAR: int,
    FIELD_TYPE.TIMESTAMP: convert_datetime,
    FIELD_TYPE.DATETIME: convert_datetime,
    FIELD_TYPE.TIME: convert_timedelta,
    FIELD_TYPE.DATE: convert_date,
    FIELD_TYPE.BLOB: through,
    FIELD_TYPE.TINY_BLOB: through,
    FIELD_TYPE.MEDIUM_BLOB: through,
    FIELD_TYPE.LONG_BLOB: through,
    FIELD_TYPE.STRING: through,
    FIELD_TYPE.VAR_STRING: through,
    FIELD_TYPE.VARCHAR: through,
    FIELD_TYPE.DECIMAL: Decimal,
    FIELD_TYPE.NEWDECIMAL: Decimal,
}


# for MySQLdb compatibility
conversions = encoders.copy()
conversions.update(decoders)
Thing2Literal = escape_str

# Run doctests with `pytest --doctest-modules pymysql/converters.py`


#: Conversion failure policy: raise ColumnConversionError.
CONVERSION_RAISE = "raise"

#: Conversion failure policy: keep the unconverted value and record a
#: ColumnConversionProblem on the cursor.
CONVERSION_KEEP = "keep"

#: Valid values for the conversion_error_policy option.
CONVERSION_ERROR_POLICIES = (CONVERSION_RAISE, CONVERSION_KEEP)


class ColumnMetadata(NamedTuple):
    """Metadata of a single result column.

    An instance of this class is passed to the column decoder selector
    (see ``column_decoder_selector`` of :class:`pymysql.connections.Connection`)
    exactly once per column per result set.

    The decoding pipeline for each non-NULL value is, in order:

    1. The raw bytes are read from the row packet.
    2. If ``encoding`` is not None, the bytes are decoded to ``str``
       (text columns). Binary columns (charset 63) stay ``bytes``.
    3. The selected converter is called with that value.

    NULL values are always returned as None; converters are never
    called for them.
    """

    #: Zero-based position of the column within the result set.
    index: int
    #: Column name (or alias) as it appears in the result set.
    name: str
    #: Original column name in the table (empty for expressions/aliases).
    org_name: str
    #: Table name (or alias) the column comes from.
    table_name: str
    #: Original table name the column comes from.
    org_table: str
    #: MySQL type code, see ``pymysql.constants.FIELD_TYPE``.
    type_code: int
    #: Column flags, see ``pymysql.constants.FLAG``.
    flags: int
    #: Character set number. 63 means binary (no decoding is applied).
    charsetnr: int
    #: Column length as reported by the server.
    length: int
    #: Number of decimals for numeric types.
    scale: int
    #: Encoding applied to the raw bytes before the converter is called,
    #: or None when the converter receives ``bytes``.
    encoding: str | None


class ColumnConversionError(Exception):
    """Raised when a column decoder fails to convert a value.

    The message intentionally contains only column metadata -- never row
    values -- so it is safe to log. The original exception is available
    as ``error`` (and as ``__cause__``).
    """

    def __init__(self, column: ColumnMetadata, error: Exception):
        self.column = column
        self.error = error
        super().__init__(
            f"failed to convert column {column.index} "
            f"({column.table_name!r}.{column.name!r}, "
            f"type_code={column.type_code}): {type(error).__name__}"
        )


class ColumnConversionProblem:
    """Record of a single column conversion failure.

    Created when ``conversion_error_policy`` is "keep" and a column
    decoder raises. The unconverted value is returned in the row; this
    record intentionally does not store it, so logged problems never
    contain row data.
    """

    __slots__ = ("column", "error", "row_number")

    def __init__(self, column: ColumnMetadata, error: Exception, row_number: int):
        #: Metadata of the column that failed to convert.
        self.column = column
        #: The exception raised by the converter.
        self.error = error
        #: Zero-based row number within the result set.
        self.row_number = row_number

    def __repr__(self):
        return (
            f"{type(self).__name__}(column={self.column.index} "
            f"name={self.column.name!r} row={self.row_number} "
            f"error={type(self.error).__name__})"
        )
