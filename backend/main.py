"""FastAPI application: attendance API + static frontend host.

Group-photo pipeline: detect faces with YuNet, embed each with SFace, score
against the multi-template gallery (max-pooled per student), assign identities
by Hungarian matching so nobody is marked present twice, and record attendance
with the capture's geo-fence status.

Both models permit commercial use - YuNet is MIT, SFace Apache-2.0 - which the
previous YOLO/InsightFace stack did not. The swap costs no measured accuracy and
runs roughly 25x faster.

Enrolment (POST /api/students) stores a template from the registration photo.
The stronger path is POST /students/{id}/enroll-multiview, which captures the
face across several poses in the room where attendance is taken.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (auth, centres as centres_mod, config, database, db as pgdb,
               liveness, routes, storage, utils)
from .detector import Face, estimate_landmarks, get_detector
from .enhancer import get_enhancer, sharpness_quality
from .recognizer import fused_similarity_to_student, fuse_scores, get_recognizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="FaceMark Attendance API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    # Session auth rides an httpOnly cookie, so credentials must be allowed -
    # and a wildcard origin is invalid alongside credentials, which is why
    # CORS_ORIGINS is an explicit list rather than "*" in production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



app.include_router(routes.router)


@app.on_event("startup")
def startup() -> None:
    # Everything that writes on first boot runs under one lock.
    #
    # `uvicorn --workers N` starts N processes that reach this within
    # milliseconds of each other, and each step below is a check-then-act pair
    # in its own transaction: count the centres and seed if empty, count the
    # users and create admin if none. Two workers both read zero, both write,
    # and the loser dies on a unique constraint - which makes uvicorn kill the
    # parent, so the whole container fails to boot against an empty database.
    #
    # Serialising is enough on its own: whichever worker arrives second finds
    # the tables created, the centres seeded and admin present, so every step
    # is a no-op for it.
    with pgdb.advisory_lock(pgdb.STARTUP_LOCK_KEY):
        database.init_db()

        n = centres_mod.seed_demo_centres()
        if n:
            log.warning(
                "Seeded %d PLACEHOLDER centres (code prefix DEMO-, is_demo=1). "
                "These are NOT real Khelo India records - replace them via "
                "POST /api/centres/import or delete with DELETE /api/centres/demo/all.", n,
            )
        pw = auth.bootstrap_default_admin()
        if pw:
            bar = "=" * 62
            log.warning(
                "%s | FIRST RUN - super admin account created | "
                "username: admin | password: %s | "
                "Change it after signing in; it is not stored in plaintext. | %s",
                bar, pw, bar,
            )

        # Also inside the lock: it rebuilds templates for anyone missing them,
        # and two workers doing that at once would write each template twice.
        try:
            healed = sync_all_student_templates()
            if healed:
                log.info("Auto-healed %d student template profiles on startup.", healed)
        except Exception as e:
            log.warning("Template sync check skipped: %s", e)

    log.info("Detector backend: %s", get_detector().backend_label)
    log.info("Recognizer ensemble: %s", get_recognizer().label)


def sync_all_student_templates() -> int:
    """Rebuild templates for anyone who has none.

    With a single recognizer there is no per-model coverage to reconcile - a
    student either has a usable template or does not. This catches the case
    where enrolment half-failed, and is a no-op otherwise.
    """
    students = database.list_students()
    if not students:
        return 0
    with database.connect() as conn:
        have = {r["student_id"] for r in conn.execute(
            "SELECT DISTINCT student_id FROM templates").fetchall()}

    updated = 0
    for s in students:
        if s["id"] in have:
            continue
        photo_path = s.get("photo_path")
        if not photo_path:
            continue
        # Rows written before the storage switch hold a full absolute path,
        # newer ones a bare filename. .name is correct for both, and is the
        # storage key under either backend.
        data = storage.get("students", Path(photo_path).name)
        if data is None:
            continue
        try:
            img = utils.decode_image(data)
        except ValueError:
            continue
        try:
            templates, face, _ = _enroll_photo_templates(img, "id")
            if templates:
                database.add_templates(s["id"], templates)
                updated += 1
                log.info("Rebuilt templates for %s (had none)", s["name"])
        except Exception as e:  # noqa: BLE001 - never block startup
            log.warning("Could not rebuild templates for %s: %s", s["name"], e)
    return updated


# --- enrollment helpers -------------------------------------------------------

def _primary_face(img: np.ndarray, detector) -> Tuple[Optional[Face], List[Face]]:
    """Largest detected face (the enrollee) + all faces found."""
    faces = detector.detect(img, mode="accurate")
    if not faces:
        return None, faces
    face = max(faces, key=lambda f: f.width * f.height)
    if face.landmarks is None:
        face.landmarks = estimate_landmarks(face.box)
    return face, faces


def _embed_as_templates(
    embeddings: dict, source: str, quality: float
) -> List[dict]:
    return [
        {"model": name, "vector": vec[0], "source": source, "quality": quality}
        for name, vec in embeddings.items()
        if len(vec)
    ]


def _enroll_photo_templates(
    img: np.ndarray, source: str
) -> Tuple[List[dict], Optional[Face], dict]:
    """Full enrollment pipeline for one photo.

    Returns (templates, face, info). For ID-card sources both the raw and the
    One template per photo. Restoration was removed with GFPGAN, whose weights
    derive from NVIDIA StyleGAN2 and are not licensed for commercial use.
    """
    detector = get_detector()
    recognizer = get_recognizer()
    enhancer = get_enhancer()

    face, all_faces = _primary_face(img, detector)
    if face is None:
        return [], None, {"faces_found": 0}
    if face.width < config.MIN_ENROLL_FACE_SIZE:
        raise HTTPException(
            400,
            f"Face too small ({face.width:.0f}px) - upload a higher-resolution photo",
        )

    aligned = utils.crop_face(img, face)
    quality = sharpness_quality(aligned)

    templates = []
    raw_emb = recognizer.embed_faces(img, [face])
    templates += _embed_as_templates(raw_emb, source, quality)

    # Restoration is gone with GFPGAN. Guided multi-view capture replaces it and
    # does the job better: a current photograph beats a synthesised one.
    restored_used = False

    info = {
        "faces_found": len(all_faces),
        "quality": round(quality, 3),
        "restored": restored_used,
        "box": [round(v, 1) for v in face.box],
    }
    return templates, face, info


# --- students ---------------------------------------------------------------

@app.get("/api/students")
def get_students(user: dict = Depends(auth.current_user)):
    scope = auth.scope_centre(user, None)
    students = [
        {**s, "photo_url": f"/api/photos/{Path(s['photo_path']).name}"}
        for s in database.list_students(centre_id=scope)
    ]
    return {"students": students}


@app.post("/api/students")
async def register_student(
    request: Request,
    name: str = Form(...),
    roll_no: str = Form(...),
    photo: UploadFile = File(...),
    live_photo: Optional[UploadFile] = File(None),
    role: str = Form("athlete"),
    centre_id: Optional[int] = Form(None),
    gender: Optional[str] = Form(None),
    sport: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    user: dict = Depends(auth.current_user),
):
    """Register a person from a photo (required) and, optionally, a second photo.

    `photo` is deliberately generic rather than "ID card": the interactive
    registration flow sends a frontal frame captured live from the camera,
    while scripts/enroll_from_pdf.py sends a passport photo extracted from a
    roster PDF for bulk import. Both are stored as an "id"-source template.
    `live_photo`, when supplied, is stored as a second independent template -
    matching takes the best score over all of a person's templates. Additional
    angles beyond these two are added afterwards via enroll-multiview.
    """
    data = await photo.read()
    try:
        img = utils.decode_image(data)
    except ValueError:
        raise HTTPException(400, "Uploaded file is not a valid image")

    templates, face, info = _enroll_photo_templates(img, "id")
    if face is None:
        raise HTTPException(400, "No face detected in the enrollment photo")

    ts = utils.timestamp()
    live_info = None
    live_data = None
    live_img = None
    live_name = None
    if live_photo is not None and live_photo.filename:
        live_data = await live_photo.read()
        try:
            live_img = utils.decode_image(live_data)
            live_templates, _, live_info = _enroll_photo_templates(live_img, "live")
            templates += live_templates
            live_name = utils.save_image(live_img, "students", f"student_{ts}_live.jpg")
        except ValueError:
            log.warning("Ignoring invalid live photo for %s", roll_no)

    photo_name = utils.save_image(img, "students", f"student_{ts}.jpg")

    if role not in ("athlete", "coach"):
        role = "athlete"
    target_centre = auth.scope_centre(user, centre_id) or user.get("centre_id")
    try:
        student_id = database.add_student(
            name.strip(), roll_no.strip(), photo_name, templates,
            role=role, centre_id=target_centre, gender=gender,
            sport=sport, phone=phone,
        )
    except database.IntegrityError:
        raise HTTPException(409, f"Roll number '{roll_no}' is already registered")
        
    device_info = request.headers.get("user-agent")
    database.save_photo_record(
        file_path=photo_name,
        photo_type="enrollment_id",
        student_id=student_id,
        file_size=len(data),
        resolution=f"{img.shape[1]}x{img.shape[0]}",
        device_info=device_info,
        faces_detected=info.get("faces_found", 0),
    )
    if live_name is not None and live_info is not None:
        database.save_photo_record(
            file_path=live_name,
            photo_type="enrollment_live",
            student_id=student_id,
            file_size=len(live_data),
            resolution=f"{live_img.shape[1]}x{live_img.shape[0]}",
            device_info=device_info,
            faces_detected=live_info.get("faces_found", 0),
        )

    log.info(
        "Registered student %s (%s) id=%s templates=%d quality=%.2f restored=%s",
        name, roll_no, student_id, len(templates),
        info.get("quality", 0), info.get("restored"),
    )
    return {
        "ok": True,
        "student": {
            "id": student_id,
            "name": name.strip(),
            "roll_no": roll_no.strip(),
            "photo_url": f"/api/photos/{photo_name}",
            **info,
            "live": live_info,
            "templates": len(templates),
        },
    }


@app.post("/api/students/{student_id}/photos")
async def add_student_photo(
    request: Request,
    student_id: int,
    photo: UploadFile = File(...),
    source: str = Form("live"),
    user: dict = Depends(auth.current_user),
):
    """Attach an extra photo (recent selfie, another ID) to an existing student."""
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    # The same guard enroll_multiview and assign_face_to_student already apply.
    # Without it a coach can attach templates to another centre's athlete,
    # which both alters that athlete's gallery and reveals they exist.
    auth.scope_centre(user, student.get("centre_id"))
    if source not in ("id", "live"):
        source = "live"

    data = await photo.read()
    try:
        img = utils.decode_image(data)
    except ValueError:
        raise HTTPException(400, "Uploaded file is not a valid image")

    templates, face, info = _enroll_photo_templates(img, source)
    if face is None:
        raise HTTPException(400, "No face detected in the photo")

    n = database.add_templates(student_id, templates)
    
    ts = utils.timestamp()
    photo_name = utils.save_image(
        img, "students", f"student_{student_id}_{ts}_{source}.jpg"
    )

    database.save_photo_record(
        file_path=photo_name,
        photo_type="extra_template",
        student_id=student_id,
        file_size=len(data),
        resolution=f"{img.shape[1]}x{img.shape[0]}",
        device_info=request.headers.get("user-agent"),
        faces_detected=info.get("faces_found", 0),
    )
    
    log.info("Added %d templates (%s) to student %s", n, source, student_id)
    return {"ok": True, "templates_added": n, **info}


@app.delete("/api/students/{student_id}")
def remove_student(student_id: int, user: dict = Depends(auth.current_user)):
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    # Without this a coach can delete any athlete at any centre in the country,
    # and ON DELETE CASCADE takes their templates and attendance history too.
    auth.scope_centre(user, student.get("centre_id"))
    database.delete_student(student_id)
    if student.get("photo_path"):
        storage.delete("students", Path(student["photo_path"]).name)
    return {"ok": True}


# --- attendance -------------------------------------------------------------


def _photo_quality(faces, img) -> dict:
    """Rate the photo by face pixel size, the dominant driver of accuracy.

    Returned with every result so a coach learns the register is unreliable at
    capture time, rather than discovering it after trusting a bad one.
    """
    if not faces:
        return {"level": "none", "median_face_px": 0, "advice":
                "No faces detected."}
    sizes = sorted(min(f.width, f.height) for f in faces)
    med = float(np.median(sizes))
    h, w = img.shape[:2]
    megapixels = (h * w) / 1e6

    if med >= config.GOOD_FACE_PX:
        level, advice = "good", "Face size is comfortable for reliable matching."
    elif med >= config.FAIR_FACE_PX:
        level, advice = "fair", (
            f"Faces average {med:.0f} px. Workable, but expect some misses. "
            "Moving a step closer would help.")
    elif med >= config.POOR_FACE_PX:
        level, advice = "poor", (
            f"Faces average only {med:.0f} px. Expect many athletes to be missed. "
            "Take the photo closer, or split the group into two photos.")
    else:
        level, advice = "unusable", (
            f"Faces average {med:.0f} px, too small to identify reliably. "
            "This register should not be trusted.")

    # A 1-2 MP image from a modern phone has almost certainly been through a
    # messaging app, and that recompression is often the whole problem.
    if megapixels < 2.0 and level in ("poor", "unusable", "fair"):
        advice += (f" This image is only {megapixels:.1f} MP - if it was shared "
                   "over WhatsApp, send the original file instead.")
    return {"level": level, "median_face_px": round(med, 1),
            "megapixels": round(megapixels, 1), "advice": advice}


@app.post("/api/attendance/process")
async def process_attendance(
    request: Request,
    photo: UploadFile = File(...),
    date_str: Optional[str] = Form(None),
    threshold: Optional[float] = Form(None),
    detection_mode: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    accuracy_m: Optional[float] = Form(None),
    centre_id: Optional[int] = Form(None),
    user: dict = Depends(auth.current_user),
):
    """Process group photo for attendance.

    detection_mode options:
    - fast: YOLO only (minimum latency, ~40-60ms)
    - fused: YOLO11s + SCRFD with WBF (best recall, ~80-120ms)
    - accurate: YOLO TTA + SCRFD with WBF (maximum recall, ~150-250ms)
    """
    data = await photo.read()
    try:
        img = utils.decode_image(data)
    except ValueError:
        raise HTTPException(400, "Uploaded file is not a valid image")

    day = date_str or date.today().strftime(config.ATTENDANCE_DATE_FORMAT)
    thr = threshold if threshold is not None else config.MATCH_THRESHOLD

    # A coach always marks for their own centre regardless of what was posted.
    active_centre = auth.scope_centre(user, centre_id) or user.get("centre_id")
    geo = centres_mod.evaluate_location(active_centre, latitude, longitude)

    valid_modes = ("fast", "fused", "accurate")
    det_mode = detection_mode if detection_mode in valid_modes else config.DETECTION_MODE

    detector = get_detector()
    recognizer = get_recognizer()
    enhancer = get_enhancer()
    
    # Recognise first, resolve the centre afterwards.
    #
    # Scoping the gallery BEFORE matching meant a photo from another centre
    # produced zero matches with nothing to distinguish that from the
    # recogniser failing. Identity is a property of the face, not of the
    # dropdown, so the face is identified against everyone enrolled and the
    # centre is checked once we know who it is.
    #
    # Measured on the five real photographs (81 faces, scripts/live_test.py):
    # recall is identical to scoped matching (45 of 45) and wrong-centre
    # matches are zero at every threshold from 0.45 to 0.70. The widened
    # impostor space costs nothing here because the centres' rosters are not
    # confusable - it is a stranger, not another centre's athlete, that the
    # threshold has to keep out.
    gallery = database.load_gallery()
    if not gallery:
        raise HTTPException(400, "No students registered yet - add students first")
    weights = {m.name: m.weight for m in recognizer.models}

    t0 = time.perf_counter()
    faces = detector.detect(img, mode=det_mode)
    t1 = time.perf_counter()
    queries = recognizer.embed_faces(img, faces)          # {model: (Q,512)} batched
    fused, gallery_ids = fuse_scores(queries, gallery, weights)
    t2 = time.perf_counter()

    if not faces:
        # A photo with no detectable faces is a normal outcome, not an error -
        # someone photographs the floor, or the group is too far away. Return an
        # empty result the UI can render rather than a confusing 4xx.
        utils.save_image(img, "uploads", f"group_{utils.timestamp()}.jpg")
        return {
            "ok": True, "date": day, "faces_detected": 0,
            "recognized_count": 0, "athletes_present": 0, "coaches_present": 0,
            "unknown_count": 0, "newly_marked": 0, "threshold": thr,
            "detection_mode": det_mode, "recognized": [], "unknown": [],
            "annotated_url": None,
            "message": "No faces were detected in this photo. Move closer, "
                       "improve the lighting, or try Accurate mode.",
            "geo": {
                "status": geo["geo_status"], "distance_m": geo["distance_m"],
                "latitude": latitude, "longitude": longitude,
                "accuracy_m": accuracy_m, "centre_id": active_centre,
            },
            "timings": {"detect_ms": round((t1 - t0) * 1000, 1), "embed_ms": 0.0,
                        "cascade_ms": 0.0, "annotate_ms": 0.0,
                        "total_ms": round((t1 - t0) * 1000, 1)},
        }

    if fused is None or not len(gallery_ids):
        raise HTTPException(
            400,
            "No usable student templates for the active recognizer models - "
            "re-register students or restart so templates can be rebuilt",
        )

    # Per-(face, student) thresholds, starting from the ACTIVE threshold
    # (the request's value if supplied, otherwise config.MATCH_THRESHOLD).
    n_f, n_g = len(faces), len(gallery_ids)
    thr_matrix = np.full((n_f, n_g), float(thr), dtype=np.float64)

    # Small faces carry a weaker identity signal, so they must clear a higher bar.
    # This is a RELATIVE bump on top of whatever threshold is already in force -
    # clamping to fixed constants here would silently discard the caller's threshold.
    for i, f in enumerate(faces):
        if min(f.width, f.height) < config.SMALL_FACE_PX:
            thr_matrix[i, :] += config.SMALL_FACE_THRESHOLD_BUMP

    from .metaheuristics import GlobalMatchOptimizer
    optimal_matches = GlobalMatchOptimizer.optimize_assignments(
        fused, gallery_ids, threshold=thr_matrix
    )
    match_by_face = {face_idx: (sid, sim) for face_idx, sid, sim in optimal_matches}

    # Stage-2 cascade removed with GFPGAN. Measured separately, restoring query
    # faces made accuracy worse (12/13 against 13/13) - it invents detail that
    # is not the person's.
    cascade_flags: dict = {}
    t3 = time.perf_counter()

    ts = utils.timestamp()
    recognized, unknown, labels, confs = [], [], [], []
    new_marks = 0
    for i, face in enumerate(faces):
        face_img = utils.crop_face(img, face)
        face_file = f"face_{ts}_{i}.jpg"
        utils.save_image(face_img, "uploads", face_file)

        if i in match_by_face:
            student_id, sim = match_by_face[i]
            student = database.get_student(student_id)
            if student is None:
                labels.append(None); confs.append(None); continue

            conf_display = utils.similarity_to_confidence(sim, thr)
            # A person is marked present at the centre they actually belong to.
            # Attributing them to whichever centre happened to be selected
            # would file the record against a centre they are not enrolled at.
            home_centre = student.get("centre_id")
            elsewhere = (active_centre is not None
                         and home_centre is not None
                         and home_centre != active_centre)

            # Global matching means a coach's upload can now identify someone
            # from a centre they have no authority over. A coach may not mark
            # attendance there, and must not learn who trains there either, so
            # the face is reported as belonging elsewhere and nothing more.
            # A super admin oversees every centre and sees the name.
            if elsewhere and user["role"] != "super_admin":
                unknown.append({
                    "face_index": i,
                    "box": [round(v, 1) for v in face.box],
                    "face_url": f"/api/uploads/{face_file}",
                    "other_centre": True,
                })
                labels.append(None)
                confs.append(None)
                continue

            # Attendance is centre-wise. Marking for a centre records that
            # centre's session, so only its own students are marked present.
            # Someone from another centre is still identified by name - that is
            # what stops the wrong-centre photo looking like a broken
            # recogniser - but no attendance is written for them, here or at
            # their own centre. They were not at their centre's session, and a
            # record saying otherwise would be false.
            if elsewhere:
                marked = False
            else:
                marked = database.mark_attendance(
                    student_id, day, sim, f"{ts}_{i}",
                    centre_id=home_centre or active_centre,
                    latitude=latitude, longitude=longitude, accuracy_m=accuracy_m,
                    geo_status=geo["geo_status"], distance_m=geo["distance_m"],
                    marked_by=user["id"],
                )
                new_marks += int(marked)

            # --- Continual learning: store today's observation as a NEW template.
            # Three gates, all required. Confidence alone is not enough: a face that
            # scores 0.65 against the right student and 0.60 against the wrong one is
            # ambiguous, and a template learned from it pulls the whole gallery toward
            # that ambiguity, raising impostor scores for every student thereafter.
            # Gate on the RAW fused score, never on a cascade-boosted one: the
            # cascade re-scores a restored crop, so its value is not comparable
            # with the runner-up drawn from the raw matrix.
            j_col = gallery_ids.index(student_id)
            raw_sim = float(fused[i, j_col])
            runner_up = float(np.max(np.delete(fused[i], j_col))) if fused.shape[1] > 1 else -1.0
            learnable = (
                not elsewhere
                and config.CONTINUAL_LEARNING
                and raw_sim >= config.CONTINUAL_MIN_CONF
                and (raw_sim - runner_up) >= config.CONTINUAL_MIN_MARGIN
                and min(face.width, face.height) >= config.CONTINUAL_MIN_FACE_PX
                and cascade_flags.get(i) != "promoted"
            )
            if learnable:
                try:
                    face_embs = {m.name: queries[m.name][i] for m in recognizer.models if m.name in queries}
                    database.add_adapted_template(
                        student_id=student_id,
                        new_embeddings=face_embs,
                        quality=sharpness_quality(face_img),
                    )
                except Exception as e:
                    log.warning("Continual learning update failed for student %s: %s", student_id, e)

            recognized.append(
                {
                    "student_id": student_id,
                    "name": student["name"],
                    "roll_no": student["roll_no"],
                    "similarity": round(conf_display, 4),
                    "raw_similarity": round(float(sim), 4),
                    "box": [round(v, 1) for v in face.box],
                    "face_url": f"/api/uploads/{face_file}",
                    "marked_now": marked,
                    "cascade": cascade_flags.get(i),
                    "role": student.get("role", "athlete"),
                    "sport": student.get("sport"),
                    "centre_id": home_centre,
                    "centre_name": student.get("centre_name"),
                    "other_centre": elsewhere,
                }
            )
            labels.append(student["name"])
            confs.append(conf_display)
        else:
            unknown.append(
                {
                    "face_index": i,
                    "box": [round(v, 1) for v in face.box],
                    "face_url": f"/api/uploads/{face_file}",
                }
            )
            labels.append(None)
            confs.append(None)

    annotated = utils.annotate(img, faces, labels, confs)
    ann_file = f"annotated_{ts}.jpg"
    utils.save_image(annotated, "uploads", ann_file)

    orig_file = f"group_{ts}.jpg"
    utils.save_image(img, "uploads", orig_file)

    database.save_photo_record(
        file_path=orig_file,
        photo_type="attendance_camera" if source in ("camera_front", "camera_rear") else "attendance_group",
        source=source or "upload",
        file_size=len(data),
        resolution=f"{img.shape[1]}x{img.shape[0]}",
        device_info=request.headers.get("user-agent"),
        faces_detected=len(faces),
    )
    
    # Recognition is global, so "which centre do these people belong to" is now
    # an observation rather than a guess. Report it when the answer is not the
    # centre that was selected, because that is a real condition the operator
    # needs to see - a photo from the wrong centre, or a visiting athlete.
    by_centre: dict = {}
    for r in recognized:
        if r.get("centre_id") is None:
            continue
        key = (r["centre_id"], r.get("centre_name") or "Unknown centre")
        by_centre[key] = by_centre.get(key, 0) + 1

    off_centre = [r for r in recognized if r.get("other_centre")]
    centre_report = None
    if off_centre:
        centre_report = {
            "selected_centre_id": active_centre,
            "off_centre_count": len(off_centre),
            "total_recognised": len(recognized),
            "names": [r["name"] for r in off_centre][:12],
            "breakdown": [
                {"centre_id": cid, "centre_name": nm, "count": n}
                for (cid, nm), n in sorted(by_centre.items(), key=lambda kv: -kv[1])
            ],
        }

    t4 = time.perf_counter()

    return {
        "ok": True,
        "centre_report": centre_report,
        "date": day,
        "faces_detected": len(faces),
        "recognized_count": len(recognized),
        "athletes_present": sum(1 for r in recognized if r.get("role") != "coach"),
        "coaches_present": sum(1 for r in recognized if r.get("role") == "coach"),
        "unknown_count": len(unknown),
        "filtered_faces": getattr(detector, "last_filtered_printed", 0),
        # Surfaced separately from the poster count: a printed face in frame is
        # an accident, a screen held up to the camera is someone trying to mark
        # an absent athlete present, and the operator should be told.
        "filtered_screen": getattr(detector, "last_filtered_screen", 0),
        "photo_quality": _photo_quality(faces, img),
        # A person attends exactly one centre. Matching against every centre's
        # roster at once invites cross-centre false positives, so say so plainly
        # rather than returning a confident-looking wrong answer.
        "scope_warning": (
            None if active_centre else
            "Matched against every centre because no centre was selected. "
            "Pick a centre to avoid cross-centre false matches."
        ),
        "geo": {
            "status": geo["geo_status"],
            "distance_m": geo["distance_m"],
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy_m,
            "centre_id": active_centre,
        },
        "newly_marked": new_marks,
        "threshold": thr,
        "detection_mode": det_mode,
        "recognized": recognized,
        "unknown": unknown,
        "annotated_url": f"/api/uploads/{ann_file}",
        "timings": {
            "detect_ms": round((t1 - t0) * 1000, 1),
            "embed_ms": round((t2 - t1) * 1000, 1),
            "cascade_ms": round((t3 - t2) * 1000, 1),
            "annotate_ms": round((t4 - t3) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1),
        },
    }


class _MemoryUpload:
    """Minimal UploadFile stand-in.

    Lets the video route hand a decoded frame to process_attendance() instead of
    duplicating 250 lines of matching, marking and annotation that are already
    correct and already tested.
    """

    def __init__(self, data: bytes, filename: str = "frame.jpg"):
        self._data = data
        self.filename = filename
        self.content_type = "image/jpeg"

    async def read(self, size: int = -1) -> bytes:  # noqa: ARG002 - API shape
        return self._data


@app.post("/api/attendance/process-video")
async def process_attendance_video(
    request: Request,
    video: UploadFile = File(...),
    date_str: Optional[str] = Form(None),
    threshold: Optional[float] = Form(None),
    detection_mode: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    accuracy_m: Optional[float] = Form(None),
    centre_id: Optional[int] = Form(None),
    user: dict = Depends(auth.current_user),
):
    """Mark attendance from a short clip, refusing photographs of photographs.

    A still frame can only be judged on appearance, and appearance is what a
    replay reproduces - which is why the earlier moire and 3D-landmark checks
    both failed. A clip carries something a photograph cannot: parallax. A
    picture on a screen is a plane, so under camera movement everything in it
    moves through one homography; a real face does not, and the residual is
    depth actually measured rather than inferred. See backend/liveness.py.

    The check runs SERVER-SIDE deliberately. Refusing to show an upload button
    in the browser is a nudge, not a control - anyone can POST to this endpoint
    directly - so the guard has to live where it cannot be skipped.
    """
    data = await video.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    result = liveness.analyse(data, get_detector())

    # Sampled frames are stored whatever the verdict. On a rejection they are
    # the evidence a coach needs to see why, and a refusal nobody can inspect
    # is one nobody can appeal.
    ts = utils.timestamp()
    frame_names: List[str] = []
    for i, frame in enumerate(result.frames[: config.LIVENESS_STORE_FRAMES]):
        name = f"clip_{ts}_{i}.jpg"
        try:
            utils.save_image(frame, "uploads", name)
            frame_names.append(name)
        except Exception as e:  # noqa: BLE001 - storage must not sink attendance
            log.warning("Could not store liveness frame %s: %s", name, e)

    liveness_payload = {
        **result.to_dict(),
        "frame_urls": [f"/api/uploads/{n}" for n in frame_names],
    }

    if not result.is_live:
        log.warning(
            "Liveness refused a clip: %s (depth=%.5f motion=%.5f) by user %s",
            result.verdict, result.depth_score, result.motion, user.get("username"),
        )
        return {
            "ok": False,
            "liveness": liveness_payload,
            "faces_detected": 0,
            "recognized_count": 0,
            "newly_marked": 0,
            "recognized": [],
            "unknown": [],
            "message": result.reason,
        }

    # Recognition runs on the sharpest frame rather than the first: the first is
    # often caught before the camera has settled, and face size and focus drive
    # accuracy far more than anything else measured on this system.
    ok, buf = cv2.imencode(".jpg", result.best_frame,
                           [cv2.IMWRITE_JPEG_QUALITY, config.CAMERA_PHOTO_QUALITY])
    if not ok:
        raise HTTPException(500, "Could not encode the chosen frame")

    response = await process_attendance(
        request=request,
        photo=_MemoryUpload(buf.tobytes(), f"clip_{ts}.jpg"),
        date_str=date_str,
        threshold=threshold,
        detection_mode=detection_mode,
        source=source or "video",
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        centre_id=centre_id,
        user=user,
    )
    response["liveness"] = liveness_payload
    return response


def _face_from_original(crop_name: str):
    """Recover a face from the archived full-resolution photo, not the crop.

    Crops are saved as face_<timestamp>_<index>.jpg alongside the original as
    group_<timestamp>.jpg. Re-detecting on the original reproduces exactly the
    geometry the matcher used, so the learned template is directly comparable
    with future query embeddings. Detection order is deterministic (sorted by
    box x), which is what makes the index meaningful.

    Returns (image, Face) or (None, None) when the original is unavailable.
    """
    stem = Path(crop_name).stem                     # face_20260821_150840_062_13
    if not stem.startswith("face_"):
        return None, None
    body = stem[len("face_"):]
    ts, _, idx = body.rpartition("_")
    if not ts or not idx.isdigit():
        return None, None
    data = storage.get("uploads", f"group_{ts}.jpg")
    if data is None:
        return None, None
    try:
        img = utils.decode_image(data)
    except ValueError:
        return None, None
    faces = get_detector().detect(img, config.DETECTION_MODE)
    i = int(idx)
    if i >= len(faces):
        return None, None
    return img, faces[i]


def _pose_label(face, requested: str) -> str:
    """Name the pose, preferring measurement but falling back to what was asked.

    The geometric estimate is only trustworthy when the detector supplied real
    landmarks - box-derived landmarks are symmetric by construction, so yaw is
    always exactly zero. Where it cannot be measured, the label the capture flow
    asked for is used, and the actual check that a view adds anything is done on
    the embeddings instead.
    """
    q = face.quality or {}
    if not q.get("pose_reliable"):
        return requested or "view"
    yaw, pitch = float(q.get("yaw", 0.0)), float(q.get("pitch", 0.0))
    if yaw <= -config.MULTIVIEW_YAW_TURN:
        return "left"
    if yaw >= config.MULTIVIEW_YAW_TURN:
        return "right"
    if pitch <= -config.MULTIVIEW_PITCH_TURN:
        return "up"
    if pitch >= config.MULTIVIEW_PITCH_TURN:
        return "down"
    return "centre"


@app.post("/api/enroll/pose-check")
async def enroll_pose_check(
    frame: UploadFile = File(...),
    step: str = Form(...),
    base_yaw: Optional[float] = Form(None),
    base_pitch: Optional[float] = Form(None),
    user: dict = Depends(auth.current_user),
):
    """Live guidance for one frame of guided enrolment.

    The phone-style enrolment people expect does not ask you to press a button
    and trust that you turned your head - it watches, tells you what is wrong,
    and fires by itself when the pose is right. This is the endpoint that
    watches. The frontend sends a downscaled frame a few times a second and
    renders whatever comes back.

    Poses are judged RELATIVE to the athlete's own straight-ahead shot rather
    than as absolute angles. Monocular pose from five landmarks carries roughly
    six degrees of noise and a per-face bias - a person with wide-set eyes or a
    long nose reads as permanently tilted - so an absolute rule would refuse
    some people entirely while waving others through without moving. Measuring
    the CHANGE from their own baseline cancels both.

    Nothing is stored here. The frame is examined and discarded; only the
    frames the client keeps are ever enrolled.
    """
    data = await frame.read()
    try:
        img = utils.decode_image(data)
    except ValueError:
        raise HTTPException(400, "Frame is not a valid image")

    faces = get_detector().detect(img, "accurate")
    if not faces:
        return {"ok": False, "reason": "no_face", "message": "No face detected"}
    if len(faces) > 1:
        return {"ok": False, "reason": "many_faces",
                "message": f"{len(faces)} faces in frame - only the athlete should be visible"}

    f = faces[0]
    q = f.quality
    size = float(min(f.width, f.height))
    yaw, pitch = float(q["yaw"]), float(q["pitch"])

    # Framing and image quality first: a correctly-posed blur is still useless.
    if size < config.MULTIVIEW_MIN_FACE_PX:
        return {"ok": False, "reason": "too_far", "message": "Move closer",
                "face_px": round(size), "yaw": yaw, "pitch": pitch}
    # Brightness is judged before blur deliberately. A very dark frame has
    # almost no Laplacian variance, so a blur-first order diagnoses bad
    # lighting as "Hold still" and the athlete stands there holding still
    # while nothing improves.
    if q["brightness"] <= 40:
        return {"ok": False, "reason": "dark", "message": "Too dark - find better light",
                "face_px": round(size), "yaw": yaw, "pitch": pitch}
    if q["brightness"] >= 240:
        return {"ok": False, "reason": "bright", "message": "Too bright - move out of direct light",
                "face_px": round(size), "yaw": yaw, "pitch": pitch}
    if q["blur_score"] < config.MIN_BLUR_SCORE:
        return {"ok": False, "reason": "blurry", "message": "Hold still",
                "face_px": round(size), "yaw": yaw, "pitch": pitch}

    dy = yaw - (base_yaw if base_yaw is not None else 0.0)
    dp = pitch - (base_pitch if base_pitch is not None else 0.0)
    YT, PT = config.MULTIVIEW_YAW_TURN, config.MULTIVIEW_PITCH_TURN

    # The centre shot has no baseline to compare against, so it is the one step
    # judged on absolute angle - loosely, since that is all it has to be.
    # "sweep" is the continuous mode: the phone-style enrolment does not ask for
    # named poses at all, it just watches which angles have been covered while
    # the head rotates. Pose is reported and the client decides what is still
    # missing, because only the client knows which parts of its ring are unlit.
    if step == "sweep":
        ok, nudge = True, "Keep turning slowly"
    else:
        checks = {
            "centre": (abs(yaw) < YT and abs(pitch) < PT * 1.5, "Look straight at the camera"),
            "left":   (dy <= -YT, "Turn further to your left"),
            "right":  (dy >= YT,  "Turn further to your right"),
            "up":     (dp <= -PT, "Tilt your chin up a little more"),
            "down":   (dp >= PT,  "Tilt your chin down a little more"),
        }
        if step not in checks:
            raise HTTPException(400, f"Unknown step '{step}'")
        ok, nudge = checks[step]

    return {
        "ok": ok,
        "reason": None if ok else "pose",
        "message": nudge if not ok else ("Keep turning slowly" if step == "sweep" else "Hold it"),
        "face_px": round(size), "yaw": yaw, "pitch": pitch,
        "delta_yaw": round(dy, 1), "delta_pitch": round(dp, 1),
        "box": [round(v, 1) for v in f.box],
        "frame": [img.shape[1], img.shape[0]],
    }


@app.post("/api/students/{student_id}/enroll-multiview")
async def enroll_multiview(
    request: Request,
    student_id: int,
    frames: List[UploadFile] = File(...),
    user: dict = Depends(auth.current_user),
):
    """Enrol an athlete from several views captured in one sitting.

    This is the practical version of a Face ID style enrolment. It cannot build
    a depth map - a browser has no depth sensor - but it captures what actually
    helps a 2D recognizer: the face across several poses, photographed today in
    the centre's own lighting at close range.

    That combination is what fixes the two failure modes measured on real data:
    a years-old registration photo of a growing child, and the gap between a
    bright studio portrait and a dim indoor group shot.

    One template is stored per distinct pose. Extra frames of a pose already
    captured are skipped rather than stored, because near-duplicates add gallery
    size without adding information.
    """
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Athlete not found")
    auth.scope_centre(user, student.get("centre_id"))

    detector, recognizer = get_detector(), get_recognizer()
    accepted, rejected = [], []
    seen_poses: dict = {}   # pose label -> its embedding

    for n, upload in enumerate(frames):
        raw = await upload.read()
        try:
            img = utils.decode_image(raw)
        except ValueError as e:
            rejected.append({"frame": n, "reason": str(e)})
            continue

        faces = detector.detect(img, "accurate")
        if not faces:
            rejected.append({"frame": n, "reason": "no face found"})
            continue
        face = max(faces, key=lambda f: f.width * f.height)
        size = min(face.width, face.height)
        if size < config.MULTIVIEW_MIN_FACE_PX:
            rejected.append({"frame": n,
                             "reason": f"face only {size:.0f}px - hold the camera closer"})
            continue

        crop = utils.crop_face(img, face)
        emb = recognizer.embed_faces(img, [face])

        # Whether a view is worth keeping is a question about the embedding, not
        # about geometry: if this frame lands almost on top of one already
        # stored, it adds gallery size and no information. Measuring it directly
        # sidesteps the pose estimate entirely, which matters because yaw cannot
        # be measured at all without real landmarks.
        lead = next(iter(emb.values()))
        dup_of = None
        for prev_pose, prev_vec in seen_poses.items():
            if float(lead[0] @ prev_vec) >= config.MULTIVIEW_DUPLICATE_SIM:
                dup_of = prev_pose
                break
        if dup_of is not None:
            rejected.append({"frame": n,
                             "reason": f"too similar to the '{dup_of}' view already captured "
                                       f"- turn your head further"})
            continue

        pose = _pose_label(face, (upload.filename or "").split("_")[0])
        n_added = database.add_templates(
            student_id, _embed_as_templates(emb, "live", sharpness_quality(crop))
        )
        seen_poses[pose] = lead[0]
        accepted.append({"frame": n, "pose": pose, "face_px": round(size),
                         "templates": n_added})

        name = utils.save_image(
            img, "students", f"student_{student_id}_{utils.timestamp()}_{pose}.jpg"
        )
        database.save_photo_record(
            file_path=name, photo_type="enrollment_multiview",
            student_id=student_id, file_size=len(raw),
            resolution=f"{img.shape[1]}x{img.shape[0]}",
            device_info=request.headers.get("user-agent"), faces_detected=len(faces),
        )

    poses = sorted(seen_poses)
    enough = len(poses) >= config.MULTIVIEW_MIN_POSES
    log.info("Multi-view enrolment for %s: poses %s, %d frames rejected",
             student["name"], poses, len(rejected))
    return {
        "ok": True,
        "student": {"id": student_id, "name": student["name"],
                    "roll_no": student["roll_no"]},
        "poses_captured": poses,
        "templates_added": sum(a["templates"] for a in accepted),
        "accepted": accepted,
        "rejected": rejected,
        "sufficient": enough,
        "message": (
            f"Captured {len(poses)} view(s): {', '.join(poses)}."
            if enough else
            f"Only {len(poses)} distinct view(s) captured. Turn your head further "
            f"between shots - identical frames add nothing."
        ),
    }


@app.get("/api/attendance/suggest")
def suggest_for_face(
    face_url: str,
    centre_id: Optional[int] = None,
    limit: int = 5,
    user: dict = Depends(auth.current_user),
):
    """Rank the most likely identities for a face the matcher could not place.

    The right person is usually still the top scorer - just under threshold. So
    rather than making a coach hunt through a dropdown of the whole roster for
    each of twenty faces, offer the ranked shortlist and let them confirm.

    Scores are the same fused similarities the matcher uses, shown so a coach
    can see when the system had no idea (all candidates near zero) versus when
    it was merely hesitant (top candidate just below the line).
    """
    scope = auth.scope_centre(user, centre_id) or user.get("centre_id")
    img, face = _face_from_original(Path(face_url).name)
    if face is None:
        raise HTTPException(404, "Original photo for this face is no longer available")

    gallery = database.load_gallery(centre_id=scope)
    if not gallery:
        return {"suggestions": [], "threshold": config.MATCH_THRESHOLD}

    recognizer = get_recognizer()
    q = recognizer.embed_faces(img, [face])
    fused, gallery_ids = fuse_scores(
        q, gallery, {m.name: m.weight for m in recognizer.models}
    )
    if fused is None or not len(gallery_ids):
        return {"suggestions": [], "threshold": config.MATCH_THRESHOLD}

    order = np.argsort(fused[0])[::-1][:max(1, min(limit, 10))]
    out = []
    for j in order:
        sid = int(gallery_ids[j])
        st = database.get_student(sid)
        if not st:
            continue
        out.append({
            "student_id": sid,
            "name": st["name"],
            "roll_no": st["roll_no"],
            "role": st.get("role", "athlete"),
            "score": round(float(fused[0, j]), 3),
            "photo_url": (f"/api/photos/{Path(st['photo_path']).name}"
                          if st.get("photo_path") else None),
        })
    return {
        "suggestions": out,
        "threshold": config.MATCH_THRESHOLD,
        "face_px": round(min(face.width, face.height), 1),
    }


@app.post("/api/attendance/assign")
async def assign_face_to_student(
    request: Request,
    face_url: str = Form(...),
    student_id: int = Form(...),
    date_str: Optional[str] = Form(None),
    learn: bool = Form(True),
    user: dict = Depends(auth.current_user),
):
    """Attribute a face the matcher missed to a known athlete, and learn from it.

    This is the correction path for the case the measurements show is hardest:
    small faces in a low-resolution photo, where the right person scores just
    under threshold. A coach who can see it is Priya fixes the register in one
    click.

    The learning half matters more than the correction. The stored crop becomes
    a `live` template captured in the centre's real conditions - the same
    lighting, distance and camera as future registers - which is exactly the
    domain gap that enrolment photos alone cannot close. Corrections therefore
    make the next photo work better, rather than being repeated every session.
    """
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Athlete not found")
    auth.scope_centre(user, student.get("centre_id"))

    crop_name = Path(face_url).name
    if not storage.exists("uploads", crop_name):
        raise HTTPException(404, "That face crop is no longer available")

    day = date_str or date.today().strftime(config.ATTENDANCE_DATE_FORMAT)
    marked = database.mark_attendance(
        student_id, day, 1.0, Path(face_url).stem,
        centre_id=student.get("centre_id"), geo_status="manual",
        marked_by=user["id"],
    )

    templates_added = 0
    if learn:
        source_img, face = _face_from_original(crop_name)
        if face is None:
            # The saved crop of a distant face can be as small as 34x38px.
            # Embedding that upscaled, with landmarks guessed from its border,
            # produces a vector unrelated to the one the matcher computes from
            # the full-resolution photo - a template built that way scored 0.137
            # against the very face it came from. Refuse rather than store a
            # template that silently never matches.
            log.warning("Assign: no full-resolution source for %s, not learning", crop_name)
        else:
            emb = get_recognizer().embed_faces(source_img, [face])
            crop = utils.crop_face(source_img, face)
            templates_added = database.add_templates(
                student_id, _embed_as_templates(emb, "live", sharpness_quality(crop))
            )

    log.info("Manual assignment: %s -> %s (marked=%s, +%d templates)",
             face_url, student["name"], marked, templates_added)
    return {
        "ok": True,
        "student": {"id": student_id, "name": student["name"],
                    "roll_no": student["roll_no"], "role": student.get("role")},
        "marked_now": marked,
        "templates_added": templates_added,
        "message": (f"{student['name']} marked present"
                    + (f" and learned from this photo (+{templates_added} templates)"
                       if templates_added else "")),
    }


@app.get("/api/attendance")
def get_attendance(
    day: Optional[str] = None,
    centre_id: Optional[int] = None,
    user: dict = Depends(auth.current_user),
):
    day = day or date.today().strftime(config.ATTENDANCE_DATE_FORMAT)
    records = database.attendance_for_day(day, auth.scope_centre(user, centre_id))
    for r in records:
        r["photo_url"] = f"/api/photos/{Path(r['photo_path']).name}"
        r["time"] = r["marked_at"].split("T")[-1][:5] if "T" in r["marked_at"] else r["marked_at"]
        # Same calibration as /api/attendance/process and /api/stats, so one match
        # never shows three different percentages across the three screens.
        r["raw_similarity"] = round(float(r["confidence"]), 4)
        r["confidence"] = round(
            utils.similarity_to_confidence(r["confidence"], config.MATCH_THRESHOLD), 4
        )
    return {"date": day, "records": records}


@app.get("/api/students/{student_id}/history")
def student_history(student_id: int, user: dict = Depends(auth.current_user)):
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    # A coach may only read the attendance record of their own centre's people.
    auth.scope_centre(user, student.get("centre_id"))
    records = database.student_attendance_history(student_id)
    return {
        "student": {
            "id": student["id"],
            "name": student["name"],
            "roll_no": student["roll_no"],
            "photo_url": f"/api/photos/{Path(student['photo_path']).name}",
            "templates": database.template_stats(student_id),
        },
        "total_present": len(records),
        "records": records,
    }


@app.get("/api/attendance/export")
def export_attendance(
    day: Optional[str] = None,
    centre_id: Optional[int] = None,
    user: dict = Depends(auth.current_user),
):
    day = day or date.today().strftime(config.ATTENDANCE_DATE_FORMAT)
    records = database.attendance_for_day(day, auth.scope_centre(user, centre_id))
    buf = io.StringIO()
    writer = csv.writer(buf)
    # `confidence` is the figure the UI shows. The raw cosine score is no longer
    # exported: it was only ever useful for threshold tuning, and in a register
    # handed to an administrator two different "scores" per row invite the wrong
    # one being read as the answer.
    # Headings are capitalised; the values are left exactly as stored. Names
    # keep their own casing because a register is a document about people, and
    # dates stay ISO so a spreadsheet still reads them as dates.
    writer.writerow(["ID NUMBER", "NAME", "DATE", "CONFIDENCE", "MARKED AT"])
    for r in records:
        writer.writerow([
            r["roll_no"],
            r["name"],
            r["date"],
            round(utils.similarity_to_confidence(r["confidence"], config.MATCH_THRESHOLD), 4),
            r["marked_at"],
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance_{day}.csv"'},
    )


# --- static files & sample images -------------------------------------------

@app.get("/api/sample-images/download")
def download_test_suite():
    import zipfile
    test_dir = config.ROOT_DIR / "samples" / "test_suite"
    if not test_dir.exists():
        raise HTTPException(404, "Test suite directory not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in test_dir.glob("*.jpg"):
            z.write(f, arcname=f.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="facemark_test_images.zip"'},
    )


@app.get("/api/sample-images/{name}")
def get_sample_image(name: str, user: dict = Depends(auth.current_user)):
    """Demo images that ship with the repository.

    Not user data, but authenticated all the same - an open image endpoint next
    to three closed ones is exactly how the closed ones drift back open.
    """
    path = config.ROOT_DIR / "samples" / "test_suite" / Path(name).name
    if not path.exists():
        raise HTTPException(404, "Sample image not found")
    return FileResponse(path)


@app.get("/api/photos/history")
def photo_history(
    student_id: Optional[int] = None,
    photo_type: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(auth.current_user),
):
    """Photo metadata, including the stored filenames.

    Authenticated because those filenames are the keys the two routes below
    take: leaving this open turns "guess a filename" into "enumerate them all,
    then download every enrolment portrait".
    """
    photos = database.get_photos(student_id=student_id, photo_type=photo_type, limit=limit)
    return {"photos": photos}


@app.get("/api/photos/{name}")
def student_photo(name: str, user: dict = Depends(auth.current_user)):
    """Enrolment portraits - photographs of children. Never unauthenticated."""
    return storage.response("students", name)


@app.get("/api/uploads/{name}")
def uploaded_file(name: str, user: dict = Depends(auth.current_user)):
    """Group photos and the face crops taken from them."""
    return storage.response("uploads", name)


@app.get("/api/health")
def health():
    try:
        detector = get_detector()
        det_label = detector.backend_label
    except Exception as e:  # noqa: BLE001
        det_label = f"error: {e}"
    try:
        rec_label = get_recognizer().label
    except Exception as e:  # noqa: BLE001
        rec_label = f"error: {e}"
    enh = get_enhancer()

    # Database and storage state are REPORTED, not raised. Health is what an
    # operator and a load balancer read to find out *why* something is wrong;
    # letting an unreachable database turn this into a 500 tells them only
    # that it is. The old version did exactly that, via list_students().
    db_ok = pgdb.ping()
    n_students = None
    if db_ok:
        try:
            n_students = len(database.list_students())
        except Exception:  # noqa: BLE001
            db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "storage": storage.backend_name(),
        "detector": det_label,
        "recognizer": rec_label,
        "restoration": "off (GFPGAN removed - licence)",
        "threshold": config.MATCH_THRESHOLD,
        "students": n_students,
    }



@app.get("/api/analytics")
def get_analytics(
    centre_id: Optional[int] = None,
    days: int = 30,
    user: dict = Depends(auth.current_user),
):
    """Aggregates behind the analytics page.

    Scoped exactly like every other read: a coach sees their own centre and
    cannot widen it by passing someone else's id.
    """
    days = max(1, min(days, 365))
    return database.analytics(
        centre_id=auth.scope_centre(user, centre_id) or user.get("centre_id"),
        days=days,
    )


@app.get("/api/stats")
def get_stats(user: dict = Depends(auth.current_user)):
    s = database.stats(centre_id=auth.scope_centre(user, None))
    # `confidence` is stored as raw cosine similarity. The dashboard and the
    # attendance result screen must show the SAME number for a given match, so
    # calibrate it here exactly as /api/attendance/process does.
    for r in s.get("recent", []):
        r["confidence"] = round(
            utils.similarity_to_confidence(r["confidence"], config.MATCH_THRESHOLD), 4
        )
    try:
        s["photo_stats"] = database.photo_stats()
    except AttributeError:
        pass
    enh = get_enhancer()
    s["model"] = {
        "detector": get_detector().backend_label,
        "recognizer": get_recognizer().label,
        "restoration": "off (GFPGAN removed - licence)",
        "threshold": config.MATCH_THRESHOLD,
    }
    return s


class NoCacheStatic(StaticFiles):
    """Serve the frontend with revalidation instead of heuristic caching.

    StaticFiles sends an ETag and Last-Modified but NO Cache-Control. With no
    explicit policy a browser falls back to heuristic freshness - typically a
    tenth of the file's age - and will happily reuse app.js for hours without
    asking the server whether it changed. That is how a deployed frontend fails
    to reach someone who already has the page open: the old UI keeps running
    against the new API, disagreeing with it silently.

    `no-cache` does not mean "do not store"; it means "revalidate before use".
    The ETag is still doing the work - an unchanged file comes back as a 304
    with no body - so this costs one conditional request, not a re-download.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _no_cache(response: FileResponse) -> FileResponse:
    """Same policy for the hand-served root documents."""
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/")
def index():
    # The shell must revalidate too, or a stale index.html keeps pointing at
    # scripts the deployment no longer ships.
    return _no_cache(FileResponse(config.FRONTEND_DIR / "index.html"))


# PWA files must be served from the site root, not from /static. A service
# worker can only control pages at or below its own path, so one served from
# /static/sw.js could never control "/" and the app would not be installable.
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(config.FRONTEND_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(config.FRONTEND_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return _no_cache(FileResponse(config.FRONTEND_DIR / "manifest.webmanifest",
                                  media_type="application/manifest+json"))


# Icons keep the default caching: they are immutable in practice, and a shortcut
# icon served from cache is not a correctness problem. Code is different, so
# /static revalidates - see NoCacheStatic.
app.mount("/icons", StaticFiles(directory=config.FRONTEND_DIR / "icons"), name="icons")
app.mount("/static", NoCacheStatic(directory=config.FRONTEND_DIR), name="static")
