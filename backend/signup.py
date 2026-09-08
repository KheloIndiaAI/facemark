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
# In its own table, not in auth_sessions: those are LOGIN sessions and
# current_user resolves them, so a signup token there would become a way to be
# signed in as a pending account.
#
# And in the DATABASE, not a dict. A dict was a per-worker store, and the
# Dockerfile runs two workers - so a signup whose next request happened to land
# on the other one was told it had expired, about half the time, on both
# deployments. The original reasoning covered restarts and not workers.


def _new_token(user_id: int, phone: str) -> str:
    tok = secrets.token_urlsafe(32)
    now = config.local_now().replace(tzinfo=None)
    expires = (now + timedelta(minutes=SIGNUP_TOKEN_TTL_MINUTES)).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            "INSERT INTO signup_tokens (token, user_id, phone, decided, expires_at, created_at) "
            "VALUES (?,?,?,0,?,?)",
            (tok, int(user_id), phone, expires, config.now_stamp()),
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
            "SELECT t.token, t.user_id, t.phone, t.decided, t.expires_at, u.status "
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
    return {"user_id": int(rec["user_id"]), "phone": rec["phone"]}


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
            "needs_coach": role == "athlete",
            # The SERVER decides which steps there are. The browser asking
            # config itself would be a second place for the answer to live, and
            # the two would disagree the first time one of them changed.
            "needs_otp": config.require_phone_otp()}


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
        row = conn.execute(
            "SELECT 1 FROM students s WHERE s.id = ? AND s.role = 'coach'"
            + _APPROVED_COACH, (int(coach_id),)
        ).fetchone()
        if not row:
            raise ValueError("That coach is not available to choose")
        conn.execute("UPDATE users SET chosen_coach_id = ? WHERE id = ?",
                     (int(coach_id), rec["user_id"]))


def send_otp(token: str, ip: str = "") -> dict:
    """Create and deliver a one-time code. The code is never returned."""
    if not config.require_phone_otp():
        # Refused rather than quietly issuing a code, so a client that has not
        # noticed the step is gone gets told, instead of sending somebody to
        # wait for a message that cannot arrive.
        raise ValueError("Phone verification is switched off at the moment.")
    rec = resolve_signup(token)
    phone = rec["phone"]

    # Counted from the table, not a per-worker dict: this is the limit with a
    # bill attached, and two workers each allowing five is ten messages an hour
    # to somebody's phone.
    with connect() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM otp_challenges WHERE phone = ? AND created_at > ?",
            (phone, (config.local_now() - timedelta(hours=1))
             .replace(tzinfo=None).isoformat(timespec="seconds")),
        ).fetchone()[0]
    if recent >= OTP_PER_NUMBER_PER_HOUR:
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
    delivered = _deliver(phone, code)
    # The truth, not "sent: true" regardless. A screen that says "check your
    # phone" when nothing was sent leaves somebody waiting for a message that
    # is never coming, and blaming their signal.
    return {"sent": delivered, "delivery": "sms" if delivered else "not_configured",
            "expires_in_minutes": OTP_TTL_MINUTES}


def _hash_code(phone: str, code: str) -> str:
    """Salted with the number, so one rainbow table does not cover every code."""
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


OTP_MESSAGE = ("{code} is your FaceMark verification code. It expires in "
               "{minutes} minutes. Do not share it with anyone.")


def _deliver(phone: str, code: str) -> bool:
    """Hand the code to a delivery channel. True if it actually went somewhere.

    The code is deliberately NEVER returned to the caller: an endpoint that
    hands back its own OTP "for testing" is one deploy away from doing it in
    production. It is not logged either once a provider is configured - a code
    in a log file is a code somebody can read.

    Returns False when no provider is configured, and the caller passes that
    honesty up to the screen instead of saying "check your phone".
    """
    text = OTP_MESSAGE.format(code=code, minutes=OTP_TTL_MINUTES)
    if config.SMS_PROVIDER == "webhook" and config.SMS_WEBHOOK_URL:
        try:
            import json as _json
            import urllib.request

            payload = {"phone": phone, "message": text,
                       "sender_id": config.SMS_SENDER_ID,
                       "entity_id": config.DLT_ENTITY_ID,
                       "template_id": config.DLT_TEMPLATE_ID}
            req = urllib.request.Request(
                config.SMS_WEBHOOK_URL,
                data=_json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         **({"Authorization": f"Bearer {config.SMS_WEBHOOK_TOKEN}"}
                            if config.SMS_WEBHOOK_TOKEN else {})},
                method="POST")
            with urllib.request.urlopen(req, timeout=config.SMS_TIMEOUT_SECONDS) as r:
                if 200 <= r.status < 300:
                    log.info("OTP delivered to %s via the SMS webhook.", _redact(phone))
                    return True
                log.error("SMS webhook refused the message for %s: HTTP %s",
                          _redact(phone), r.status)
        except Exception as e:                # noqa: BLE001
            log.error("SMS webhook failed for %s: %s", _redact(phone), e)
        return False

    # Nothing configured. Logged so a pilot can still be run by reading the
    # server log, and at WARNING so it is obvious this is not a real deployment.
    log.warning("NO SMS PROVIDER: the code for %s is %s. Set FACEMARK_SMS_PROVIDER "
                "once DLT registration is done - see DEVELOPMENT.md.", phone, code)
    return False


def _redact(phone: str) -> str:
    """Enough of a number to match it to a person, not enough to dial it."""
    p = (phone or "").strip()
    return ("*" * max(0, len(p) - 4)) + p[-4:] if len(p) > 4 else "****"


def verify_otp(token: str, code: str) -> bool:
    if not config.require_phone_otp():
        raise ValueError("Phone verification is switched off at the moment.")
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


# =============================================================================
# Password reset
# =============================================================================
# Deliberately keyed on the username plus the code rather than on a reset token:
# the code IS the credential, it already expires, already counts attempts and is
# already stored hashed. A token would be a second secret to get right.

RESET_PER_ACCOUNT_PER_HOUR = 3


def start_reset(username: str, ip: str = "") -> dict:
    """Send a reset code, if that account exists and has a verified phone.

    Always reports the same thing. A caller must not be able to tell a real
    username from a made-up one by the answer they get.
    """
    # Must be byte-identical to what a REAL account produces, not merely the
    # same shape. An earlier version answered {"sent": true, "delivery":
    # "unknown"} here while a real account with no SMS provider answered
    # {"sent": false, "delivery": "not_configured"} - which told a caller
    # exactly which usernames exist. Derived from the same config the real path
    # reads, so the two cannot drift apart again.
    _sms = config.sms_configured()
    quiet = {"sent": _sms, "delivery": "sms" if _sms else "not_configured"}
    if not _sms:
        # No channel, so no self-service reset. Refused for every username
        # alike - a real account and a made-up one get the same answer, which
        # is the property that matters here.
        raise LookupError(
            "Password reset by text message is not available yet. "
            "Ask your coach or an administrator to reset it for you.")
    if _throttled(f"reset:ip:{ip}", OTP_PER_IP_PER_HOUR, 3600):
        raise PermissionError("Too many reset attempts from this connection. Try later.")

    user = auth.get_user_by_username(username or "")
    if not user or not user.get("phone") or not user.get("phone_verified_at"):
        # No phone, no account, or a phone never verified: nothing to send to.
        # Same answer either way.
        log.info("Password reset requested for an account that cannot use it (%r)",
                 (username or "")[:40])
        return quiet
    if (user.get("status") or "active") != "active":
        log.info("Password reset requested for a %s account", user.get("status"))
        return quiet

    phone = user["phone"]
    now = config.local_now()
    with connect() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM otp_challenges WHERE phone = ? AND created_at > ?",
            (phone, (now - timedelta(hours=1)).replace(tzinfo=None)
             .isoformat(timespec="seconds")),
        ).fetchone()[0]
        if recent >= RESET_PER_ACCOUNT_PER_HOUR + OTP_PER_NUMBER_PER_HOUR:
            raise PermissionError("Too many codes for that number. Try again later.")
        code = f"{secrets.randbelow(1000000):06d}"
        expires = (now + timedelta(minutes=OTP_TTL_MINUTES)) \
            .replace(tzinfo=None).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO otp_challenges (phone, code_hash, expires_at, attempts, created_at) "
            "VALUES (?,?,?,0,?)",
            (phone, _hash_code(phone, code), expires, config.now_stamp()),
        )
    delivered = _deliver(phone, code)
    return {"sent": delivered, "delivery": "sms" if delivered else "not_configured"}


def complete_reset(username: str, code: str, new_password: str) -> None:
    """Check the code and set the password. Raises ValueError with the reason.

    One step rather than verify-then-set: a separate verify would have to hand
    back something proving it happened, and that something is another credential
    to protect. Here the code is spent exactly when the password changes.
    """
    user = auth.get_user_by_username(username or "")
    # Checked before the code so a wrong username cannot consume somebody's
    # attempts, but reported identically so it still reveals nothing.
    if not config.sms_configured():
        raise ValueError(
            "Password reset by text message is not available yet. "
            "Ask your coach or an administrator to reset it for you.")
    generic = "That code is not right, or it has expired."
    if not user or not user.get("phone") or not user.get("phone_verified_at"):
        raise ValueError(generic)
    if (user.get("status") or "active") != "active":
        raise ValueError(generic)

    phone = user["phone"]
    now = config.local_now().replace(tzinfo=None).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT id, code_hash, expires_at, attempts, consumed_at FROM otp_challenges "
            "WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
        if row is None or row["consumed_at"] or row["expires_at"] < now:
            raise ValueError(generic)
        if int(row["attempts"]) >= OTP_MAX_ATTEMPTS:
            raise ValueError("Too many wrong attempts. Ask for a new code.")
        # Counted before comparing, so a crash cannot buy a free guess.
        conn.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?",
                     (row["id"],))
        if not secrets.compare_digest(row["code_hash"],
                                      _hash_code(phone, (code or "").strip())):
            raise ValueError(generic)
        conn.execute("UPDATE otp_challenges SET consumed_at = ? WHERE id = ?",
                     (config.now_stamp(), row["id"]))

    # change_password revokes every live session for this account, which is the
    # point: whoever prompted the reset must not still be signed in elsewhere.
    auth.change_password(int(user["id"]), new_password)
    log.warning("Password reset completed for account %s via phone code.", user["id"])


def student_for(token: str) -> int:
    """The person id behind a signup token, for the face-capture step."""
    rec = resolve_signup(token)
    with connect() as conn:
        row = conn.execute("SELECT student_id, phone_verified_at FROM users WHERE id = ?",
                           (rec["user_id"],)).fetchone()
    if row is None or not row["student_id"]:
        raise ValueError("This signup is incomplete")
    # Only when there is a working way to deliver a code. Otherwise this
    # refuses every signup for failing a step nobody could complete.
    if config.require_phone_otp() and not row["phone_verified_at"]:
        raise ValueError("Verify your phone number first")
    return int(row["student_id"])
