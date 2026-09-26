"""Copy an existing SQLite FaceMark database into PostgreSQL.

    python -m scripts.migrate_to_postgres --dry-run     # report, change nothing
    python -m scripts.migrate_to_postgres               # copy the rows
    python -m scripts.migrate_to_postgres --photos      # copy the photo files too

Reads DATABASE_URL for the destination and config.LEGACY_SQLITE_PATH for the
source. The SQLite file is only ever read; nothing here writes to it.

WHAT IT DOES NOT COPY
---------------------
`auth_sessions` is skipped deliberately. Sessions are opaque tokens with a 12
hour TTL, so carrying them over buys at most half a day of not signing in, and
costs a table of live credentials sitting in two databases at once. Everyone
signs in again after the cutover.

THE SEQUENCE RESET IS NOT OPTIONAL
----------------------------------
Rows are inserted with their original ids so that every foreign key - a
template's student_id, an attendance row's centre_id - still points at the same
person. But a SERIAL column's sequence does not advance when an id is supplied
explicitly, so it would still be sitting at 1 afterwards and the next enrolment
would collide with student #1. _reset_sequences() moves each sequence past the
highest id actually present. Skipping it produces a database that imports
cleanly and then fails on first write.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow `python scripts/migrate_to_postgres.py` as well as `-m scripts...`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database, storage  # noqa: E402
from backend import db as pgdb  # noqa: E402

# Parents before children, so a foreign key never points at a row that has not
# been inserted yet.
TABLES = ["centres", "students", "users", "templates", "attendance", "photos"]

# (table, column) pairs holding a photo key, used by --photos.
PHOTO_COLUMNS = [
    ("students", "photo_path", "students"),
    ("photos", "file_path", None),        # prefix inferred from photo_type
]


def _sqlite_columns(sq: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in sq.execute(f"PRAGMA table_info({table})").fetchall()]


def _sqlite_has_table(sq: sqlite3.Connection, table: str) -> bool:
    return sq.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _copy_table(sq: sqlite3.Connection, conn, table: str, dry: bool) -> int:
    if not _sqlite_has_table(sq, table):
        print(f"  {table:<12} not present in source, skipped")
        return 0

    src = _sqlite_columns(sq, table)
    dst = {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
    }
    # Intersect rather than assume: the two schemas can differ by a column that
    # _ensure_columns added on one side only.
    cols = [c for c in src if c in dst]
    dropped = [c for c in src if c not in dst]
    if dropped:
        print(f"  {table:<12} ignoring source-only column(s): {', '.join(dropped)}")
    if not cols:
        print(f"  {table:<12} no columns in common, skipped")
        return 0

    rows = sq.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if dry:
        print(f"  {table:<12} would copy {len(rows)} row(s)")
        return len(rows)

    placeholders = ",".join("?" * len(cols))
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )
    n = 0
    for row in rows:
        values = []
        for c, v in zip(cols, row):
            # SQLite hands BLOBs back as bytes already; psycopg maps bytes to
            # BYTEA. memoryview would also work, but bytes keeps it explicit.
            values.append(bytes(v) if isinstance(v, memoryview) else v)
        conn.execute(sql, values)
        n += 1
    print(f"  {table:<12} copied {n} row(s)")
    return n


def _reset_sequences(conn, dry: bool) -> None:
    """Advance each SERIAL sequence past the ids that were inserted explicitly."""
    for table in TABLES:
        if dry:
            print(f"  {table:<12} would reset id sequence")
            continue
        # setval(..., false) on COALESCE(max,0)+1 is correct for an empty table
        # too: it leaves the sequence about to hand out 1.
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
        )
        print(f"  {table:<12} id sequence reset")


def _migrate_photos(sq: sqlite3.Connection, dry: bool) -> None:
    """Push photo files into the configured storage backend.

    Only useful when FACEMARK_STORAGE=s3; under local storage the files are
    already where they belong and this is a no-op that reports what it found.
    """
    backend = storage.backend_name()
    print(f"\nPhotos -> {backend} storage")
    if backend == "local":
        print("  storage is local; files already in place, nothing to upload")

    seen: set = set()
    copied = missing = 0
    for prefix, local_dir in (("students", config.STUDENTS_DIR),
                              ("uploads", config.UPLOADS_DIR)):
        if not local_dir.exists():
            print(f"  {prefix:<9} no local directory at {local_dir}")
            continue
        for f in sorted(local_dir.glob("*.jpg")):
            key = (prefix, f.name)
            if key in seen:
                continue
            seen.add(key)
            if dry:
                copied += 1
                continue
            try:
                storage.put(prefix, f.name, f.read_bytes())
                copied += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ! {prefix}/{f.name}: {e}")
                missing += 1
        print(f"  {prefix:<9} {'would upload' if dry else 'uploaded'} "
              f"{copied} file(s) so far")
    if missing:
        print(f"  {missing} file(s) failed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen and change nothing")
    ap.add_argument("--photos", action="store_true",
                    help="also copy photo files into the storage backend")
    ap.add_argument("--sqlite", type=Path, default=config.LEGACY_SQLITE_PATH,
                    help="source SQLite file")
    args = ap.parse_args()

    if not args.sqlite.exists():
        print(f"No SQLite database at {args.sqlite}")
        print("Nothing to migrate - a fresh Postgres database needs no import.")
        return 1
    if not config.DATABASE_URL:
        print("DATABASE_URL is not set. Point it at the destination Postgres:")
        print("  postgresql://user:password@host:5432/facemark")
        return 1

    print(f"Source      {args.sqlite}")
    print(f"Destination {config.DATABASE_URL.split('@')[-1]}")  # no credentials
    print(f"Mode        {'DRY RUN - nothing will be written' if args.dry_run else 'live'}\n")

    sq = sqlite3.connect(args.sqlite)
    sq.row_factory = sqlite3.Row

    # Create the schema first; init_db is idempotent, so running this against a
    # database that already has tables is safe.
    if not args.dry_run:
        database.init_db()
        print("Schema ready\n")

    total = 0
    print("Rows")
    with pgdb.connect() as conn:
        for t in TABLES:
            total += _copy_table(sq, conn, t, args.dry_run)
        print("\nSequences")
        _reset_sequences(conn, args.dry_run)

    if args.photos:
        _migrate_photos(sq, args.dry_run)

    sq.close()
    print(f"\n{'Would copy' if args.dry_run else 'Copied'} {total} row(s) in total.")
    if not args.dry_run:
        print("Sign-in sessions were not carried over; everyone signs in again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
