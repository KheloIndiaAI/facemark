"""Khelo India centre registry, search, and geo-fencing maths.

IMPORTANT - the seeded rows are NOT real government data. `seed_demo_centres()`
inserts clearly-marked placeholders (every code starts with DEMO- and `is_demo`
is 1) purely so the search UI has something to show before real records are
loaded. They must never be presented as genuine Khelo India records. Load real
data through the super-admin centre form or `import_centres()`, and drop the
placeholders with `delete_demo_centres()`.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import List, Optional

from . import config, database

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS-84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _row(r) -> dict:
    d = dict(r)
    try:
        d["sports"] = json.loads(d["sports"]) if d.get("sports") else []
    except (json.JSONDecodeError, TypeError):
        d["sports"] = []
    d["is_demo"] = bool(d.get("is_demo"))
    return d


# --- CRUD --------------------------------------------------------------------

def create_centre(
    code: str,
    name: str,
    centre_type: str = "KIC",
    state: Optional[str] = None,
    district: Optional[str] = None,
    address: Optional[str] = None,
    pincode: Optional[str] = None,
    sports: Optional[List[str]] = None,
    capacity: int = 0,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    geofence_m: int = 300,
    incharge_name: Optional[str] = None,
    contact_phone: Optional[str] = None,
    contact_email: Optional[str] = None,
    established: Optional[str] = None,
    is_demo: bool = False,
) -> int:
    now = config.now_stamp()
    with database.connect() as conn:
        return conn.insert(
            "INSERT INTO centres (code, name, centre_type, state, district, address, pincode, "
            "sports, capacity, latitude, longitude, geofence_m, incharge_name, contact_phone, "
            "contact_email, established, is_demo, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code.strip().upper(), name.strip(), centre_type, state, district, address,
                pincode, json.dumps(sports or []), capacity, latitude, longitude, geofence_m,
                incharge_name, contact_phone, contact_email, established, int(is_demo), now,
            ),
        )


def update_centre(centre_id: int, **fields) -> None:
    allowed = {
        "code", "name", "centre_type", "state", "district", "address", "pincode",
        "sports", "capacity", "latitude", "longitude", "geofence_m",
        "incharge_name", "contact_phone", "contact_email", "established",
    }
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "sports" and isinstance(v, list):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return
    params.append(centre_id)
    with database.connect() as conn:
        conn.execute(f"UPDATE centres SET {', '.join(sets)} WHERE id = ?", params)


def delete_centre(centre_id: int) -> None:
    with database.connect() as conn:
        conn.execute("DELETE FROM centres WHERE id = ?", (centre_id,))


def get_centre(centre_id: int) -> Optional[dict]:
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM centres WHERE id = ?", (centre_id,)).fetchone()
        return _row(row) if row else None


def search_centres(
    query: str = "",
    state: Optional[str] = None,
    sport: Optional[str] = None,
    centre_id: Optional[int] = None,
    limit: int = 100,
) -> List[dict]:
    """Free-text search across code, name, state, district, address and sports."""
    sql = "SELECT * FROM centres WHERE 1=1"
    params: list = []
    if centre_id is not None:            # a coach is pinned to one centre
        sql += " AND id = ?"
        params.append(centre_id)
    q = (query or "").strip()
    if q:
        sql += (
            " AND (code LIKE ? OR name LIKE ? OR state LIKE ? OR district LIKE ?"
            " OR address LIKE ? OR sports LIKE ? OR incharge_name LIKE ?)"
        )
        params += [f"%{q}%"] * 7
    if state:
        sql += " AND state = ?"
        params.append(state)
    if sport:
        sql += " AND sports LIKE ?"
        params.append(f"%{sport}%")
    sql += " ORDER BY name LIMIT ?"
    params.append(limit)
    with database.connect() as conn:
        rows = [_row(r) for r in conn.execute(sql, params).fetchall()]
        # Enrolled headcount travels with each centre so the UI can default to
        # the one actually in use rather than guessing from a demo flag.
        # Built column-by-column rather than dict(rows). A row is now a mapping,
        # so dict() over a list of them would consume each row's column NAMES as
        # the key/value pair and silently produce {"centre_id": "count"}.
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT centre_id, COUNT(*) FROM students "
            "WHERE centre_id IS NOT NULL GROUP BY centre_id").fetchall()}
    for r in rows:
        r["people_count"] = counts.get(r["id"], 0)
    return rows


def centre_detail(centre_id: int) -> Optional[dict]:
    """Full profile: the centre plus its roster, staff and attendance summary."""
    centre = get_centre(centre_id)
    if not centre:
        return None
    with database.connect() as conn:
        centre["athletes"] = [dict(r) for r in conn.execute(
            "SELECT id, name, roll_no, gender, sport, photo_path, role "
            "FROM students WHERE centre_id = ? AND role = 'athlete' ORDER BY name",
            (centre_id,),
        ).fetchall()]
        centre["coaches"] = [dict(r) for r in conn.execute(
            "SELECT id, name, roll_no, gender, sport, photo_path, role "
            "FROM students WHERE centre_id = ? AND role = 'coach' ORDER BY name",
            (centre_id,),
        ).fetchall()]
        centre["staff_accounts"] = [dict(r) for r in conn.execute(
            "SELECT id, username, full_name, role, is_active, last_login "
            "FROM users WHERE centre_id = ? ORDER BY role, full_name",
            (centre_id,),
        ).fetchall()]
        centre["attendance_days"] = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM attendance WHERE centre_id = ?", (centre_id,)
        ).fetchone()[0]
        centre["attendance_records"] = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE centre_id = ?", (centre_id,)
        ).fetchone()[0]
        centre["recent_attendance"] = [dict(r) for r in conn.execute(
            "SELECT a.date, COUNT(*) AS present FROM attendance a "
            "WHERE a.centre_id = ? GROUP BY a.date ORDER BY a.date DESC LIMIT 14",
            (centre_id,),
        ).fetchall()]
    centre["athlete_count"] = len(centre["athletes"])
    centre["coach_count"] = len(centre["coaches"])
    return centre


def distinct_states() -> List[str]:
    with database.connect() as conn:
        return [
            r[0] for r in conn.execute(
                "SELECT DISTINCT state FROM centres WHERE state IS NOT NULL AND state != '' "
                "ORDER BY state"
            ).fetchall()
        ]


def distinct_sports() -> List[str]:
    out = set()
    for c in search_centres(limit=10_000):
        out.update(c["sports"])
    return sorted(out)


# --- geo-fencing -------------------------------------------------------------

def evaluate_location(
    centre_id: Optional[int],
    latitude: Optional[float],
    longitude: Optional[float],
) -> dict:
    """Classify a capture location against its centre's geo-fence.

    Returns geo_status: no_fix (browser gave no coordinates), unknown (the centre
    has no coordinates on file, so nothing to compare against), inside, outside.
    """
    if latitude is None or longitude is None:
        return {"geo_status": "no_fix", "distance_m": None}
    if centre_id is None:
        return {"geo_status": "unknown", "distance_m": None}
    centre = get_centre(centre_id)
    if not centre or centre.get("latitude") is None or centre.get("longitude") is None:
        return {"geo_status": "unknown", "distance_m": None}
    dist = haversine_m(latitude, longitude, centre["latitude"], centre["longitude"])
    fence = centre.get("geofence_m") or 300
    return {
        "geo_status": "inside" if dist <= fence else "outside",
        "distance_m": round(dist, 1),
    }


# --- demo data ---------------------------------------------------------------

# Placeholders only. Names are generic ("Demo Centre - Delhi"), codes are prefixed
# DEMO-, and is_demo=1 so the UI can badge them and an admin can purge them.
_DEMO = [
    ("DEMO-DL-01", "Demo Centre - New Delhi", "Delhi", "South West Delhi",
     "Placeholder address, New Delhi", "110010", ["Athletics", "Wrestling", "Boxing"],
     120, 28.5921, 77.1691, "Demo Incharge A"),
    ("DEMO-MH-01", "Demo Centre - Pune", "Maharashtra", "Pune",
     "Placeholder address, Pune", "411001", ["Hockey", "Athletics", "Swimming"],
     150, 18.5204, 73.8567, "Demo Incharge B"),
    ("DEMO-HR-01", "Demo Centre - Sonipat", "Haryana", "Sonipat",
     "Placeholder address, Sonipat", "131001", ["Wrestling", "Boxing", "Kabaddi"],
     200, 28.9931, 77.0151, "Demo Incharge C"),
    ("DEMO-KA-01", "Demo Centre - Bengaluru", "Karnataka", "Bengaluru Urban",
     "Placeholder address, Bengaluru", "560001", ["Swimming", "Badminton", "Athletics"],
     180, 12.9716, 77.5946, "Demo Incharge D"),
    ("DEMO-MN-01", "Demo Centre - Imphal", "Manipur", "Imphal West",
     "Placeholder address, Imphal", "795001", ["Boxing", "Weightlifting", "Football"],
     90, 24.8170, 93.9368, "Demo Incharge E"),
    ("DEMO-KL-01", "Demo Centre - Thiruvananthapuram", "Kerala", "Thiruvananthapuram",
     "Placeholder address, Thiruvananthapuram", "695001", ["Athletics", "Volleyball", "Football"],
     110, 8.5241, 76.9366, "Demo Incharge F"),
    ("DEMO-PB-01", "Demo Centre - Jalandhar", "Punjab", "Jalandhar",
     "Placeholder address, Jalandhar", "144001", ["Hockey", "Athletics", "Kabaddi"],
     140, 31.3260, 75.5762, "Demo Incharge G"),
    ("DEMO-AS-01", "Demo Centre - Guwahati", "Assam", "Kamrup Metropolitan",
     "Placeholder address, Guwahati", "781001", ["Football", "Boxing", "Athletics"],
     100, 26.1445, 91.7362, "Demo Incharge H"),
]


def seed_demo_centres() -> int:
    """Insert the placeholder centres if the registry is empty. Returns count added."""
    with database.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM centres").fetchone()[0]:
            return 0
    n = 0
    for code, name, state, district, addr, pin, sports, cap, lat, lng, incharge in _DEMO:
        create_centre(
            code=code, name=name, state=state, district=district, address=addr,
            pincode=pin, sports=sports, capacity=cap, latitude=lat, longitude=lng,
            incharge_name=incharge, contact_email="demo@example.invalid",
            contact_phone="+91-00000-00000", established="2020",
            is_demo=True,
        )
        n += 1
    return n


def delete_demo_centres() -> int:
    with database.connect() as conn:
        return conn.execute("DELETE FROM centres WHERE is_demo = 1").rowcount


def import_centres(rows: List[dict]) -> int:
    """Bulk-load real centres. Each row needs at least `code` and `name`."""
    n = 0
    for r in rows:
        if not r.get("code") or not r.get("name"):
            continue
        sports = r.get("sports")
        if isinstance(sports, str):
            sports = [s.strip() for s in sports.split(",") if s.strip()]
        create_centre(
            code=r["code"], name=r["name"], centre_type=r.get("centre_type", "KIC"),
            state=r.get("state"), district=r.get("district"), address=r.get("address"),
            pincode=r.get("pincode"), sports=sports, capacity=int(r.get("capacity") or 0),
            latitude=_f(r.get("latitude")), longitude=_f(r.get("longitude")),
            geofence_m=int(r.get("geofence_m") or 300),
            incharge_name=r.get("incharge_name"), contact_phone=r.get("contact_phone"),
            contact_email=r.get("contact_email"), established=r.get("established"),
            is_demo=False,
        )
        n += 1
    return n


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
