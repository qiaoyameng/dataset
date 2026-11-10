import logging
import sqlite3
import threading
import warnings
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import case, false, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, NoSuchTableError, OperationalError
from sqlalchemy.schema import Column, Index
from sqlalchemy.schema import Table as SQLATable
from sqlalchemy.sql import and_, expression
from sqlalchemy.sql.expression import (
    ClauseElement,
    ColumnElement,
    UnaryExpression,
    bindparam,
)
from sqlalchemy.sql.dml import Insert as SqlInsert

from dataset.types import MYSQL_LENGTH_TYPES, ColumnType, Types
from dataset.util import (
    QUERY_STEP,
    DatasetError,
    MutableRow,
    OutRow,
    QueryError,
    ResultIter,
    SQLWriteValue,
    WriteRow,
    ensure_strings,
    index_name,
    normalize_column_key,
    normalize_column_name,
    normalize_table_name,
    pad_chunk_columns,
)

if TYPE_CHECKING:
    from dataset.database import Database

log = logging.getLogger(__name__)


class Table:
    """Represents a table in a database and exposes common operations."""

    PRIMARY_DEFAULT = "id"

    def __init__(
        self,
        database: "Database",
        table_name: str,
        primary_id: str | Literal[False] | None = None,
        primary_type: ColumnType | None = None,
        primary_increment: bool | None = None,
        auto_create: bool = False,
    ):
        """Initialise the table from database schema."""
        self.db = database
        self.name = normalize_table_name(table_name)
        self._table: SQLATable | None = None
        self._columns: dict[str, str] | None = None
        self._indexes: list[set[str]] = []
        self._primary_id: str | Literal[False] = (
            primary_id if primary_id is not None else self.PRIMARY_DEFAULT
        )
        self._primary_type: ColumnType = (
            primary_type if primary_type is not None else Types.integer
        )
        if primary_increment is None:
            primary_increment = self._primary_type in (Types.integer, Types.bigint)
        self._primary_increment = primary_increment
        self._auto_create = auto_create

    @property
    def exists(self) -> bool:
        """Check to see if the table currently exists in the database."""
        if self._table is not None:
            return True
        return self.name in self.db

    @property
    def table(self) -> SQLATable:
        """Get a reference to the table, which may be reflected or created.

        This property guarantees to return a non-None SQLATable instance.
        If the table doesn't exist and auto_create is False, raises DatasetError.
        """
        if self._table is None:
            self._sync_table(())
        assert self._table is not None, "_sync_table should ensure _table is set"
        return self._table

    @property
    def _column_keys(self) -> dict[str, str]:
        """Get a dictionary of all columns and their case mapping."""
        if not self.exists:
            return {}
        with self.db.lock:
            if self._columns is None:
                # Initialise the table if it doesn't exist
                table = self.table
                self._columns = {}
                for column in table.columns:
                    name = normalize_column_name(column.name)
                    key = normalize_column_key(name)
                    if key in self._columns:
                        log.warning("Duplicate column: %s", name)
                    if key is None:
                        log.warning("Invalid column name: %s", name)
                        continue
                    self._columns[key] = name
            return self._columns

    @property
    def columns(self) -> list[str]:
        """Get a listing of all columns that exist in the table."""
        return list(self._column_keys.values())

    def has_column(self, column: str | None) -> bool:
        """Check if a column with the given name exists on this table."""
        if column is None:
            return False
        key = normalize_column_key(normalize_column_name(column))
        return key in self._column_keys

    def _get_column_name(self, name: str) -> str:
        """Find the best column name with case-insensitive matching."""
        name = normalize_column_name(name)
        key = normalize_column_key(name)
        if key is None:
            return name
        return self._column_keys.get(key, name)

    def insert(
        self,
        row: WriteRow,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> Any:
        """Add a ``row`` dict by inserting it into the table.

        If ``ensure`` is set, any of the keys of the row are not
        table columns, they will be created automatically.

        During column creation, ``types`` will be checked for a key
        matching the name of a column to be created, and the given
        SQLAlchemy column type will be used. Otherwise, the type is
        guessed from the row value, defaulting to a simple unicode
        field.
        ::

            data = dict(title='I am a banana!')
            table.insert(data)

        Returns the inserted row's primary key.
        """
        row = self._sync_columns(row, ensure, types=types)
        res = self.db.executable.execute(self.table.insert().values(row))
        self.db._auto_commit()
        if res.inserted_primary_key is not None and len(res.inserted_primary_key) > 0:
            return res.inserted_primary_key[0]
        return True

    def insert_ignore(
        self,
        row: WriteRow,
        keys: Sequence[str],
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> Any:
        """Add a ``row`` dict into the table if the row does not exist.

        If rows with matching ``keys`` exist no change is made.

        Setting ``ensure`` results in automatically creating missing columns,
        i.e., keys of the row are not table columns.

        During column creation, ``types`` will be checked for a key
        matching the name of a column to be created, and the given
        SQLAlchemy column type will be used. Otherwise, the type is
        guessed from the row value, defaulting to a simple unicode
        field.
        ::

            data = dict(id=10, title='I am a banana!')
            table.insert_ignore(data, ['id'])
        """
        row = self._sync_columns(row, ensure, types=types)
        if self._check_ensure(ensure):
            self.create_index(keys)
        args, _ = self._keys_to_args(row, keys)
        if self.count(**args) == 0:
            return self.insert(row, ensure=False)
        return False

    def insert_many(
        self,
        rows: Sequence[WriteRow],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> None:
        """Add many rows at a time.

        This is significantly faster than adding them one by one. Per default
        the rows are processed in chunks of 1000 per commit, unless you specify
        a different ``chunk_size``.

        See :py:meth:`insert() <dataset.Table.insert>` for details on
        the other parameters.
        ::

            rows = [dict(name='Dolly')] * 10000
            table.insert_many(rows)
        """
        # Sync table before inputting rows.
        sync_row: MutableRow = {}
        for row in rows:
            # Only get non-existing columns.
            sync_keys = list(sync_row.keys())
            for key in [k for k in row if k not in sync_keys]:
                # Get a sample of the new column(s) from the row.
                sync_row[key] = row[key]
        self._sync_columns(sync_row, ensure, types=types)

        # Get columns name list to be used for padding later.
        columns = sync_row.keys()

        chunk: list[MutableRow] = []
        for index, row in enumerate(rows):
            chunk.append(dict(row))

            # Insert when chunk_size is fulfilled or this is the last row
            if len(chunk) == chunk_size or index == len(rows) - 1:
                chunk = pad_chunk_columns(chunk, columns)
                self.db.executable.execute(self.table.insert(), chunk)
                self.db._auto_commit()
                chunk = []

    def update(
        self,
        row: WriteRow,
        keys: Sequence[str],
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        return_count: bool = False,
    ) -> bool | int:
        """Update a row in the table.

        The update is managed via the set of column names stated in ``keys``:
        they will be used as filters for the data to be updated, using the
        values in ``row``.
        ::

            # update all entries with id matching 10, setting their title
            # columns
            data = dict(id=10, title='I am a banana!')
            table.update(data, ['id'])

        If keys in ``row`` update columns not present in the table, they will
        be created based on the settings of ``ensure`` and ``types``, matching
        the behavior of :py:meth:`insert() <dataset.Table.insert>`.
        """
        row = self._sync_columns(row, ensure, types=types)
        args, row = self._keys_to_args(row, keys)
        clause = self._args_to_clause(args)
        if not len(row):
            return self.count(clause)
        stmt = self.table.update().where(clause).values(row)
        rp = self.db.executable.execute(stmt)
        self.db._auto_commit()
        if rp.supports_sane_rowcount():
            return rp.rowcount
        if return_count:
            return self.count(clause)
        return False

    def update_many(
        self,
        rows: Sequence[WriteRow],
        keys: Sequence[str],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> None:
        """Update many rows in the table at a time.

        This is significantly faster than updating them one by one. Per default
        the rows are processed in chunks of 1000 per commit, unless you specify
        a different ``chunk_size``.

        See :py:meth:`update() <dataset.Table.update>` for details on
        the other parameters.
        """
        keys = ensure_strings(keys)

        chunk: list[MutableRow] = []
        columns: list[str] = []
        for index, row in enumerate(rows):
            columns.extend(
                col for col in row if (col not in columns) and (col not in keys)
            )

            # bindparam requires names to not conflict (cannot be "id" for id)
            row_ = dict(row)
            for key in keys:
                row_[f"_{key}"] = row_[key]
                row_.pop(key)
            chunk.append(row_)

            # Update when chunk_size is fulfilled or this is the last row
            if len(chunk) == chunk_size or index == len(rows) - 1:
                cl = [self.table.c[k] == bindparam(f"_{k}") for k in keys]
                stmt = (
                    self.table.update()
                    .where(and_(True, *cl))
                    .values({col: bindparam(col, required=False) for col in columns})
                )
                self.db.executable.execute(stmt, chunk)
                self.db._auto_commit()
                chunk = []

    def upsert(
        self,
        row: WriteRow,
        keys: Sequence[str],
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
    ) -> Any:
        """An UPSERT is a smart combination of insert and update.

        If rows with matching ``keys`` exist they will be updated, otherwise a
        new row is inserted in the table.
        ::

            data = dict(id=10, title='I am a banana!')
            table.upsert(data, ['id'])
        """
        row = self._sync_columns(row, ensure, types=types)
        if self._check_ensure(ensure):
            self.create_index(keys)
        row_count = self.update(row, keys, ensure=False, return_count=True)
        if row_count == 0:
            return self.insert(row, ensure=False)
        return True

    def upsert_many(
        self,
        rows: Sequence[WriteRow],
        keys: Sequence[str],
        chunk_size: int = 1000,
        ensure: bool | None = None,
        types: dict[str, ColumnType] | None = None,
        bulk: bool = False,
    ) -> None:
        """
        Sorts multiple input rows into upserts and inserts. Inserts are passed
        to insert and upserts are updated.

        See :py:meth:`upsert() <dataset.Table.upsert>` and
        :py:meth:`insert_many() <dataset.Table.insert_many>`.

        On SQLite, ``bulk=True`` selects a native batched path built on
        ``INSERT ... ON CONFLICT DO UPDATE``. It preserves the row-by-row
        semantics while issuing far fewer SQL statements:

        * every key column must be present on each input row and must not be
          ``None``, otherwise a :class:`~dataset.util.DatasetError` is raised
          before any write happens;
        * a column missing from a row keeps its existing value on update,
          while an explicit ``None`` writes SQL ``NULL``;
        * if the same key occurs more than once in ``rows``, the rows are
          applied in input order, and a later row never undoes a column set
          by an earlier row;
        * a unique constraint (or primary key) covering the key columns must
          exist. If it does not and ``ensure`` allows schema changes, a unique
          index is created; otherwise a clear :class:`~dataset.util.DatasetError`
          is raised up front;
        * the whole batch is atomic: either every row takes effect or the
          table is left untouched;
        * batches exceeding SQLite's host-parameter limit are split into
          smaller chunks without changing the result.

        On other backends, or when ``bulk`` is ``False``, the established
        row-by-row :py:meth:`upsert() <dataset.Table.upsert>` loop is used as
        the compatibility baseline.
        """
        if not bulk:
            # Removing a bulk implementation in 5e09aba401. Doing this one by
            # one is incredibly slow, but doesn't run into issues with column
            # creation. It stays available as the compatibility baseline.
            for row in rows:
                self.upsert(row, keys, ensure=ensure, types=types)
            return
        if not self.db.is_sqlite:
            raise DatasetError(
                "Bulk upsert is only supported on SQLite. Use bulk=False "
                "for the portable row-by-row implementation."
            )
        self._sqlite_upsert_many(rows, keys, chunk_size, ensure, types)

    # Host-parameter limit used when the sqlite3 module does not expose a
    # runtime ``sqlite_limit()`` query (Python 3.10 and older builds).
    SQLITE_FALLBACK_VARIABLE_LIMIT = 999

    def _sqlite_variable_limit(self) -> int:
        """Return SQLite's SQLITE_LIMIT_VARIABLE_NUMBER for the connection."""
        dbapi_connection = self.db.executable.connection
        sqlite_limit = getattr(dbapi_connection, "sqlite_limit", None)
        if sqlite_limit is not None:
            try:
                limit = sqlite_limit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            except (AttributeError, TypeError):
                limit = None
            if isinstance(limit, int) and limit > 0:
                return limit
        return self.SQLITE_FALLBACK_VARIABLE_LIMIT

    def _validate_upsert_key_values(
        self, rows: Sequence[WriteRow], key_names: Sequence[str]
    ) -> None:
        """Make sure every row carries a complete, non-NULL composite key.

        This only inspects the input objects and therefore runs before any
        schema change, so malformed batches cannot create tables or columns.
        """
        normalized_keys = [normalize_column_key(key) for key in key_names]
        for index, row in enumerate(rows):
            lookup: dict[str, str] = {}
            for name in row:
                column_key = normalize_column_key(name)
                if column_key is not None:
                    lookup[column_key] = name
            for key, key_key in zip(key_names, normalized_keys):
                if key_key is None:
                    raise DatasetError(f"{key!r} is not a valid key column.")
                actual = lookup.get(key_key)
                if actual is None:
                    raise DatasetError(
                        f"upsert_many row {index} is missing key column "
                        f"{key!r}; all key columns must be provided."
                    )
                if row[actual] is None:
                    raise DatasetError(
                        f"upsert_many row {index} has NULL value for key "
                        f"column {key!r}; key columns must not be NULL."
                    )

    def _validate_upsert_key_columns(
        self, key_names: Sequence[str], column_keys: dict[str, str]
    ) -> None:
        """Ensure the composite key columns exist on the table after sync."""
        missing = [
            key
            for key in key_names
            if normalize_column_key(key) not in column_keys
        ]
        if missing:
            raise DatasetError(
                f"Cannot bulk upsert: key column(s) {missing!r} do not exist "
                "on the table and could not be created."
            )

    def _resolve_conflict_target(
        self, key_names: Sequence[str], ensure: bool | None
    ) -> list[str]:
        """Find the PRIMARY KEY/UNIQUE constraint that makes keys unique.

        Returns the constraint columns in the order SQLite knows them in.
        If no matching constraint exists, create a unique index when schema
        changes are allowed, otherwise fail before any data is written.
        """
        key_set = set(key_names)
        primary_key = [column.name for column in self.table.primary_key.columns]
        if set(primary_key) == key_set:
            return primary_key

        inspector = self.db.inspect
        for index in inspector.get_indexes(self.name, schema=self.db.schema):
            if index.get("unique"):
                columns = index.get("column_names", [])
                if set(columns) == key_set:
                    return list(columns)
        for constraint in inspector.get_unique_constraints(
            self.name, schema=self.db.schema
        ):
            columns = constraint.get("column_names", [])
            if set(columns) == key_set:
                return list(columns)

        if not self._check_ensure(ensure):
            raise DatasetError(
                f"Cannot bulk upsert on keys {list(key_names)!r}: no PRIMARY "
                "KEY or UNIQUE constraint covers these columns, and schema "
                "changes are disabled (ensure=False)."
            )

        with self.db.lock:
            self._threading_warn()
            index = Index(
                index_name(self.name, list(key_names)),
                *[self.table.c[key] for key in key_names],
                unique=True,
            )
            try:
                index.create(self.db.executable)
            except (IntegrityError, OperationalError) as exc:
                raise DatasetError(
                    f"Cannot bulk upsert on keys {list(key_names)!r}: existing "
                    "rows are not unique on these columns, so no UNIQUE "
                    "constraint could be created to locate rows reliably."
                ) from exc
            self.db._auto_commit()
        return list(key_names)

    def _build_sqlite_upsert_statement(
        self,
        key_names: Sequence[str],
        data_columns: Sequence[str],
        conflict_target: Sequence[str],
    ) -> SqlInsert:
        """Build the parameterized ON CONFLICT DO UPDATE statement.

        Each data column gets a value bind (``_v_<col>``) and a presence flag
        (``_s_<col>``). On conflict the flag decides whether the supplied
        value (which may be explicit NULL) is written or the old column value
        is kept, so missing fields never clear stored data.
        """
        table = self.table
        set_values: dict[str, Any] = {}
        insert_values: dict[str, Any] = {}
        for column in data_columns:
            set_values[column] = case(
                (bindparam(f"_s_{column}"), bindparam(f"_v_{column}")),
                else_=table.c[column],
            )
            insert_values[column] = bindparam(f"_v_{column}")
        for key in key_names:
            insert_values[key] = bindparam(key)
        insert_statement = sqlite_insert(table).values(**insert_values)
        if not set_values:
            # Rows only carry key columns: an existing match has nothing to
            # update, matching the row-by-row path (empty update, no insert).
            return insert_statement.on_conflict_do_nothing(
                index_elements=list(conflict_target)
            )
        return insert_statement.on_conflict_do_update(
            index_elements=list(conflict_target),
            set_=set_values,
        )

    def _build_upsert_params(
        self,
        rows: Sequence[WriteRow],
        key_names: Sequence[str],
        data_columns: Sequence[str],
    ) -> list[MutableRow]:
        """Convert input rows to bind-parameter dicts without mutating them.

        Rows sharing a composite key are merged in input order: a later row
        only overwrites a column when it actually provides one. An explicit
        ``None`` counts as provided (flag stays 1) and writes SQL NULL, while
        a missing column keeps the value carried by an earlier row. This
        matters because SQLite evaluates ``DO UPDATE`` expressions against
        the pre-conflict row, so repeating a key within one executemany could
        otherwise erase values inserted moments earlier in the same batch.
        """
        normalized_keys = [normalize_column_key(key) for key in key_names]
        normalized_data = [normalize_column_key(column) for column in data_columns]
        param_rows: list[MutableRow] = []
        merged_index: dict[tuple[Any, ...], int] = {}
        for row in rows:
            lookup: dict[str, str] = {}
            for name in row:
                column_key = normalize_column_key(name)
                if column_key is not None:
                    lookup[column_key] = name
            key_values = []
            for key, key_key in zip(key_names, normalized_keys):
                if key_key is None:
                    raise DatasetError(f"{key!r} is not a valid key column.")
                actual_key = lookup[key_key]
                key_values.append(row[actual_key])
            params_key = tuple(key_values)
            if params_key in merged_index:
                params = param_rows[merged_index[params_key]]
            else:
                params = {}
                for key, value in zip(key_names, key_values):
                    params[key] = value
                for column in data_columns:
                    params[f"_v_{column}"] = None
                    params[f"_s_{column}"] = 0
                merged_index[params_key] = len(param_rows)
                param_rows.append(params)
            for column, column_key in zip(data_columns, normalized_data):
                actual = lookup.get(column_key)
                if actual is not None:
                    params[f"_v_{column}"] = row[actual]
                    params[f"_s_{column}"] = 1
        return param_rows

    def _sqlite_upsert_many(
        self,
        rows: Sequence[WriteRow],
        keys: Sequence[str],
        chunk_size: int,
        ensure: bool | None,
        types: dict[str, ColumnType] | None,
    ) -> None:
        """SQLite-native batched upsert preserving row-by-row semantics."""
        if len(rows) == 0:
            return
        key_names = [
            self._get_column_name(key) for key in ensure_strings(keys)
        ]
        if not key_names:
            raise DatasetError("upsert_many requires at least one key column.")
        if chunk_size <= 0:
            raise DatasetError("chunk_size must be a positive integer.")

        # Key presence/NULL checks must fail before any table or column is
        # created, so an invalid batch never changes the schema.
        self._validate_upsert_key_values(rows, key_names)

        # Sync any missing table/columns once, using the first value seen for
        # each column so type guessing matches the row-by-row path. The table
        # is reflected here; everything afterwards resolves columns in memory.
        # Touching ``table`` first forces a single reflection (or table
        # creation), so ``_get_column_name`` below never hits the database.
        self.table
        sync_row: MutableRow = {}
        sync_name_by_key: dict[str, str] = {}
        for row in rows:
            for name, value in row.items():
                resolved = self._get_column_name(name)
                column_key = normalize_column_key(resolved)
                if column_key is not None and column_key in sync_name_by_key:
                    # Case-insensitive duplicate within the batch itself; the
                    # table cannot exist yet for this column, so collapse onto
                    # the first spelling instead of creating two columns.
                    resolved = sync_name_by_key[column_key]
                elif resolved not in sync_row:
                    sync_row[resolved] = value
                    if column_key is not None:
                        sync_name_by_key[column_key] = resolved
        self._sync_columns(sync_row, ensure, types=types)

        # Take one schema snapshot and resolve everything against it instead
        # of triggering reflection per row/column via has_column().
        column_keys = self._column_keys
        # Key names given by the caller are normalized onto real columns.
        resolved_key_names: list[str] = []
        for key in key_names:
            actual = column_keys.get(normalize_column_key(key), key)
            resolved_key_names.append(actual)
        key_names = resolved_key_names
        key_key_set = {normalize_column_key(key) for key in key_names}

        # After syncing, every key column must actually exist.
        self._validate_upsert_key_columns(key_names, column_keys)

        # Data columns follow first-seen order across the batch, restricted to
        # columns that actually exist on the table.
        data_columns: list[str] = []
        seen_data_keys: set[str] = set()
        for row in rows:
            for name in row:
                column_key = normalize_column_key(name)
                if column_key is None or column_key in key_key_set:
                    continue
                actual_column = column_keys.get(column_key)
                if actual_column is not None and column_key not in seen_data_keys:
                    seen_data_keys.add(column_key)
                    data_columns.append(actual_column)

        conflict_target = self._resolve_conflict_target(key_names, ensure)
        statement = self._build_sqlite_upsert_statement(
            key_names, data_columns, conflict_target
        )
        param_rows = self._build_upsert_params(rows, key_names, data_columns)

        # Each row binds one value per key plus a value/flag pair per data
        # column. Split conservatively so SQLite's variable limit is never
        # exceeded, even when insertmanyvalues re-batches the rows.
        params_per_row = len(key_names) + 2 * len(data_columns)
        variable_limit = self._sqlite_variable_limit()
        if params_per_row > variable_limit:
            raise DatasetError(
                "Cannot bulk upsert: a single row needs "
                f"{params_per_row} SQL parameters but SQLite allows at most "
                f"{variable_limit}."
            )
        rows_per_chunk = min(chunk_size, variable_limit // params_per_row)

        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.begin()
        try:
            for start in range(0, len(param_rows), rows_per_chunk):
                chunk = param_rows[start : start + rows_per_chunk]
                self.db.executable.execute(statement, chunk)
        except (IntegrityError, OperationalError) as exc:
            if own_transaction:
                self.db.rollback()
            raise DatasetError(
                "Bulk upsert failed and the whole batch was rolled back: "
                f"{exc.orig}"
            ) from exc
        except Exception:
            if own_transaction:
                self.db.rollback()
            raise
        if own_transaction:
            self.db.commit()

    def delete(self, *clauses: ColumnElement[bool], **filters: SQLWriteValue) -> bool:
        """Delete rows from the table.

        Keyword arguments can be used to add column-based filters. The filter
        criterion will always be equality:
        ::

            table.delete(place='Berlin')

        If no arguments are given, all records are deleted.
        """
        if not self.exists:
            return False
        clause = self._args_to_clause(filters, clauses=clauses)
        stmt = self.table.delete().where(clause)
        rp = self.db.executable.execute(stmt)
        self.db._auto_commit()
        return rp.rowcount > 0

    def _reflect_table(self) -> None:
        """Load the tables definition from the database."""
        with self.db.lock:
            self._columns = None
            try:
                self._table = SQLATable(
                    self.name,
                    self.db.metadata,
                    schema=self.db.schema,
                    autoload_with=self.db.executable,
                )
            except NoSuchTableError:
                self._table = None

    def _threading_warn(self) -> None:
        if self.db.in_transaction and threading.active_count() > 1:
            warnings.warn(
                "Changing the database schema inside a transaction "
                "in a multi-threaded environment is likely to lead "
                "to race conditions and synchronization issues.",
                RuntimeWarning,
                stacklevel=2,
            )

    def _sync_table(self, columns: Sequence[Column[Any]]) -> None:
        """Lazy load, create or adapt the table structure in the database.

        This method guarantees that self._table will be set to a non-None value
        after successful execution. If the table cannot be created or loaded,
        it raises DatasetError.
        """
        if self._table is None:
            # Load an existing table from the database.
            self._reflect_table()
        if self._table is None:
            # Create the table with an initial set of columns.
            if not self._auto_create:
                raise DatasetError(f"Table does not exist: {self.name}")
            # Keep the lock scope small because this is run very often.
            with self.db.lock:
                self._threading_warn()
                self._table = SQLATable(
                    self.name, self.db.metadata, schema=self.db.schema
                )
                if self._primary_id is not False:
                    # This can go wrong on DBMS like MySQL and SQLite where
                    # tables cannot have no columns.
                    column = Column(
                        self._primary_id,
                        self._primary_type,
                        primary_key=True,
                        autoincrement=self._primary_increment,
                    )
                    self._table.append_column(column)
                for column in columns:
                    if column.name != self._primary_id:
                        self._table.append_column(column)
                self._table.create(self.db.executable, checkfirst=True)
                self._columns = None
                self.db._auto_commit()
        elif len(columns):
            with self.db.lock:
                self._reflect_table()
                self._threading_warn()
                for column in columns:
                    if not self.has_column(column.name):
                        self.db.op.add_column(self.name, column, schema=self.db.schema)
                self._reflect_table()
                self.db._auto_commit()

    def _sync_columns(
        self,
        row: WriteRow,
        ensure: bool | None,
        types: dict[str, ColumnType] | None = None,
    ) -> MutableRow:
        """Create missing columns (or the table) prior to writes.

        If automatic schema generation is disabled (``ensure`` is ``False``),
        this will remove any keys from the ``row`` for which there is no
        matching column.
        """
        ensure = self._check_ensure(ensure)
        types = types or {}
        types = {self._get_column_name(k): v for (k, v) in types.items()}
        out = {}
        sync_columns = {}
        for name, value in row.items():
            name = self._get_column_name(name)
            if self.has_column(name):
                out[name] = value
            elif ensure:
                _type = types.get(name)
                if _type is None:
                    _type = self.db.types.guess(value)
                sync_columns[name] = Column(name, _type)
                out[name] = value
        self._sync_table(list(sync_columns.values()))
        return out

    def _check_ensure(self, ensure: bool | None) -> bool:
        if ensure is None:
            return self.db.ensure_schema
        return ensure

    def _generate_clause(
        self, column: str, op: str, value: SQLWriteValue
    ) -> ColumnElement[bool]:
        if op in ("like",):
            return self.table.c[column].like(value)
        if op in ("ilike",):
            return self.table.c[column].ilike(value)
        if op in ("notlike",):
            return self.table.c[column].notlike(value)
        if op in ("notilike",):
            return self.table.c[column].notilike(value)
        if op in (">", "gt"):
            return self.table.c[column] > value
        if op in ("<", "lt"):
            return self.table.c[column] < value
        if op in (">=", "gte"):
            return self.table.c[column] >= value
        if op in ("<=", "lte"):
            return self.table.c[column] <= value
        if op in ("=", "==", "is"):
            return self.table.c[column] == value
        if op in ("!=", "<>", "not"):
            return self.table.c[column] != value
        if op in ("in",):
            if not isinstance(value, (list, tuple, set)):
                raise QueryError(f"'in' filter requires a list, got {type(value)}")
            return self.table.c[column].in_(value)
        if op in ("notin",):
            if not isinstance(value, (list, tuple, set)):
                raise QueryError(f"'notin' filter requires a list, got {type(value)}")
            return self.table.c[column].notin_(value)
        if op in ("between", ".."):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryError("'between' filter requires a list of two values")
            start, end = value
            return self.table.c[column].between(start, end)
        if op in ("startswith",):
            if not isinstance(value, str):
                raise QueryError("'startswith' filter requires a string")
            return self.table.c[column].startswith(value, autoescape=True)
        if op in ("endswith",):
            if not isinstance(value, str):
                raise QueryError("'endswith' filter requires a string")
            return self.table.c[column].endswith(value, autoescape=True)
        return false()

    def _args_to_clause(
        self,
        args: MutableRow,
        clauses: Iterable[ColumnElement[bool]] = (),
    ) -> ColumnElement[bool]:
        clauses = list(clauses)
        for column, value in args.items():
            column = self._get_column_name(column)
            if not self.has_column(column):
                clauses.append(false())
            elif isinstance(value, (list, tuple, set)):
                clauses.append(self._generate_clause(column, "in", value))
            elif isinstance(value, dict):
                for op, op_value in value.items():
                    clauses.append(self._generate_clause(column, op, op_value))
            else:
                clauses.append(self._generate_clause(column, "=", value))
        return and_(True, *clauses)

    def _args_to_order_by(
        self, order_by: str | Sequence[str] | None
    ) -> list[UnaryExpression[Any]]:
        orderings: list[UnaryExpression[Any]] = []
        for ordering in ensure_strings(order_by):
            if ordering is None:
                continue
            column = ordering.lstrip("-")
            column = self._get_column_name(column)
            if not self.has_column(column):
                continue
            if ordering.startswith("-"):
                orderings.append(self.table.c[column].desc())
            else:
                orderings.append(self.table.c[column].asc())
        return orderings

    def _keys_to_args(
        self, row: WriteRow, keys: Sequence[str]
    ) -> tuple[MutableRow, MutableRow]:
        keys = [self._get_column_name(k) for k in ensure_strings(keys)]
        row_ = dict(row)
        args = {k: row_.pop(k, None) for k in keys}
        return args, row_

    def create_column(
        self, name: str, type: ColumnType, **kwargs: object  # noqa: A002
    ) -> None:
        """Create a new column ``name`` of a specified type.
        ::

            table.create_column('created_at', db.types.datetime)

        `type` corresponds to an SQLAlchemy type as described by
        `dataset.db.Types`. Additional keyword arguments are passed
        to the constructor of `Column`, so that default values, and
        options like `nullable` and `unique` can be set.
        ::

            table.create_column('key', unique=True, nullable=False)
            table.create_column('food', default='banana')
        """
        name = self._get_column_name(name)
        if self.has_column(name):
            log.debug(f"Column exists: {name}")
            return
        self._sync_table((Column(name, type, **kwargs),))  # type: ignore[arg-type]
        self.db._auto_commit()

    def create_column_by_example(self, name: str, value: SQLWriteValue) -> None:
        """
        Explicitly create a new column ``name`` with a type that is appropriate
        to store the given example ``value``.  The type is guessed in the same
        way as for the insert method with ``ensure=True``.
        ::

            table.create_column_by_example('length', 4.2)

        If a column of the same name already exists, no action is taken, even
        if it is not of the type we would have created.
        """
        type_ = self.db.types.guess(value)
        self.create_column(name, type_)

    def drop_column(self, name: str) -> None:
        """
        Drop the column ``name``.
        ::

            table.drop_column('created_at')

        """
        if self.db.engine is None:
            raise RuntimeError("Cannot drop columns when no engine is available.")
        if self.db.engine.dialect.name == "sqlite":
            raise RuntimeError("SQLite does not support dropping columns.")
        name = self._get_column_name(name)
        with self.db.lock:
            if not self.exists or not self.has_column(name):
                log.debug("Column does not exist: %s", name)
                return

            self._threading_warn()
            self.db.op.drop_column(self.table.name, name, schema=self.table.schema)
            self._reflect_table()
            self.db._auto_commit()

    def drop(self) -> None:
        """Drop the table from the database.

        Deletes both the schema and all the contents within it.
        """
        with self.db.lock:
            if self.exists:
                self._threading_warn()
                self.table.drop(self.db.executable, checkfirst=True)
                self._table = None
                self._columns = None
                self.db._tables.pop(self.name, None)
                self.db._auto_commit()

    def has_index(self, columns: Iterable[str]) -> bool:
        """Check if an index exists to cover the given ``columns``."""
        if not self.exists:
            return False
        columns_ = {self._get_column_name(c) for c in ensure_strings(columns)}
        if columns_ in self._indexes:
            return True
        for column in columns_:
            if not self.has_column(column):
                return False
        indexes = self.db.inspect.get_indexes(self.name, schema=self.db.schema)
        for index in indexes:
            idx_columns = index.get("column_names", [])
            if len(columns_.intersection(idx_columns)) == len(columns_):
                self._indexes.append(columns_)
                return True
        if self.table.primary_key is not None:
            pk_columns = [c.name for c in self.table.primary_key.columns]
            if len(columns_.intersection(pk_columns)) == len(columns_):
                self._indexes.append(columns_)
                return True
        return False

    def create_index(
        self, columns: Sequence[str], name: str | None = None, **kw: object
    ) -> None:
        """Create an index to speed up queries on a table.

        If no ``name`` is given a random name is created.
        ::

            table.create_index(['name', 'country'])
        """
        columns = [self._get_column_name(c) for c in ensure_strings(columns)]
        with self.db.lock:
            if not self.exists:
                raise DatasetError("Table has not been created yet.")

            for column in columns:
                if not self.has_column(column):
                    return

            if not self.has_index(columns):
                self._threading_warn()
                name = name or index_name(self.name, columns)
                columns_ = [self.table.c[c] for c in columns]

                # MySQL crashes out if you try to index very long text fields,
                # apparently. This defines (a somewhat random) prefix that
                # will be captured by the index, after which I assume the engine
                # conducts a more linear scan:
                mysql_length = {}
                for col in columns_:
                    if isinstance(col.type, MYSQL_LENGTH_TYPES):
                        mysql_length[col.name] = 10
                kw["mysql_length"] = mysql_length

                idx = Index(name, *columns_, **kw)  # type: ignore[arg-type]
                idx.create(self.db.executable)
                self.db._auto_commit()

    def find(
        self,
        *_clauses: ColumnElement[bool],
        _limit: int | None = None,
        _offset: int = 0,
        order_by: str | Sequence[str] | None = None,
        _streamed: bool = False,
        _step: int | None = QUERY_STEP,
        **kwargs: SQLWriteValue,
    ) -> ResultIter:
        """Perform a simple search on the table.

        Simply pass keyword arguments as ``filter``.
        ::

            results = table.find(country='France')
            results = table.find(country='France', year=1980)

        Using ``_limit``::

            # just return the first 10 rows
            results = table.find(country='France', _limit=10)

        You can sort the results by single or multiple columns. Append a minus
        sign to the column name for descending order::

            # sort results by a column 'year'
            results = table.find(country='France', order_by='year')
            # return all rows sorted by multiple columns (descending by year)
            results = table.find(order_by=['country', '-year'])

        You can also submit filters based on criteria other than equality,
        see :ref:`advanced_filters` for details.

        To run more complex queries with JOINs, or to perform GROUP BY-style
        aggregation, you can also use :py:meth:`db.query() <dataset.Database.query>`
        to run raw SQL queries instead.
        """
        if not self.exists:
            return ResultIter(None, row_type=self.db.row_type)
        if self.db.engine is None:
            raise RuntimeError("Cannot run queries when no engine is available.")

        if _step is False or _step == 0:
            _step = None

        orderings = self._args_to_order_by(order_by)
        args = self._args_to_clause(kwargs, clauses=_clauses)
        query = self.table.select().where(args).limit(_limit).offset(_offset)
        if len(orderings):
            query = query.order_by(*orderings)

        stream_conn = None
        conn = self.db.executable
        if _streamed:
            stream_conn = self.db.engine.connect()
            conn = stream_conn.execution_options(stream_results=True)

        return ResultIter(
            conn.execute(query),
            row_type=self.db.row_type,
            step=_step,
            connection=stream_conn,
        )

    def find_one(
        self, *args: ColumnElement[bool], **kwargs: SQLWriteValue
    ) -> OutRow | None:
        """Get a single result from the table.

        Works just like :py:meth:`find() <dataset.Table.find>` but returns one
        result, or ``None``.
        ::

            row = table.find_one(country='United States')
        """
        if not self.exists:
            return None

        resiter = self.find(*args, _limit=1, _step=None, **kwargs)  # type: ignore[arg-type]
        try:
            for row in resiter:
                return row
        finally:
            resiter.close()
        return None

    def count(self, *_clauses: ColumnElement[bool], **kwargs: SQLWriteValue) -> int:
        """Return the count of results for the given filter set."""
        # NOTE: this does not have support for limit and offset since I can't
        # see how this is useful. Still, there might be compatibility issues
        # with people using these flags. Let's see how it goes.
        if not self.exists:
            return 0

        args = self._args_to_clause(kwargs, clauses=_clauses)
        query = select(func.count()).where(args)
        query = query.select_from(self.table)
        rp = self.db.executable.execute(query)
        res = rp.fetchone()
        if res is not None:
            return int(res[0])
        return 0

    def __len__(self) -> int:
        """Return the number of rows in the table."""
        return self.count()

    def distinct(
        self,
        *args: str | ColumnElement[bool],
        _limit: int | None = None,
        _offset: int | None = 0,
        **kwargs: SQLWriteValue,
    ) -> ResultIter:
        """Return all the unique (distinct) values for the given ``columns``.
        ::

            # returns only one row per year, ignoring the rest
            table.distinct('year')
            # works with multiple columns, too
            table.distinct('year', 'country')
            # you can also combine this with a filter
            table.distinct('year', country='China')
        """
        if not self.exists:
            return ResultIter(None, row_type=self.db.row_type)

        columns = []
        clauses = []
        for column in args:
            if isinstance(column, ClauseElement):
                clauses.append(column)
            else:
                if not self.has_column(column):
                    raise DatasetError(f"No such column: {column}")
                columns.append(self.table.c[column])

        clause = self._args_to_clause(kwargs, clauses=clauses)
        if not len(columns):
            return ResultIter(None, row_type=self.db.row_type)

        q = (
            expression.select(*columns)
            .distinct()
            .where(clause)
            .limit(_limit)
            .offset(_offset)
            .order_by(*[c.asc() for c in columns])
        )
        return self.db.query(q)

    # Legacy methods for running find queries.
    all = find

    def __iter__(self) -> ResultIter:
        """Return all rows of the table as simple dictionaries.

        Allows for iterating over all rows in the table without explicitly
        calling :py:meth:`find() <dataset.Table.find>`.
        ::

            for row in table:
                print(row)
        """
        return self.find()

    def __repr__(self) -> str:
        """Get table representation."""
        return f"<Table({self.table.name})>"
