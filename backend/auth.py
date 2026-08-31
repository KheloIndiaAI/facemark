"""Authentication and role-based access control.

Two roles, deliberately different in what they can see:

  super_admin - every centre, every athlete and coach, user management
                and centre management.
  coach       - exactly one centre. Every query a coach makes is narrowed to
                their `centre_id`, enforced server-side rather than by hiding
                buttons in the UI.

Passwords are PBKDF2-HMAC-SHA256 (600k iterations, per-user random salt) via
hashlib, so there is no new dependency and no plaintext ever reaches the disk.
Sessions are opaque random tokens stored server-side, which means logout and
deactivation take effect immediately - a self-contained JWT could not be
revoked before it expired.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request

from . import database

log = logging.getLogger("auth")

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_HOURS = 12
TOKEN_BYTES = 32


# --- password hashing --------------------------------------------------------

def hash_password(password: str) -> str:
    """-> 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'."""
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification; False on any malformed record."""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# --- users -------------------------------------------------------------------

def create_user(
    username: str,
    password: str,
    role: str,
    full_name: str,
    centre_id: Optional[int] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    student_id: Optional[int] = None,
) -> int:
    if role not in ("super_admin", "coach"):
        raise ValueError(f"Unknown role: {role}")
    if role == "coach" and centre_id is None:
        raise ValueError("A coach must be assigned to a centre")
    now = datetime.now().isoformat(timespec="seconds")
    with database.connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, email, phone, "
            "centre_id, student_id, is_active, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (
                username.strip().lower(), hash_password(password), role, full_name.strip(),
                email, phone, centre_id, student_id, now,
            ),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> Optional[dict]:
    """Join the centre so the login response carries centre_name, matching what
    /auth/me returns. Without the join a coach's session started life labelled
    'unassigned' until the next page load."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT u.*, c.name AS centre_name FROM users u "
            "LEFT JOIN centres c ON c.id = u.centre_id WHERE u.username = ?",
            (username.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None


def list_users(centre_id: Optional[int] = None) -> list:
    q = (
        "SELECT u.id, u.username, u.role, u.full_name, u.email, u.phone, u.centre_id, "
        "u.is_active, u.last_login, u.created_at, c.name AS centre_name "
        "FROM users u LEFT JOIN centres c ON c.id = u.centre_id"
    )
    params = []
    if centre_id is not None:
        q += " WHERE u.centre_id = ?"
        params.append(centre_id)
    q += " ORDER BY u.role, u.full_name"
    with database.connect() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def set_user_active(user_id: int, active: bool) -> None:
    with database.connect() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(active), user_id))
        if not active:  # revoke live sessions immediately
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def change_password(user_id: int, new_password: str) -> None:
    with database.connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id: int) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- sessions ----------------------------------------------------------------

def login(username: str, password: str) -> Optional[dict]:
    """Verify credentials and open a session. Returns {token, user} or None."""
    user = get_user_by_username(username)
    if not user or not user["is_active"]:
        return None
    if not verify_password(password, user["password_hash"]):
        return None

    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = datetime.now()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
            (
                token, user["id"],
                (now + timedelta(hours=SESSION_TTL_HOURS)).isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (now.isoformat(timespec="seconds"), user["id"]),
        )
        conn.execute(
            "DELETE FROM auth_sessions WHERE expires_at < ?",
            (now.isoformat(timespec="seconds"),),
        )
    return {"token": token, "user": public_user(user)}


def logout(token: str) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


def resolve_token(token: str) -> Optional[dict]:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at, c.name AS centre_name FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id "
            "LEFT JOIN centres c ON c.id = u.centre_id "
            "WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        logout(token)
        return None
    if not row["is_active"]:
        return None
    return dict(row)


def public_user(user: dict) -> dict:
    """The user object safe to send to the browser (no hash, no salt)."""
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "full_name": user["full_name"],
        "email": user.get("email"),
        "centre_id": user.get("centre_id"),
        "centre_name": user.get("centre_name"),
        "is_super_admin": user["role"] == "super_admin",
    }


# --- FastAPI dependencies ----------------------------------------------------

def _token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get("facemark_token")


def current_user(request: Request) -> dict:
    """Require any authenticated user."""
    token = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Sign in to continue")
    user = resolve_token(token)
    if not user:
        raise HTTPException(401, "Session expired - sign in again")
    return user


def require_super_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "super_admin":
        raise HTTPException(403, "This action is restricted to super admins")
    return user


def scope_centre(user: dict, requested: Optional[int] = None) -> Optional[int]:
    """The centre id a request may actually read.

    A coach is pinned to their own centre no matter what the client asks for -
    passing someone else's centre_id in the query string must not widen access.
    A super admin gets whatever they asked for, or None meaning "all centres".
    """
    if user["role"] == "super_admin":
        return requested
    if requested is not None and requested != user["centre_id"]:
        raise HTTPException(403, "You can only access your own centre")
    return user["centre_id"]


def bootstrap_default_admin() -> Optional[str]:
    """Create the first super admin if no users exist yet.

    The generated password is returned once so it can be printed to the server
    console; it is never stored in plaintext. Override via FACEMARK_ADMIN_PASSWORD.
    """
    with database.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
    password = os.environ.get("FACEMARK_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
    create_user(
        username="admin",
        password=password,
        role="super_admin",
        full_name="System Administrator",
    )
    return password
