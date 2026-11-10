from datetime import datetime

import pytest

import dataset

D1 = datetime(2020, 1, 1)
D2 = datetime(2020, 1, 2)
D3 = datetime(2020, 1, 3)

ORDERS = [
    {"place": "Berlin", "day": D1, "amount": 10},
    {"place": "Berlin", "day": D1, "amount": 20},
    {"place": "Paris", "day": D2, "amount": 30},
    {"place": "Rome", "day": D3, "amount": 40},
    {"place": None, "day": D3, "amount": 50},
]


@pytest.fixture(scope="function")
def orders(db):
    tbl = db["orders"]
    tbl.insert_many(ORDERS)
    return tbl


def temp_table_names(db: dataset.Database) -> list[str]:
    if db.is_sqlite:
        rows = db.executable.exec_driver_sql(
            "SELECT name FROM sqlite_temp_master WHERE type='table'"
        ).fetchall()
        return [row[0] for row in rows]
    # PostgreSQL and MySQL expose session-local temporary tables separately.
    if db.is_postgres:
        rows = db.executable.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'pg_temp_1'"
        ).fetchall()
        return [row[0] for row in rows if row[0].startswith("tmp_dataset_keys_")]
    rows = db.executable.exec_driver_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN "
        "('mysql', 'information_schema', 'performance_schema')"
    ).fetchall()
    return [row[0] for row in rows if row[0].startswith("tmp_dataset_keys_")]


def test_find_by_composite_keys(orders):
    keys = [("Berlin", D1), ("Paris", D2)]
    rows = list(orders.find_by_keys(keys, ["place", "day"]))
    assert [r["amount"] for r in rows] == [10, 20, 30]
    assert orders.count_by_keys(keys, ["place", "day"]) == 3


def test_find_by_keys_accepts_iterator(orders):
    def gen():
        yield ("Berlin", D1)
        yield ("Paris", D2)
        yield ("Missing", D1)

    rows = list(orders.find_by_keys(gen(), ("place", "day")))
    assert len(rows) == 3


def test_find_by_keys_accepts_mappings(orders):
    rows = list(
        orders.find_by_keys(
            [{"day": D2, "place": "Paris"}, {"place": "Rome", "day": D3}],
            ["place", "day"],
        )
    )
    assert {r["amount"] for r in rows} == {30, 40}


def test_find_by_single_column_scalars(orders):
    rows = list(orders.find_by_keys(["Paris", "Rome"], "place"))
    assert {r["amount"] for r in rows} == {30, 40}
    assert orders.count_by_keys(["Paris"], "place") == 1


def test_duplicate_keys_deduped_but_duplicate_rows_kept(orders, monkeypatch):
    executed = []
    bound_execute = orders.db.executable.execute

    def spy(statement, parameters=None):
        executed.append(parameters)
        if parameters is None:
            return bound_execute(statement)
        return bound_execute(statement, parameters)

    monkeypatch.setattr(orders.db.executable, "execute", spy)

    keys = [("Berlin", D1), ("Berlin", D1), ("Paris", D2), ("Berlin", D1)]
    rows = list(orders.find_by_keys(keys, ["place", "day"]))
    # Duplicate Berlin rows in the table are both returned ...
    assert sorted(r["amount"] for r in rows) == [10, 20, 30]
    # ... while the duplicated key was inserted into the temp table only once.
    inserted_rows = [p for p in executed if isinstance(p, list)]
    staged = [row for batch in inserted_rows for row in batch]
    assert len(staged) == 2


def test_null_keys_do_not_match_by_default(orders):
    keys = [(None, D3)]
    assert list(orders.find_by_keys(keys, ["place", "day"])) == []
    assert orders.count_by_keys(keys, ["place", "day"]) == 0
    # Standard SQL semantics: NULL = NULL is not true. SQLAlchemy rewrites
    # Core `== None` to IS NULL, so check the generated join condition with a
    # raw query instead: a NULL key value matches no row.
    row = orders.db.query(
        "SELECT count(*) AS c FROM orders WHERE place = :place",
        place=None,
    ).next()
    assert row["c"] == 0


def test_nulls_equal_matches_null_rows(orders):
    keys = [(None, D3), ("Paris", D2)]
    rows = list(orders.find_by_keys(keys, ["place", "day"], nulls_equal=True))
    assert {r["amount"] for r in rows} == {50, 30}
    assert orders.count_by_keys(keys, ["place", "day"], nulls_equal=True) == 2


def test_projection_order_limit_offset(orders):
    keys = [("Berlin", D1), ("Paris", D2), ("Rome", D3)]
    rows = list(
        orders.find_by_keys(
            keys,
            ["place", "day"],
            columns=["amount"],
            order_by="-amount",
            _limit=2,
        )
    )
    assert [set(r.keys()) for r in rows] == [{"amount"}, {"amount"}]
    assert [r["amount"] for r in rows] == [40, 30]

    page = list(
        orders.find_by_keys(
            keys, ["place", "day"], order_by="-amount", _limit=2, _offset=2
        )
    )
    assert [r["amount"] for r in page] == [20, 10]


def test_ordering_matches_ordinary_find(orders):
    keys = [("Berlin", D1), ("Paris", D2), ("Rome", D3), (None, D3)]
    keyed = list(
        orders.find_by_keys(
            keys, ["place", "day"], nulls_equal=True, order_by=["-amount", "place"]
        )
    )
    ordinary = list(
        orders.find(
            orders.table.c.place.in_(["Berlin", "Paris", "Rome", None])
            | orders.table.c.place.is_(None),
            order_by=["-amount", "place"],
        )
    )
    assert [r["amount"] for r in keyed] == [r["amount"] for r in ordinary]


def test_many_batches_match_small_list_query(orders, monkeypatch):
    # Force a tiny parameter threshold so the key list spans many batches.
    monkeypatch.setattr("dataset.table.KEY_INSERT_CHUNK_SIZE", 3)
    many_keys = [
        ("Berlin", D1),
        ("Paris", D2),
        ("Rome", D3),
        ("Atlantis", D1),
        ("Berlin", D1),
    ]
    # The table holds duplicate Berlin rows; expand the fixture for volume.
    orders.insert_many(ORDERS * 20)

    chunked_rows = list(
        orders.find_by_keys(
            many_keys,
            ["place", "day"],
            nulls_equal=True,
            order_by=["place", "day", "amount"],
            _limit=7,
            _offset=2,
        )
    )
    unique_keys = list(dict.fromkeys(many_keys))
    ordinary_rows = list(
        orders.find(
            orders.table.c.place.in_([k[0] for k in unique_keys]),
            order_by=["place", "day", "amount"],
            _limit=7,
            _offset=2,
        )
    )
    assert [r["amount"] for r in chunked_rows] == [
        r["amount"] for r in ordinary_rows
    ]
    assert orders.count_by_keys(many_keys, ["place", "day"], nulls_equal=True) == (
        orders.count(orders.table.c.place.in_([k[0] for k in unique_keys]))
    )

    # More than one insert batch must actually have been executed.
    assert len(unique_keys) > 3
    with pytest.MonkeyPatch.context() as mp:
        batches = []
        bound_execute = orders.db.executable.execute

        def counting_execute(statement, parameters=None):
            if isinstance(parameters, list):
                batches.append(parameters)
            if parameters is None:
                return bound_execute(statement)
            return bound_execute(statement, parameters)

        mp.setattr(orders.db.executable, "execute", counting_execute)
        list(orders.find_by_keys(many_keys, ["place", "day"]))
        assert len(batches) >= 2
        assert all(len(batch) <= 3 for batch in batches)


def test_empty_key_list(orders):
    assert list(orders.find_by_keys([], ["place", "day"])) == []
    assert orders.count_by_keys(iter([]), "place") == 0


def test_invalid_arguments(orders):
    with pytest.raises(Exception):
        list(orders.find_by_keys([], []))
    with pytest.raises(Exception):
        list(orders.find_by_keys([1], "no_such_column"))
    with pytest.raises(Exception):
        list(orders.find_by_keys([("Berlin",)], ["place", "day"]))
    with pytest.raises(Exception):
        list(
            orders.find_by_keys(
                [("Berlin", D1)], ["place", "day"], columns=["nope"]
            )
        )


def test_temp_table_dropped_after_consumption(orders, db):
    # The temporary table is dropped before the iterator is handed out, so
    # state never outlives the call regardless of how the result is consumed.
    rows = orders.find_by_keys([("Paris", D2)], ["place", "day"])
    assert temp_table_names(db) == []
    assert [r["amount"] for r in rows] == [30]
    # Repeated calls must not leave state behind.
    list(orders.find_by_keys([("Rome", D3)], ["place", "day"]))
    assert temp_table_names(db) == []


def test_temp_table_dropped_on_early_close(orders, db):
    rows = orders.find_by_keys([("Paris", D2)], ["place", "day"])
    next(rows)
    rows.close()
    assert temp_table_names(db) == []


def test_temp_table_dropped_on_query_error(orders, db, monkeypatch):
    bound_execute = orders.db.executable.execute

    def failing_execute(statement, parameters=None):
        if statement.__class__.__name__ == "Select":
            raise RuntimeError("boom")
        if parameters is None:
            return bound_execute(statement)
        return bound_execute(statement, parameters)

    monkeypatch.setattr(orders.db.executable, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="boom"):
        list(orders.find_by_keys([("Paris", D2)], ["place", "day"]))
    assert temp_table_names(db) == []

    with pytest.raises(RuntimeError, match="boom"):
        orders.count_by_keys([("Paris", D2)], ["place", "day"])
    assert temp_table_names(db) == []


def test_outer_transaction_is_not_committed(orders, db):
    conn = db.executable
    commits = []
    rollbacks = []
    original_commit = conn.commit
    original_rollback = conn.rollback
    conn.commit = lambda: (  # type: ignore[method-assign]
        commits.append(1),
        original_commit(),
    )
    conn.rollback = lambda: (  # type: ignore[method-assign]
        rollbacks.append(1),
        original_rollback(),
    )
    try:
        db.begin()
        list(orders.find_by_keys([("Paris", D2)], ["place", "day"]))
        orders.count_by_keys([("Paris", D2)], ["place", "day"])
        # The helper must neither commit nor roll back the outer transaction.
        assert commits == []
        assert rollbacks == []
        assert db.in_transaction
        # Every temporary table used above is dropped before the call returns.
        assert conn not in db._temp_tables or db._temp_tables[conn] == []
    finally:
        conn.commit = original_commit  # type: ignore[method-assign]
        conn.rollback = original_rollback  # type: ignore[method-assign]
        db.rollback()
    # rollback() releases the thread's connection; its session-local temp
    # tables are destroyed together with it.
    assert conn.closed
    assert conn not in db._temp_tables


def test_rollback_discards_outer_writes(orders, db):
    # The key query must not force-commit the surrounding transaction, so
    # user writes issued inside it can still be rolled back.
    with db:
        orders.insert({"place": "Madrid", "day": D1, "amount": 99})
        list(orders.find_by_keys([("Madrid", D1)], ["place", "day"]))
        db.rollback()
    assert orders.count_by_keys([("Madrid", D1)], ["place", "day"]) == 0


def test_rollback_cleans_temp_state(orders, db):
    db.begin()
    rows = orders.find_by_keys([("Paris", D2)], ["place", "day"])
    # Temp state is already gone; nothing is registered for cleanup.
    assert temp_table_names(db) == []
    old_conn = db.executable
    db.rollback()
    # The explicit rollback released the connection; closing the stale
    # iterator afterwards must not raise.
    rows.close()
    assert old_conn.closed
    assert old_conn not in db._temp_tables


def test_explicit_commit_keeps_outer_writes(orders, db):
    with db:
        orders.insert({"place": "Madrid", "day": D1, "amount": 99})
        found = list(
            orders.find_by_keys([("Madrid", D1)], ["place", "day"])
        )
        assert [r["amount"] for r in found] == [99]
    assert orders.count_by_keys([("Madrid", D1)], ["place", "day"]) == 1
    assert temp_table_names(db) == []


def test_find_on_missing_table(db):
    ghost = dataset.Table(db, "ghost")
    assert list(ghost.find_by_keys([1], "id")) == []
    assert ghost.count_by_keys([1], "id") == 0
