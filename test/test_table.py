import copy
from datetime import datetime

import pytest
import sqlalchemy
from sqlalchemy.exc import ArgumentError
from sqlalchemy.types import BIGINT, TEXT

import dataset
from dataset import chunked
from dataset.util import DatasetError

from .conftest import TEST_CITY_1, TEST_DATA


def test_insert(table):
    assert len(table) == len(TEST_DATA), len(table)
    last_id = table.insert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"}
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    assert table.find_one(id=last_id)["place"] == "Berlin"


def test_insert_ignore(table):
    table.insert_ignore(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    table.insert_ignore(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_insert_ignore_all_key(table):
    for _i in range(0, 4):
        table.insert_ignore(
            {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
            ["date", "temperature", "place"],
        )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_insert_json(table):
    last_id = table.insert(
        {
            "date": datetime(2011, 1, 2),
            "temperature": -10,
            "place": "Berlin",
            "info": {
                "currency": "EUR",
                "language": "German",
                "population": 3292365,
            },
        }
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    assert table.find_one(id=last_id)["place"] == "Berlin"


def test_upsert(table):
    table.upsert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    table.upsert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_upsert_single_column(db):
    table = db["banana_single_col"]
    table.upsert({"color": "Yellow"}, ["color"])
    assert len(table) == 1, len(table)
    table.upsert({"color": "Yellow"}, ["color"])
    assert len(table) == 1, len(table)


def test_upsert_all_key(table):
    assert len(table) == len(TEST_DATA), len(table)
    for _i in range(0, 2):
        table.upsert(
            {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
            ["date", "temperature", "place"],
        )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_upsert_id(db):
    table = db["banana_with_id"]
    data = {"id": 10, "title": "I am a banana!"}
    table.upsert(data, ["id"])
    assert len(table) == 1, len(table)


def test_update_while_iter(table):
    for row in table:
        row["foo"] = "bar"
        table.update(row, ["place", "date"])
    assert len(table) == len(TEST_DATA), len(table)


def test_weird_column_names(table):
    with pytest.raises(ValueError):
        table.insert(
            {
                "date": datetime(2011, 1, 2),
                "temperature": -10,
                "foo.bar": "Berlin",
                "qux.bar": "Huhu",
            }
        )


def test_cased_column_names(db):
    tbl = db["cased_column_names"]
    tbl.insert({"place": "Berlin"})
    tbl.insert({"Place": "Berlin"})
    tbl.insert({"PLACE ": "Berlin"})
    assert len(tbl.columns) == 2, tbl.columns
    assert len(list(tbl.find(Place="Berlin"))) == 3
    assert len(list(tbl.find(place="Berlin"))) == 3
    assert len(list(tbl.find(PLACE="Berlin"))) == 3


def test_invalid_column_names(db):
    tbl = db["weather"]
    with pytest.raises(ValueError):
        tbl.insert({None: "banana"})

    with pytest.raises(ValueError):
        tbl.insert({"": "banana"})

    with pytest.raises(ValueError):
        tbl.insert({"-": "banana"})


def test_delete(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    original_count = len(table)
    assert len(table) == len(TEST_DATA) + 1, len(table)
    # Test bad use of API
    with pytest.raises(ArgumentError):
        table.delete({"place": "Berlin"})
    assert len(table) == original_count, len(table)

    assert table.delete(place="Berlin") is True, "should return 1"
    assert len(table) == len(TEST_DATA), len(table)
    assert table.delete() is True, "should return non zero"
    assert len(table) == 0, len(table)


def test_repr(table):
    assert repr(table) == "<Table(weather)>", (
        "the representation should be <Table(weather)>"
    )


def test_delete_nonexist_entry(table):
    assert table.delete(place="Berlin") is False, (
        "entry not exist, should fail to delete"
    )


def test_find_one(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    d = table.find_one(place="Berlin")
    assert d["temperature"] == -10, d
    d = table.find_one(place="Atlantis")
    assert d is None, d


def test_count(table):
    assert len(table) == 6, len(table)
    length = table.count(place=TEST_CITY_1)
    assert length == 3, length


def test_find(table):
    ds = list(table.find(place=TEST_CITY_1))
    assert len(ds) == 3, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2, _step=1))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=1, _step=2))
    assert len(ds) == 1, ds
    ds = list(table.find(_step=2))
    assert len(ds) == len(TEST_DATA), ds
    ds = list(table.find(order_by=["temperature"]))
    assert ds[0]["temperature"] == -1, ds
    ds = list(table.find(order_by=["-temperature"]))
    assert ds[0]["temperature"] == 8, ds
    ds = list(table.find(table.table.columns.temperature > 4))
    assert len(ds) == 3, ds


def test_find_dsl(table):
    ds = list(table.find(place={"like": "%lw%"}))
    assert len(ds) == 3, ds
    ds = list(table.find(temperature={">": 5}))
    assert len(ds) == 2, ds
    ds = list(table.find(temperature={">=": 5}))
    assert len(ds) == 3, ds
    ds = list(table.find(temperature={"<": 0}))
    assert len(ds) == 1, ds
    ds = list(table.find(temperature={"<=": 0}))
    assert len(ds) == 2, ds
    ds = list(table.find(temperature={"!=": -1}))
    assert len(ds) == 5, ds
    ds = list(table.find(temperature={"between": [5, 8]}))
    assert len(ds) == 3, ds
    ds = list(table.find(place={"=": "G€lway"}))
    assert len(ds) == 3, ds
    ds = list(table.find(place={"ilike": "%LwAy"}))
    assert len(ds) == 3, ds


def test_startswith_endswith(db):
    t = db["prefix_test"]
    t.insert({"org": "acme"})
    t.insert({"org": "acme_labs"})
    t.insert({"org": "other"})
    t.insert({"org": "admin"})

    # Basic startswith
    rows = list(t.find(org={"startswith": "acme"}))
    assert len(rows) == 2, rows

    # Basic endswith
    rows = list(t.find(org={"endswith": "labs"}))
    assert len(rows) == 1, rows

    # LIKE metacharacters must be escaped (CVE-style regression test)
    rows = list(t.find(org={"startswith": "%"}))
    assert len(rows) == 0, f"startswith % should match nothing, got {rows}"

    rows = list(t.find(org={"startswith": "_"}))
    assert len(rows) == 0, f"startswith _ should match nothing, got {rows}"

    rows = list(t.find(org={"endswith": "%"}))
    assert len(rows) == 0, f"endswith % should match nothing, got {rows}"

    # Delete must not overmatch
    t.delete(org={"startswith": "%"})
    assert t.count() == 4, "delete with % startswith should not remove rows"


def test_offset(table):
    ds = list(table.find(place=TEST_CITY_1, _offset=1))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2, _offset=2))
    assert len(ds) == 1, ds


def test_streamed_update(table):
    ds = list(table.find(place=TEST_CITY_1, _streamed=True, _step=1))
    assert len(ds) == 3, len(ds)
    for row in table.find(place=TEST_CITY_1, _streamed=True, _step=1):
        row["temperature"] = -1
        table.update(row, ["id"])


def test_distinct(table):
    x = list(table.distinct("place"))
    assert len(x) == 2, x
    x = list(table.distinct("place", "date"))
    assert len(x) == 6, x
    x = list(
        table.distinct(
            "place",
            "date",
            table.table.columns.date >= datetime(2011, 1, 2, 0, 0),
        )
    )
    assert len(x) == 4, x

    x = list(table.distinct("temperature", place="B€rkeley"))
    assert len(x) == 3, x
    x = list(table.distinct("temperature", place=["B€rkeley", "G€lway"]))
    assert len(x) == 6, x


def test_insert_many(table):
    data = TEST_DATA * 100
    table.insert_many(data, chunk_size=13)
    assert len(table) == len(data) + 6, (len(table), len(data))


def test_chunked_insert(table):
    data = TEST_DATA * 100
    with chunked.ChunkedInsert(table) as chunk_tbl:
        for item in data:
            chunk_tbl.insert(item)
    assert len(table) == len(data) + 6, (len(table), len(data))


def test_chunked_insert_callback(table):
    data = TEST_DATA * 100
    n_items = 0

    def callback(queue):
        nonlocal n_items
        n_items += len(queue)

    with chunked.ChunkedInsert(table, callback=callback) as chunk_tbl:
        for item in data:
            chunk_tbl.insert(item)
    assert len(data) == n_items
    assert len(table) == len(data) + 6


def test_update_many(db):
    tbl = db["update_many_test"]
    tbl.insert_many([{"temp": 10}, {"temp": 20}, {"temp": 30}])
    tbl.update_many([{"id": 1, "temp": 50}, {"id": 3, "temp": 50}], "id")

    # Ensure data has been updated.
    assert tbl.find_one(id=1)["temp"] == tbl.find_one(id=3)["temp"]


def test_chunked_update(db):
    tbl = db["update_many_test"]
    tbl.insert_many(
        [
            {"temp": 10, "location": "asdf"},
            {"temp": 20, "location": "qwer"},
            {"temp": 30, "location": "asdf"},
        ]
    )

    chunked_tbl = chunked.ChunkedUpdate(tbl, ["id"])
    chunked_tbl.update({"id": 1, "temp": 50})
    chunked_tbl.update({"id": 2, "location": "asdf"})
    chunked_tbl.update({"id": 3, "temp": 50})
    chunked_tbl.flush()

    # Ensure data has been updated.
    assert tbl.find_one(id=1)["temp"] == tbl.find_one(id=3)["temp"] == 50
    assert tbl.find_one(id=2)["location"] == tbl.find_one(id=3)["location"] == "asdf"


def test_upsert_many(db):
    # Also tests updating on records with different attributes
    tbl = db["upsert_many_test"]

    weight = 100
    tbl.upsert_many([{"age": 10}, {"weight": weight}], "id")
    assert tbl.find_one(id=1)["age"] == 10

    tbl.upsert_many([{"id": 1, "age": 70}, {"id": 2, "weight": weight / 2}], "id")
    assert tbl.find_one(id=2)["weight"] == weight / 2


def _bulk_rows():
    # Sparse rows, explicit NULLs and a composite key repeated in input order.
    return [
        {"a": 1, "b": 10, "c": "x"},
        {"a": 2, "b": 20, "c": "y", "d": 1.5},
        {"a": 1, "b": 10, "c": None},
        {"a": 1, "b": 10, "d": 2.5},
        {"a": 2, "b": 20},
        {"a": 3, "b": 30, "c": "z", "d": None},
        {"a": 2, "b": 20, "c": "y2", "d": 9.9},
        {"a": 3, "b": 30, "d": 7.7},
    ]


def _table_snapshot(tbl):
    return sorted(
        (dict(row) for row in tbl.find()),
        key=lambda row: tuple(sorted(row.items(), key=str)),
    )


def _count_cursor_execs(engine):
    events = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        events.append((statement, executemany))

    sqlalchemy.event.listen(engine, "before_cursor_execute", listener)
    return events


def test_bulk_upsert_matches_row_by_row(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    rows = _bulk_rows()

    reference = dataset.connect()
    reference["parity"].upsert_many(copy.deepcopy(rows), ["a", "b"])
    expected = _table_snapshot(reference["parity"])

    tbl = db["parity_bulk"]
    tbl.upsert_many(copy.deepcopy(rows), ["a", "b"], bulk=True)
    assert _table_snapshot(tbl) == expected

    # Explicit NULL was written, missing columns kept their values, and the
    # repeated composite key was applied in input order without undoing prior
    # column updates.
    first = tbl.find_one(a=1, b=10)
    assert first["c"] is None
    assert first["d"] == 2.5
    second = tbl.find_one(a=2, b=20)
    assert second["c"] == "y2"
    assert second["d"] == 9.9
    third = tbl.find_one(a=3, b=30)
    assert third["c"] == "z"
    assert third["d"] == 7.7
    assert len(tbl) == 3
    reference.close()


def test_bulk_upsert_does_not_mutate_input(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_immutable_input"]
    rows = _bulk_rows()
    saved = copy.deepcopy(rows)
    tbl.upsert_many(rows, ["a", "b"], bulk=True)
    assert rows == saved


def test_bulk_upsert_uses_fewer_sql_statements(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    rows = [
        {"a": i, "b": i * 2, "c": None if i % 4 == 0 else f"v{i}"}
        for i in range(30)
    ]

    reference = dataset.connect()
    ref_events = _count_cursor_execs(reference.engine)
    reference["slow"].upsert_many(copy.deepcopy(rows), ["a", "b"])
    ref_dml = [
        event for event in ref_events if event[0].lstrip("(").split()[0].upper()
        in ("INSERT", "UPDATE")
    ]

    tbl = db["fast"]
    bulk_events = _count_cursor_execs(db.engine)
    tbl.upsert_many(copy.deepcopy(rows), ["a", "b"], bulk=True)
    bulk_dml = [
        event
        for event in bulk_events
        if event[0].lstrip("(").split()[0].upper() in ("INSERT", "UPDATE")
    ]

    # Row-by-row needs an UPDATE plus an INSERT attempt per row; the bulk
    # path collapses the whole batch into one executemany statement.
    assert len(bulk_dml) == 1
    assert bulk_dml[0][1] is True
    assert len(bulk_dml) < len(ref_dml)
    assert len(ref_dml) == 2 * len(rows)
    reference.close()


def test_bulk_upsert_missing_or_null_key_errors_upfront(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_key_errors"]
    tbl.insert({"a": 1, "b": 1, "c": "seed"})

    with pytest.raises(DatasetError, match="missing key column"):
        tbl.upsert_many([{"a": 1, "c": "x"}], ["a", "b"], bulk=True)
    with pytest.raises(DatasetError, match="NULL value for key"):
        tbl.upsert_many(
            [{"a": 1, "b": None, "c": "x"}], ["a", "b"], bulk=True
        )
    # Nothing was written and no column was created by the failed batch.
    assert tbl.find_one(a=1, b=1)["c"] == "seed"
    assert "c" in tbl.columns
    assert tbl.count() == 1


def test_bulk_upsert_requires_unique_constraint(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_no_constraint"]
    tbl.insert({"a": 1, "b": 1, "c": "v"})
    with pytest.raises(DatasetError, match="UNIQUE constraint"):
        tbl.upsert_many(
            [{"a": 1, "b": 1, "c": "w"}],
            ["a", "b"],
            bulk=True,
            ensure=False,
        )
    assert tbl.find_one(a=1, b=1)["c"] == "v"

    # With schema changes allowed, a unique index is created and the upsert
    # then goes through.
    tbl.upsert_many([{"a": 1, "b": 1, "c": "w"}], ["a", "b"], bulk=True)
    assert tbl.find_one(a=1, b=1)["c"] == "w"


def test_bulk_upsert_rejects_non_unique_existing_rows(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_duplicate_data"]
    tbl.insert_many([{"a": 1, "b": 1}, {"a": 1, "b": 1}])
    with pytest.raises(DatasetError, match="not unique"):
        tbl.upsert_many(
            [{"a": 1, "b": 1, "c": "x"}], ["a", "b"], bulk=True
        )
    assert tbl.count() == 2


def test_bulk_upsert_is_atomic(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_atomic"]
    tbl.insert({"a": 1, "b": 1, "c": "old"})
    tbl.create_index(["a", "b"], name="uab", unique=True)
    tbl.create_column("e", db.types.text)
    tbl.create_index(["e"], name="ue", unique=True)
    tbl.update({"a": 1, "b": 1, "e": "dup"}, ["a", "b"])

    rows = [
        {"a": 1, "b": 1, "c": "new"},
        {"a": 9, "b": 9, "e": "dup"},
        {"a": 10, "b": 10, "e": "dup"},
    ]
    with pytest.raises(DatasetError, match="rolled back"):
        tbl.upsert_many(rows, ["a", "b"], bulk=True)

    assert tbl.find_one(a=1, b=1)["c"] == "old"
    assert tbl.count(a=9) == 0
    assert tbl.count(a=10) == 0


def test_bulk_upsert_splits_on_sqlite_variable_limit(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_split"]
    tbl.SQLITE_FALLBACK_VARIABLE_LIMIT = 200
    data_columns = [f"col{i}" for i in range(30)]
    rows = []
    for row_index in range(40):
        row = {"a": row_index, "b": row_index * 2}
        for col_index, column in enumerate(data_columns):
            if (row_index + col_index) % 3:
                row[column] = (
                    None if (row_index + col_index) % 7 == 0
                    else f"{row_index}-{col_index}"
                )
        rows.append(row)

    tbl.upsert_many(rows, ["a", "b"], bulk=True, chunk_size=1000)
    # Re-applying the same batch must not create duplicates and must keep
    # every value stable, proving chunk splits preserve semantics.
    tbl.upsert_many(copy.deepcopy(rows), ["a", "b"], bulk=True, chunk_size=1000)
    assert len(tbl) == 40
    sample = tbl.find_one(a=7)
    for column in data_columns:
        if column in rows[7]:
            assert sample[column] == rows[7][column]
        else:
            assert sample[column] is None


def test_bulk_upsert_respects_chunk_size(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_chunks"]
    events = _count_cursor_execs(db.engine)
    rows = [{"a": i, "b": i, "c": i} for i in range(25)]
    tbl.upsert_many(rows, ["a", "b"], bulk=True, chunk_size=10)
    dml = [
        event
        for event in events
        if event[0].lstrip("(").split()[0].upper() in ("INSERT", "UPDATE")
    ]
    assert len(dml) == 3
    assert all(event[1] for event in dml)
    assert len(tbl) == 25


def test_bulk_upsert_unsupported_backend(db):
    if db.is_sqlite:
        # Pretend to be another backend; the guard must not depend on actual
        # driver behavior beyond this flag.
        db.is_sqlite = False
    tbl = db["bulk_unsupported"]
    with pytest.raises(DatasetError, match="only supported on SQLite"):
        tbl.upsert_many([{"a": 1}], ["a"], bulk=True)


def test_bulk_upsert_key_only_rows_do_nothing_on_conflict(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_key_only"]
    tbl.insert({"k": 1, "v": "keep"})
    tbl.create_index(["k"], unique=True)
    # No data columns: a conflicting row must leave the stored value alone.
    tbl.upsert_many([{"k": 1}, {"k": 2}], ["k"], bulk=True)
    assert tbl.find_one(k=1)["v"] == "keep"
    assert tbl.find_one(k=2)["v"] is None
    assert len(tbl) == 2


def test_bulk_upsert_cased_column_names(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_cased"]
    tbl.upsert_many(
        [{"Place": "Berlin", "n": 1}, {"place": "Berlin", "n": 2}],
        ["Place"],
        bulk=True,
    )
    assert len(tbl) == 1
    row = tbl.find_one()
    assert row["n"] == 2
    assert "Place" in row


def test_bulk_upsert_rolls_back_inside_explicit_transaction(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_txn_rollback"]
    with db:
        tbl.upsert_many([{"k": 1, "v": "a"}], ["k"], bulk=True)
        db.rollback()
    assert tbl.count() == 0


def test_bulk_upsert_honors_types_override(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_types"]
    tbl.upsert_many(
        [{"k": 1, "note": 5}],
        ["k"],
        bulk=True,
        types={"note": db.types.text},
    )
    # Without the override the integer sample would yield BigInteger.
    assert tbl.table.c["note"].type.__class__.__name__ in ("TEXT", "UnicodeText")


def test_bulk_upsert_rejects_non_positive_chunk_size(db):
    if not db.is_sqlite:
        pytest.skip("bulk upsert is SQLite-only")
    tbl = db["bulk_bad_chunk"]
    with pytest.raises(DatasetError, match="chunk_size"):
        tbl.upsert_many([{"k": 1}], ["k"], bulk=True, chunk_size=0)


def test_drop_operations(table):
    assert table._table is not None, "table shouldn't be dropped yet"
    table.drop()
    assert table._table is None, "table should be dropped now"
    assert list(table.all()) == [], table.all()
    assert table.count() == 0, table.count()


def test_table_drop(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db


def test_table_drop_then_create(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db
    db["weather"].insert({"foo": "bar"})


def test_columns(table):
    cols = table.columns
    assert len(list(cols)) == 4, "column count mismatch"
    assert "date" in cols and "temperature" in cols and "place" in cols


def test_drop_column(table):
    try:
        table.drop_column("date")
        assert "date" not in table.columns
    except RuntimeError:
        pass


def test_iter(table):
    c = 0
    for _row in table:
        c += 1
    assert c == len(table)


def test_update(table):
    date = datetime(2011, 1, 2)
    res = table.update(
        {"date": date, "temperature": -10, "place": TEST_CITY_1}, ["place", "date"]
    )
    assert res, "update should return True"
    m = table.find_one(place=TEST_CITY_1, date=date)
    assert m["temperature"] == -10, (
        f"new temp. should be -10 but is {m['temperature']}"
    )


def test_create_column(db, table):
    flt = db.types.float
    table.create_column("foo", flt)
    assert "foo" in table.table.c, table.table.c
    assert isinstance(table.table.c["foo"].type, flt), table.table.c["foo"].type
    assert "foo" in table.columns, table.columns


def test_ensure_column(db, table):
    flt = db.types.float
    table.create_column_by_example("foo", 0.1)
    assert "foo" in table.table.c, table.table.c
    assert isinstance(table.table.c["foo"].type, flt), table.table.c["foo"].type
    table.create_column_by_example("bar", 1)
    assert "bar" in table.table.c, table.table.c
    assert isinstance(table.table.c["bar"].type, BIGINT), table.table.c["bar"].type
    table.create_column_by_example("pippo", "test")
    assert "pippo" in table.table.c, table.table.c
    assert isinstance(table.table.c["pippo"].type, TEXT), table.table.c["pippo"].type
    table.create_column_by_example("bigbar", 11111111111)
    assert "bigbar" in table.table.c, table.table.c
    assert isinstance(table.table.c["bigbar"].type, BIGINT), table.table.c[
        "bigbar"
    ].type
    table.create_column_by_example("littlebar", -11111111111)
    assert "littlebar" in table.table.c, table.table.c
    assert isinstance(table.table.c["littlebar"].type, BIGINT), table.table.c[
        "littlebar"
    ].type


def test_key_order(db, table):
    res = db.query("SELECT temperature, place FROM weather LIMIT 1")
    keys = list(res.next().keys())
    assert keys[0] == "temperature"
    assert keys[1] == "place"


def test_empty_query(table):
    empty = list(table.find(place="not in data"))
    assert len(empty) == 0, empty
