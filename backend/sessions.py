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

import numpy as np

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


def roster_options(coach_id: int, centre_id: Optional[int]) -> dict:
    """Who is on this coach's register, and who else could be.

    Candidates are active athletes at the coach's own centre. Pending and
    rejected people are excluded: they are not matchable and attendance cannot
    be written for them, so offering them a checkbox promises something the
    rest of the system refuses to deliver.
    """
    with connect() as conn:
        linked = [int(r[0]) for r in conn.execute(
            "SELECT athlete_id FROM coach_athletes WHERE coach_id = ?",
            (int(coach_id),)).fetchall()]
        q = ("SELECT s.id, s.name, s.roll_no, s.photo_path, s.gender, s.sport "
             "FROM students s WHERE s.role = 'athlete' AND s.status = 'active'")
        p: list = []
        if centre_id is not None:
            q += " AND s.centre_id = ?"
            p.append(int(centre_id))
        rows = [dict(r) for r in conn.execute(q + " ORDER BY s.name", p).fetchall()]
    have = set(linked)
    for r in rows:
        r["linked"] = int(r["id"]) in have
    return {"athletes": rows, "linked": linked}


def set_roster(coach_id: int, athlete_ids: List[int]) -> dict:
    """Make this coach's roster exactly `athlete_ids`. Returns what changed.

    Reconciled in ONE transaction rather than applied as a stream of link and
    unlink calls: a coach ticking thirty boxes should either get thirty or get
    an error, not whatever arrived before their phone lost signal.

    Removing a link does NOT touch attendance already recorded. Somebody who
    trained here in March was present in March, whoever coaches them now.
    """
    want = {int(a) for a in athlete_ids}
    with connect() as conn:
        if want:
            marks = ",".join("?" for _ in want)
            valid = {int(r[0]) for r in conn.execute(
                f"SELECT id FROM students WHERE id IN ({marks}) "
                "AND role = 'athlete' AND status = 'active'", list(want)).fetchall()}
            refused = want - valid
            if refused:
                raise ValueError(
                    "Not an active athlete: " + ", ".join(str(x) for x in sorted(refused)))
            want = valid
        if int(coach_id) in want:
            raise ValueError("A coach cannot be their own athlete")

        have = {int(r[0]) for r in conn.execute(
            "SELECT athlete_id FROM coach_athletes WHERE coach_id = ?",
            (int(coach_id),)).fetchall()}
        now = config.now_stamp()
        added = removed = 0
        for a in sorted(want - have):
            added += conn.execute(
                "INSERT INTO coach_athletes (coach_id, athlete_id, is_primary, created_at) "
                "VALUES (?,?,0,?) ON CONFLICT (coach_id, athlete_id) DO NOTHING",
                (int(coach_id), a, now)).rowcount
        for a in sorted(have - want):
            removed += conn.execute(
                "DELETE FROM coach_athletes WHERE coach_id = ? AND athlete_id = ?",
                (int(coach_id), a)).rowcount
    log.info("Roster for coach %s: +%d -%d, now %d", coach_id, added, removed, len(want))
    return {"added": added, "removed": removed, "total": len(want)}


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

def _sweep() -> None:
    """Housekeeping, hung off the one action every coach takes every day.

    Imported inside the call rather than at module scope: main imports sessions
    during startup, and a cycle through this module is not worth risking for
    something that does real work once every fifteen minutes. Never raises - a
    failed sweep must not break the register somebody is trying to open.
    """
    try:
        from . import maintenance
        maintenance.run_due()
    except Exception as e:                # noqa: BLE001
        log.warning("Maintenance sweep skipped: %s", e)


def get_or_create(centre_id: int, coach_id: Optional[int], opened_by: int,
                  day: Optional[str] = None) -> dict:
    """Today's register for this coach at this centre. Idempotent.

    `coach_id=None` is a super-admin sweep, which is one-per-centre-per-day and
    is why the uniqueness lives in two partial indexes rather than one
    constraint - see database._swap_attendance_uniqueness.
    """
    # Opening a register is the one thing every coach does every day, so it is
    # where the housekeeping hangs - and it is the right moment: yesterday's
    # abandoned draft should be closed before today's is opened.
    _sweep()
    day = day or config.today_str()
    now = config.local_now()
    expires = (now + timedelta(hours=SESSION_TTL_HOURS)).replace(tzinfo=None).isoformat(timespec="seconds")

    with connect() as conn:
        row = _find(conn, centre_id, coach_id, day)
        if row:
            # AN EXPIRED REGISTER IS NOT A CLOSED ONE. The sweep above closes a
            # draft nobody submitted, and this returned that row as-is - so a
            # coach who came back to a register that had timed out got a session
            # in status 'expired', which every caller reads as "not draft" and
            # reports as "already submitted". They were locked out of their own
            # day with a message that was not true, and could not open another
            # because (centre, coach, date) is unique.
            #
            # Asking for the register IS the intent to use it, so reopen it.
            # Submitted stays submitted: that one really is finished.
            if row["status"] == "expired":
                conn.execute(
                    "UPDATE attendance_sessions SET status = 'draft', expires_at = ? "
                    " WHERE id = ? AND status = 'expired'",
                    (expires, int(row["id"])),
                )
                log.info("Reopened expired register %s for coach %s on %s",
                         row["id"], coach_id, day)
                row = _find(conn, centre_id, coach_id, day)
            return dict(row)
        # ON CONFLICT cannot be used here: the uniqueness is two partial
        # indexes, and which one applies depends on coach_id being NULL. So the
        # insert is attempted and, if it loses a race, the row is re-read.
        #
        # The SAVEPOINT is what makes that work. In PostgreSQL a failed
        # statement aborts the WHOLE transaction, and every statement after it
        # raises InFailedSqlTransaction - so `try: insert / except: pass`
        # followed by a re-read never recovered anything. It swallowed the real
        # error and then failed on the next line with a message about the
        # transaction rather than about the insert. Rolling back to a savepoint
        # returns the transaction to a usable state so the re-read can run.
        conn.execute("SAVEPOINT open_session")
        try:
            conn.execute(
                "INSERT INTO attendance_sessions "
                "(centre_id, coach_id, opened_by, date, status, created_at, "
                " expires_at, is_sweep) "
                "VALUES (?,?,?,?,'draft',?,?,?)",
                (centre_id, coach_id, opened_by, day,
                 config.now_stamp(), expires, 1 if coach_id is None else 0),
            )
            conn.execute("RELEASE SAVEPOINT open_session")
        except Exception as e:
            conn.execute("ROLLBACK TO SAVEPOINT open_session")
            insert_error = e
        else:
            insert_error = None

        row = _find(conn, centre_id, coach_id, day)
        if row is None:
            # Losing the race is the only reason a failed insert is acceptable.
            # If nothing is there afterwards, the insert failed for its own
            # reasons - a missing centre, an opened_by that is not a real user -
            # and that error is worth showing rather than a generic one.
            raise RuntimeError(
                "Could not open a register for this centre and day"
                + (f": {insert_error}" if insert_error else "")
            )
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

def _is_active_person(conn: Conn, student_id: int) -> bool:
    """Whether attendance may be written for this person at all."""
    row = conn.execute("SELECT status FROM students WHERE id = ?",
                       (int(student_id),)).fetchone()
    return bool(row) and row["status"] == "active"


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

    Refuses anybody who is not an active person. The gallery exclusion stops
    the RECOGNISER reaching a pending or rejected face; this stops the other
    route, where a name appears on a roster and somebody ticks it. Enforced at
    the write rather than in each caller, because there are three of them and
    the cost of missing one is an unapproved person marked present.

    AND REFUSES A REGISTER THAT IS NO LONGER OPEN. Callers checked the status
    first and then wrote - two statements, with a gap. A capture takes seconds
    to recognise a group, and a submit landing inside that gap left draft rows
    attached to a submitted register: never confirmed, never shown, silently
    losing the attendance of everyone in that capture. Making the session's
    status part of the INSERT closes the gap for every caller at once, because
    the row cannot appear unless the register is open at the moment it is
    written.
    """
    with connect() as conn:
        if not _is_active_person(conn, student_id):
            return False
        cur = conn.execute(
            "INSERT INTO attendance (student_id, date, confidence, image_path, "
            " marked_at, centre_id, latitude, longitude, accuracy_m, geo_status, "
            " distance_m, marked_by, session_id, capture_id, status, origin) "
            "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',? "
            "  WHERE EXISTS (SELECT 1 FROM attendance_sessions "
            "                 WHERE id = ? AND status = 'draft') "
            "ON CONFLICT (student_id, session_id) DO NOTHING",
            (student_id, day, confidence, image_path, config.now_stamp(),
             centre_id, latitude, longitude, accuracy_m, geo_status, distance_m,
             marked_by, session_id, capture_id, origin, session_id),
        )
        return cur.rowcount > 0


def set_present(session_id: int, student_id: int, present: bool, day: str,
                centre_id: Optional[int], marked_by: Optional[int]) -> str:
    """Toggle someone in the register by hand. Returns 'added' | 'removed' | 'noop'.

    Only ever touches DRAFT rows. A confirmed row belongs to a submitted
    register and is not editable here - re-opening a submitted register is a
    different operation and deliberately does not exist yet.

    THE ACTIVE-PERSON CHECK IS DONE HERE, not delegated. A note above this
    function used to say "adding goes through draft(), which refuses a
    non-active person" - it does not, and never did: the branch below writes its
    own INSERT. So a pending applicant, or somebody already rejected, could be
    ticked present by hand and then confirmed by submitting the register, which
    is precisely what _is_active_person exists to prevent.

    Removing deliberately does NOT check - if a row exists for somebody who has
    since been rejected, taking it off the register must still work.
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
            if not _is_active_person(conn, student_id):
                return "noop"
            cur = conn.execute(
                "INSERT INTO attendance (student_id, date, confidence, image_path, "
                " marked_at, centre_id, marked_by, session_id, status, origin) "
                "VALUES (?,?,?,?,?,?,?,?,'draft','coach_added') "
                # Two coaches ticking the same person at once raced the SELECT
                # above and the loser got a UniqueViolation, which surfaces as a
                # 500. The row they both wanted exists either way.
                "ON CONFLICT (student_id, session_id) DO NOTHING",
                (student_id, day, 0.0, None, config.now_stamp(),
                 centre_id, marked_by, session_id),
            )
            return "added" if cur.rowcount > 0 else "noop"
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


# =============================================================================
# 1:1 verification and submission
# =============================================================================

def verify_face(img, student_id: int) -> dict:
    """Does this face belong to this specific person?

    Scored through the SAME fuse_scores path as open-set recognition, on a
    gallery narrowed to one person, so the number means the same thing on both
    sides and the two thresholds are comparable. Returns the best similarity
    over that person's templates.
    """
    from .detector import get_detector
    from .recognizer import fuse_scores, get_recognizer
    from . import database

    detector, recognizer = get_detector(), get_recognizer()
    faces = detector.detect(img)
    if not faces:
        return {"ok": False, "score": 0.0, "reason": "No face found in that clip"}

    # The largest face: the person holding the phone at arm's length is nearer
    # the lens than anyone behind them.
    face = max(faces, key=lambda f: f.width * f.height)

    gallery = database.load_gallery()
    narrowed = {}
    for model, (tids, sids, mat) in gallery.items():
        keep = np.asarray(sids).astype(int) == int(student_id)
        if keep.any():
            narrowed[model] = (np.asarray(tids)[keep], np.asarray(sids)[keep], mat[keep])
    if not narrowed:
        return {"ok": False, "score": 0.0,
                "reason": "This person has no enrolled face to check against"}

    queries = recognizer.embed_faces(img, [face])
    weights = {m.name: m.weight for m in recognizer.models}
    fused, ids = fuse_scores(queries, narrowed, weights)
    if fused is None or not len(ids):
        return {"ok": False, "score": 0.0, "reason": "Could not read that face"}
    score = float(np.max(fused[0]))
    return {"ok": True, "score": score, "reason": ""}


def find_existing_person(img) -> dict:
    """The best open-set match for this face among people already enrolled.

    The same fuse_scores path and the same MATCH_THRESHOLD the register uses,
    because it is the same question - "which of these people is this?" - and
    that threshold is the one measured on this corpus. The 1:1 thresholds are
    deliberately not used: those answer "is this the person I already believe
    it is", which is a different and much easier question.

    Searched across every centre, not just the one being applied to. An athlete
    moving between centres is exactly the case that produces a duplicate, and
    narrowing to one centre would miss it.

    Pending people are already outside the gallery, so an applicant's own
    templates can never come back as their own duplicate.
    """
    from .detector import get_detector
    from .recognizer import fuse_scores, get_recognizer
    from . import database

    detector, recognizer = get_detector(), get_recognizer()
    faces = detector.detect(img)
    if not faces:
        return {"student_id": None, "score": 0.0}
    face = max(faces, key=lambda f: f.width * f.height)

    gallery = database.load_gallery()
    if not gallery:
        return {"student_id": None, "score": 0.0}
    queries = recognizer.embed_faces(img, [face])
    weights = {m.name: m.weight for m in recognizer.models}
    fused, ids = fuse_scores(queries, gallery, weights)
    if fused is None or not len(ids):
        return {"student_id": None, "score": 0.0}
    best = int(np.argmax(fused[0]))
    score = float(fused[0][best])
    if score < config.MATCH_THRESHOLD:
        return {"student_id": None, "score": score}
    return {"student_id": int(ids[best]), "score": score}


def submit(session_id: int, verified: bool, score: float,
           liveness_verdict: str) -> dict:
    """Promote every draft in this register to confirmed, in ONE transaction.

    All-or-nothing on purpose. A partial promotion would leave a register that
    is half real and half not, with nothing on screen to say which half - and
    the coach has already signed for the whole thing.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM attendance_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise ValueError("No such register")
        if row["status"] != "draft":
            raise ValueError("This register has already been submitted")

        promoted = conn.execute(
            "UPDATE attendance SET status = 'confirmed' "
            "WHERE session_id = ? AND status = 'draft'",
            (session_id,),
        ).rowcount
        conn.execute(
            "UPDATE attendance_sessions SET status = 'submitted', submitted_at = ?, "
            "submitter_verified = ?, submitter_score = ?, submitter_liveness = ? "
            "WHERE id = ?",
            (config.now_stamp(), 1 if verified else 0, score, liveness_verdict,
             session_id),
        )
    return {"promoted": promoted, "verified": verified, "score": score}


# =============================================================================
# Athlete self-marking
# =============================================================================

# Failure cooldown, per (athlete, coach). In-process, like the login throttle:
# N workers allow N times the burst, accepted deliberately because the
# alternative is letting a caller write a row per attempt. The ACCEPTED-once
# rule does not rely on this - it is enforced in the database by the
# (student_id, session_id) constraint, which no amount of process-restarting
# gets around.
_self_fail: Dict[tuple, float] = {}
_SELF_COOLDOWN_S = 20


def self_mark_cooldown(athlete_id: int, coach_id: int) -> int:
    """Seconds still to wait after a recent failure, or 0."""
    import time
    left = _self_fail.get((int(athlete_id), int(coach_id)), 0) - time.time()
    return int(left) if left > 0 else 0


def note_self_failure(athlete_id: int, coach_id: int) -> None:
    import time
    _self_fail[(int(athlete_id), int(coach_id))] = time.time() + _SELF_COOLDOWN_S
    if len(_self_fail) > 4096:
        now = time.time()
        for k in [k for k, v in _self_fail.items() if v < now]:
            _self_fail.pop(k, None)


def already_self_marked(athlete_id: int, coach_id: int, day: str) -> bool:
    """Has this athlete already been recorded in this coach's register today?"""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM attendance a "
            "JOIN attendance_sessions s ON s.id = a.session_id "
            "WHERE a.student_id = ? AND s.coach_id = ? AND s.date = ?",
            (int(athlete_id), int(coach_id), day),
        ).fetchone()
        return row is not None


# =============================================================================
# Oversight
# =============================================================================

def admin_overview(day: Optional[str] = None, centre_id: Optional[int] = None) -> dict:
    """One screen's worth of "is today's attendance actually happening".

    The question an administrator has is not "how many were present" - it is
    which registers are MISSING, and which of the ones that exist should not be
    trusted. So the expensive part of this is the coaches who have done
    nothing, which no other view surfaces.
    """
    day = day or config.today_str()
    cs = " AND s.centre_id = ?" if centre_id is not None else ""
    cp = [centre_id] if centre_id is not None else []

    with connect() as conn:
        submitted = [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS centre_name, p.name AS coach_name "
            "FROM attendance_sessions s "
            "LEFT JOIN centres c ON c.id = s.centre_id "
            "LEFT JOIN students p ON p.id = s.coach_id "
            "WHERE s.date = ? AND s.status = 'submitted'" + cs +
            " ORDER BY s.submitted_at DESC", [day] + cp).fetchall()]

        drafts = [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS centre_name, p.name AS coach_name, "
            "  (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS rows "
            "FROM attendance_sessions s "
            "LEFT JOIN centres c ON c.id = s.centre_id "
            "LEFT JOIN students p ON p.id = s.coach_id "
            "WHERE s.date = ? AND s.status = 'draft'" + cs +
            " ORDER BY s.expires_at", [day] + cp).fetchall()]

        # Registers that timed out. Without this list they were invisible:
        # `submitted` and `drafts` both filter on a status an expired register
        # no longer has, and `missing` only finds coaches with NO session row at
        # all - so a coach who opened a register, captured into it and never
        # submitted vanished from the one screen that exists to notice that.
        # The attendance inside was deleted by the sweep, so nothing else was
        # ever going to mention it either.
        expired = [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS centre_name, p.name AS coach_name "
            "FROM attendance_sessions s "
            "LEFT JOIN centres c ON c.id = s.centre_id "
            "LEFT JOIN students p ON p.id = s.coach_id "
            "WHERE s.date = ? AND s.status = 'expired'" + cs +
            " ORDER BY s.expires_at DESC", [day] + cp).fetchall()]

        # Coaches with no register at all today. LEFT JOIN, not NOT IN: a coach
        # with no session produces a NULL row rather than being dropped, which
        # is the entire list this view exists to show.
        acs = " AND st.centre_id = ?" if centre_id is not None else ""
        missing = [dict(r) for r in conn.execute(
            "SELECT st.id AS coach_id, st.name AS coach_name, st.centre_id, "
            "       c.name AS centre_name "
            "FROM students st "
            "LEFT JOIN centres c ON c.id = st.centre_id "
            "LEFT JOIN attendance_sessions s "
            "       ON s.coach_id = st.id AND s.date = ? "
            # An unapproved coach application is not a coach who failed to
            # open a register; they cannot open one at all.
            "WHERE st.role = 'coach' AND st.status = 'active' AND s.id IS NULL"
            "  AND EXISTS (SELECT 1 FROM coach_athletes ca WHERE ca.coach_id = st.id)"
            + acs + " ORDER BY st.name", [day] + cp).fetchall()]

        unverified = [dict(r) for r in conn.execute(
            "SELECT s.*, c.name AS centre_name, p.name AS coach_name "
            "FROM attendance_sessions s "
            "LEFT JOIN centres c ON c.id = s.centre_id "
            "LEFT JOIN students p ON p.id = s.coach_id "
            # Scoped to the day, like every other list here. Without it the
            # page showed a date, and this panel answered a different question -
            # every unverified register ever submitted, anywhere in its centre
            # scope - so changing the date changed four lists and not this one.
            "WHERE s.date = ? AND s.status = 'submitted' "
            "  AND COALESCE(s.submitter_verified, 0) = 0"
            + cs + " ORDER BY s.submitted_at DESC LIMIT 50", [day] + cp).fetchall()]

        # A capture that could not be checked at all. Not an accusation - it is
        # the one thing the liveness guard cannot speak to, so it is listed.
        photo_only = [dict(r) for r in conn.execute(
            "SELECT cap.*, s.date, s.centre_id, p.name AS coach_name "
            "FROM session_captures cap "
            "JOIN attendance_sessions s ON s.id = cap.session_id "
            "LEFT JOIN students p ON p.id = s.coach_id "
            "WHERE cap.kind = 'photo' "
            "   OR cap.liveness_verdict IN ('not_checked', 'too_far')"
            + cs + " ORDER BY cap.id DESC LIMIT 50", cp).fetchall()]

        pending = conn.execute(
            "SELECT COUNT(*) FROM users WHERE status = 'pending'"
        ).fetchone()[0]
        # Counted separately because these are the ones nobody will action on
        # their own: no coach sees them, so they wait until someone goes
        # looking. A number on the oversight page is that someone.
        # Two ways to end up in nobody's queue, not one: no coach chosen, or
        # a coach chosen who has no account to open a queue with. The second
        # was invisible - the applicant sees "waiting for your coach" and waits
        # for a person who will never be shown them.
        orphaned = conn.execute(
            "SELECT COUNT(*) FROM users u WHERE u.status = 'pending' "
            "  AND u.role = 'athlete' AND ("
            "        u.chosen_coach_id IS NULL"
            "     OR NOT EXISTS (SELECT 1 FROM users c "
            "                     WHERE c.student_id = u.chosen_coach_id "
            "                       AND c.role = 'coach' AND c.status = 'active'))"
        ).fetchone()[0]

    now = config.local_now().replace(tzinfo=None).isoformat(timespec="seconds")
    expiring = [d for d in drafts if (d.get("expires_at") or "") <= now]

    return {
        "date": day,
        "submitted": submitted,
        "expired": expired,
        "submitted_count": len(submitted),
        "drafts": drafts,
        "draft_count": len(drafts),
        "expiring": expiring,
        "expiring_count": len(expiring),
        "missing": missing,
        "missing_count": len(missing),
        "unverified": unverified,
        "unverified_count": len(unverified),
        "photo_only": photo_only,
        "photo_only_count": len(photo_only),
        "pending_approvals": pending,
        "orphaned_approvals": orphaned,
    }


# =============================================================================
# Approvals
# =============================================================================

def pending_for_coach(coach_student_id: Optional[int]) -> List[dict]:
    """The approval queue. None means every pending account (super admin)."""
    q = ("SELECT u.id AS user_id, u.username, u.full_name, u.email, u.phone, "
         "       u.status, u.role, u.created_at, u.student_id, u.chosen_coach_id, "
         "       u.guardian_name, "
         "       u.duplicate_of, u.duplicate_score, "
         "       d.name AS duplicate_name, d.roll_no AS duplicate_roll_no, "
         "       dc.name AS duplicate_centre_name, "
         "       s.name AS person_name, s.roll_no, s.photo_path, s.centre_id, "
         "       c.name AS centre_name, "
         "       (SELECT COUNT(*) FROM templates t WHERE t.student_id = u.student_id) AS templates, "
         # Whether the coach they chose can actually be shown this queue.
         # A coach enrolled by an admin may have no login at all, and an
         # application attached to one waits for somebody who will never
         # see it - which is what `orphaned` below is for.
         "       EXISTS (SELECT 1 FROM users cu WHERE cu.student_id = u.chosen_coach_id "
         "                 AND cu.role = 'coach' AND cu.status = 'active') "
         "         AS chosen_coach_has_account "
         "FROM users u "
         "LEFT JOIN students s ON s.id = u.student_id "
         "LEFT JOIN students d ON d.id = u.duplicate_of "
         "LEFT JOIN centres dc ON dc.id = d.centre_id "
         "LEFT JOIN centres c ON c.id = u.centre_id "
         "WHERE u.status = 'pending'")
    p: list = []
    if coach_student_id is not None:
        # Role as well as ownership. A coach's queue contains athletes and
        # nothing else - an application for coach access is a super admin's
        # decision, and must not be one tap away from a coach who is working
        # through a list they reasonably assume is all athletes.
        q += " AND u.role = 'athlete' AND u.chosen_coach_id = ?"
        p.append(int(coach_student_id))
    q += " ORDER BY u.created_at"
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    # An athlete with no chosen coach is not a new arrival - the coach they
    # picked was deleted, and chosen_coach_id is ON DELETE SET NULL. They then
    # sat in nobody's queue. Flagged rather than silently reassigned: which
    # coach they belong to now is a question for a person.
    for r in rows:
        # Matches the count above: no coach chosen, or one who cannot be
        # notified because they have no account.
        r["orphaned"] = bool(
            r.get("role") == "athlete"
            and (not r.get("chosen_coach_id") or not r.get("chosen_coach_has_account", True))
        )
    return rows


def reopen(user_id: int) -> dict:
    """Put a decided account back in the queue.

    Rejecting was a one-way door: the username stayed taken and nothing could
    undo it short of database access. This is the way back, and it lands the
    application exactly where it started rather than approving it - the second
    decision gets made the same way the first one was.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT status, student_id, role FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            raise ValueError("No such account")
        if row["status"] == "pending":
            raise ValueError("That account is already waiting for a decision")
        if row["status"] == "active":
            raise ValueError("That account is active. Deactivate it instead of "
                             "sending it back to the queue.")
        conn.execute(
            "UPDATE users SET status = 'pending', approved_by = NULL, approved_at = NULL "
            "WHERE id = ?", (int(user_id),))
        # The person goes back to pending too, so the face stays out of the
        # gallery while the second decision is pending - the same rule as the
        # first time round.
        if row["student_id"]:
            conn.execute("UPDATE students SET status = 'pending' WHERE id = ?",
                         (int(row["student_id"]),))
    return {"status": "pending", "role": row["role"]}


def set_chosen_coach(user_id: int, coach_id: Optional[int]) -> dict:
    """Point a pending athlete at the coach who should decide them.

    For applicants whose chosen coach was deleted, and for the case where the
    applicant simply picked the wrong name off the list. Refuses anything but a
    pending ATHLETE: moving a coach application into a coach's queue is the
    escalation the whole approval split exists to prevent.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT status, role, centre_id FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            raise ValueError("No such account")
        if row["status"] != "pending":
            raise ValueError("That account is not waiting for a decision")
        if row["role"] != "athlete":
            raise ValueError("A coach application is decided by a super admin, "
                             "so it has no coach to assign")
        if coach_id is not None:
            ok = conn.execute(
                "SELECT 1 FROM students s WHERE s.id = ? AND s.role = 'coach' "
                "  AND s.status = 'active'", (int(coach_id),)
            ).fetchone()
            if not ok:
                raise ValueError("That is not an active coach")
        conn.execute("UPDATE users SET chosen_coach_id = ? WHERE id = ?",
                     (int(coach_id) if coach_id is not None else None, int(user_id)))
    return {"chosen_coach_id": coach_id}


def merge_person(conn: Conn, user_id: int, new_person: int, keep_person: int) -> None:
    """Fold a duplicate signup into the person who was already enrolled.

    The account is re-pointed at the existing record and the duplicate's
    templates move across - they are extra views of the same face, taken today,
    which is worth keeping. The duplicate students row then goes, so there is
    never a moment where both exist and attendance can land on the wrong one.
    """
    if int(new_person) == int(keep_person):
        return
    conn.execute("UPDATE templates SET student_id = ? WHERE student_id = ?",
                 (int(keep_person), int(new_person)))
    conn.execute("UPDATE users SET student_id = ? WHERE id = ?",
                 (int(keep_person), int(user_id)))
    conn.execute("DELETE FROM coach_athletes WHERE coach_id = ? OR athlete_id = ?",
                 (int(new_person), int(new_person)))
    conn.execute("DELETE FROM students WHERE id = ?", (int(new_person),))


def decide(user_id: int, approve: bool, approver_user_id: int,
           guardian_name: Optional[str] = None,
           guardian_consent: bool = False,
           merge: bool = False) -> dict:
    """Approve or reject a pending account.

    On approval three things happen together, in one transaction: the account
    becomes active, the coach_athletes link is created, and - because the
    gallery excludes pending accounts by query rather than by copying rows -
    their templates become matchable. "Approving makes both true in one action"
    is therefore a property of the transaction, not of remembering to do a
    second step.
    """
    now = config.now_stamp()
    with connect() as conn:
        row = conn.execute(
            "SELECT id, status, role, student_id, chosen_coach_id, duplicate_of "
            "FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            raise ValueError("No such account")
        if row["status"] != "pending":
            raise ValueError("That account is not pending")
        role = row["role"]

        if not approve:
            conn.execute(
                "UPDATE users SET status = 'rejected', approved_by = ?, approved_at = ? "
                "WHERE id = ?", (int(approver_user_id), now, int(user_id)),
            )
            # The person too. Rejecting used to leave students.status alone and
            # the gallery keyed only on 'pending', so a rejected face became
            # MATCHABLE - the opposite of the decision just made.
            if row["student_id"]:
                conn.execute("UPDATE students SET status = 'rejected' WHERE id = ?",
                             (int(row["student_id"]),))
            from . import signup as signup_mod
            signup_mod.invalidate_for_user(int(user_id))
            return {"status": "rejected", "linked": False,
                    "role": row["role"]}

        # Merging first, so everything below acts on the person who survives.
        person = int(row["student_id"]) if row["student_id"] else None
        merged_into = None
        if merge:
            if not row["duplicate_of"] or not person:
                raise ValueError("There is no existing person to merge this into")
            merged_into = int(row["duplicate_of"])
            merge_person(conn, int(user_id), person, merged_into)
            person = merged_into
        if person:
            conn.execute("UPDATE students SET status = 'active' WHERE id = ?", (person,))
        conn.execute(
            "UPDATE users SET status = 'active', approved_by = ?, approved_at = ?, "
            "guardian_name = COALESCE(?, guardian_name), "
            "guardian_consent_at = CASE WHEN ? THEN ? ELSE guardian_consent_at END "
            "WHERE id = ?",
            (int(approver_user_id), now, guardian_name,
             bool(guardian_consent), now, int(user_id)),
        )
        # A coach has no coach, so there is no link to make. Guarded on role
        # rather than on chosen_coach_id being absent, so that a stray value in
        # that column could never enrol an approved coach as somebody's athlete.
        linked = False
        if row["role"] == "athlete" and person and row["chosen_coach_id"]:
            cur = conn.execute(
                "INSERT INTO coach_athletes (coach_id, athlete_id, is_primary, created_at) "
                "VALUES (?,?,1,?) ON CONFLICT (coach_id, athlete_id) DO NOTHING",
                (int(row["chosen_coach_id"]), person, now),
            )
            linked = cur.rowcount > 0
    # Imported here rather than at module scope: signup imports database and
    # auth, and this is the only edge that would point back the other way.
    from . import signup as signup_mod
    signup_mod.invalidate_for_user(int(user_id))
    return {"status": "active", "linked": linked, "role": role,
            "student_id": person, "merged_into": merged_into}
