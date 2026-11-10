"""Optional SQLite FTS5 full-text search for dataset tables.

The index is an FTS5 *external content* table that mirrors a set of text
columns of an existing dataset table. Three triggers keep it in sync inside
the same database transaction as the table writes they react to, so no
separate search service and no out-of-band synchronisation is needed.

Every identifier that ends up in a DDL statement is quoted with
:func:`quote_identifier`, while MATCH expressions and all other values are
passed as bound parameters.
"""

import json
from dataclasses import asdict, dataclass
from hashlib import sha1
from typing import TYPE_CHECKING

from sqlalchemy import Integer, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import Connection

from dataset.util import DatasetError, ensure_strings, normalize_column_name

if TYPE_CHECKING:
    from dataset.database import Database
    from dataset.table import Table

#: Name of the table persisting the FTS configuration of every indexed table.
FTS_META_TABLE = "_dataset_fts_meta"

#: Tokenizers offered by stock SQLite FTS5 builds.
ALLOWED_TOKENIZERS = ("unicode61", "ascii", "porter")


class FTSCapabilityError(DatasetError):
    """Raised when the connected SQLite build cannot provide FTS5."""


class FTSConfigError(DatasetError):
    """Raised when an FTS configuration is invalid or already differs."""


def quote_identifier(name: str) -> str:
    """Quote an SQLite identifier, escaping embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


def fts_table_name(table_name: str) -> str:
    """Deterministic name of the FTS5 virtual table backing *table_name*."""
    digest = sha1(table_name.encode("utf-8")).hexdigest()[:16]
    return f"_fts_{table_name}_{digest}"


def fts_trigger_names(table_name: str) -> dict[str, str]:
    """Names of the synchronisation triggers for *table_name*."""
    digest = sha1(table_name.encode("utf-8")).hexdigest()[:10]
    return {
        "insert": f"_fts_{table_name}_{digest}_ai",
        "delete": f"_fts_{table_name}_{digest}_ad",
        "update": f"_fts_{table_name}_{digest}_au",
    }


@dataclass(frozen=True)
class FTSConfig:
    """Persisted configuration of a table's FTS5 index."""

    table: str
    rowid_column: str
    columns: tuple[str, ...]
    tokenize: str = "unicode61"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "FTSConfig":
        data = json.loads(payload)
        return cls(
            table=data["table"],
            rowid_column=data["rowid_column"],
            columns=tuple(data["columns"]),
            tokenize=data.get("tokenize", "unicode61"),
        )


@dataclass(frozen=True)
class FTSRebuildPlan:
    """Inspectable description of a pending FTS index rebuild.

    Call :meth:`Table.rebuild_fts` to execute a plan returned by
    :meth:`Table.plan_fts_rebuild`.
    """

    table: str
    current: FTSConfig | None
    requested: FTSConfig
    reason: str

    @property
    def columns_changed(self) -> bool:
        return self.current is None or self.current.columns != self.requested.columns

    def describe(self) -> str:
        """Human-readable summary of what the rebuild would do."""
        if self.current is None:
            current = "(none)"
        else:
            current = ", ".join(self.current.columns)
        requested = ", ".join(self.requested.columns)
        return (
            f"FTS rebuild for table {self.table!r} ({self.reason}): "
            f"columns [{current}] -> [{requested}], "
            f"rowid={self.requested.rowid_column!r}, "
            f"tokenize={self.requested.tokenize!r}"
        )


class FTSManager:
    """Create, persist and query FTS5 external-content indexes for a table."""

    def __init__(self, table: "Table") -> None:
        self.table = table

    @property
    def db(self) -> "Database":
        return self.table.db

    @property
    def conn(self) -> Connection:
        return self.db.executable

    # -- capability -------------------------------------------------------

    def check_supported(self) -> None:
        """Raise :class:`FTSCapabilityError` unless FTS5 is usable."""
        if not self.db.is_sqlite or self.db.engine is None:
            raise FTSCapabilityError(
                "FTS5 full-text search is only available on SQLite databases."
            )
        # A virtual table created inside a savepoint is fully rolled back, so
        # this leaves no trace behind on either in-memory and file databases.
        try:
            savepoint = self.conn.begin_nested()
            try:
                self.conn.execute(
                    text(
                        "CREATE VIRTUAL TABLE _dataset_fts5_probe "
                        "USING fts5(probe)"
                    )
                )
            finally:
                savepoint.rollback()
        except OperationalError as exc:
            raise FTSCapabilityError(
                "The connected SQLite build does not support the FTS5 "
                "extension, so full-text search is unavailable."
            ) from exc

    # -- configuration ----------------------------------------------------

    def _validate_tokenize(self, tokenize: str) -> str:
        # The tokenizer name is interpolated into DDL, therefore only the
        # known stock tokenizers are accepted to keep the statement safe.
        name = tokenize.strip()
        base = name.split()[0] if name else ""
        if base not in ALLOWED_TOKENIZERS:
            raise FTSConfigError(
                f"Unsupported FTS5 tokenizer {tokenize!r}; expected one of "
                f"{', '.join(ALLOWED_TOKENIZERS)}."
            )
        return name

    def _validate_columns(
        self, columns: list[str], rowid_column: str
    ) -> tuple[str, list[str]]:
        rowid_column = self.table._get_column_name(
            normalize_column_name(rowid_column)
        )
        if not self.table.has_column(rowid_column):
            raise FTSConfigError(
                f"Row identity column {rowid_column!r} does not exist on "
                f"table {self.table.name!r}."
            )
        pk_columns = {column.name for column in self.table.table.primary_key.columns}
        if rowid_column not in pk_columns:
            raise FTSConfigError(
                f"Row identity column {rowid_column!r} must be part of the "
                "primary key so that FTS5 rowids stay stable."
            )
        pk_column = self.table.table.c[rowid_column]
        if not isinstance(pk_column.type, Integer):
            raise FTSConfigError(
                f"Row identity column {rowid_column!r} must have an integer "
                "type because FTS5 rowids are integers."
            )
        normalized: list[str] = []
        for column in ensure_strings(columns):
            name = self.table._get_column_name(normalize_column_name(column))
            if name == rowid_column:
                continue
            if not self.table.has_column(name):
                raise FTSConfigError(
                    f"Indexed column {name!r} does not exist on table "
                    f"{self.table.name!r}."
                )
            if name not in normalized:
                normalized.append(name)
        if not normalized:
            raise FTSConfigError("At least one text column must be indexed.")
        return rowid_column, normalized

    def build_config(
        self,
        columns: list[str],
        rowid_column: str,
        tokenize: str = "unicode61",
    ) -> FTSConfig:
        """Validate arguments and produce a normalised :class:`FTSConfig`."""
        self.check_supported()
        tokenize = self._validate_tokenize(tokenize)
        rowid_column, text_columns = self._validate_columns(columns, rowid_column)
        return FTSConfig(
            table=self.table.name,
            rowid_column=rowid_column,
            columns=tuple(text_columns),
            tokenize=tokenize,
        )

    def _ensure_meta_table(self) -> None:
        self.conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {quote_identifier(FTS_META_TABLE)} "
                "(table_name TEXT PRIMARY KEY, config TEXT NOT NULL)"
            )
        )

    def get_config(self) -> FTSConfig | None:
        """Return the persisted config for the table, if any."""
        if not self.db.has_table(FTS_META_TABLE):
            return None
        rp = self.conn.execute(
            text(
                f"SELECT config FROM {quote_identifier(FTS_META_TABLE)} "
                "WHERE table_name = :name"
            ),
            {"name": self.table.name},
        )
        row = rp.fetchone()
        if row is None:
            return None
        return FTSConfig.from_json(str(row[0]))

    def _store_config(self, config: FTSConfig) -> None:
        self._ensure_meta_table()
        self.conn.execute(
            text(
                f"INSERT INTO {quote_identifier(FTS_META_TABLE)} "
                "(table_name, config) VALUES (:name, :config) "
                "ON CONFLICT(table_name) DO UPDATE SET config = :config"
            ),
            {"name": self.table.name, "config": config.to_json()},
        )

    def _delete_config(self) -> None:
        if not self.db.has_table(FTS_META_TABLE):
            return
        self.conn.execute(
            text(
                f"DELETE FROM {quote_identifier(FTS_META_TABLE)} "
                "WHERE table_name = :name"
            ),
            {"name": self.table.name},
        )

    # -- DDL --------------------------------------------------------------

    def _create_virtual_table(self, config: FTSConfig) -> None:
        fts_name = fts_table_name(config.table)
        column_sql = ", ".join(quote_identifier(col) for col in config.columns)
        statement = (
            f"CREATE VIRTUAL TABLE {quote_identifier(fts_name)} USING fts5("
            f"{column_sql}, content={quote_identifier(config.table)}, "
            f"content_rowid={quote_identifier(config.rowid_column)}, "
            f"tokenize={quote_identifier(config.tokenize)})"
        )
        self.conn.execute(text(statement))

    def _create_triggers(self, config: FTSConfig) -> None:
        table_q = quote_identifier(config.table)
        fts_q = quote_identifier(fts_table_name(config.table))
        rowid_q = quote_identifier(config.rowid_column)
        column_list = ", ".join(quote_identifier(col) for col in config.columns)
        new_values = ", ".join(
            f"new.{quote_identifier(col)}" for col in config.columns
        )
        old_values = ", ".join(
            f"old.{quote_identifier(col)}" for col in config.columns
        )
        triggers = fts_trigger_names(config.table)
        statements = {
            "insert": (
                f"CREATE TRIGGER {quote_identifier(triggers['insert'])} "
                f"AFTER INSERT ON {table_q} BEGIN INSERT INTO {fts_q}"
                f"(rowid, {column_list}) VALUES (new.{rowid_q}, {new_values}); END"
            ),
            "delete": (
                f"CREATE TRIGGER {quote_identifier(triggers['delete'])} "
                f"AFTER DELETE ON {table_q} BEGIN INSERT INTO {fts_q}"
                f"({fts_q}, rowid, {column_list}) VALUES ('delete', "
                f"old.{rowid_q}, {old_values}); END"
            ),
            "update": (
                f"CREATE TRIGGER {quote_identifier(triggers['update'])} "
                f"AFTER UPDATE ON {table_q} BEGIN INSERT INTO {fts_q}"
                f"({fts_q}, rowid, {column_list}) VALUES ('delete', "
                f"old.{rowid_q}, {old_values}); "
                f"INSERT INTO {fts_q}(rowid, {column_list}) "
                f"VALUES (new.{rowid_q}, {new_values}); END"
            ),
        }
        for statement in statements.values():
            self.conn.execute(text(statement))

    def _drop_artifacts(self, table_name: str) -> None:
        """Drop triggers and the FTS virtual table for *table_name*."""
        triggers = fts_trigger_names(table_name)
        for trigger in triggers.values():
            self.conn.execute(
                text(f"DROP TRIGGER IF EXISTS {quote_identifier(trigger)}")
            )
        fts_name = fts_table_name(table_name)
        self.conn.execute(text(f"DROP TABLE IF EXISTS {quote_identifier(fts_name)}"))

    def _backfill(self, config: FTSConfig) -> None:
        fts_q = quote_identifier(fts_table_name(config.table))
        column_list = ", ".join(quote_identifier(col) for col in config.columns)
        self.conn.execute(
            text(
                f"INSERT INTO {fts_q}(rowid, {column_list}) "
                f"SELECT {quote_identifier(config.rowid_column)}, {column_list} "
                f"FROM {quote_identifier(config.table)} "
                f"WHERE {quote_identifier(config.rowid_column)} IS NOT NULL"
            )
        )
        # Fails if the index contents do not match the external content table.
        self.conn.execute(
            text(f"INSERT INTO {fts_q}({fts_q}) VALUES('integrity-check')")
        )

    # -- lifecycle --------------------------------------------------------

    def create(self, config: FTSConfig) -> None:
        """Build the index, triggers and backfill inside one savepoint."""
        savepoint = self.conn.begin_nested()
        try:
            self._drop_artifacts(config.table)
            self._create_virtual_table(config)
            self._create_triggers(config)
            self._backfill(config)
            self._store_config(config)
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise
        self.db._auto_commit()

    def drop(self) -> None:
        """Remove the index, its triggers and its persisted configuration."""
        config = self.get_config()
        savepoint = self.conn.begin_nested()
        try:
            self._drop_artifacts(self.table.name)
            if config is not None:
                self._delete_config()
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise
        self.db._auto_commit()

    def plan_rebuild(self, requested: FTSConfig) -> FTSRebuildPlan:
        """Return an inspectable plan describing a pending rebuild."""
        current = self.get_config()
        if current is None:
            reason = "no existing index"
        elif current.columns != requested.columns:
            reason = "indexed columns changed"
        elif current.rowid_column != requested.rowid_column:
            reason = "row identity column changed"
        elif current.tokenize != requested.tokenize:
            reason = "tokenizer changed"
        else:
            reason = "configuration changed"
        return FTSRebuildPlan(
            table=self.table.name,
            current=current,
            requested=requested,
            reason=reason,
        )

    def rebuild(self, config: FTSConfig) -> None:
        """Replace any existing index with one matching *config*."""
        self.create(config)
