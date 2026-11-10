
API documentation
=================

Connecting
----------

.. autofunction:: dataset.connect

Database
--------

.. autoclass:: dataset.Database
   :members: tables, views, has_table, get_table, create_table, load_table, query, begin, commit, rollback, close
   :special-members:


Table
-----

.. autoclass:: dataset.Table
   :members: exists, columns, find, find_one, all, count, distinct, insert, insert_ignore, insert_many, update, update_many, upsert, upsert_many, delete, create_column, create_column_by_example, drop_column, create_index, drop, rename, has_column, has_index, enable_fts, disable_fts, rebuild_fts, plan_fts_rebuild, has_fts, fts_config, search
   :special-members: __len__, __iter__


Full-text search (SQLite FTS5)
------------------------------

SQLite databases can be searched through an optional FTS5 external-content
index. No separate search service is required: the index is stored in the
same database and kept in sync by triggers, inside the same transaction as
the underlying writes.

.. autoclass:: dataset.FTSConfig
   :members:

.. autoclass:: dataset.FTSRebuildPlan
   :members:

.. autoexception:: dataset.FTSCapabilityError

.. autoexception:: dataset.FTSConfigError


Data Export
-----------

  **Note:** Data exporting has been extracted into a stand-alone package, datafreeze. See the relevant repository here_.

.. _here: https://github.com/pudo/datafreeze
