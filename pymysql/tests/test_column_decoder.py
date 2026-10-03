from decimal import Decimal

from pymysql import converters
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.cursors import Cursor, DictCursor, SSCursor, SSDictCursor
from pymysql.tests import base


def _cents(value):
    return int(Decimal(value) * 100)


def _boom(value):
    raise ValueError("cannot convert")


def _amount_cents_selector(column):
    if column.org_name == "amount":
        return _cents
    return None


def _amount_boom_selector(column):
    if column.org_name == "amount":
        return _boom
    return None


class TestColumnDecoder(base.PyMySQLTestCase):
    def setUp(self):
        super().setUp()
        self.conn = self.connect()
        self.safe_create_table(
            self.conn,
            "test_cd_a",
            "create table test_cd_a ("
            " amount decimal(10,2) not null,"
            " ratio decimal(8,4) not null,"
            " label varchar(50),"
            " payload varbinary(16),"
            " note varchar(50)"
            " )",
        )
        self.safe_create_table(
            self.conn,
            "test_cd_b",
            "create table test_cd_b (amount decimal(10,2) not null)",
        )
        cur = self.conn.cursor()
        cur.execute(
            "insert into test_cd_a (amount, ratio, label, payload, note)"
            " values (12.34, 0.5678, 'hello', %s, null),"
            " (100.00, 1.0000, 'world', %s, 'top-secret'),"
            " (0.01, 0.0001, 'lorem', %s, null)",
            (b"\x01\x02", b"\x03", b""),
        )
        cur.execute("insert into test_cd_b (amount) values (99.99)")
        cur.close()
        self.conn.commit()

    def test_selector_sees_metadata_and_is_called_once_per_result_set(self):
        seen = []

        def selector(column):
            seen.append(column)
            return None

        conn = self.connect(column_decoder=selector)
        cur = conn.cursor()
        cur.execute("select amount, ratio, label, payload, note from test_cd_a")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 3)
        # One call per column for the whole result set, not per row.
        self.assertEqual(len(seen), 5)
        self.assertEqual([c.index for c in seen], [0, 1, 2, 3, 4])
        amount = seen[0]
        self.assertEqual(amount.name, "amount")
        self.assertEqual(amount.org_name, "amount")
        self.assertEqual(amount.table_name, "test_cd_a")
        self.assertEqual(amount.org_table, "test_cd_a")
        self.assertEqual(amount.type_code, FIELD_TYPE.NEWDECIMAL)
        self.assertIsInstance(amount.flags, int)
        self.assertIsInstance(amount.charsetnr, int)
        self.assertEqual(amount.scale, 2)
        # None means: fall back to the type-based decoders.
        self.assertEqual(rows[0][0], Decimal("12.34"))

        # A new result set selects again.
        cur.execute("select amount from test_cd_a")
        cur.fetchall()
        self.assertEqual(len(seen), 6)

    def test_decimal_columns_get_distinct_decoders(self):
        def selector(column):
            if column.type_code != FIELD_TYPE.NEWDECIMAL:
                return None
            if column.org_name == "amount":
                return _cents
            if column.org_name == "ratio":
                return float
            return None

        conn = self.connect(column_decoder=selector)
        cur = conn.cursor()
        cur.execute("select amount, ratio from test_cd_a order by amount desc")
        row = cur.fetchone()
        self.assertEqual(row, (10000, 1.0))
        self.assertIsInstance(row[0], int)
        self.assertIsInstance(row[1], float)

    def test_selector_can_disable_conversion(self):
        def selector(column):
            if column.org_name == "amount":
                return converters.through
            return None

        conn = self.connect(column_decoder=selector)
        cur = conn.cursor()
        cur.execute("select amount from test_cd_a where label = 'hello'")
        self.assertEqual(cur.fetchone(), ("12.34",))

    def test_null_stays_none_and_decoder_is_not_called(self):
        calls = []

        def selector(column):
            if column.org_name == "note":
                return lambda v: calls.append(v) or v.upper()
            return None

        conn = self.connect(column_decoder=selector)
        cur = conn.cursor()
        cur.execute("select note from test_cd_a order by label")
        self.assertEqual([r[0] for r in cur.fetchall()], [None, "TOP-SECRET", None])
        self.assertEqual(calls, ["top-secret"])

    def test_text_and_binary_values_decode_before_conversion(self):
        received = {}

        def selector(column):
            if column.org_name not in ("label", "payload"):
                return None

            def convert(value, name=column.org_name):
                received[name] = type(value)
                return value

            return convert

        conn = self.connect(column_decoder=selector)
        cur = conn.cursor()
        cur.execute("select label, payload from test_cd_a where label = 'hello'")
        row = cur.fetchone()
        # Text columns are decoded to str before conversion; binary columns
        # stay bytes.
        self.assertEqual(row, ("hello", b"\x01\x02"))
        self.assertEqual(received, {"label": str, "payload": bytes})

    def test_same_name_columns_do_not_share_converters(self):
        def selector(column):
            if column.org_name != "amount":
                return None
            if column.org_table == "test_cd_a":
                return _cents
            if column.org_table == "test_cd_b":
                return float
            return None

        query = (
            "select a.amount, b.amount from test_cd_a a"
            " cross join test_cd_b b where a.label = 'hello'"
        )
        conn = self.connect(column_decoder=selector)

        cur = conn.cursor()
        cur.execute(query)
        row = cur.fetchone()
        self.assertEqual(row, (1234, 99.99))
        self.assertIsInstance(row[0], int)
        self.assertIsInstance(row[1], float)

        dict_cur = conn.cursor(DictCursor)
        dict_cur.execute(query)
        dict_row = dict_cur.fetchone()
        self.assertEqual(dict_row["amount"], 1234)
        others = [v for k, v in dict_row.items() if k != "amount"]
        self.assertEqual(others, [99.99])

    def test_cursor_level_overrides(self):
        def cursor_selector(column):
            if column.org_name == "amount":
                return lambda v: f"amount:{v}"
            return None

        conn = self.connect(
            column_decoder=_amount_cents_selector,
            conversion_error_policy=converters.CONVERT_RAISE,
        )
        query = "select amount from test_cd_a where label = 'hello'"

        default_cur = conn.cursor()
        override_cur = conn.cursor()
        override_cur.column_decoder = cursor_selector
        override_cur.conversion_error_policy = converters.CONVERT_KEEP_RAW

        default_cur.execute(query)
        self.assertEqual(default_cur.fetchone(), (1234,))

        override_cur.execute(query)
        self.assertEqual(override_cur.fetchone(), ("amount:12.34",))

        # The override does not leak into other cursors of the connection.
        default_cur.execute(query)
        self.assertEqual(default_cur.fetchone(), (1234,))
        self.assertEqual(
            conn._effective_conversion_error_policy(), converters.CONVERT_RAISE
        )

    def test_cursor_types_convert_consistently(self):
        expected = (1234, Decimal("0.5678"), "hello", b"\x01\x02", None)
        for cursorclass in (Cursor, DictCursor, SSCursor, SSDictCursor):
            conn = self.connect(
                column_decoder=_amount_cents_selector, cursorclass=cursorclass
            )
            cur = conn.cursor()
            cur.execute(
                "select amount, ratio, label, payload, note"
                " from test_cd_a where label = 'hello'"
            )
            rows = cur.fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            if isinstance(row, dict):
                row = tuple(
                    row[k] for k in ("amount", "ratio", "label", "payload", "note")
                )
            self.assertEqual(row, expected, cursorclass.__name__)
            cur.close()

    def test_raise_policy_is_the_default(self):
        conn = self.connect(column_decoder=_amount_boom_selector)
        cur = conn.cursor()
        with self.assertRaises(ValueError):
            cur.execute("select amount from test_cd_a")

    def test_raise_policy_streaming(self):
        conn = self.connect(column_decoder=_amount_boom_selector, cursorclass=SSCursor)
        cur = conn.cursor()
        cur.execute("select amount from test_cd_a")
        with self.assertRaises(ValueError):
            cur.fetchall()
        cur.close()

    def test_keep_raw_policy_records_column_problem(self):
        conn = self.connect(
            column_decoder=_amount_boom_selector,
            conversion_error_policy=converters.CONVERT_KEEP_RAW,
        )
        cur = conn.cursor()
        cur.execute("select amount, note from test_cd_a where label = 'world'")
        row = cur.fetchone()
        # The raw (decoded) value is kept for the failing column.
        self.assertEqual(row, ("100.00", "top-secret"))

        self.assertEqual(len(cur.conversion_errors), 1)
        problem = cur.conversion_errors[0]
        self.assertIsInstance(problem, converters.ColumnConversionError)
        self.assertEqual(problem.column.index, 0)
        self.assertEqual(problem.column.name, "amount")
        self.assertEqual(problem.column.org_table, "test_cd_a")
        self.assertEqual(problem.column.type_code, FIELD_TYPE.NEWDECIMAL)
        self.assertIsInstance(problem.error, ValueError)

        # The record describes the column, but never copies row data.
        message = str(problem)
        self.assertIn("amount", message)
        self.assertIn("ValueError", message)
        self.assertNotIn("top-secret", message)
        self.assertNotIn("100.00", message)
        self.assertNotIn("top-secret", repr(problem))

    def test_keep_raw_policy_streaming(self):
        conn = self.connect(
            column_decoder=_amount_boom_selector,
            conversion_error_policy=converters.CONVERT_KEEP_RAW,
            cursorclass=SSCursor,
        )
        cur = conn.cursor()
        cur.execute("select amount from test_cd_a order by amount")
        rows = cur.fetchall()
        self.assertEqual([r[0] for r in rows], ["0.01", "12.34", "100.00"])
        self.assertEqual(len(cur.conversion_errors), 3)
        cur.close()

    def test_invalid_conversion_error_policy(self):
        with self.assertRaises(ValueError):
            self.connect(conversion_error_policy="ignore")

    def test_connections_with_same_metadata_are_isolated(self):
        def text_selector(column):
            if column.org_name == "amount":
                return lambda v: f"[{v}]"
            return None

        conn1 = self.connect(column_decoder=_amount_cents_selector)
        conn2 = self.connect(column_decoder=text_selector)
        conn3 = self.connect()
        query = "select amount from test_cd_a where label = 'hello'"

        cur1 = conn1.cursor()
        cur1.execute(query)
        self.assertEqual(cur1.fetchone(), (1234,))

        cur2 = conn2.cursor()
        cur2.execute(query)
        self.assertEqual(cur2.fetchone(), ("[12.34]",))

        cur3 = conn3.cursor()
        cur3.execute(query)
        self.assertEqual(cur3.fetchone(), (Decimal("12.34"),))

        # Interleaving queries does not leak selectors across connections.
        cur1.execute(query)
        self.assertEqual(cur1.fetchone(), (1234,))
        cur2.execute(query)
        self.assertEqual(cur2.fetchone(), ("[12.34]",))

    def test_nextset_rebinds_decoders(self):
        calls = []

        def selector(column):
            calls.append((column.index, column.org_name))
            if column.org_name == "amount":
                return _cents
            return None

        conn = self.connect(
            column_decoder=selector, client_flag=CLIENT.MULTI_STATEMENTS
        )
        cur = conn.cursor()
        cur.execute(
            "select amount from test_cd_a where label = 'hello';"
            " select ratio, amount from test_cd_a where label = 'hello'"
        )
        self.assertEqual(list(cur), [(1234,)])
        self.assertEqual(calls, [(0, "amount")])

        self.assertTrue(cur.nextset())
        # The second result set selected its own decoders again.
        self.assertEqual(list(cur), [(Decimal("0.5678"), 1234)])
        self.assertEqual(calls, [(0, "amount"), (0, "ratio"), (1, "amount")])
        self.assertIsNone(cur.nextset())
