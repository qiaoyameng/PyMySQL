.. _examples:

========
Examples
========

.. _CRUD:

CRUD
----

The following examples make use of a simple table

.. code:: sql

   CREATE TABLE `users` (
       `id` int(11) NOT NULL AUTO_INCREMENT,
       `email` varchar(255) COLLATE utf8_bin NOT NULL,
       `password` varchar(255) COLLATE utf8_bin NOT NULL,
       PRIMARY KEY (`id`)
   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
   AUTO_INCREMENT=1 ;


.. code:: python

    import pymysql.cursors

    # Connect to the database
    connection = pymysql.connect(host='localhost',
                                 user='user',
                                 password='passwd',
                                 database='db',
                                 charset='utf8mb4',
                                 cursorclass=pymysql.cursors.DictCursor)

    with connection:
        with connection.cursor() as cursor:
            # Create a new record
            sql = "INSERT INTO `users` (`email`, `password`) VALUES (%s, %s)"
            cursor.execute(sql, ('webmaster@python.org', 'very-secret'))

        # connection is not autocommit by default. So you must commit to save
        # your changes.
        connection.commit()

        with connection.cursor() as cursor:
            # Read a single record
            sql = "SELECT `id`, `password` FROM `users` WHERE `email`=%s"
            cursor.execute(sql, ('webmaster@python.org',))
            result = cursor.fetchone()
            print(result)


This example will print:

.. code:: python

    {'id': 1, 'password': 'very-secret'}

Iterating multiple result sets
------------------------------

A stored procedure (or a multi-statement query) can return several result
sets.  :meth:`Cursor.iter_result_sets` iterates them explicitly.  Each item
exposes the set's metadata (``description``, ``rowcount``/``affected_rows``,
``warning_count`` and ``more_results``) and a lazy iterator over its rows;
rows are read from the socket as needed instead of being buffered.

If the current result set was not fully read, advancing to the next one
drains its remaining rows.  Leaving the ``with`` block drains every
remaining set, so the connection can run another query afterwards; if the
remaining data cannot be drained (for example because the connection was
lost), the connection is closed instead.

.. code:: python

    with connection.cursor() as cursor:
        with cursor.iter_result_sets("CALL my_procedure()") as result_sets:
            for result_set in result_sets:
                print(result_set.description, result_set.rowcount)
                for row in result_set:
                    print(row)

        # The trailing empty result set created by CALL has been consumed,
        # so the connection is ready for the next query.
        cursor.execute("SELECT 1")
