import decimal

from pymysql import converters
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.cursors import Cursor, DictCursor, SSCursor, SSDictCursor
from pymysql.tests import base


class ColumnDecoderTestCase(base.PyMySQLTestCase):
    def setUp(self):
        super().setUp()
        self.conn = self.connect()
        self.safe_create_table(
            self.conn,
            "test_column_decoder",
            """\
CREATE TABLE test_column_decoder (
    id INT PRIMARY KEY AUTO_INCREMENT,
    amount DECIMAL(10, 2),
    ratio DECIMAL(10, 4),
    note VARCHAR(50),
    payload VARBINARY(16)
)""",
        )
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO test_column_decoder (amount, ratio, note, payload)"
            " VALUES (1234.50, 0.1250, 'hello', X'00FF'),"
            " (NULL, 0.5000, 'world', X'0102'),"
            " (99999.99, NULL, 'secret', X'FF')"
        )
        cur.close()

    @staticmethod
    def amount_ratio_selector(seen=None):
        def selector(column):
            if seen is not None:
                seen.append(column)
            if column.org_name == "amount":
                return decimal.Decimal
            if column.org_name == "ratio":
                return float
            return None

        return selector

    def test_selector_receives_column_metadata(self):
        seen = []
        conn = self.connect(column_decoder_selector=self.amount_ratio_selector(seen))
        cur = conn.cursor()
        cur.execute("SELECT amount, ratio FROM test_column_decoder ORDER BY id")
        rows = cur.fetchall()

        self.assertIsInstance(rows[0][0], decimal.Decimal)
        self.assertIsInstance(rows[0][1], float)

        # The selector runs once per column per result set, not per row.
        self.assertEqual(2, len(seen))
        amount, ratio = seen
        self.assertEqual(0, amount.index)
        self.assertEqual("amount", amount.name)
        self.assertEqual("amount", amount.org_name)
        self.assertEqual("test_column_decoder", amount.org_table)
        self.assertEqual(FIELD_TYPE.NEWDECIMAL, amount.type_code)
        self.assertIsInstance(amount.flags, int)
        self.assertIsInstance(amount.charsetnr, int)
        self.assertEqual(2, amount.scale)
        self.assertEqual(1, ratio.index)
        self.assertEqual("ratio", ratio.org_name)
        self.assertEqual(4, ratio.scale)

    def test_null_stays_none_and_converter_not_called(self):
        calls = []

        def amount_decoder(value):
            calls.append(value)
            return ("amount", value)

        def selector(column):
            if column.org_name == "amount":
                return amount_decoder
            return None

        conn = self.connect(column_decoder_selector=selector)
        cur = conn.cursor()
        cur.execute("SELECT amount FROM test_column_decoder ORDER BY id")
        rows = cur.fetchall()

        self.assertIsNone(rows[1][0])
        self.assertEqual(("amount", "1234.50"), rows[0][0])
        # 3 rows but only 2 non-NULL values were converted.
        self.assertEqual(["1234.50", "99999.99"], calls)

    def test_conversion_order_text_and_binary(self):
        received = []

        def recorder(column):
            def convert(value):
                received.append((column.org_name, value))
                return value

            return convert

        conn = self.connect(column_decoder_selector=recorder)
        cur = conn.cursor()
        cur.execute("SELECT note, payload FROM test_column_decoder WHERE id = 1")
        note, payload = cur.fetchone()

        self.assertIsInstance(note, str)
        self.assertIsInstance(payload, bytes)
        # Text columns are decoded to str before the converter is called;
        # binary columns (charset 63) are passed as bytes.
        self.assertEqual(
            [("note", str), ("payload", bytes)],
            [(name, type(value)) for name, value in received],
        )

    def test_same_name_columns_use_positional_converters(self):
        def selector(column):
            if column.index == 0:
                return decimal.Decimal
            if column.index == 1:
                return float
            return None

        conn = self.connect(column_decoder_selector=selector)
        cur = conn.cursor()
        cur.execute(
            "SELECT amount AS d, ratio AS d FROM test_column_decoder"
            " ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsInstance(row[0], decimal.Decimal)
        self.assertIsInstance(row[1], float)

    def test_same_name_columns_dict_cursor(self):
        def selector(column):
            if column.index == 0:
                return decimal.Decimal
            if column.index == 1:
                return float
            return None

        conn = self.connect(column_decoder_selector=selector)
        cur = conn.cursor(DictCursor)
        cur.execute(
            "SELECT amount AS d, ratio AS d FROM test_column_decoder"
            " ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()
        self.assertIsInstance(row["d"], decimal.Decimal)
        self.assertIsInstance(row["test_column_decoder.d"], float)

    def test_cursor_types_are_consistent(self):
        conn = self.connect(column_decoder_selector=self.amount_ratio_selector())
        expected = None
        for cursorclass in (Cursor, DictCursor, SSCursor, SSDictCursor):
            cur = conn.cursor(cursorclass)
            cur.execute("SELECT amount, ratio FROM test_column_decoder ORDER BY id")
            rows = cur.fetchall()
            if issubclass(cursorclass, (DictCursor, SSDictCursor)):
                values = [tuple(row.values()) for row in rows]
            else:
                values = list(rows)
            if expected is None:
                expected = values
            self.assertEqual(expected, values)
            self.assertIsInstance(values[0][0], decimal.Decimal)
            self.assertIsInstance(values[0][1], float)
            cur.close()

    def test_raise_policy_hides_row_data(self):
        def bad_decoder(value):
            raise ValueError(f"cannot parse {value!r}")

        def selector(column):
            if column.org_name == "amount":
                return bad_decoder
            return None

        conn = self.connect(column_decoder_selector=selector)
        cur = conn.cursor()
        with self.assertRaises(converters.ColumnConversionError) as ctx:
            cur.execute("SELECT note, amount FROM test_column_decoder")

        exc = ctx.exception
        self.assertEqual("amount", exc.column.name)
        self.assertEqual(1, exc.column.index)
        self.assertIsInstance(exc.error, ValueError)
        self.assertIs(exc.error, exc.__cause__)
        message = str(exc)
        self.assertIn("amount", message)
        # The error must not leak row values, neither the failing column's
        # nor other columns'.
        self.assertNotIn("secret", message)
        self.assertNotIn("1234.50", message)

    def test_keep_policy_returns_raw_and_records_problem(self):
        def bad_decoder(value):
            raise ValueError("cannot parse")

        def selector(column):
            if column.org_name == "amount":
                return bad_decoder
            return None

        conn = self.connect(
            column_decoder_selector=selector,
            conversion_error_policy="keep",
        )
        cur = conn.cursor()
        cur.execute("SELECT amount FROM test_column_decoder ORDER BY id")
        rows = cur.fetchall()

        # Unconverted (but charset-decoded) values are returned.
        self.assertEqual("1234.50", rows[0][0])
        self.assertIsNone(rows[1][0])
        self.assertEqual("99999.99", rows[2][0])

        problems = cur.column_conversion_problems
        self.assertEqual(2, len(problems))
        self.assertEqual([0, 2], [p.row_number for p in problems])
        for problem in problems:
            self.assertEqual("amount", problem.column.name)
            self.assertEqual("test_column_decoder", problem.column.org_table)
            self.assertIsInstance(problem.error, ValueError)
            # Problem records must not contain row data.
            self.assertNotIn("1234.50", repr(problem))
            self.assertNotIn("99999.99", repr(problem))

    def test_keep_policy_streaming_cursor(self):
        def bad_decoder(value):
            raise ValueError("cannot parse")

        def selector(column):
            if column.org_name == "amount":
                return bad_decoder
            return None

        conn = self.connect(column_decoder_selector=selector)
        # Per-cursor policy override on a streaming cursor.
        cur = conn.cursor(SSCursor, conversion_error_policy="keep")
        cur.execute("SELECT amount FROM test_column_decoder ORDER BY id")
        rows = cur.fetchall()

        self.assertEqual("1234.50", rows[0][0])
        self.assertIsNone(rows[1][0])
        self.assertEqual("99999.99", rows[2][0])
        self.assertEqual(2, len(cur.column_conversion_problems))
        cur.close()

    def test_cursor_override(self):
        def connection_selector(column):
            if column.org_name == "amount":
                return lambda value: ("connection", value)
            return None

        def cursor_selector(column):
            if column.org_name == "amount":
                return lambda value: ("cursor", value)
            return None

        conn = self.connect(column_decoder_selector=connection_selector)
        query = "SELECT amount FROM test_column_decoder WHERE id = 1"

        cur = conn.cursor()
        cur.execute(query)
        self.assertEqual(("connection", "1234.50"), cur.fetchone()[0])

        # Override via cursor() keyword argument.
        cur = conn.cursor(column_decoder_selector=cursor_selector)
        cur.execute(query)
        self.assertEqual(("cursor", "1234.50"), cur.fetchone()[0])

        # Override via attribute assignment.
        cur = conn.cursor()
        cur.column_decoder_selector = cursor_selector
        cur.execute(query)
        self.assertEqual(("cursor", "1234.50"), cur.fetchone()[0])

        # Other cursors are unaffected by the overrides above.
        cur = conn.cursor()
        cur.execute(query)
        self.assertEqual(("connection", "1234.50"), cur.fetchone()[0])

    def test_connection_isolation(self):
        def make_selector(tag):
            def selector(column):
                if column.org_name == "amount":
                    return lambda value: (tag, value)
                return None

            return selector

        conn_a = self.connect(column_decoder_selector=make_selector("A"))
        conn_b = self.connect(column_decoder_selector=make_selector("B"))
        conn_c = self.connect()
        query = "SELECT amount FROM test_column_decoder WHERE id = 1"

        cur = conn_a.cursor()
        cur.execute(query)
        self.assertEqual(("A", "1234.50"), cur.fetchone()[0])

        cur = conn_b.cursor()
        cur.execute(query)
        self.assertEqual(("B", "1234.50"), cur.fetchone()[0])

        # A connection without a selector uses the default conv decoders.
        cur = conn_c.cursor()
        cur.execute(query)
        self.assertEqual(decimal.Decimal("1234.50"), cur.fetchone()[0])

    def test_nextset_rebinds_converters(self):
        seen = []
        conn = self.connect(
            client_flag=CLIENT.MULTI_STATEMENTS,
            column_decoder_selector=self.amount_ratio_selector(seen),
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT amount FROM test_column_decoder WHERE id = 1;"
            " SELECT ratio FROM test_column_decoder WHERE id = 1"
        )
        self.assertIsInstance(cur.fetchone()[0], decimal.Decimal)

        self.assertTrue(cur.nextset())
        self.assertIsInstance(cur.fetchone()[0], float)
        self.assertIsNone(cur.nextset())

        # The selector ran once per column for each result set.
        self.assertEqual(["amount", "ratio"], [c.org_name for c in seen])

    def test_invalid_conversion_error_policy(self):
        with self.assertRaises(ValueError):
            self.connect(conversion_error_policy="bogus")

        conn = self.connect()
        with self.assertRaises(ValueError):
            conn.cursor(conversion_error_policy="bogus")

    def test_selector_none_falls_back_to_conv(self):
        def selector(column):
            return None

        conn = self.connect(column_decoder_selector=selector)
        cur = conn.cursor()
        cur.execute("SELECT amount FROM test_column_decoder WHERE id = 1")
        self.assertEqual(decimal.Decimal("1234.50"), cur.fetchone()[0])


if __name__ == "__main__":
    import unittest

    unittest.main()
