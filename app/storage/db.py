"""SQLite engine and session management.

Will create the SQLite engine/session factory (path from config), enable
WAL mode for better concurrent read/write behavior, and create tables on
startup.

Phase 2 implements this; a SQLite/ORM layer will be added as a dependency then.
"""

# Phase 2: implement engine/session setup and WAL pragma.
