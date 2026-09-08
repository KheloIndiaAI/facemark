"""Self-signup: pending account, phone verification, face capture.

Three unauthenticated endpoints back this, which makes it the largest abuse
surface in the app. Everything here is shaped by that:

  * **A signup token, not an open door.** POST /api/signup returns an opaque
    short-lived token, and every later step requires it. Without one, anyone
    who could guess a user id could post a face into somebody else's pending
    account, or read the coach roster of any centre.

  * **Codes are hashed, never stored.** A leaked database should not hand over
    live OTPs, and the code never appears in an API response either - only in
    the delivery channel. Returning it "for convenience in dev" is how it ends
    up in production.

  * **Throttled per number AND per address.** Per-number alone lets one caller
    walk a list of numbers and run up somebody else's SMS bill; per-address
    alone lets a botnet hammer one number.

The account this produces is INERT. It cannot sign in and its face is excluded
from the gallery until a coach approves it - see database.load_gallery.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from datetime import timedelta
from typing import Dict, List, Optional

from . import auth, centres as centres_mod, config, database
from .db import connect

log = logging.getLogger(__name__)

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_S = 60
OTP_PER_NUMBER_PER_HOUR = 5
OTP_PER_IP_PER_HOUR = 20

SIGNUP_TOKEN_TTL_MINUTES = 45
SIGNUP_PER_IP_PER_HOUR = 5

# In-process counters, like the login throttle. N workers allow N times the
# burst; accepted deliberately, because the alternative is letting an
# unauthenticated caller write a row per attempt.
_hits: Dict[str, list] = {}
_lock = threading.Lock()


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
# Kept in auth_sessions would be wrong - those are LOGIN sessions and
# current_user resolves them, so a signup token would become a way to be
# signed in as a pending account. Its own table would be tidier; a dict is
# honest about what it is and disappears on restart, which for a 45-minute
# half-finished signup is acceptable.
_signups: Dict[str, dict] = {}


def _new_token(user_id: int, phone: str) -> str:
    tok = secrets.token_urlsafe(32)
    _signups[tok] = {
        "user_id": int(user_id),
        "phone": phone,
        "expires": time.time() + SIGNUP_TOKEN_TTL_MINUTES * 60,
        "phone_verified": False,
    }
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
    rec = _signups.get(token or "")
    if not rec or rec["expires"] < time.time():
        _signups.pop(token or "", None)
        raise ValueError("This signup has expired. Start again.")
    # A token retired by decide() is kept just long enough to say WHY. Popping
    # it there instead left the caller reading "this signup has expired" one
    # second after their coach approved them, which sends them round the whole
    # flow again rather than to the sign-in page.
    if rec.get("decided"):
        _signups.pop(token, None)
        raise ValueError("This signup has already been decided. "
                         "Sign in, or ask your coach.")
    with connect() as conn:
        row = conn.execute("SELECT status FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
    if row is None:
        _signups.pop(token, None)
        raise ValueError("This signup no longer exists. Start again.")
    if row["status"] != "pending":
        _signups.pop(token, None)
        raise ValueError("This signup has already been decided. "
                         "Sign in, or ask your coach.")
    return rec


def invalidate_for_user(user_id: int) -> int:
    """Drop any live signup token for this account. Returns how many.

    Belt to resolve_signup's braces: the status check already refuses them, and
    this stops a decided signup sitting in memory for another 45 minutes. Only
    reaches tokens held by THIS process, which is why it is not the guard.
    """
    dead = [t for t, r in _signups.items() if int(r["user_id"]) == int(user_id)]
    for t in dead:
        # Marked, not dropped, so the next use gets the accurate message and
        # then clears itself. It still cannot be used for anything.
        _signups[t]["decided"] = True
    return len(dead)


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


def start(username: str, password: str, full_name: str, phone: str,
          centre_id: int, ip: str = "", role: str = "athlete",
          join_code: str = "") -> dict:
    """Create the pending account. Returns {token, user_id}."""
    if _throttled(f"signup:{ip}", SIGNUP_PER_IP_PER_HOUR, 3600):
        raise PermissionError("Too many signups from this connection. Try later.")

    # Whitelisted, not merely "not super_admin". This value arrives on an
    # unauthenticated request, and the one thing it must never be able to say
    # is which role it becomes beyond these two.
    role = (role or "athlete").strip().lower()
    if role not in SELF_SIGNUP_ROLES:
        raise ValueError("Choose whether you are registering as an athlete or a coach")

    username = (username or "").strip().lower()
    full_name = (full_name or "").strip()
    phone = (phone or "").strip()
    if len(username) < 3:
        raise ValueError("Choose a username of at least 3 characters")
    if not full_name:
        raise ValueError("Your full name is required")
    if len(phone) < 8:
        raise ValueError("A valid phone number is required")
    if auth.get_user_by_username(username):
        raise ValueError("That username is taken")
    with connect() as conn:
        if conn.execute("SELECT 1 FROM centres WHERE id = ?",
                        (int(centre_id),)).fetchone() is None:
            raise ValueError("Choose a centre")

    # Coaches only. An athlete's coach approves them in person and knows their
    # face; a coach may be approved by a super admin who has never met them, so
    # something the centre actually issued has to come with the application.
    if role == "coach" and not centres_mod.check_join_code(centre_id, join_code):
        raise ValueError("That centre code is not right. Ask the centre for the "
                         "coach registration code.")

    # The person record comes first: an account with no person could never be
    # recognised, and users.student_id is required for an athlete. A coach gets
    # one too - they are recognised at capture time and sign the register with
    # their own face, so they are a person in `students` exactly like an athlete.
    roll = f"PEND-{secrets.token_hex(4).upper()}"
    student_id = database.add_student(
        full_name, roll, "", [], role=role, centre_id=centre_id, phone=phone,
    )
    user_id = auth.create_user(username, password, role, full_name,
                               centre_id=centre_id, phone=phone, student_id=student_id)
    with connect() as conn:
        conn.execute("UPDATE users SET status = 'pending' WHERE id = ?", (user_id,))
        # The person is marked too, not just the account. The face lands on
        # this row and has to stay unmatchable even if the account is later
        # deleted rather than decided.
        conn.execute("UPDATE students SET status = 'pending' WHERE id = ?",
                     (student_id,))
    log.info("Signup started: %s user %s (person %s), pending %s approval",
             role, user_id, student_id, approver_for(role))
    return {"token": _new_token(user_id, phone), "user_id": user_id,
            "student_id": student_id, "roll_no": roll, "role": role,
            "approver": approver_for(role),
            "needs_coach": role == "athlete"}


# A coach who has applied but not been approved is a stranger with a name in
# the table. Every place that offers "the coaches at this centre" has to say so,
# or the first thing self-registration buys an impostor is a queue of athletes
# choosing them - and, on approval, a signed register.
_APPROVED_COACH = (
    " AND NOT EXISTS (SELECT 1 FROM users u "
    "WHERE u.student_id = s.id AND u.status <> 'active')"
)


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
        p = d.pop("photo_path", None)
        d["photo_url"] = f"/api/photos/{os.path.basename(p)}" if p else None
        out.append(d)
    return out


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
        row = conn.execute(
            "SELECT 1 FROM students s WHERE s.id = ? AND s.role = 'coach'"
            + _APPROVED_COACH, (int(coach_id),)
        ).fetchone()
        if not row:
            raise ValueError("That coach is not available to choose")
        conn.execute("UPDATE users SET chosen_coach_id = ? WHERE id = ?",
                     (int(coach_id), rec["user_id"]))


def send_otp(token: str, ip: str = "") -> dict:
    """Create and 'deliver' a one-time code. The code is never returned."""
    rec = resolve_signup(token)
    phone = rec["phone"]

    if _throttled(f"otp:num:{phone}", OTP_PER_NUMBER_PER_HOUR, 3600):
        raise PermissionError("Too many codes for that number. Try again later.")
    if _throttled(f"otp:ip:{ip}", OTP_PER_IP_PER_HOUR, 3600):
        raise PermissionError("Too many codes from this connection. Try later.")

    now = config.local_now()
    with connect() as conn:
        last = conn.execute(
            "SELECT created_at FROM otp_challenges WHERE phone = ? "
            "ORDER BY id DESC LIMIT 1", (phone,)
        ).fetchone()
        if last:
            try:
                from datetime import datetime
                age = (now.replace(tzinfo=None)
                       - datetime.fromisoformat(last["created_at"])).total_seconds()
                if age < OTP_RESEND_COOLDOWN_S:
                    raise PermissionError(
                        f"A code was just sent. Wait {int(OTP_RESEND_COOLDOWN_S - age)}s.")
            except (ValueError, TypeError):
                pass

        code = f"{secrets.randbelow(1000000):06d}"
        expires = (now + timedelta(minutes=OTP_TTL_MINUTES)) \
            .replace(tzinfo=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO otp_challenges (phone, code_hash, expires_at, attempts, created_at) "
            "VALUES (?,?,?,0,?)",
            (phone, _hash_code(phone, code), expires, config.now_stamp()),
        )
    _deliver(phone, code)
    return {"sent": True, "expires_in_minutes": OTP_TTL_MINUTES}


def _hash_code(phone: str, code: str) -> str:
    """Salted with the number, so one rainbow table does not cover every code."""
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


def _deliver(phone: str, code: str) -> None:
    """Hand the code to a delivery channel.

    SMS to Indian numbers needs DLT registration with a TRAI-approved platform
    before a single message can be sent - entity, sender ID and every template
    approved in advance. That is procurement, not code, so until it exists this
    logs at WARNING and nothing is sent. The code is deliberately NOT returned
    to the caller: an endpoint that hands back its own OTP "for testing" is one
    deploy away from doing it in production.
    """
    log.warning("OTP for %s is %s (no SMS provider configured - see IMPLEMENTATION-PLAN "
                "section 4.4 on DLT registration)", phone, code)


def verify_otp(token: str, code: str) -> bool:
    rec = resolve_signup(token)
    phone = rec["phone"]
    now = config.local_now().replace(tzinfo=None).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT id, code_hash, expires_at, attempts, consumed_at "
            "FROM otp_challenges WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)
        ).fetchone()
        if row is None:
            raise ValueError("Ask for a code first")
        if row["consumed_at"]:
            raise ValueError("That code has already been used")
        if row["expires_at"] < now:
            raise ValueError("That code has expired. Ask for a new one.")
        if int(row["attempts"]) >= OTP_MAX_ATTEMPTS:
            raise ValueError("Too many wrong attempts. Ask for a new code.")

        # Counted BEFORE comparing, so a crash or a race cannot give a free try.
        conn.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?",
                     (row["id"],))
        if not secrets.compare_digest(row["code_hash"],
                                      _hash_code(phone, (code or "").strip())):
            return False
        conn.execute("UPDATE otp_challenges SET consumed_at = ? WHERE id = ?",
                     (config.now_stamp(), row["id"]))
        conn.execute("UPDATE users SET phone_verified_at = ? WHERE id = ?",
                     (config.now_stamp(), rec["user_id"]))
    rec["phone_verified"] = True
    return True


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
        row = conn.execute("SELECT student_id, phone_verified_at FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
    if row is None or not row["student_id"]:
        raise ValueError("This signup is incomplete")
    if not row["phone_verified_at"]:
        raise ValueError("Verify your phone number first")
    return int(row["student_id"])
