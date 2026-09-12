"""Self-signup: pending account, phone verification, face capture.

Three unauthenticated endpoints back this, which makes it the largest abuse
surface in the app. Everything here is shaped by that:

  * **A signup token, not an open door.** POST /api/signup returns an opaque
    short-lived token, and every later step requires it. Without one, anyone
    who could guess a user id could post a face into somebody else's pending
    account, or read the coach roster of any centre.

  * **Throttled per address.** This is the one unauthenticated write path, so
    an address that keeps hitting it is slowed down.

  * **No phone verification.** It was removed with the SMS provider it needed -
    a code nobody can receive is a wall, not a check. The number is collected so
    a coach can ring an athlete, and it is an unverified claim. What stands
    between a stranger and the register is approval, not the number.

The account this produces is INERT. It cannot sign in and its face is excluded
from the gallery until a coach approves it - see database.load_gallery.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from datetime import timedelta
from typing import Dict, List, Optional

from . import auth, config, database
from .db import connect

log = logging.getLogger(__name__)

SIGNUP_TOKEN_TTL_MINUTES = 45
# A whole squad registers from the centre's one wifi, so every applicant shares
# an address. At 5 the sixth athlete of the afternoon was refused and the centre
# was told, in effect, that the app was broken. 120 covers any real intake and
# still stops a script; it is a bound on machines, not a quota for people.
SIGNUP_PER_IP_PER_HOUR = 120

# In-process counters, like the login throttle. N workers allow N times the
# burst; accepted deliberately, because the alternative is letting an
# unauthenticated caller write a row per attempt.
_hits: Dict[str, list] = {}
_lock = threading.Lock()


def throttled(key: str, limit: int, window_s: int) -> bool:
    """Public name for the same counter, so routes can use it too."""
    return _throttled(key, limit, window_s)


def _throttled(key: str, limit: int, window_s: int) -> bool:
    now = time.time()
    cutoff = now - window_s
    with _lock:
        seen = [t for t in _hits.get(key, ()) if t > cutoff]
        seen.append(now)
        _hits[key] = seen
        if len(_hits) > 8192:
            for k in [k for k, v in _hits.items() if not any(t > cutoff for t in v)]:
                _hits.pop(k, None)
        return len(seen) > limit


# =============================================================================
# Signup tokens
# =============================================================================
# In its own table, not in auth_sessions: those are LOGIN sessions and
# current_user resolves them, so a signup token there would become a way to be
# signed in as a pending account.
#
# And in the DATABASE, not a dict. A dict was a per-worker store, and the
# Dockerfile runs two workers - so a signup whose next request happened to land
# on the other one was told it had expired, about half the time, on both
# deployments. The original reasoning covered restarts and not workers.


def _new_token(user_id: int) -> str:
    tok = secrets.token_urlsafe(32)
    now = config.local_now().replace(tzinfo=None)
    expires = (now + timedelta(minutes=SIGNUP_TOKEN_TTL_MINUTES)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            "INSERT INTO signup_tokens (token, user_id, decided, expires_at, created_at) "
            "VALUES (?,?,0,?,?)",
            (tok, int(user_id), expires, config.now_stamp()),
        )
        # Swept here rather than on a schedule: this is the only place rows are
        # added, so it is the only place they can accumulate.
        conn.execute("DELETE FROM signup_tokens WHERE expires_at < ?",
                     (now.isoformat(timespec="seconds"),))
    return tok


def resolve_signup(token: str) -> dict:
    """The signup behind a token, or ValueError.

    Two conditions, and the second is the one that matters. A token is only a
    convenience for carrying a half-finished signup between requests; what
    makes it usable is the account still being undecided. Without that check a
    token kept working after approval, so for the rest of its 45 minutes an
    unauthenticated caller could keep posting face clips into a LIVE account -
    the one window in this app where an outsider can write to an active
    identity. Read from the row every time rather than cached in the token,
    because the decision happens elsewhere and this has to see it.
    """
    now = config.local_now().replace(tzinfo=None).isoformat(timespec="seconds")
    with connect() as conn:
        rec = conn.execute(
            "SELECT t.token, t.user_id, t.decided, t.expires_at, u.status "
            "FROM signup_tokens t LEFT JOIN users u ON u.id = t.user_id "
            "WHERE t.token = ?", (token or "",),
        ).fetchone()
        if rec is None or rec["expires_at"] < now:
            if rec is not None:
                conn.execute("DELETE FROM signup_tokens WHERE token = ?", (token,))
            raise ValueError("This signup has expired. Start again.")
        if rec["status"] is None:
            conn.execute("DELETE FROM signup_tokens WHERE token = ?", (token,))
            raise ValueError("This signup no longer exists. Start again.")
        # A token retired by decide() is kept just long enough to say WHY.
        # Deleting it there instead left the caller reading "this signup has
        # expired" a second after their coach approved them, which sends them
        # round the whole flow again rather than to the sign-in page.
        if int(rec["decided"]) or rec["status"] != "pending":
            conn.execute("DELETE FROM signup_tokens WHERE token = ?", (token,))
            raise ValueError("This signup has already been decided. "
                             "Sign in, or ask your coach.")
    return {"user_id": int(rec["user_id"])}


def invalidate_for_user(user_id: int) -> int:
    """Drop any live signup token for this account. Returns how many.

    Belt to resolve_signup's braces: the status check already refuses them, and
    this makes the reason accurate rather than "expired".
    """
    with connect() as conn:
        # Marked, not dropped, so the next use gets the accurate message and
        # then clears itself. It still cannot be used for anything - and now it
        # reaches tokens held by every worker, not just this one.
        return conn.execute("UPDATE signup_tokens SET decided = 1 WHERE user_id = ?",
                            (int(user_id),)).rowcount


# =============================================================================
# The steps
# =============================================================================

SELF_SIGNUP_ROLES = ("athlete", "coach")


def approver_for(role: str) -> str:
    """Who decides this application.

    An athlete is approved by the coach they chose. A coach cannot be, because
    there is nobody at the centre above them to ask - and a coach approving
    coaches would let the first person to register at a new centre wave in
    everyone who followed. So a coach goes to a super admin, and the two never
    appear in each other's queue.
    """
    return "super_admin" if role == "coach" else "coach"


def start(username: str, password: str, full_name: str,
          centre_id: int, ip: str = "", role: str = "athlete") -> dict:
    """Create the pending account. Returns {token, user_id}."""
    if _throttled(f"signup:{ip}", SIGNUP_PER_IP_PER_HOUR, 3600):
        # Says what to do about it. "Try later" left somebody at a centre with
        # no idea whether to wait a minute or an hour, or whether it was them.
        log.warning("Signup throttled for address %s (over %d in an hour)",
                    ip or "unknown", SIGNUP_PER_IP_PER_HOUR)
        raise PermissionError(
            "This centre has registered a lot of accounts in the last hour. "
            "Wait a few minutes and try again, or ask your coach to add you.")

    # Whitelisted, not merely "not super_admin". This value arrives on an
    # unauthenticated request, and the one thing it must never be able to say
    # is which role it becomes beyond these two.
    role = (role or "athlete").strip().lower()
    if role not in SELF_SIGNUP_ROLES:
        raise ValueError("Choose whether you are registering as an athlete or a coach")

    username = (username or "").strip().lower()
    full_name = (full_name or "").strip()
    if len(username) < 3:
        raise ValueError("Choose a username of at least 3 characters")
    if not full_name:
        raise ValueError("Your full name is required")
    if auth.get_user_by_username(username):
        raise ValueError("That username is taken")
    with connect() as conn:
        if conn.execute("SELECT 1 FROM centres WHERE id = ?",
                        (int(centre_id),)).fetchone() is None:
            raise ValueError("Choose a centre")

    # VALIDATE THE PASSWORD BEFORE WRITING ANYTHING. This used to happen inside
    # create_user, which ran AFTER the students row had already been committed -
    # so whether a password was six characters decided whether the centre's
    # roster gained a permanent stranger. The check is a length test, not a
    # hash, so it costs nothing.
    auth.validate_password(password)

    # The person record comes first: an account with no person could never be
    # recognised, and users.student_id is required for an athlete. A coach gets
    # one too - they are recognised at capture time and sign the register with
    # their own face, so they are a person in `students` exactly like an athlete.
    #
    # THREE TRANSACTIONS, ONE APPLICATION. The person was committed, then the
    # account, then both were marked pending - so an interruption anywhere in
    # between left a person on the centre's roster with the DEFAULT status,
    # which is 'active'. Nothing purges that: the retention sweep only looks at
    # pending and rejected applications. With role=coach it was worse than
    # untidy, because an active coach person appears in the signup coach picker
    # for everyone who registers afterwards.
    #
    # The status is now written by the INSERT, so the row is never active even
    # for an instant, and a failure part-way removes what was already made.
    roll = f"PEND-{secrets.token_hex(4).upper()}"
    student_id = database.add_student(
        full_name, roll, "", [], role=role, centre_id=centre_id,
    )
    with connect() as conn:
        conn.execute("UPDATE students SET status = 'pending' WHERE id = ?",
                     (student_id,))
    try:
        user_id = auth.create_user(username, password, role, full_name,
                                   centre_id=centre_id, student_id=student_id,
                                   status="pending")
    except Exception:
        # Compensate. Half an application is worse than none: a person nobody
        # can approve, sign in as, or find a reason for.
        with connect() as conn:
            conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
        log.warning("Signup failed after creating person %s; person removed",
                    student_id)
        raise
    # The account was created pending; nothing to promote or demote here.
    log.info("Signup started: %s user %s (person %s), pending %s approval",
             role, user_id, student_id, approver_for(role))
    return {"token": _new_token(user_id), "user_id": user_id,
            "student_id": student_id, "roll_no": roll, "role": role,
            "approver": approver_for(role),
            # The SERVER decides which steps there are, so the browser never
            # has to hold a second copy of the answer.
            "needs_coach": role == "athlete"}


# A coach who has applied but not been approved is a stranger with a name in
# the table. Every place that offers "the coaches at this centre" has to say so,
# or the first thing self-registration buys an impostor is a queue of athletes
# choosing them - and, on approval, a signed register.
_APPROVED_COACH = (
    " AND NOT EXISTS (SELECT 1 FROM users u "
    "WHERE u.student_id = s.id AND u.status <> 'active')"
)


def centre_of(token: str) -> int:
    """The centre the applicant behind this token registered at.

    The centre was chosen when the application began and is recorded on the
    account. Every later step should read it from there rather than accept it
    again from the client: /api/signup/coaches took a centre_id parameter, so a
    single self-issued token listed ANY centre's coach roster - names and
    enrolment photographs - and choose_coach then accepted a coach from any
    centre, dropping the application into a queue at a centre the applicant has
    nothing to do with.
    """
    rec = resolve_signup(token)
    with connect() as conn:
        row = conn.execute("SELECT centre_id FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
    if not row or row["centre_id"] is None:
        raise ValueError("This application has no centre")
    return int(row["centre_id"])


def coaches_at(centre_id: int) -> List[dict]:
    """Coaches a new athlete may choose, with their enrolment photo.

    Pending, rejected and suspended coaches are excluded. Coaches with no
    account at all are included: most were enrolled by an admin and never
    needed one, and dropping them would empty this list at every existing
    centre.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.name, s.photo_path, c.name AS centre_name "
            "FROM students s LEFT JOIN centres c ON c.id = s.centre_id "
            "WHERE s.role = 'coach' AND s.centre_id = ?" + _APPROVED_COACH
            + " ORDER BY s.name",
            (int(centre_id),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["photo"] = _thumbnail(d.pop("photo_path", None))
        out.append(d)
    return out


# The picker showed a coach's face so a new athlete could recognise the person
# rather than parse a name they may have only heard. It pointed at /api/photos,
# which requires a session - correctly, those are photographs of children - and
# the applicant has no session yet, so every one of them was a broken image.
#
# Inlined rather than given a token-gated image route: the list is already
# behind the signup token, a handful of coaches is a handful of small
# thumbnails, and it keeps the token out of <img src>, where it would end up in
# proxy and browser logs.
THUMB_PX = 96
THUMB_QUALITY = 70


def _thumbnail(photo_path: Optional[str]) -> Optional[str]:
    """A small square-ish JPEG data URI, or None if there is no usable photo."""
    if not photo_path:
        return None
    try:
        import base64

        import cv2
        import numpy as np

        from . import storage

        raw = storage.get("students", os.path.basename(photo_path))
        if not raw:
            return None
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        scale = THUMB_PX / max(h, w, 1)
        if scale < 1:
            img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                             interpolation=cv2.INTER_AREA)
        okj, buf = cv2.imencode(".jpg", img,
                                [int(cv2.IMWRITE_JPEG_QUALITY), THUMB_QUALITY])
        if not okj:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()
    except Exception as e:               # noqa: BLE001 - a missing face is not fatal
        log.warning("Could not build a coach thumbnail for %s: %s", photo_path, e)
        return None


def choose_coach(token: str, coach_id: int) -> None:
    """Attach an ATHLETE application to the coach who will approve it.

    Refused for a coach application. chosen_coach_id is what puts an account
    into a coach's approval queue, so letting a coach applicant set it would
    put a stranger asking for coach access in front of a coach who believes
    that queue only ever contains athletes - and one tap would grant it.
    A coach application is decided by a super admin, and by nobody else.
    """
    rec = resolve_signup(token)
    with connect() as conn:
        who = conn.execute("SELECT role FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
        if who is not None and who["role"] != "athlete":
            raise ValueError("A coach application is approved by a super admin, "
                             "so it does not choose a coach")
        # Re-checked here, not just filtered in the list above: the list is a
        # convenience for the browser, and this is the write.
        # Same centre as the applicant. Without this an application could be
        # attached to a coach anywhere in the country, landing in the approval
        # queue of somebody who has never heard of them - and taking the
        # applicant's name and face with it.
        row = conn.execute(
            "SELECT 1 FROM students s WHERE s.id = ? AND s.role = 'coach' "
            "  AND s.centre_id = (SELECT centre_id FROM users WHERE id = ?)"
            + _APPROVED_COACH, (int(coach_id), rec["user_id"])
        ).fetchone()
        if not row:
            raise ValueError("That coach is not available to choose")
        # A coach with no ACCOUNT cannot open an approval queue, so an
        # application attached to one waits for somebody who will never be
        # shown it. It is NOT refused here: coaches_at lists those coaches
        # deliberately - most were enrolled by an admin and never needed a
        # login - and refusing would empty the picker at every existing centre
        # and block registration entirely. Instead admin_overview counts these
        # as orphaned, which is the screen that exists to catch exactly the
        # applications nobody else will see.
        conn.execute("UPDATE users SET chosen_coach_id = ? WHERE id = ?",
                     (int(coach_id), rec["user_id"]))


def role_for(token: str) -> str:
    """The role behind a signup token, so the closing copy can name the right
    approver. Read from the row rather than from anything the caller sent."""
    rec = resolve_signup(token)
    with connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
    return (row["role"] if row else "athlete")


def student_for(token: str) -> int:
    """The person id behind a signup token, for the face-capture step."""
    rec = resolve_signup(token)
    with connect() as conn:
        row = conn.execute("SELECT student_id FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
    if row is None or not row["student_id"]:
        raise ValueError("This signup is incomplete")
    return int(row["student_id"])
