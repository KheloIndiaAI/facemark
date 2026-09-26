"""Attendance reports: every upload, every record, and the totals per centre,
over a date range.

Read-only. Everything here is the raw stored values; turning a cosine score
into the percentage a screen shows is main._shown_confidence's job, so the
report and every other screen give one match the same number.

"Recognised" on an upload is faces the recogniser matched to somebody on the
roster, out of faces it found in the photo. That is a recognition rate, not a
verified accuracy: a face matched to the wrong person counts as recognised,
and nobody here knows who was really in the photo.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import List, Optional

from .db import connect

# The widest range one request may cover. A year of a busy centre is tens of
# thousands of rows - still one page, but no further.
MAX_DAYS = 366
UPLOAD_KINDS = ("group", "single", "self")


def parse_range(date_from: Optional[str], date_to: Optional[str], today: str) -> tuple:
    """(from, to) as YYYY-MM-DD. Defaults to the last 7 days. Raises ValueError
    with a sentence fit for the screen."""
    try:
        end = date.fromisoformat(date_to) if date_to else date.fromisoformat(today)
        start = date.fromisoformat(date_from) if date_from else end - timedelta(days=6)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD") from None
    if start > end:
        raise ValueError("The start date is after the end date")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"Choose a range of {MAX_DAYS} days or fewer")
    return start.isoformat(), end.isoformat()


def _where(alias_date: str, alias_centre: str, centre_id: Optional[int]):
    sql = f" WHERE {alias_date} BETWEEN ? AND ?"
    extra = []
    if centre_id is not None:
        sql += f" AND {alias_centre} = ?"
        extra.append(int(centre_id))
    return sql, extra


def centre_summary(d_from: str, d_to: str, centre_id: Optional[int]) -> List[dict]:
    """One row per centre: attendance marked, and uploads by kind with their
    faces found / recognised. Every centre is listed, zeros included - a centre
    marking nothing is part of the answer to "who marked how much"."""
    with connect() as conn:
        cq = "SELECT id, name, code, is_demo FROM centres"
        cp: list = []
        if centre_id is not None:
            cq += " WHERE id = ?"
            cp.append(int(centre_id))
        centres = {int(r["id"]): {
            "centre_id": int(r["id"]), "centre_name": r["name"], "centre_code": r["code"],
            "is_demo": bool(r["is_demo"]),
            "confirmed": 0, "drafts": 0, "people": 0, "days": 0, "no_face_scan": 0,
            "uploads": {k: 0 for k in (*UPLOAD_KINDS, "older")},
            "faces_found": 0, "faces_recognised": 0,
        } for r in conn.execute(cq, cp).fetchall()}

        w, extra = _where("a.date", "a.centre_id", centre_id)
        for r in conn.execute(
                "SELECT a.centre_id, "
                "  SUM(CASE WHEN a.status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed, "
                "  SUM(CASE WHEN a.status = 'draft' THEN 1 ELSE 0 END) AS drafts, "
                "  COUNT(DISTINCT CASE WHEN a.status = 'confirmed' THEN a.student_id END) AS people, "
                "  COUNT(DISTINCT CASE WHEN a.status = 'confirmed' THEN a.date END) AS days, "
                "  SUM(CASE WHEN a.status = 'confirmed' AND COALESCE(a.confidence, 0) <= 0 "
                "      THEN 1 ELSE 0 END) AS no_face_scan "
                "FROM attendance a" + w + " AND a.centre_id IS NOT NULL GROUP BY a.centre_id",
                [d_from, d_to, *extra]).fetchall():
            c = centres.get(int(r["centre_id"]))
            if c:
                for k in ("confirmed", "drafts", "people", "days", "no_face_scan"):
                    c[k] = int(r[k] or 0)

        w, extra = _where("ses.date", "ses.centre_id", centre_id)
        for r in conn.execute(
                "SELECT ses.centre_id, cap.kind, COUNT(*) AS n, "
                "  COALESCE(SUM(cap.faces_detected), 0) AS faces, "
                "  COALESCE(SUM(cap.recognised), 0) AS recog "
                "FROM session_captures cap JOIN attendance_sessions ses ON ses.id = cap.session_id"
                + w + " GROUP BY ses.centre_id, cap.kind",
                [d_from, d_to, *extra]).fetchall():
            c = centres.get(int(r["centre_id"]))
            if not c:
                continue
            kind = r["kind"] if r["kind"] in UPLOAD_KINDS else "older"
            c["uploads"][kind] += int(r["n"])
            c["faces_found"] += int(r["faces"])
            c["faces_recognised"] += int(r["recog"])

    out = list(centres.values())
    for c in out:
        c["upload_count"] = sum(c["uploads"].values())
    # Most attendance first; demo placeholders sink below real centres.
    out.sort(key=lambda c: (c["is_demo"], -c["confirmed"], -c["upload_count"], c["centre_name"] or ""))
    return out


def uploads(d_from: str, d_to: str, centre_id: Optional[int], kind: Optional[str]) -> List[dict]:
    """Every uploaded photo in the range, newest first, with who it matched."""
    w, extra = _where("ses.date", "ses.centre_id", centre_id)
    if kind in UPLOAD_KINDS:
        w += " AND cap.kind = ?"
        extra.append(kind)
    with connect() as conn:
        rows = conn.execute(
            "SELECT cap.id, cap.media_key, cap.kind, cap.faces_detected, cap.recognised, "
            "  cap.matches, cap.created_at, cap.geo_status, cap.distance_m, "
            "  ses.id AS session_id, ses.date, ses.status AS register_status, ses.centre_id, "
            "  c.name AS centre_name, co.name AS coach_name, u.full_name AS uploaded_by_name "
            "FROM session_captures cap "
            "JOIN attendance_sessions ses ON ses.id = cap.session_id "
            "LEFT JOIN centres c ON c.id = ses.centre_id "
            "LEFT JOIN students co ON co.id = ses.coach_id "
            "LEFT JOIN users u ON u.id = cap.uploaded_by"
            + w + " ORDER BY cap.created_at DESC, cap.id DESC",
            [d_from, d_to, *extra]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["matches"] = json.loads(d["matches"]) if d.get("matches") else []
        except (TypeError, ValueError):
            d["matches"] = []
        out.append(d)
    return out


def records(d_from: str, d_to: str, centre_id: Optional[int]) -> List[dict]:
    """Every attendance row in the range - confirmed and still-draft - newest
    first. No cap: this is the full history the dashboard only samples."""
    w, extra = _where("a.date", "a.centre_id", centre_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.id, a.student_id, a.date, a.marked_at, a.status, a.origin, "
            "  a.confidence, a.geo_status, a.distance_m, a.centre_id, a.capture_id, "
            "  s.name, s.roll_no, s.role, c.name AS centre_name, co.name AS coach_name, "
            "  cap.kind AS capture_kind, cap.media_key AS capture_media "
            "FROM attendance a JOIN students s ON s.id = a.student_id "
            "LEFT JOIN centres c ON c.id = a.centre_id "
            "LEFT JOIN session_captures cap ON cap.id = a.capture_id "
            "LEFT JOIN attendance_sessions ses ON ses.id = a.session_id "
            "LEFT JOIN students co ON co.id = ses.coach_id"
            + w + " ORDER BY a.date DESC, a.marked_at DESC, a.id DESC",
            [d_from, d_to, *extra]).fetchall()]
