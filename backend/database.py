"""SQLite persistence: students, multi-source face templates and attendance.

A student owns MULTIPLE templates - the registration photo, one per pose from
guided multi-view capture, and any confirmed from daily group photos - tagged by
`source` and ranked by `quality`. Matching max-pools over them, so an old
registration photo and the athlete's current appearance are both represented.
Learning ADDS templates rather than overwriting, so nothing is lost.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import config
from .db import Conn, IntegrityError, Row, connect  # noqa: F401 - re-exported

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    roll_no    TEXT NOT NULL UNIQUE,
    photo_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id         SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    vector     BYTEA NOT NULL,
    source     TEXT NOT NULL DEFAULT 'enrollment',
    quality    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_student ON templates(student_id);
CREATE INDEX IF NOT EXISTS idx_templates_model ON templates(model, student_id);
CREATE TABLE IF NOT EXISTS attendance (
    id         SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    image_path TEXT,
    marked_at  TEXT NOT NULL,
    UNIQUE(student_id, date)
);
CREATE TABLE IF NOT EXISTS photos (
    id           SERIAL PRIMARY KEY,
    student_id   INTEGER REFERENCES students(id) ON DELETE SET NULL,
    photo_type   TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    file_size    INTEGER,
    resolution   TEXT,
    source       TEXT NOT NULL DEFAULT 'upload',
    device_info  TEXT,
    faces_detected INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_student ON photos(student_id);
CREATE INDEX IF NOT EXISTS idx_photos_type ON photos(photo_type);

-- Khelo India centres. `is_demo` marks placeholder rows seeded for evaluation so
-- they can never be mistaken for real government records (and can be bulk-deleted).
CREATE TABLE IF NOT EXISTS centres (
    id            SERIAL PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    centre_type   TEXT NOT NULL DEFAULT 'KIC',
    state         TEXT,
    district      TEXT,
    address       TEXT,
    pincode       TEXT,
    sports        TEXT,                       -- JSON array of disciplines
    capacity      INTEGER DEFAULT 0,
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    geofence_m    INTEGER DEFAULT 300,        -- attendance radius in metres
    incharge_name TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    established   TEXT,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_centres_state ON centres(state, district);

-- Coaches and super admins. Passwords are PBKDF2-HMAC-SHA256, never plaintext.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('super_admin','coach')),
    full_name     TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    centre_id     INTEGER REFERENCES centres(id) ON DELETE SET NULL,
    student_id    INTEGER REFERENCES students(id) ON DELETE SET NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login    TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_centre ON users(centre_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(user_id);

"""

# Sources: id (raw ID-card crop), restored (GFPGAN), live (recent photo),
# adapted (learned from daily attendance), legacy (pre-upgrade embedding).
TEMPLATE_SOURCES = ("id", "restored", "live", "adapted", "legacy")


# Arbitrary but fixed application-wide key for the schema-setup advisory lock.
# Any constant works; it only has to be the same in every process.
_SCHEMA_LOCK_KEY = 2749170101


def init_db() -> None:
    with connect() as conn:
        # Serialise schema setup across processes.
        #
        # CREATE TABLE IF NOT EXISTS is NOT concurrency-safe in PostgreSQL: two
        # sessions can both observe a table as absent, both attempt to create
        # it, and the loser fails with UniqueViolation on pg_type's unique index
        # rather than quietly becoming a no-op. `uvicorn --workers N` starts N
        # processes that all reach this line within milliseconds of each other,
        # so against an empty database the container used to die on boot -
        # uvicorn kills the parent when any child fails to start.
        #
        # pg_advisory_xact_lock blocks rather than erroring, and releases when
        # this transaction commits, so the losing worker simply waits and then
        # finds every table already present. The same applies to the ALTER and
        # DROP statements below, which have the same race.
        conn.execute("SELECT pg_advisory_xact_lock(?)", (_SCHEMA_LOCK_KEY,))
        conn.executescript(SCHEMA)
        _migrate_legacy_embeddings(conn)
        _ensure_columns(conn, "students", {
            "role": "TEXT NOT NULL DEFAULT 'athlete'",   # 'athlete' | 'coach'
            "centre_id": "INTEGER REFERENCES centres(id) ON DELETE SET NULL",
            "gender": "TEXT",
            "sport": "TEXT",
            "phone": "TEXT",
        })
        _ensure_columns(conn, "users", {
            # Login throttling state. On the users row rather than in a new
            # table because it is one-to-one with an account and needs to be
            # read on the same query that fetches the password hash.
            "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
            "locked_until": "TEXT",
        })
        _drop_removed_tables(conn)
        _ensure_columns(conn, "attendance", {
            "centre_id": "INTEGER REFERENCES centres(id) ON DELETE SET NULL",
            "latitude": "DOUBLE PRECISION",
            "longitude": "DOUBLE PRECISION",
            "accuracy_m": "DOUBLE PRECISION",
            "geo_status": "TEXT",              # inside | outside | unknown | no_fix
            "distance_m": "DOUBLE PRECISION",
            "marked_by": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
        })
        # Runs last: dropping before _ensure_columns would let it re-add them.
        _drop_age_columns(conn)


def _drop_removed_tables(conn: Conn) -> None:
    """Drop tables left behind by the removed performance-tracking feature."""
    for t in ("performance", "metrics"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")


def _table_columns(conn: Conn, table: str) -> set:
    """Column names of `table` - Postgres's answer to PRAGMA table_info."""
    return {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
    }


def _ensure_columns(conn: Conn, table: str, columns: Dict[str, str]) -> None:
    """Add any missing columns to `table` (idempotent, runs on every startup)."""
    have = _table_columns(conn, table)
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_legacy_embeddings(conn: Conn) -> None:
    """One-time migration: old `embeddings` rows become source='legacy' templates.

    This can only fire on a database carried over from the SQLite era by
    scripts/migrate_to_postgres.py - a freshly created one has no such table.
    """
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'embeddings'"
    ).fetchone()
    if not exists:
        return
    # The timestamp comes from Python rather than now() so it matches the ISO
    # strings every other row carries; the columns are TEXT, and a Postgres
    # timestamp would render differently and break date comparisons.
    n = conn.execute(
        "INSERT INTO templates (student_id, model, vector, source, quality, created_at) "
        "SELECT student_id, model, vector, 'legacy', 0.0, ? FROM embeddings",
        (datetime.now().isoformat(timespec="seconds"),),
    ).rowcount
    conn.execute("DROP TABLE embeddings")
    if n:
        print(f"[db] migrated {n} legacy embedding(s) to multi-template schema")


def _drop_age_columns(conn: Conn) -> None:
    """Remove the age columns left by the withdrawn age feature.

    `est_age` held the genderage model's guess, which proved unreliable on the
    school-age athletes this system serves; `dob` was only read by that same
    feature. Both are dropped so nothing downstream can resurface a wrong age.
    """
    cols = _table_columns(conn, "students")
    for col in ("est_age", "dob"):
        if col in cols:
            conn.execute(f"ALTER TABLE students DROP COLUMN {col}")


# --- students ---------------------------------------------------------------

def add_student(
    name: str,
    roll_no: str,
    photo_path: str,
    templates: List[dict],
    role: str = "athlete",
    centre_id: Optional[int] = None,
    gender: Optional[str] = None,
    sport: Optional[str] = None,
    phone: Optional[str] = None,
) -> int:
    """Insert a student plus their template rows.

    templates: [{"model": str, "vector": np.ndarray, "source": str,
                 "quality": float}, ...] - one row per (model, source image).
    """
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        student_id = conn.insert(
            "INSERT INTO students (name, roll_no, photo_path, created_at, "
            "role, centre_id, gender, sport, phone) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, roll_no, photo_path, now, role, centre_id, gender, sport, phone),
        )
        _insert_templates(conn, student_id, templates, now)
        return student_id


def add_templates(student_id: int, templates: List[dict]) -> int:
    """Attach more templates (extra photo, another ID) to an existing student."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        return _insert_templates(conn, student_id, templates, now)


def _insert_templates(conn: Conn, student_id: int, templates: List[dict], now: str) -> int:
    rows = 0
    for t in templates:
        source = t.get("source", "enrollment")
        if source not in TEMPLATE_SOURCES:
            source = "live"
        conn.execute(
            "INSERT INTO templates (student_id, model, vector, source, quality, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                student_id,
                t["model"],
                np.asarray(t["vector"], dtype=np.float32).tobytes(),
                source,
                float(t.get("quality", 0.0)),
                now,
            ),
        )
        rows += 1
    return rows


def delete_student(student_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM students WHERE id = ?", (student_id,))


def list_students(centre_id: Optional[int] = None, role: Optional[str] = None) -> List[dict]:
    with connect() as conn:
        # Correlated subqueries, NOT parallel LEFT JOINs: joining attendance and
        # templates in one query multiplies the two row sets together, so a student
        # with 2 attendance rows and 12 templates reports 24 of each.
        rows = conn.execute(
            "SELECT s.id, s.name, s.roll_no, s.photo_path, s.created_at, "
            "(SELECT COUNT(*) FROM attendance a WHERE a.student_id = s.id) AS total_present, "
            "(SELECT COUNT(*) FROM templates t WHERE t.student_id = s.id) AS templates, "
            "(SELECT COUNT(*) FROM templates t WHERE t.student_id = s.id AND t.source = 'adapted') AS adapted, "
            "s.role, s.centre_id, s.gender, s.sport, s.phone "
            "FROM students s WHERE 1=1"
            + (" AND s.centre_id = ?" if centre_id is not None else "")
            + (" AND s.role = ?" if role else "")
            + " ORDER BY s.created_at DESC",
            [x for x in (centre_id, role) if x is not None],
        ).fetchall()
        return [dict(r) for r in rows]


def get_student(student_id: int) -> Optional[dict]:
    with connect() as conn:
        # The centre name comes along because recognition is global: callers
        # need to say which centre a recognised person belongs to, not just
        # that they were found.
        row = conn.execute(
            "SELECT s.*, c.name AS centre_name, c.code AS centre_code "
            "FROM students s LEFT JOIN centres c ON c.id = s.centre_id "
            "WHERE s.id = ?", (student_id,)).fetchone()
        return dict(row) if row else None


def get_student_by_roll(roll_no: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM students WHERE roll_no = ?", (roll_no.strip(),)).fetchone()
        return dict(row) if row else None


def template_stats(student_id: int) -> Dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(DISTINCT model) AS n FROM templates "
            "WHERE student_id = ? GROUP BY source",
            (student_id,),
        ).fetchall()
        return {r["source"]: r["n"] for r in rows}


# --- gallery ----------------------------------------------------------------

def load_gallery(centre_id: Optional[int] = None) -> Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Templates grouped by model for matching.

    Returns {model_name: (template_ids (T,), student_ids (T,), matrix (T,512))}.

    Passing `centre_id` narrows the gallery to that centre's athletes AND
    coaches. Beyond access control this helps accuracy: a smaller candidate
    pool means fewer chances for a look-alike from another centre to outscore
    the right person.
    """
    q = "SELECT t.id, t.student_id, t.model, t.vector FROM templates t"
    p: list = []
    if centre_id is not None:
        q += " JOIN students s ON s.id = t.student_id WHERE s.centre_id = ?"
        p.append(centre_id)
    with connect() as conn:
        rows = conn.execute(q, p).fetchall()
    gallery: Dict[str, list] = {}
    for r in rows:
        gallery.setdefault(r["model"], []).append(
            (int(r["id"]), int(r["student_id"]), np.frombuffer(r["vector"], dtype=np.float32))
        )
    return {
        model: (
            np.array([tid for tid, _, _ in items]),
            np.array([sid for _, sid, _ in items]),
            np.stack([v for _, _, v in items]),
        )
        for model, items in gallery.items()
    }


def load_gallery_with_quality() -> Tuple[Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]], Dict[str, np.ndarray]]:
    """Like load_gallery() but also returns {model: quality_scores_array}."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, student_id, model, vector, quality FROM templates"
        ).fetchall()
    gallery: Dict[str, list] = {}
    for r in rows:
        gallery.setdefault(r["model"], []).append(
            (int(r["id"]), int(r["student_id"]), np.frombuffer(r["vector"], dtype=np.float32), float(r["quality"]))
        )
    gallery_dict = {
        model: (
            np.array([tid for tid, _, _, _ in items]),
            np.array([sid for _, sid, _, _ in items]),
            np.stack([v for _, _, v, _ in items]),
        )
        for model, items in gallery.items()
    }
    quality_dict = {
        model: np.array([q for _, _, _, q in items])
        for model, items in gallery.items()
    }
    return gallery_dict, quality_dict


def add_adapted_template(
    student_id: int,
    new_embeddings: Dict[str, np.ndarray],
    quality: float = 0.0,
) -> bool:
    """Continual learning: store today's high-confidence observation as a NEW
    'adapted' template (the original ID anchor is never touched).

    Skips insertion when the observation is nearly identical to an existing
    adapted template, and evicts the lowest-quality adapted template when the
    per-(student, model) cap is reached.
    """
    stored = False
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        for model_name, new_vec in new_embeddings.items():
            new_vec = np.asarray(new_vec, dtype=np.float32).flatten()
            rows = conn.execute(
                "SELECT id, vector, quality FROM templates "
                "WHERE student_id = ? AND model = ? AND source = 'adapted'",
                (student_id, model_name),
            ).fetchall()
            if rows:
                mats = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
                if float((mats @ new_vec).max()) >= config.CONTINUAL_SIMILARITY_SKIP:
                    continue  # too similar to add information
            if len(rows) >= config.CONTINUAL_MAX_TEMPLATES:
                worst = min(rows, key=lambda r: r["quality"])
                conn.execute("DELETE FROM templates WHERE id = ?", (worst["id"],))
            conn.execute(
                "INSERT INTO templates (student_id, model, vector, source, quality, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (student_id, model_name, new_vec.tobytes(), "adapted", quality, now),
            )
            stored = True
    return stored


# --- attendance -------------------------------------------------------------

def mark_attendance(
    student_id: int,
    day: str,
    confidence: float,
    image_path: str,
    centre_id: Optional[int] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    geo_status: Optional[str] = None,
    distance_m: Optional[float] = None,
    marked_by: Optional[int] = None,
) -> bool:
    """Insert a record; returns False if already marked for that day.

    Geo fields describe where the capture happened, so a coach can later show
    that attendance was taken at the centre rather than anywhere convenient.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO attendance (student_id, date, confidence, image_path, "
            "marked_at, centre_id, latitude, longitude, accuracy_m, geo_status, distance_m, marked_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (student_id, date) DO NOTHING",
            (
                student_id, day, confidence, image_path,
                datetime.now().isoformat(timespec="seconds"),
                centre_id, latitude, longitude, accuracy_m, geo_status, distance_m, marked_by,
            ),
        )
        return cur.rowcount > 0


def attendance_for_day(day: str, centre_id: Optional[int] = None) -> List[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.student_id, a.date, a.confidence, a.image_path, a.marked_at, "
            "a.latitude, a.longitude, a.accuracy_m, a.geo_status, a.distance_m, a.centre_id, "
            "s.name, s.roll_no, s.photo_path, s.role, s.sport, c.name AS centre_name "
            "FROM attendance a JOIN students s ON s.id = a.student_id "
            "LEFT JOIN centres c ON c.id = a.centre_id "
            "WHERE a.date = ?" + (" AND a.centre_id = ?" if centre_id is not None else "") +
            " ORDER BY a.marked_at DESC",
            (day,) if centre_id is None else (day, centre_id),
        ).fetchall()
        return [dict(r) for r in rows]


def student_attendance_history(student_id: int) -> List[dict]:
    """Full attendance history for one student, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.date, a.confidence, a.marked_at "
            "FROM attendance a WHERE a.student_id = ? ORDER BY a.date DESC",
            (student_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_attendance() -> int:
    """Delete all attendance rows (used by the evaluation harness)."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM attendance")
        return cur.rowcount


def stats(centre_id: Optional[int] = None) -> dict:
    today = date.today().strftime(config.ATTENDANCE_DATE_FORMAT)
    cs = " AND centre_id = ?" if centre_id is not None else ""
    cp = [centre_id] if centre_id is not None else []
    with connect() as conn:
        n_students = conn.execute(
            "SELECT COUNT(*) FROM students WHERE role = 'athlete'" + cs, cp
        ).fetchone()[0]
        n_coaches = conn.execute(
            "SELECT COUNT(*) FROM students WHERE role = 'coach'" + cs, cp
        ).fetchone()[0]
        # A student with no templates can never be matched, so counting them in the
        # denominator makes a fully-present class look half-absent forever.
        # Every count here is athlete-only so the four dashboard tiles agree with
        # each other. Mixing coaches into one tile and not the others produced
        # "11 enrolled, 13 absent".
        n_enrolled = conn.execute(
            "SELECT COUNT(*) FROM students s WHERE s.role = 'athlete' "
            "AND EXISTS (SELECT 1 FROM templates t WHERE t.student_id = s.id)"
            + (" AND s.centre_id = ?" if centre_id is not None else ""), cp
        ).fetchone()[0]
        acs = " AND a.centre_id = ?" if centre_id is not None else ""
        present_today = conn.execute(
            "SELECT COUNT(*) FROM attendance a JOIN students s ON s.id = a.student_id "
            "WHERE a.date = ? AND s.role = 'athlete'" + acs, [today] + cp
        ).fetchone()[0]
        coaches_present_today = conn.execute(
            "SELECT COUNT(*) FROM attendance a JOIN students s ON s.id = a.student_id "
            "WHERE a.date = ? AND s.role = 'coach'" + acs, [today] + cp
        ).fetchone()[0]
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM attendance a WHERE 1=1" + acs, cp
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT a.date, a.marked_at, a.confidence, s.name, s.roll_no "
            "FROM attendance a JOIN students s ON s.id = a.student_id "
            "WHERE 1=1" + (" AND a.centre_id = ?" if centre_id is not None else "") +
            " ORDER BY a.marked_at DESC LIMIT 8", cp
        ).fetchall()
    return {
        "students": n_students,
        "coaches": n_coaches,
        "enrolled": n_enrolled,
        "unenrolled": max(n_students - n_enrolled, 0),
        "present_today": present_today,
        "coaches_present_today": coaches_present_today,
        "absent_today": max(n_enrolled - present_today, 0),
        "attendance_rate": round(100.0 * present_today / n_enrolled, 1) if n_enrolled else 0.0,
        "total_records": total_rows,
        "today": today,
        "recent": [_recent_row(r) for r in recent],
    }


def _recent_row(row: Row) -> dict:
    """One Recent Activity entry, with the HH:MM the dashboard renders."""
    d = dict(row)
    marked = d.get("marked_at") or ""
    d["time"] = marked.split("T")[-1][:5] if "T" in marked else marked
    return d


# --- photos -----------------------------------------------------------------

def save_photo_record(
    file_path: str,
    photo_type: str,
    source: str = 'upload',
    student_id: Optional[int] = None,
    file_size: Optional[int] = None,
    resolution: Optional[str] = None,
    device_info: Optional[str] = None,
    faces_detected: int = 0,
) -> int:
    """Log a photo upload/capture with metadata. Returns photo record id."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        return conn.insert(
            "INSERT INTO photos (student_id, photo_type, file_path, file_size, resolution, source, device_info, faces_detected, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (student_id, photo_type, file_path, file_size, resolution, source, device_info, faces_detected, now),
        )


def get_photos(
    student_id: Optional[int] = None,
    photo_type: Optional[str] = None,
    limit: int = 50,
) -> List[dict]:
    """Query photo history with optional filters."""
    query = "SELECT * FROM photos WHERE 1=1"
    params = []
    if student_id is not None:
        query += " AND student_id = ?"
        params.append(student_id)
    if photo_type is not None:
        query += " AND photo_type = ?"
        params.append(photo_type)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def photo_stats() -> dict:
    """Aggregate photo counts by type and source."""
    with connect() as conn:
        types = conn.execute("SELECT photo_type, COUNT(*) as c FROM photos GROUP BY photo_type").fetchall()
        sources = conn.execute("SELECT source, COUNT(*) as c FROM photos GROUP BY source").fetchall()
        
    return {
        "by_type": {r["photo_type"]: r["c"] for r in types},
        "by_source": {r["source"]: r["c"] for r in sources},
    }


def analytics(centre_id: Optional[int] = None, days: int = 30) -> dict:
    """Aggregates for the analytics page.

    Everything is athlete-only and, when a centre is given, scoped to it - the
    same rule the dashboard tiles follow, so the two screens cannot disagree.
    Coaches are excluded from attendance rates because a coach marked present
    is not an athlete attending a session.
    """
    cs = " AND s.centre_id = ?" if centre_id is not None else ""
    acs = " AND a.centre_id = ?" if centre_id is not None else ""
    cp = [centre_id] if centre_id is not None else []

    with connect() as conn:
        # The denominator: athletes who could actually be matched. Someone with
        # no template can never be recognised, so counting them makes a full
        # session look half-empty.
        enrolled = conn.execute(
            "SELECT COUNT(*) FROM students s WHERE s.role = 'athlete' "
            "AND EXISTS (SELECT 1 FROM templates t WHERE t.student_id = s.id)" + cs, cp
        ).fetchone()[0]

        trend = [
            {"date": r[0], "present": r[1]}
            for r in conn.execute(
                "SELECT a.date, COUNT(DISTINCT a.student_id) "
                "FROM attendance a JOIN students s ON s.id = a.student_id "
                "WHERE s.role = 'athlete'" + acs +
                " GROUP BY a.date ORDER BY a.date DESC LIMIT ?", cp + [days]
            )
        ][::-1]                      # oldest first for the chart's x-axis

        geo_rows = conn.execute(
            "SELECT COALESCE(a.geo_status, 'unverified'), COUNT(*) "
            "FROM attendance a JOIN students s ON s.id = a.student_id "
            "WHERE s.role = 'athlete'" + acs + " GROUP BY 1", cp
        ).fetchall()
        geo = {r[0]: r[1] for r in geo_rows}

        by_centre = [
            {"code": r[0] or "-", "name": r[1] or "Unassigned", "present": r[2], "athletes": r[3]}
            for r in conn.execute(
                "SELECT c.code, c.name, "
                "  (SELECT COUNT(*) FROM attendance a JOIN students s2 ON s2.id = a.student_id "
                "     WHERE a.centre_id = c.id AND s2.role = 'athlete'), "
                "  (SELECT COUNT(*) FROM students s3 WHERE s3.centre_id = c.id AND s3.role = 'athlete') "
                "FROM centres c ORDER BY 3 DESC"
            ) if r[2] or r[3]
        ]

        # Confidence is the raw cosine similarity actually stored. Bucketing it
        # shows whether matches are clustering near the threshold (fragile) or
        # well above it (comfortable) - the single most useful signal for
        # whether enrolment photos need refreshing.
        conf = [
            {"bucket": r[0], "count": r[1]}
            for r in conn.execute(
                # 20.0 alone is a numeric literal in Postgres, so the quotient
                # comes back as Decimal where SQLite gave a float. Casting to
                # float8 keeps the bucket a JSON number, as the chart expects.
                "SELECT CAST(a.confidence * 20 AS INT) / 20.0::double precision, COUNT(*) "
                "FROM attendance a JOIN students s ON s.id = a.student_id "
                "WHERE s.role = 'athlete' AND a.confidence IS NOT NULL" + acs +
                " GROUP BY 1 ORDER BY 1", cp
            )
        ]

        sessions = conn.execute(
            "SELECT COUNT(DISTINCT a.date) FROM attendance a "
            "JOIN students s ON s.id = a.student_id WHERE s.role = 'athlete'" + acs, cp
        ).fetchone()[0]

        # Per-athlete reliability, worst first: this is the list a coach acts on.
        per_athlete = [
            {"name": r[0], "roll_no": r[1], "present": r[2],
             "rate": round(100.0 * r[2] / sessions, 1) if sessions else 0.0}
            for r in conn.execute(
                "SELECT s.name, s.roll_no, COUNT(DISTINCT a.date) "
                "FROM students s LEFT JOIN attendance a ON a.student_id = s.id "
                "WHERE s.role = 'athlete' "
                "AND EXISTS (SELECT 1 FROM templates t WHERE t.student_id = s.id)" + cs +
                " GROUP BY s.id ORDER BY 3 ASC", cp
            )
        ]

    return {
        "enrolled": enrolled,
        "sessions": sessions,
        "trend": trend,
        "geo": geo,
        "by_centre": by_centre,
        "confidence": conf,
        "per_athlete": per_athlete,
    }
