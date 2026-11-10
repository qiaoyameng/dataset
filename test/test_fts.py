"""Tests for the optional SQLite FTS5 full-text search support."""

import pytest
from sqlalchemy import text

import dataset
from dataset import DatasetError, FTSCapabilityError, FTSConfigError
from dataset.fts import (
    FTS_META_TABLE,
    fts_table_name,
    fts_trigger_names,
    quote_identifier,
)


@pytest.fixture(scope="function")
def docs(db):
    table = db["documents"]
    table.insert_many(
        [
            {"title": "banana split", "body": "sweet dessert with banana"},
            {"title": "apple pie", "body": "warm apple and cinnamon"},
            {"title": "fruit salad", "body": "apple banana grape mix"},
        ]
    )
    table.enable_fts(["title", "body"])
    return table


# -- enablement / backfill / idempotency -----------------------------------


def test_enable_fts_backfills_existing_rows(docs):
    assert docs.has_fts()
    config = docs.fts_config()
    assert config is not None
    assert config.columns == ("title", "body")
    hits = [row["id"] for row in docs.search("banana")]
    assert hits == [1, 3]


def test_enable_fts_is_idempotent(db):
    table = db["notes"]
    table.insert({"body": "hello world"})
    first = table.enable_fts(["body"])
    second = table.enable_fts(["body"])
    assert first == second
    trigger_names = fts_trigger_names("notes").values()
    rows = db.query(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    )
    persisted = {row["name"] for row in rows}
    assert persisted.issuperset(trigger_names)
    # enabling twice must not duplicate triggers
    assert len(persisted.intersection(trigger_names)) == 3


def test_enable_fts_requires_existing_table(db):
    table = db.create_table("ghost", primary_id=False)
    with pytest.raises(DatasetError):
        table.enable_fts(["body"])


def test_enable_fts_validates_columns(docs):
    with pytest.raises(FTSConfigError):
        docs.enable_fts(["no_such_column"])
    with pytest.raises(FTSConfigError):
        docs.enable_fts([], rowid_column="id")
    with pytest.raises(FTSConfigError):
        docs.enable_fts(["body"], rowid_column="body")


def test_rowid_must_be_integer_primary_key(db):
    table = db.create_table(
        "string_keyed",
        primary_id="code",
        primary_type=db.types.text,
        primary_increment=False,
    )
    table.insert({"code": "a", "body": "alpha"})
    with pytest.raises(FTSConfigError):
        table.enable_fts(["body"], rowid_column="code")


def test_bad_tokenizer_rejected(docs):
    with pytest.raises(FTSConfigError):
        docs.enable_fts(["title"], tokenize="evil('x')")


# -- write synchronisation -------------------------------------------------


def test_update_removes_old_terms(docs):
    docs.update(
        {"id": 1, "title": "melon split", "body": "sweet dessert with melon"},
        ["id"],
    )
    assert [row["id"] for row in docs.search("banana")] == [3]
    assert [row["id"] for row in docs.search("melon")] == [1]


def test_delete_removes_index_terms(docs):
    docs.delete(id=2)
    assert [row["id"] for row in docs.search("pie")] == []
    assert [row["id"] for row in docs.search("apple")] == [3]


def test_bulk_writes_are_indexed(db):
    table = db["bulk"]
    table.insert({"body": "seed row"})
    table.enable_fts(["body"])
    table.insert_many([{"body": "mango juice"}, {"body": "lemon tea"}])
    assert sorted(row["id"] for row in table.search("mango OR lemon")) == [2, 3]
    table.update_many([{"id": 2, "body": "lime tea"}], ["id"])
    assert [row["id"] for row in table.search("mango")] == []
    assert [row["id"] for row in table.search("lime")] == [2]
    assert [row["id"] for row in table.search("lemon")] == [3]
    table.upsert({"id": 3, "body": "papaya bowl"}, ["id"])
    assert [row["id"] for row in table.search("papaya")] == [3]
    table.upsert({"id": 3, "body": "guava bowl"}, ["id"])
    assert [row["id"] for row in table.search("papaya")] == []
    assert [row["id"] for row in table.search("guava")] == [3]


def test_transaction_rollback_leaves_no_index_items(docs):
    with pytest.raises(RuntimeError):
        with docs.db:
            docs.insert({"title": "peach cake", "body": "peachy peach"})
            assert [row["id"] for row in docs.search("peach")] == [4]
            raise RuntimeError("rollback please")
    assert [row["id"] for row in docs.search("peach")] == []
    # the external content index still matches the table contents
    fts_name = fts_table_name("documents")
    docs.db.executable.execute(
        text(
            f"INSERT INTO {quote_identifier(fts_name)}"
            f"({quote_identifier(fts_name)}) VALUES('integrity-check')"
        )
    )


# -- search semantics ------------------------------------------------------


def test_search_returns_records_rank_and_snippet(docs):
    rows = list(docs.search("grape", snippet=True))
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "fruit salad"
    assert isinstance(row["rank"], float)
    assert "grape" in row["snippet"]


def test_search_snippet_for_specific_column(docs):
    row = list(docs.search("banana", snippet="body", _limit=1))[0]
    assert "<b>banana</b>" in row["snippet"]
    with pytest.raises(FTSConfigError):
        list(docs.search("banana", snippet="id"))


def test_search_combines_filters_and_limit(docs):
    rows = list(
        docs.search("apple OR banana", title={"like": "%salad%"}, _limit=1)
    )
    assert [row["id"] for row in rows] == [3]
    rows = list(docs.search("apple", id={"gte": 2}))
    assert {row["id"] for row in rows} == {2, 3}


def test_match_expression_is_parameter_bound(docs):
    malicious = "banana'); DROP TABLE documents;--"
    with pytest.raises(Exception):
        list(docs.search(malicious))
    assert docs.count() == 3


def test_search_without_index_raises(db):
    table = db["plain"]
    table.insert({"body": "hello"})
    with pytest.raises(FTSConfigError):
        list(table.search("hello"))


# -- configuration changes -------------------------------------------------


def test_changed_columns_produce_rebuild_plan(docs):
    # the rowid column is normalised away, so this is the same config
    plan = docs.plan_fts_rebuild(["title", "body", "id"], rowid_column="id")
    assert plan.columns_changed is False
    assert plan.reason == "configuration changed"
    plan = docs.plan_fts_rebuild(["body"])
    assert plan.columns_changed is True
    assert plan.reason == "indexed columns changed"
    assert "body" in plan.describe()
    with pytest.raises(FTSConfigError):
        docs.enable_fts(["body"])
    # the rejected change leaves the old index fully functional
    assert [row["id"] for row in docs.search("split")] == [1]
    docs.enable_fts(["body"], rebuild=True)
    assert list(docs.search("split")) == []
    assert [row["id"] for row in docs.search("cinnamon")] == [2]
    # source table data is untouched
    assert docs.find_one(id=1)["title"] == "banana split"


def test_failed_enable_rolls_back_everything(db):
    table = db.create_table(
        "broken",
        primary_id="rid",
        primary_type=db.types.bigint,
        primary_increment=False,
    )
    table.insert({"rid": 1, "body": "alpha"})
    # store a non-integer value in the integer-affinity rowid column via
    # raw SQL; SQLite stores it as text and the FTS5 backfill then aborts
    # halfway through the index build.
    fts_name = fts_table_name("broken")
    db.executable.execute(
        text("INSERT INTO broken(rid, body) VALUES ('not-a-number', 'beta')")
    )
    with pytest.raises(Exception):
        table.enable_fts(["body"], rowid_column="rid")
    assert table.has_fts() is False
    assert db.has_table(fts_name) is False
    assert db.has_table(FTS_META_TABLE) is False
    triggers = [
        row["name"]
        for row in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    ]
    assert triggers == []
    # the connection stays usable after the rolled-back failure
    assert table.count() == 2
    # the source table remains usable and indexing can be retried
    db.executable.execute(
        text("UPDATE broken SET rid = 2 WHERE body = 'beta'")
    )
    table.enable_fts(["body"], rowid_column="rid")
    assert [row["rid"] for row in table.search("alpha")] == [1]


# -- rename / drop coordination --------------------------------------------


def test_rename_rebuilds_index_and_refresh_cache(docs):
    docs.rename("renamed_docs")
    assert docs.name == "renamed_docs"
    assert docs.fts_config().table == "renamed_docs"
    assert docs.db.has_table("documents") is False
    # writes after the rename keep maintaining the index
    docs.insert({"title": "kiwi smoothie", "body": "kiwi and mint"})
    assert [row["id"] for row in docs.search("kiwi")] == [4]
    docs.update(
        {"id": 4, "title": "mango smoothie", "body": "mango and mint"},
        ["id"],
    )
    assert [row["id"] for row in docs.search("kiwi")] == []
    assert [row["id"] for row in docs.search("mango")] == [4]


def test_rename_conflict_and_noop(docs):
    other = docs.db["other_place"]
    other.insert({"body": "x"})
    with pytest.raises(DatasetError):
        docs.rename("other_place")
    same = docs.rename("documents")
    assert same is docs


def test_failed_rename_restores_state(docs, monkeypatch):
    from sqlalchemy.exc import OperationalError

    original_execute = docs.db.executable.execute

    def fail(*args, **kwargs):
        statement = args[0] if args else kwargs.get("statement")
        if "ALTER TABLE" in str(statement):
            raise OperationalError("rename", {}, Exception("disk I/O error"))
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(docs.db.executable, "execute", fail)
    with pytest.raises(OperationalError):
        docs.rename("should_not_exist")
    monkeypatch.undo()
    assert docs.name == "documents"
    assert docs.db.has_table("documents")
    assert docs.db.has_table("should_not_exist") is False
    # the index survived and still works
    assert [row["id"] for row in docs.search("banana")] == [1, 3]


def test_drop_removes_index_and_config(docs):
    docs.drop()
    assert docs.db.has_table(fts_table_name("documents")) is False
    assert list(docs.db.query(f"SELECT * FROM {FTS_META_TABLE}")) == []
    triggers = [
        row["name"]
        for row in docs.db.query(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    ]
    assert triggers == []


def test_disable_fts_keeps_table(docs):
    docs.disable_fts()
    assert docs.has_fts() is False
    assert docs.count() == 3
    docs.disable_fts()  # idempotent


# -- weird identifiers -----------------------------------------------------


def test_fts_with_spaces_and_quotes_in_identifiers(db):
    table = db.create_table(
        'we ird"tbl',
        primary_id='row id',
        primary_type=db.types.bigint,
        primary_increment=False,
    )
    table.insert_many(
        [
            {"row id": 10, 'co l"1': "hello world"},
            {"row id": 11, 'co l"1': "foo bar"},
        ]
    )
    table.enable_fts(['co l"1'], rowid_column="row id")
    assert [row["row id"] for row in table.search("hello")] == [10]
    table.update(
        {"row id": 10, 'co l"1': "goodbye world"}, ["row id"]
    )
    assert [row["row id"] for row in table.search("hello")] == []
    assert [row["row id"] for row in table.search("goodbye")] == [10]
    row = list(table.search("goodbye", snippet='co l"1'))[0]
    assert "<b>goodbye</b>" in row["snippet"]
    with pytest.raises(RuntimeError):
        with db:
            table.insert({"row id": 12, 'co l"1': "peach"})
            raise RuntimeError("rollback")
    assert [row["row id"] for row in table.search("peach")] == []


def test_fts_normalizes_column_name_casing(db):
    table = db.create_table(
        "cased_fts",
        primary_id="MyId",
        primary_type=db.types.bigint,
        primary_increment=False,
    )
    table.insert({"MyId": 7, "Body": "hello world"})
    config = table.enable_fts(["BODY"], rowid_column="myid")
    assert config.columns == ("Body",)
    assert config.rowid_column == "MyId"
    assert [row["MyId"] for row in table.search("hello")] == [7]
    row = list(table.search("hello", snippet="body"))[0]
    assert "<b>hello</b>" in row["snippet"]


# -- capability detection --------------------------------------------------


def test_capability_error_without_fts5(db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    table = db["probe_docs"]
    table.insert({"body": "hello"})

    def fail(*args, **kwargs):
        raise OperationalError("probe", {}, Exception("no such module: fts5"))

    monkeypatch.setattr(db.executable, "begin_nested", fail)
    with pytest.raises(FTSCapabilityError):
        table.enable_fts(["body"])
    # the failed probe leaves nothing behind
    assert table.has_fts() is False


def test_capability_error_on_non_sqlite(db):
    table = db["pg_only"]
    table.insert({"body": "hello"})
    db.is_sqlite = False
    with pytest.raises(FTSCapabilityError):
        table.enable_fts(["body"])


# -- persistence -----------------------------------------------------------


def test_fts_persists_across_connections(tmp_path):
    path = tmp_path / "persistent.db"
    url = f"sqlite:///{path}"
    first = dataset.connect(url)
    table = first["docs"]
    table.insert({"body": "persistent content"})
    table.enable_fts(["body"])
    first.close()

    second = dataset.connect(url)
    reopened = second.load_table("docs")
    assert reopened.fts_config() is not None
    assert [row["id"] for row in reopened.search("persistent")] == [1]
    reopened.insert({"body": "second row"})
    assert [row["id"] for row in reopened.search("second")] == [2]
    second.close()
