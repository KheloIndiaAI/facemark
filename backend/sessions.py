"""Attendance registers: sessions, captures, draft rows.

Attendance is no longer written by the recogniser. A coach opens a session,
captures the group one or more times, reviews the result against their whole
roster, and submits it under their own face. Only submission promotes drafts to
confirmed attendance.

Two rules run through everything here:

  * **Nothing in this module writes a confirmed row.** Recognition produces
    drafts. Promotion happens in one place, on submit (phase 2), so there is
    exactly one route by which attendance becomes real.

  * **Match against the whole gallery, filter afterwards.** Narrowing the
    gallery to a coach's own athletes before matching was tried and reversed -
    a wrong-centre photo then returned zero matches with no way to tell that
    apart from the recogniser failing. Routing happens after scoring, in
    `route_recognised`.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, List, Optional

from . import config
from .db import Conn, connect

log = logging.getLogger(__name__)

# How long a draft register stays open before it is no use to anybody. A coach
# who captures in the morning and forgets to submit should not find yesterday's
# drafts waiting when they open the app tomorrow.
SESSION_TTL_HOURS = 18


# =============================================================================
# Coach <-> athlete links
# =============================================================================

def link_athlete(coach_id: int, athlete_id: int, is_primary: bool = False) -> bool:
    """Attach an athlete to a coach. Returns False if already linked."""
    if coach_id == athlete_id:
        raise ValueError("A coach cannot be their own athlete")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO coach_athletes (coach_id, athlete_id, is_primary, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT (coach_id, athlete_id) DO NOTHING",
            (coach_id, athlete_id, 1 if is_primary else 0, config.now_stamp()),
        )
        return cur.rowcount > 0


def unlink_athlete(coach_id: int, athlete_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM coach_athletes WHERE coach_id = ? AND athlete_id = ?",
            (coach_id, athlete_id),
        )
        return cur.rowcount > 0


def athletes_of(coach_id: int) -> List[dict]:
    """Every athlete linked to this coach, with their centre name."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, c.name AS centre_name, ca.is_primary "
            "FROM coach_athletes ca "
            "JOIN students s ON s.id = ca.athlete_id "
            "LEFT JOIN centres c ON c.id = s.centre_id "
            "WHERE ca.coach_id = ? ORDER BY s.name",
            (coach_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def coaches_of(athlete_id: int) -> List[dict]:
    """Every coach this athlete trains under."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.name, s.roll_no, s.photo_path, s.centre_id, "
            "       c.name AS centre_name, ca.is_primary "
            "FROM coach_athletes ca "
            "JOIN students s ON s.id = ca.coach_id "
            "LEFT JOIN centres c ON c.id = s.centre_id "
            "WHERE ca.athlete_id = ? ORDER BY ca.is_primary DESC, s.name",
            (athlete_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def athlete_ids_of(coach_id: int) -> set:
    with connect() as conn:
        return {
            int(r["athlete_id"])
            for r in conn.execute(
                "SELECT athlete_id FROM coach_athletes WHERE coach_id = ?", (coach_id,)
            ).fetchall()
        }


def coach_of_athlete_ids(athlete_ids) -> Dict[int, List[dict]]:
    """Which coaches each of these athletes belongs to, in one query.

    Used to label someone recognised in a capture as "Coach X's athlete"
    without a query per face.
    """
    ids = [int(i) for i in dict.fromkeys(athlete_ids)]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: Dict[int, List[dict]] = {i: [] for i in ids}
    with connect() as conn:
        rows = conn.execute(
            "SELECT ca.athlete_id, s.id AS coach_id, s.name AS coach_name "
            "FROM coach_athletes ca JOIN students s ON s.id = ca.coach_id "
            f"WHERE ca.athlete_id IN ({marks})", ids,
        ).fetchall()
    for r in rows:
        out[int(r["athlete_id"])].append(
            {"coach_id": int(r["coach_id"]), "coach_name": r["coach_name"]}
        )
    return out


# =============================================================================
# Sessions
# =============================================================================

def get_or_create(centre_id: int, coach_id: Optional[int], opened_by: int,
                  day: Optional[str] = None) -> dict:
    """Today's register for this coach at this centre. Idempotent.

    `coach_id=None` is a super-admin sweep, which is one-per-centre-per-day and
    is why the uniqueness lives in two partial indexes rather than one
    constraint - see database._swap_attendance_uniqueness.
    """
    day = day or config.today_str()
    now = config.local_now()
    expires = (now + timedelta(hours=SESSION_TTL_HOURS)).replace(tzinfo=None).isoformat(timespec="seconds")

    with connect() as conn:
        row = _find(conn, centre_id, coach_id, day)
        if row:
            return dict(row)
        # ON CONFLICT cannot be used here: the uniqueness is two partial
        # indexes, and which one applies depends on coach_id being NULL. A
        # re-read after IntegrityError is simpler than inferring either.
        try:
            conn.execute(
                "INSERT INTO attendance_sessions "
                "(centre_id, coach_id, opened_by, date, status, created_at, expires_at) "
                "VALUES (?,?,?,?,'draft',?,?)",
                (centre_id, coach_id, opened_by, day,
                 config.now_stamp(), expires),
            )
        except Exception:
            pass
        row = _find(conn, centre_id, coach_id, day)
        if row is None:
            raise RuntimeError("Could not open a session for this centre and day")
        return dict(row)


def _find(conn: Conn, centre_id: int, coach_id: Optional[int], day: str):
    if coach_id is None:
        return conn.execute(
            "SELECT * FROM attendance_sessions "
            "WHERE centre_id = ? AND coach_id IS NULL AND date = ?",
            (centre_id, day),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM attendance_sessions "
        "WHERE centre_id = ? AND coach_id = ? AND date = ?",
        (centre_id, coach_id, day),
    ).fetchone()


def get(session_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM attendance_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def add_capture(session_id: int, media_key: str, kind: str,
                liveness_verdict: Optional[str] = None,
                liveness_depth: Optional[float] = None,
                faces_detected: int = 0, recognised: int = 0,
                latitude: Optional[float] = None, longitude: Optional[float] = None,
                geo_status: Optional[str] = None,
                distance_m: Optional[float] = None) -> int:
    with connect() as conn:
        return conn.insert(
            "INSERT INTO session_captures "
            "(session_id, media_key, kind, liveness_verdict, liveness_depth, "
            " faces_detected, recognised, latitude, longitude, geo_status, "
            " distance_m, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, media_key, kind, liveness_verdict, liveness_depth,
             faces_detected, recognised, latitude, longitude, geo_status,
             distance_m, config.now_stamp()),
        )


def captures_of(session_id: int) -> List[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM session_captures WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# Draft rows
# =============================================================================

def draft(session_id: int, student_id: int, day: str, confidence: float,
          origin: str, image_path: Optional[str] = None,
          capture_id: Optional[int] = None, centre_id: Optional[int] = None,
          latitude: Optional[float] = None, longitude: Optional[float] = None,
          accuracy_m: Optional[float] = None, geo_status: Optional[str] = None,
          distance_m: Optional[float] = None, marked_by: Optional[int] = None) -> bool:
    """Add a DRAFT row. Returns False if this person is already in the session.

    Never writes 'confirmed'. Capturing the same group twice must not create a
    second row for the same person, which is what the (student_id, session_id)
    constraint enforces - so the second capture simply adds whoever was missed.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO attendance (student_id, date, confidence, image_path, "
            " marked_at, centre_id, latitude, longitude, accuracy_m, geo_status, "
            " distance_m, marked_by, session_id, capture_id, status, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?) "
            "ON CONFLICT (student_id, session_id) DO NOTHING",
            (student_id, day, confidence, image_path, config.now_stamp(),
             centre_id, latitude, longitude, accuracy_m, geo_status, distance_m,
             marked_by, session_id, capture_id, origin),
        )
        return cur.rowcount > 0


def set_present(session_id: int, student_id: int, present: bool, day: str,
                centre_id: Optional[int], marked_by: Optional[int]) -> str:
    """Toggle someone in the register by hand. Returns 'added' | 'removed' | 'noop'.

    Only ever touches DRAFT rows. A confirmed row belongs to a submitted
    register and is not editable here - re-opening a submitted register is a
    different operation and deliberately does not exist yet.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM attendance WHERE session_id = ? AND student_id = ?",
            (session_id, student_id),
        ).fetchone()
        if present:
            if row and row["status"] == "draft":
                return "noop"
            if row:
                return "noop"       # already confirmed - leave it alone
            conn.execute(
                "INSERT INTO attendance (student_id, date, confidence, image_path, "
                " marked_at, centre_id, marked_by, session_id, status, origin) "
                "VALUES (?,?,?,?,?,?,?,?,'draft','coach_added')",
                (student_id, day, 0.0, None, config.now_stamp(),
                 centre_id, marked_by, session_id),
            )
            return "added"
        if row and row["status"] == "draft":
            conn.execute("DELETE FROM attendance WHERE id = ?", (row["id"],))
            return "removed"
        return "noop"


def rows_of(session_id: int) -> Dict[int, dict]:
    """Every attendance row in this session, keyed by student id."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM attendance WHERE session_id = ?", (session_id,)
        ).fetchall()
        return {int(r["student_id"]): dict(r) for r in rows}


# =============================================================================
# Routing a capture's results
# =============================================================================

def route_recognised(coach_id: Optional[int], recognised: List[dict],
                     centre_id: Optional[int]) -> Dict[str, list]:
    """Split recognised people into what may be drafted and what may only be named.

    `recognised` is whatever the matcher returned against the WHOLE gallery.
    Each entry needs `student_id`, and may carry `centre_id`.

    A super-admin sweep (coach_id None) drafts everyone at that centre. A coach
    drafts only their own athletes; somebody else's athlete is named so the
    coach can see the recogniser worked, but is not drafted - one capture must
    not seed another coach's register.
    """
    out = {"draft": [], "other_coach": [], "other_centre": []}
    if not recognised:
        return out

    ids = [int(r["student_id"]) for r in recognised]
    links = coach_of_athlete_ids(ids)
    mine = athlete_ids_of(coach_id) if coach_id is not None else set()

    for r in recognised:
        sid = int(r["student_id"])
        home = r.get("centre_id")
        if centre_id is not None and home is not None and int(home) != int(centre_id):
            out["other_centre"].append(r)
            continue
        if coach_id is None:
            out["draft"].append(r)          # admin sweep: everyone at the centre
            continue
        if sid in mine:
            out["draft"].append(r)
            continue
        owners = links.get(sid, [])
        r = dict(r)
        r["coaches"] = owners
        out["other_coach"].append(r)
    return out
