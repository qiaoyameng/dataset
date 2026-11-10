
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


Full-text search with SQLite FTS5
---------------------------------

On SQLite, a table can be given an optional FTS5 full-text index without
running any additional service. The index lives in the same database file
and is maintained by triggers, so every insert, update and delete updates
the search index within the same transaction. Rows are identified by a
stable integer primary key column (``id`` by default)::

    import dataset

    db = dataset.connect('sqlite:///articles.db')
    table = db['articles']
    table.insert_many([
        {'title': 'banana split', 'body': 'a sweet banana dessert'},
        {'title': 'apple pie', 'body': 'warm apple and cinnamon'},
    ])

    # Backfills existing rows, then keeps the index in sync automatically.
    table.enable_fts(['title', 'body'], rowid_column='id')

    # MATCH expressions are always passed as bound parameters.
    for row in table.search('banana OR apple'):
        print(row['rank'], row['title'])

    # Combine full-text matching with ordinary filters and a limit.
    rows = table.search('apple', title={'like': '%pie%'}, _limit=10)

    # Optionally return a highlighted snippet of an indexed column.
    rows = table.search('banana', snippet='body')

Enabling an identical configuration twice is a no-op. If the indexed
columns (or another setting) change, request an inspectable rebuild plan
first and apply it explicitly::

    plan = table.plan_fts_rebuild(['body'])
    print(plan.describe())
    table.rebuild_fts(['body'])
    # or, equivalently:
    table.enable_fts(['body'], rebuild=True)

The index can be removed again with ``table.disable_fts()``. Renaming or
dropping the table via ``table.rename()`` / ``table.drop()`` coordinates
the index, its triggers and the reflected schema cache automatically.

SQLite builds without the FTS5 extension raise
:class:`dataset.FTSCapabilityError`, and invalid configurations raise
:class:`dataset.FTSConfigError`.
