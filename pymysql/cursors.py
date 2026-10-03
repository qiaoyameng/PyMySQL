import re
import warnings

from . import err

#: Regular expression for :meth:`Cursor.executemany`.
#: executemany only supports simple bulk insert.
#: You can use it to load large dataset.
RE_INSERT_VALUES = re.compile(
    r"\s*((?:INSERT|REPLACE)\b.+\bVALUES?\s*)"
    + r"(\(\s*(?:%s|%\([^)]+\)s)\s*(?:,\s*(?:%s|%\([^)]+\)s)\s*)*\))"
    + r'(\s*(?:AS\s+(?:`[^`]+`|"[^"]+"|[0-9A-Za-z_$]+)\s*'
    + r'(?:\(\s*(?:`[^`]+`|"[^"]+"|[0-9A-Za-z_$]+)\s*'
    + r'(?:,\s*(?:`[^`]+`|"[^"]+"|[0-9A-Za-z_$]+)\s*)*\))?\s*)?'
    + r"(?:ON DUPLICATE.*)?);?\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _backquote_escape(s):
    return s.replace("`", "``")


class ResultSet:
    """Metadata and lazy rows of a single result set.

    Instances are produced by :meth:`Cursor.iter_result_sets`; they must not
    be created directly.

    Each object exposes this result set's metadata and a lazy iterator over
    its rows.  Rows are read from the socket one packet at a time, so result
    sets with many rows do not need to fit in memory.

    A ``ResultSet`` is consumed once: after the owning
    :class:`ResultSetIterator` advances to the next result set, iterating an
    older result set raises :class:`~pymysql.err.ProgrammingError`; this
    prevents two row iterators from interleaving reads on the same socket.
    """

    def __init__(self, stream, result):
        self._stream = stream
        self._result = result
        self._cursor = stream._cursor
        self._consumed = False
        self._superseded = False

        self.description = result.description
        self.affected_rows = result.affected_rows
        # The MySQL protocol has no row count for an unbuffered SELECT until
        # its EOF packet is seen.  Follow the DB-API convention of -1 while
        # the total is unknown and expose the final count once it is.
        self.rowcount = -1 if result.unbuffered_active else result.affected_rows
        self.warning_count = result.warning_count
        self.lastrowid = result.insert_id
        self.more_results = bool(result.has_next)
        self._rownumber = 0

    def _check_current(self):
        stream = self._stream
        if stream._closed:
            raise err.ProgrammingError("result set iterator is closed")
        if self._superseded or stream._current is not self:
            raise err.ProgrammingError("result set is no longer the current result set")

    def _read_row(self):
        stream = self._stream
        result = self._result
        self._check_current()
        if not result.unbuffered_active:
            self._finish()
            return None
        try:
            row = result._read_rowdata_packet_unbuffered()
        except BaseException:
            stream._fail()
            raise
        if row is None:
            self._finish()
            return None
        self._rownumber += 1
        return self._cursor._conv_row(row)

    def _finish(self):
        result = self._result
        self._consumed = True
        self.warning_count = result.warning_count
        self.affected_rows = self._rownumber
        self.rowcount = self._rownumber
        self.more_results = bool(result.has_next)
        self._cursor.warning_count = result.warning_count

    def fetchone(self):
        """Fetch the next row of this result set without advancing sets.

        Returns ``None`` once the result set's EOF has been reached.

        :raise pymysql.err.ProgrammingError: if the owning
        :class:`ResultSetIterator` has already advanced past this set or
        has been closed.
        """
        if self._superseded:
            self._check_current()
        if self._consumed:
            return None
        return self._read_row()

    def drain(self):
        """Read and discard every remaining row of this result set.

        Calling it on an already consumed result set is a no-op.  An error
        packet received while draining ends this set normally: it is
        propagated and the connection stays usable.

        :return: The number of rows that were discarded.
        :rtype: int
        """
        stream = self._stream
        result = self._result
        if self._superseded:
            self._check_current()
        if self._consumed:
            return 0
        discarded = 0
        try:
            while result.unbuffered_active:
                packet = result.connection._read_packet()
                if result._check_packet_is_eof(packet):
                    result.unbuffered_active = False
                    result.connection = None
                    break
                discarded += 1
        except BaseException:
            stream._fail()
            raise
        self._rownumber += discarded
        self._finish()
        return discarded

    def __iter__(self):
        return self

    def __next__(self):
        if self._superseded:
            self._check_current()
        if self._consumed:
            raise StopIteration
        row = self._read_row()
        if row is None:
            raise StopIteration
        return row

    @property
    def rownumber(self):
        """Number of rows read from this result set so far."""
        return self._rownumber


class ResultSetIterator:
    """Lazily iterate the result sets returned by one query or ``CALL``.

    Returned by :meth:`Cursor.iter_result_sets`.  Every iteration yields the
    next :class:`ResultSet`, including empty result sets and the trailing
    empty set produced by a stored-procedure ``CALL``.

    Advancing to the next result set drains the rows of the current one when
    it was not fully read, so the connection never starts a new command while
    positioned inside a result set.  As a context manager, leaving the
    ``with`` block drains all remaining result sets and the connection can
    then run another query.  If the remaining data cannot be drained safely
    (for example because the connection was lost), the connection is closed.
    """

    def __init__(self, cursor, query, args):
        self._cursor = cursor
        self._conn = cursor._get_db()
        self._current = None
        self._closed = False

        # Match execute(): a statement may only start once all earlier
        # (unbuffered or multi-statement) result sets have been consumed.
        while cursor.nextset():
            pass

        if args is not None:
            query = cursor._mogrify(query, args)
        self._query = query
        try:
            self._conn.query(query, unbuffered=True)
        except BaseException:
            # The first result set already failed (an error packet).  The
            # server has no pending data, so the connection stays usable.
            self._conn._result = None
            self._closed = True
            raise
        cursor._do_get_result()

    def _make_current(self):
        if self._current is not None:
            self._current._superseded = True
        result_set = ResultSet(self, self._conn._result)
        self._current = result_set
        self._cursor._do_get_result()
        return result_set

    def _read_next_header(self):
        """Read the header of the following result set."""
        conn = self._conn
        try:
            conn.next_result(unbuffered=True)
        except BaseException:
            self._fail()
            raise

    def __next__(self):
        if self._closed:
            raise StopIteration
        current = self._current
        if current is not None:
            if not current._consumed:
                # Clear contract: drain the current set before reading the
                # next set's header, so no two sets interleave socket reads.
                current.drain()
            if not current.more_results:
                # EOF of the whole multi-result stream.
                self._closed = True
                self._conn._result = None
                raise StopIteration
            self._read_next_header()
        return self._make_current()

    def __iter__(self):
        return self

    def _abort(self):
        """Mark the stream dead and close the connection that is out of sync."""
        self._closed = True
        conn = self._conn
        if conn is not None and conn.open:
            conn._force_close()

    def _fail(self):
        """End the stream after an error while reading it.

        A server-side error packet ends the statement normally: the server
        has no more data, so the connection can stay usable.  A transport
        failure already closed the connection, which cannot be drained and
        must stay closed.
        """
        conn = self._conn
        conn._result = None
        if conn.open:
            self._closed = True
        else:
            self._abort()

    def close(self):
        """Drain every remaining result set.

        The connection is left ready for another query when draining
        succeeds; if the socket is unusable it is closed instead.
        """
        if self._closed:
            return
        try:
            for result_set in self:
                result_set.drain()
        except BaseException:
            self._abort()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_value, traceback
        if self._closed:
            return False
        try:
            self.close()
        except BaseException:
            # Preserve an exception already raised inside the with-block.
            if exc_type is not None:
                return False
            raise
        return False


class Cursor:
    """
    This is the object used to interact with the database.

    Do not create an instance of a Cursor yourself. Call
    connections.Connection.cursor().

    See `Cursor <https://www.python.org/dev/peps/pep-0249/#cursor-objects>`_ in
    the specification.
    """

    #: Max statement size which :meth:`executemany` generates.
    #:
    #: Max size of allowed statement is max_allowed_packet - packet_header_size.
    #: Default value of max_allowed_packet is 1048576.
    max_stmt_length = 1024000

    def __init__(self, connection):
        self.connection = connection
        self.warning_count = 0
        self.description = None
        self.rownumber = 0
        self.rowcount = -1
        self.arraysize = 1
        self._executed = None
        self._result = None
        self._rows = None

    def close(self):
        """
        Closing a cursor just exhausts all remaining data.
        """
        conn = self.connection
        if conn is None:
            return
        try:
            while self.nextset():
                pass
        finally:
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        del exc_info
        self.close()

    def _get_db(self):
        if not self.connection:
            raise err.ProgrammingError("Cursor closed")
        return self.connection

    def _check_executed(self):
        if not self._executed:
            raise err.ProgrammingError("execute() first")

    def _conv_row(self, row):
        return row

    def setinputsizes(self, *args):
        """Does nothing, required by DB API."""

    def setoutputsizes(self, *args):
        """Does nothing, required by DB API."""

    def _nextset(self, unbuffered=False):
        """Get the next query set."""
        conn = self._get_db()
        current_result = self._result
        if current_result is None or current_result is not conn._result:
            return None
        if not current_result.has_next:
            return None
        self._result = None
        self._clear_result()
        conn.next_result(unbuffered=unbuffered)
        self._do_get_result()
        return True

    def nextset(self):
        return self._nextset(False)

    def iter_result_sets(self, query, args=None):
        """Iterate multiple result sets lazily.

        Execute *query* (typically a multi-statement query or a ``CALL`` of a
        stored procedure) and return a :class:`ResultSetIterator` that yields
        one :class:`ResultSet` per result set.  Rows are read from the server
        on demand rather than buffered, so skipping a result set does not
        pull its rows into memory.

        Advancing past a result set that was not fully read drains its
        remaining rows first.  Two result sets never hold active row
        iterators at once: iterating an older result set once the stream has
        advanced raises :class:`~pymysql.err.ProgrammingError`.

        The returned object is a context manager.  Leaving the ``with``
        block drains every remaining result set, after which this cursor's
        connection can execute another query.  If draining is impossible
        because the connection was lost, the connection is closed::

            with cur.iter_result_sets("CALL my_proc()") as sets:
                for result_set in sets:
                    for row in result_set:
                        process(row)
            cur.execute("SELECT 1")

        :param query: Query to execute.
        :type query: str

        :param args: Parameters used with the query. (optional)
        :type args: tuple, list or dict

        :rtype: ResultSetIterator
        """
        return ResultSetIterator(self, query, args)

    def _mogrify(self, query, args) -> str:
        """Return query after binding args."""
        escape = self._get_db().escape

        if isinstance(args, dict):
            args = {key: escape(item) for key, item in args.items()}
        elif isinstance(args, (list, tuple)):
            args = tuple(map(escape, args))
        else:
            # Escaping a single argument is not part of the DB-API or mysqlclient.
            # It will be removed in the next version.
            warnings.warn(
                "single argument is deprecated and will be removed in the next version.",
                DeprecationWarning,
                stacklevel=3,
            )
            args = escape(args)
        try:
            return query % args
        except TypeError as m:
            raise err.ProgrammingError(str(m))

    def mogrify(self, query, args=None) -> str:
        """
        Returns the exact string that would be sent to the database by calling the
        execute() method.

        :param query: Query to mogrify.
        :type query: str

        :param args: Parameters used with query. (optional)
        :type args: tuple, list or dict

        :return: The query with argument binding applied.
        :rtype: str

        This method follows the extension to the DB API 2.0 followed by Psycopg.
        """
        if args is None:
            return query

        return self._mogrify(query, args)

    def execute(self, query, args=None):
        """Execute a query.

        :param query: Query to execute.
        :type query: str

        :param args: Parameters used with query. (optional)
        :type args: tuple, list or dict

        :return: Number of affected rows.
        :rtype: int

        If args is a list or tuple, %s can be used as a placeholder in the query.
        If args is a dict, %(name)s can be used as a placeholder in the query.
        """
        while self.nextset():
            pass

        if args is not None:
            query = self._mogrify(query, args)
        result = self._query(query)
        self._executed = query
        return result

    def executemany(self, query, args):
        """Run several data against one query.

        :param query: Query to execute.
        :type query: str

        :param args: Sequence of sequences or mappings. It is used as parameter.
        :type args: tuple or list

        :return: Number of rows affected, if any.
        :rtype: int or None

        This method improves performance on multiple-row INSERT and
        REPLACE. Otherwise it is equivalent to looping over args with
        execute().
        """
        if not args:
            return

        m = RE_INSERT_VALUES.match(query)
        if m:
            q_prefix = m.group(1) % ()
            q_values = m.group(2).rstrip()
            q_postfix = (m.group(3) or "") % ()
            assert q_values[0] == "(" and q_values[-1] == ")"
            return self._do_execute_many(
                q_prefix,
                q_values,
                q_postfix,
                args,
                self.max_stmt_length,
                self._get_db().encoding,
            )

        self.rowcount = sum(self.execute(query, arg) for arg in args)
        return self.rowcount

    def _do_execute_many(
        self, prefix, values, postfix, args, max_stmt_length, encoding
    ):
        encoding = self._get_db().encoding
        if isinstance(prefix, str):
            prefix = prefix.encode(encoding)
        if isinstance(postfix, str):
            postfix = postfix.encode(encoding)
        sql = bytearray(prefix)
        args = iter(args)
        sql += self._mogrify(values, next(args)).encode(encoding)
        rows = 0
        for arg in args:
            v = self._mogrify(values, arg).encode(encoding)
            if len(sql) + len(v) + len(postfix) + 1 > max_stmt_length:
                rows += self.execute(sql + postfix)
                sql = bytearray(prefix)
            else:
                sql += b","
            sql += v
        rows += self.execute(sql + postfix)
        self.rowcount = rows
        return rows

    def callproc(self, procname, args=()):
        """Execute stored procedure procname with args.

        :param procname: Name of procedure to execute on server.
        :type procname: str

        :param args: Sequence of parameters to use with procedure.
        :type args: tuple or list

        Returns the original args.

        Compatibility warning: PEP-249 specifies that any modified
        parameters must be returned. This is currently impossible
        as they are only available by storing them in a server
        variable and then retrieved by a query. Since stored
        procedures return zero or more result sets, there is no
        reliable way to get at OUT or INOUT parameters via callproc.
        The server variables are named @_procname_n, where procname
        is the parameter above and n is the position of the parameter
        (from zero). Once all result sets generated by the procedure
        have been fetched, you can issue a SELECT @_procname_0, ...
        query using .execute() to get any OUT or INOUT values.

        Compatibility warning: The act of calling a stored procedure
        itself creates an empty result set. This appears after any
        result sets generated by the procedure. This is non-standard
        behavior with respect to the DB-API. Be sure to use nextset()
        to advance through all result sets; otherwise you may get
        disconnected.
        """
        procname_escaped = _backquote_escape(procname)
        conn = self._get_db()

        if args:
            fmt = f"@`_{procname_escaped}_%d`=%s"
            self._query(
                "SET %s"
                % ",".join(
                    fmt % (index, conn.escape(arg)) for index, arg in enumerate(args)
                )
            )
            self.nextset()

        q = "CALL `{}`({})".format(
            procname_escaped,
            ",".join([f"@`_{procname_escaped}_{i}`" for i in range(len(args))]),
        )
        self._query(q)
        self._executed = q
        return args

    def fetchone(self):
        """Fetch the next row."""
        self._check_executed()
        if self._rows is None or self.rownumber >= len(self._rows):
            return None
        result = self._rows[self.rownumber]
        self.rownumber += 1
        return result

    def fetchmany(self, size=None):
        """Fetch several rows."""
        self._check_executed()
        if self._rows is None:
            # Django expects () for EOF.
            # https://github.com/django/django/blob/0c1518ee429b01c145cf5b34eab01b0b92f8c246/django/db/backends/mysql/features.py#L8
            return ()
        end = self.rownumber + (size or self.arraysize)
        result = self._rows[self.rownumber : end]
        self.rownumber = min(end, len(self._rows))
        return result

    def fetchall(self):
        """Fetch all the rows."""
        self._check_executed()
        if self._rows is None:
            return []
        if self.rownumber:
            result = self._rows[self.rownumber :]
        else:
            result = self._rows
        self.rownumber = len(self._rows)
        return result

    def scroll(self, value, mode="relative"):
        self._check_executed()
        if mode == "relative":
            r = self.rownumber + value
        elif mode == "absolute":
            r = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)

        if not (0 <= r < len(self._rows)):
            raise IndexError("out of range")
        self.rownumber = r

    def _query(self, q):
        conn = self._get_db()
        self._clear_result()
        conn.query(q)
        self._do_get_result()
        return self.rowcount

    def _clear_result(self):
        self.rownumber = 0
        self._result = None

        self.rowcount = 0
        self.warning_count = 0
        self.description = None
        self.lastrowid = None
        self._rows = None

    def _do_get_result(self):
        conn = self._get_db()

        self._result = result = conn._result

        self.rowcount = result.affected_rows
        self.warning_count = result.warning_count
        self.description = result.description
        self.lastrowid = result.insert_id
        self._rows = result.rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class DictCursorMixin:
    # You can override this to use OrderedDict or other dict-like types.
    dict_type = dict

    def _do_get_result(self):
        super()._do_get_result()
        fields = []
        if self.description:
            for f in self._result.fields:
                name = f.name
                if name in fields:
                    name = f.table_name + "." + name
                fields.append(name)
            self._fields = fields

        if fields and self._rows:
            self._rows = [self._conv_row(r) for r in self._rows]

    def _conv_row(self, row):
        if row is None:
            return None
        return self.dict_type(zip(self._fields, row))


class DictCursor(DictCursorMixin, Cursor):
    """A cursor which returns results as a dictionary"""


class SSCursor(Cursor):
    """
    Unbuffered Cursor, mainly useful for queries that return a lot of data,
    or for connections to remote servers over a slow network.

    Instead of copying every row of data into a buffer, this will fetch
    rows as needed. The upside of this is the client uses much less memory,
    and rows are returned much faster when traveling over a slow network
    or if the result set is very big.

    There are limitations, though. The MySQL protocol doesn't support
    returning the total number of rows, so the only way to tell how many rows
    there are is to iterate over every row returned. Also, it currently isn't
    possible to scroll backwards, as only the current row is held in memory.
    """

    def _conv_row(self, row):
        return row

    def close(self):
        conn = self.connection
        if conn is None:
            return

        if self._result is not None and self._result is conn._result:
            self._result._finish_unbuffered_query()

        try:
            while self.nextset():
                pass
        finally:
            self.connection = None

    __del__ = close

    def _query(self, q):
        conn = self._get_db()
        self._clear_result()
        conn.query(q, unbuffered=True)
        self._do_get_result()
        return self.rowcount

    def nextset(self):
        return self._nextset(unbuffered=True)

    def read_next(self):
        """Read next row."""
        return self._conv_row(self._result._read_rowdata_packet_unbuffered())

    def fetchone(self):
        """Fetch next row."""
        self._check_executed()
        row = self.read_next()
        if row is None:
            self.warning_count = self._result.warning_count
            return None
        self.rownumber += 1
        return row

    def fetchall(self):
        """
        Fetch all, as per MySQLdb. Pretty useless for large queries, as
        it is buffered. See fetchall_unbuffered(), if you want an unbuffered
        generator version of this method.
        """
        return list(self.fetchall_unbuffered())

    def fetchall_unbuffered(self):
        """
        Fetch all, implemented as a generator, which isn't to standard,
        however, it doesn't make sense to return everything in a list, as that
        would use ridiculous memory for large result sets.
        """
        return iter(self.fetchone, None)

    def fetchmany(self, size=None):
        """Fetch many."""
        self._check_executed()
        if size is None:
            size = self.arraysize

        rows = []
        for i in range(size):
            row = self.read_next()
            if row is None:
                self.warning_count = self._result.warning_count
                break
            rows.append(row)
            self.rownumber += 1
        if not rows:
            # Django expects () for EOF.
            # https://github.com/django/django/blob/0c1518ee429b01c145cf5b34eab01b0b92f8c246/django/db/backends/mysql/features.py#L8
            return ()
        return rows

    def scroll(self, value, mode="relative"):
        self._check_executed()

        if mode == "relative":
            if value < 0:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor"
                )

            for _ in range(value):
                self.read_next()
            self.rownumber += value
        elif mode == "absolute":
            if value < self.rownumber:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor"
                )

            end = value - self.rownumber
            for _ in range(end):
                self.read_next()
            self.rownumber = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)


class SSDictCursor(DictCursorMixin, SSCursor):
    """An unbuffered cursor, which returns results as a dictionary"""
