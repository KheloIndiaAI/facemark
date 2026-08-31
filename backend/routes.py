"""API routes for auth, users, centres and people.

Kept out of main.py, which already owns the recognition pipeline. Every route
that reads centre-scoped data passes the caller through `auth.scope_centre`, so
a coach is narrowed to their own centre in SQL rather than by hiding UI.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

from . import auth, centres as centres_mod, config, database

log = logging.getLogger("routes")
router = APIRouter(prefix="/api")


# =============================================================================
# Auth
# =============================================================================

@router.post("/auth/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    result = auth.login(username, password)
    if not result:
        # Deliberately identical for unknown user, wrong password and disabled
        # account: distinguishing them tells an attacker which usernames exist.
        raise HTTPException(401, "Incorrect username or password")
    response.set_cookie(
        "facemark_token", result["token"],
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite=config.COOKIE_SAMESITE,
        max_age=auth.SESSION_TTL_HOURS * 3600,
    )
    return {"ok": True, **result}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("facemark_token") or ""
    if token:
        auth.logout(token)
    response.delete_cookie("facemark_token")
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(auth.current_user)):
    return {"user": auth.public_user(user)}


@router.post("/auth/password")
def change_own_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: dict = Depends(auth.current_user),
):
    if not auth.verify_password(current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    try:
        auth.change_password(user["id"], new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "message": "Password changed - sign in again"}


# --- user management (super admin only) --------------------------------------

@router.get("/users")
def get_users(user: dict = Depends(auth.require_super_admin)):
    return {"users": auth.list_users()}


@router.post("/users")
def add_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    full_name: str = Form(...),
    centre_id: Optional[int] = Form(None),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    user: dict = Depends(auth.require_super_admin),
):
    try:
        uid = auth.create_user(username, password, role, full_name, centre_id, email, phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # UNIQUE violation on username
        raise HTTPException(409, f"Username '{username}' is already taken") from e
    return {"ok": True, "user_id": uid}


@router.patch("/users/{user_id}/active")
def toggle_user(user_id: int, active: bool = Form(...), user: dict = Depends(auth.require_super_admin)):
    if user_id == user["id"] and not active:
        raise HTTPException(400, "You cannot deactivate your own account")
    auth.set_user_active(user_id, active)
    return {"ok": True}


@router.post("/users/{user_id}/password")
def reset_password(user_id: int, new_password: str = Form(...), user: dict = Depends(auth.require_super_admin)):
    try:
        auth.change_password(user_id, new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/users/{user_id}")
def remove_user(user_id: int, user: dict = Depends(auth.require_super_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "You cannot delete your own account")
    auth.delete_user(user_id)
    return {"ok": True}


# =============================================================================
# Centres
# =============================================================================

@router.get("/centres")
def get_centres(
    q: str = "",
    state: Optional[str] = None,
    sport: Optional[str] = None,
    user: dict = Depends(auth.current_user),
):
    scope = auth.scope_centre(user, None)
    rows = centres_mod.search_centres(query=q, state=state, sport=sport, centre_id=scope)
    return {
        "centres": rows,
        "count": len(rows),
        "states": centres_mod.distinct_states(),
        "sports": centres_mod.distinct_sports(),
    }


@router.get("/centres/{centre_id}")
def get_centre_detail(centre_id: int, user: dict = Depends(auth.current_user)):
    auth.scope_centre(user, centre_id)      # raises 403 for a coach from elsewhere
    detail = centres_mod.centre_detail(centre_id)
    if not detail:
        raise HTTPException(404, "Centre not found")
    for group in ("athletes", "coaches"):
        for p in detail[group]:
            if p.get("photo_path"):
                p["photo_url"] = f"/api/photos/{Path(p['photo_path']).name}"
    return detail


@router.post("/centres")
def add_centre(
    code: str = Form(...),
    name: str = Form(...),
    centre_type: str = Form("KIC"),
    state: Optional[str] = Form(None),
    district: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    pincode: Optional[str] = Form(None),
    sports: Optional[str] = Form(None),
    capacity: int = Form(0),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    geofence_m: int = Form(300),
    incharge_name: Optional[str] = Form(None),
    contact_phone: Optional[str] = Form(None),
    contact_email: Optional[str] = Form(None),
    established: Optional[str] = Form(None),
    user: dict = Depends(auth.require_super_admin),
):
    sport_list = [s.strip() for s in (sports or "").split(",") if s.strip()]
    try:
        cid = centres_mod.create_centre(
            code=code, name=name, centre_type=centre_type, state=state, district=district,
            address=address, pincode=pincode, sports=sport_list, capacity=capacity,
            latitude=latitude, longitude=longitude, geofence_m=geofence_m,
            incharge_name=incharge_name, contact_phone=contact_phone,
            contact_email=contact_email, established=established, is_demo=False,
        )
    except Exception as e:
        raise HTTPException(409, f"Centre code '{code}' already exists") from e
    return {"ok": True, "centre_id": cid}


@router.patch("/centres/{centre_id}")
async def edit_centre(centre_id: int, request: Request, user: dict = Depends(auth.require_super_admin)):
    form = dict(await request.form())
    if "sports" in form and isinstance(form["sports"], str):
        form["sports"] = [s.strip() for s in form["sports"].split(",") if s.strip()]
    for k in ("latitude", "longitude"):
        if form.get(k) not in (None, ""):
            form[k] = float(form[k])
    for k in ("capacity", "geofence_m"):
        if form.get(k) not in (None, ""):
            form[k] = int(form[k])
    centres_mod.update_centre(centre_id, **form)
    return {"ok": True}


@router.delete("/centres/{centre_id}")
def remove_centre(centre_id: int, user: dict = Depends(auth.require_super_admin)):
    centres_mod.delete_centre(centre_id)
    return {"ok": True}


@router.delete("/centres/demo/all")
def purge_demo(user: dict = Depends(auth.require_super_admin)):
    return {"ok": True, "deleted": centres_mod.delete_demo_centres()}


@router.post("/centres/import")
async def import_centres(file: UploadFile = File(...), user: dict = Depends(auth.require_super_admin)):
    """Bulk-load real centres from CSV or JSON, replacing the demo placeholders."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    name = (file.filename or "").lower()
    try:
        rows = json.loads(raw) if name.endswith(".json") else list(csv.DictReader(io.StringIO(raw)))
    except (json.JSONDecodeError, csv.Error) as e:
        raise HTTPException(400, f"Could not parse {file.filename}: {e}")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(400, "File contained no rows")
    added = centres_mod.import_centres(rows)
    return {"ok": True, "imported": added, "skipped": len(rows) - added}


# =============================================================================
# People (athletes + coaches share the recognition gallery)
# =============================================================================

@router.get("/people")
def list_people(
    role: Optional[str] = None,
    centre_id: Optional[int] = None,
    user: dict = Depends(auth.current_user),
):
    scope = auth.scope_centre(user, centre_id)
    q = (
        "SELECT s.id, s.name, s.roll_no, s.photo_path, s.role, s.gender, s.sport, "
        "s.phone, s.centre_id, c.name AS centre_name, "
        "(SELECT COUNT(*) FROM attendance a WHERE a.student_id = s.id) AS total_present, "
        "(SELECT COUNT(*) FROM templates t WHERE t.student_id = s.id) AS templates "
        "FROM students s LEFT JOIN centres c ON c.id = s.centre_id WHERE 1=1"
    )
    p: list = []
    if role in ("athlete", "coach"):
        q += " AND s.role = ?"
        p.append(role)
    if scope is not None:
        q += " AND s.centre_id = ?"
        p.append(scope)
    q += " ORDER BY s.role, s.name"
    with database.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    for r in rows:
        r["photo_url"] = f"/api/photos/{Path(r['photo_path']).name}" if r.get("photo_path") else None
    return {"people": rows, "count": len(rows)}


@router.patch("/people/{student_id}")
async def update_person(student_id: int, request: Request, user: dict = Depends(auth.current_user)):
    """Edit profile fields. A coach may only edit people at their own centre."""
    with database.connect() as conn:
        row = conn.execute("SELECT centre_id FROM students WHERE id = ?", (student_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Person not found")
    auth.scope_centre(user, row["centre_id"])

    form = dict(await request.form())
    allowed = {"name", "role", "centre_id", "gender", "sport", "phone", "roll_no"}
    sets, params = [], []
    for k, v in form.items():
        if k not in allowed or v == "":
            continue
        if k == "role" and v not in ("athlete", "coach"):
            raise HTTPException(400, "role must be 'athlete' or 'coach'")
        if k == "centre_id":
            v = int(v)
            auth.scope_centre(user, v)      # a coach cannot move someone elsewhere
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return {"ok": True, "updated": 0}
    params.append(student_id)
    with database.connect() as conn:
        conn.execute(f"UPDATE students SET {', '.join(sets)} WHERE id = ?", params)
    return {"ok": True, "updated": len(sets)}
