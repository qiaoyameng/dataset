
.. _advanced_filters:

Advanced filters
================

``dataset`` provides two methods for running queries: :py:meth:`table.find() <dataset.Table.find>`
and :py:meth:`db.query() <dataset.Database.query>`. The table find helper method provides 
limited, but simple filtering options::

    results = table.find(column={operator: value})
    # e.g.:
    results = table.find(name={'like': '%mole rat%'})

A special form is using keyword searches on specific columns::

    results = table.find(value=5)
    # equal to:
    results = table.find(value={'=': 5})

    # Lists, tuples and sets are turned into `IN` queries:
    results = table.find(category=('foo', 'bar'))
    # equal to:
    results = table.find(value={'in': ('foo', 'bar')})

The following comparison operators are supported:

============== ============================================================
Operator       Description
============== ============================================================
gt, >          Greater than
lt, <          Less than
gte, >=        Greater or equal
lte, <=        Less or equal
!=, <>, not    Not equal to a single value
in             Value is in the given sequence
notin          Value is not in the given sequence
like, ilike    Text search, ILIKE is case-insensitive. Use ``%`` as a wildcard
notlike        Like text search, except check if pattern does not exist
between, ..    Value is between two values in the given tuple
startswith     String starts with
endswith       String ends with
============== ============================================================

Querying for a specific value on a column that does not exist on the table
will return no results.

You can also pass `SQLAlchemy core expressions`_ directly into the
:py:meth:`table.find() <dataset.Table.find>` method as positional arguments.
Access the underlying SQLAlchemy table via ``table.table`` and its columns
via ``table.table.columns``::

    from sqlalchemy import or_

    # Get a column object:
    city = table.table.columns.city
    # Use a SQLAlchemy clause:
    results = table.find(city.ilike('amsterda%'))

    # Combine with OR:
    country = table.table.columns.country
    results = table.find(or_(city == 'Amsterdam', country == 'Germany'))

    # Combine SQLAlchemy clauses with keyword filters:
    results = table.find(city.ilike('new%'), country='US')

These clauses also work with :py:meth:`table.count() <dataset.Table.count>`,
:py:meth:`table.find_one() <dataset.Table.find_one>`, and
:py:meth:`table.delete() <dataset.Table.delete>`.

Finding rows by a list of composite keys
----------------------------------------

An ``IN`` filter becomes awkward for composite business keys, and a long key
list can exceed the database's parameter limit (for example SQLite allows at
most 999 host variables per statement). :py:meth:`table.find_by_keys()
<dataset.Table.find_by_keys>` instead stages the deduplicated key list in a
session-local temporary table and joins it against the target table, so it
works for arbitrarily long key lists and multi-column keys::

    rows = table.find_by_keys(
        [('Berlin', 2011), ('Paris', 2012)],
        key_columns=['place', 'year'],
    )

The key list may be a one-shot iterator. With a single key column each item
can be a scalar; for composite keys pass one tuple per key (mappings keyed by
column name are accepted as well). Repeated keys produce only one filter
condition, while duplicate rows stored in the table are all returned.

By default SQL NULL semantics apply, so keys containing ``None`` match
nothing. Pass ``nulls_equal=True`` to treat NULL values on both sides as
equal. Ordering, ``_limit``/``_offset`` and the ``columns`` projection are
all applied to the complete result in SQL, so global pagination stays
correct even when the key list is inserted in many batches::

    rows = table.find_by_keys(
        key_iter,
        ['place', 'year'],
        nulls_equal=True,
        columns=['place', 'amount'],
        order_by=['-amount'],
        _limit=50,
    )
    matched = table.count_by_keys(key_iter, ['place', 'year'])

The temporary table lives on the current connection and is dropped before
the call returns; query errors and outer transaction rollbacks leave no
temporary state behind, and an outer transaction is never committed by the
lookup itself.

Queries using raw SQL
---------------------

To run more complex queries with JOINs, or to perform GROUP BY-style
aggregation, you can also use :py:meth:`db.query() <dataset.Database.query>`
to run raw SQL queries instead. This also supports parameterisation to avoid
SQL injections::

    statement = 'SELECT user, COUNT(*) c FROM photos GROUP BY user'
    for row in db.query(statement):
        print(row['user'], row['c'])

    # With parameter binding:
    results = db.query('SELECT * FROM users WHERE age > :min_age', min_age=21)

For fully programmatic, composable query building, consider using
`SQLAlchemy core expressions`_ directly.

.. _SQLAlchemy core expressions: https://docs.sqlalchemy.org/en/21/tutorial/data_select.html#tutorial-selecting-data
