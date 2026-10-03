import pymysql
from pymysql.constants import CLIENT
from pymysql.tests import base


class TestQueryIter(base.PyMySQLTestCase):
    def test_three_sets_skip_second(self):
        con = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        with con.query_iter(
            "SELECT 1 AS a UNION ALL SELECT 2 UNION ALL SELECT 3;"
            " SELECT 10 AS b UNION ALL SELECT 20 UNION ALL SELECT 30;"
            " SELECT 7 AS c"
        ) as stream:
            it = iter(stream)

            first = next(it)
            self.assertEqual(first.index, 0)
            self.assertEqual(first.description[0][0], "a")
            self.assertEqual(list(first.rows()), [(1,), (2,), (3,)])
            self.assertTrue(first.exhausted)
            self.assertTrue(first.has_next)
            self.assertEqual(first.affected_rows, 3)

            second = next(it)
            self.assertEqual(second.index, 1)
            rows = second.rows()
            self.assertEqual(next(rows), (10,))  # read only one row
            self.assertFalse(second.exhausted)

            third = next(it)  # advancing drains the rest of the second set
            self.assertTrue(second.exhausted)
            self.assertEqual(second.affected_rows, 3)
            self.assertEqual(list(rows), [])  # old iterator is spent
            with self.assertRaises(RuntimeError):
                second.rows()

            self.assertEqual(list(third.rows()), [(7,)])
            self.assertFalse(third.has_next)
            self.assertRaises(StopIteration, next, it)

        # the connection is ready for the next query right away
        cur = con.cursor()
        cur.execute("SELECT 42")
        self.assertEqual(cur.fetchall(), ((42,),))

    def test_empty_set_and_warning_count(self):
        con = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        with con.query_iter(
            "SELECT 1;"
            " DROP TABLE IF EXISTS _pymysql_no_such_table_;"
            " SELECT 2"
        ) as stream:
            it = iter(stream)

            first = next(it)
            self.assertEqual(list(first.rows()), [(1,)])
            self.assertTrue(first.has_next)

            middle = next(it)
            self.assertIsNone(middle.description)
            self.assertEqual(list(middle.rows()), [])
            self.assertEqual(middle.affected_rows, 0)
            self.assertEqual(middle.warning_count, 1)
            self.assertTrue(middle.has_next)

            last = next(it)
            self.assertEqual(list(last.rows()), [(2,)])
            self.assertFalse(last.has_next)
            self.assertRaises(StopIteration, next, it)

    def test_error_attributed_to_failing_set(self):
        con = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        with con.query_iter("SELECT 1; xyzzy; SELECT 3") as stream:
            it = iter(stream)
            first = next(it)
            self.assertEqual(list(first.rows()), [(1,)])
            self.assertIsNone(first.error)
            with self.assertRaises(pymysql.ProgrammingError):
                next(it)
            self.assertIsInstance(stream.error, pymysql.ProgrammingError)
            self.assertEqual(stream.error_index, 1)
            self.assertRaises(StopIteration, next, it)

        # a server ERR packet aborts the batch but keeps the
        # connection usable
        cur = con.cursor()
        cur.execute("SELECT 2")
        self.assertEqual(cur.fetchall(), ((2,),))

    def test_context_exit_drains_remaining(self):
        con = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        with con.query_iter(
            "SELECT 1 UNION ALL SELECT 2; SELECT 3; SELECT 4"
        ) as stream:
            first = next(iter(stream))
            self.assertEqual(next(first.rows()), (1,))
            # leave the with block without finishing any set

        cur = con.cursor()
        cur.execute("SELECT 42")
        self.assertEqual(cur.fetchall(), ((42,),))

    def test_close_without_iterating(self):
        con = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        stream = con.query_iter("SELECT 1; SELECT 2")
        stream.close()

        cur = con.cursor()
        cur.execute("SELECT 42")
        self.assertEqual(cur.fetchall(), ((42,),))

    def test_new_query_invalidates_stream(self):
        con = self.connect(client_flag=CLIENT.MULTI_STATEMENTS)
        stream = con.query_iter("SELECT 1 UNION ALL SELECT 2; SELECT 3")
        first = next(iter(stream))
        self.assertEqual(next(first.rows()), (1,))

        with self.assertWarns(UserWarning):
            con.query("SELECT 42")
        self.assertEqual(con._result.rows, ((42,),))

        # the stale stream terminates instead of reading the new response
        self.assertEqual(list(stream), [])
