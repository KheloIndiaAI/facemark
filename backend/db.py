"""PostgreSQL connection layer.

WHY A SHIM RATHER THAN A REWRITE
--------------------------------
The application's SQL was written against SQLite and uses `?` placeholders in
77 places. Hand-converting every one of them to psycopg's `%s` would be 77
chances to introduce a typo that only shows up at runtime, on a query some
screen reaches once a week. `translate()` does it mechanically instead, so the
SQL in database.py / auth.py / centres.py / routes.py stays exactly as written
and keeps reading like the rest of the project.

Two SQLite behaviours the rest of the code depends on are reproduced here:

  Row      sqlite3.Row could be indexed by NAME or POSITION, and the codebase
           uses both - `r["name"]` everywhere, `.fetchone()[0]` in the
           aggregate counts, `r[0]`/`r[1]` in analytics(). A plain dict row
           would break the positional half silently, returning key strings
           instead of values.

  insert() sqlite3 exposed `cur.lastrowid`. Postgres has no equivalent, so the
           eight insert-and-return-id call sites go through this, which appends
           RETURNING id.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

import psycopg
from psycopg_pool import ConnectionPool

from . import config

log = logging.getLogger("db")

# Re-exported so callers can catch a duplicate roll_no / username without
# importing psycopg themselves.
IntegrityError = psycopg.IntegrityError
UniqueViolation = psycopg.errors.UniqueViolation


class Row(dict):
    """A result row addressable by column name *or* column position.

    Subclassing dict (rather than wrapping a tuple) means `dict(row)` and
    `**row` keep working untouched, which is what nearly every caller does.

    ONE DIFFERENCE FROM sqlite3.Row: iteration yields KEYS, because that is
    what a dict does. So `a, b = row` binds column *names*, not values, where
    sqlite3.Row would have given values. Index instead - `row[0], row[1]` - or
    name the columns. Nothing in this codebase unpacks a row, and dict
    iteration is the least surprising behaviour for something that is a dict
    everywhere else, so the semantics are kept rather than special-cased.
    """

    __slots__ = ("_values",)

    def __init__(self, pairs):
        pairs = list(pairs)
        super().__init__(pairs)
        # Values are kept positionally as well as by name, because column
        # names are NOT unique. analytics() selects two anonymous COUNT(*)
        # subqueries; Postgres names both of them "count", so a dict keyed on
        # name silently collapses four columns into three and every positional
        # read after the duplicate is off by one. Deriving position from
        # .values() would inherit exactly that bug.
        self._values = tuple(v for _, v in pairs)

    def __getitem__(self, key):
        if isinstance(key, int):
            try:
                return self._values[key]
            except IndexError:
                raise IndexError(
                    f"row has {len(self._values)} column(s), asked for index {key}"
                ) from None
        return super().__getitem__(key)


def _row_factory(cursor):
    """psycopg row factory producing Row objects."""
    desc = cursor.description
    if desc is None:                       # INSERT/UPDATE/DDL: no result columns
        return lambda values: values
    cols = [c.name for c in desc]
    return lambda values: Row(zip(cols, values))


def translate(sql: str) -> str:
    """Rewrite SQLite `?` placeholders as psycopg `%s`.

    Two details matter:

    - `?` inside a quoted literal is left alone. No query currently does that,
      but a future one would otherwise be corrupted silently.
    - Every literal `%` is doubled. psycopg scans the whole query for
      placeholders whenever parameters are supplied, so an un-doubled `%`
      raises. The LIKE queries in centres.py are safe today only because their
      wildcards travel in the *parameters* ("%delhi%"), never in the SQL text -
      this keeps them safe if that ever changes.
    """
    out: list[str] = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_string:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class Conn:
    """Thin wrapper over a pooled psycopg connection.

    Exposes the small slice of the sqlite3.Connection API this codebase used,
    so `with database.connect() as conn: conn.execute(...)` reads identically.
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: psycopg.Connection):
        self._raw = raw

    def execute(self, sql: str, params: Optional[Sequence[Any]] = ()):
        # No parameters means no placeholder scanning, so the SQL can go
        # through verbatim - and must, or a literal % in a DDL default would
        # be doubled into the schema.
        if not params:
            return self._raw.execute(sql)
        return self._raw.execute(translate(sql), tuple(params))

    def insert(self, sql: str, params: Optional[Sequence[Any]] = ()) -> int:
        """INSERT and return the generated id (replaces sqlite3's lastrowid)."""
        cur = self.execute(sql.rstrip().rstrip(";") + " RETURNING id", params)
        row = cur.fetchone()
        return int(row[0])

    def executescript(self, sql: str) -> None:
        """Run several statements separated by semicolons, as sqlite3 did."""
        self._raw.execute(sql)

    @property
    def raw(self) -> psycopg.Connection:
        return self._raw


_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    """The process-wide connection pool, opened on first use.

    FastAPI runs sync endpoints in a thread pool, so several requests hold
    connections at once. Opening a fresh Postgres connection costs tens of
    milliseconds - trivial beside 0.6 s of face recognition, but not beside a
    dashboard poll that issues eight small counts.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not config.DATABASE_URL:
                    raise RuntimeError(
                        "DATABASE_URL is not set. FaceMark needs a PostgreSQL "
                        "connection string, for example:\n"
                        "  postgresql://user:password@host:5432/facemark"
                    )
                _pool = ConnectionPool(
                    conninfo=config.DATABASE_URL,
                    min_size=config.DB_POOL_MIN,
                    max_size=config.DB_POOL_MAX,
                    kwargs={"row_factory": _row_factory},
                    open=True,
                )
                log.info(
                    "Postgres pool open (min=%d max=%d)",
                    config.DB_POOL_MIN, config.DB_POOL_MAX,
                )
    return _pool


# Key for the lock held across the whole first-boot sequence. Any constant
# works; it only has to be the same in every process.
STARTUP_LOCK_KEY = 2749170102


@contextmanager
def advisory_lock(key: int) -> Iterator[None]:
    """Hold a session-scoped advisory lock across several transactions.

    `pg_advisory_xact_lock` releases at the end of ITS transaction, which is no
    use when the work spans a sequence of independent `connect()` blocks. This
    takes a connection of its own and holds a session-level lock for the whole
    `with` body.

    Needed because the startup sequence is full of check-then-act pairs -
    "count the users, and create admin if there are none" - each in its own
    transaction. Two uvicorn workers both read zero and both write, and the
    loser dies on a unique constraint, taking the parent process with it.

    The unlock is explicit and in a finally: the connection goes back to the
    pool afterwards, and a session lock left behind would be held by whichever
    request borrowed it next.
    """
    with pool().connection() as raw:
        raw.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield
        finally:
            raw.execute("SELECT pg_advisory_unlock(%s)", (key,))


@contextmanager
def connect() -> Iterator[Conn]:
    """Borrow a connection; commit on clean exit, roll back on exception.

    This is the same contract sqlite3's connection context manager gave, which
    is why every `with database.connect() as conn:` block in the codebase is
    unchanged.
    """
    with pool().connection() as raw:
        yield Conn(raw)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ping() -> bool:
    """True if the database answers. Used by /api/health."""
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as e:  # noqa: BLE001 - health must report, not raise
        log.warning("Database ping failed: %s", e)
        return False
