Prepared Statements
===================

.. module:: pymysql.prepared

Server-side prepared statements are created with
:meth:`pymysql.connections.Connection.prepare`::

    stmt = conn.prepare("SELECT n FROM t WHERE id = ?")
    try:
        result = stmt.execute((1,))
        print(result.rows)
        result = stmt.execute((2,))  # parameter types may change per call
    finally:
        stmt.close()

The statement handle is bound to the connection generation that created
it: reconnecting the connection invalidates all previously prepared
statements.

.. autoclass:: PreparedStatement
   :members:

.. autoclass:: StmtResult
   :members:
