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
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (auth, centres as centres_mod, config, database, db as pgdb,
               sessions as sessions_mod, signup as signup_mod,
               maintenance as maintenance_mod, portrait as portrait_mod,
               liveness, metaheuristics, routes, storage, utils)
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

        # Housekeeping, inside the same lock so two workers do not both sweep
        # on boot. Forced: a container that has been down for a week has a
        # week of expired registers waiting, and the caller-triggered sweeps
        # only fire once somebody visits.
        swept = maintenance_mod.run_due(force=True)
        if any(swept.values()):
            log.info("Startup housekeeping: %s", swept)

    log.info("Detector backend: %s", get_detector().backend_label)
    log.info("Recognizer ensemble: %s", get_recognizer().label)
    # Logged because the fallback is not the Hungarian algorithm and produces a
    # different assignment. Running one matcher while quoting another matcher's
    # accuracy figures is the kind of drift that is invisible until someone
    # measures, so the log says which is live on every boot.
    solver = metaheuristics.assignment_solver_name()
    (log.warning if "GREEDY" in solver else log.info)("Assignment solver: %s", solver)


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
def get_students(
    role: Optional[str] = None,
    user: dict = Depends(auth.require_staff),
):
    """Enrolled people at the caller's centre, optionally one role.

    `role` exists because the directory is the Athlete Directory: a coach is an
    enrolled person with a face, and belongs in this table, but not in a list
    the screen calls athletes. Unfiltered is still the default - several callers
    want everybody.
    """
    if role is not None and role not in ("athlete", "coach"):
        raise HTTPException(400, "role must be 'athlete' or 'coach'")
    scope = auth.scope_centre(user, None)
    students = [
        # None, not "/api/photos/". Building the URL unconditionally gave
        # somebody with no photo a 404, which the browser drew as a broken
        # image with their name beside it.
        {**s, "photo_url": (f"/api/photos/{Path(s['photo_path']).name}"
                            if s.get("photo_path") else None)}
        for s in database.list_students(centre_id=scope, role=role)
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
    user: dict = Depends(auth.require_staff),
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

    # The card in the directory shows this, so it is cropped to the face
    # rather than stored as whatever was framed. Templates above already came
    # from the full image; this only changes the picture a human sees.
    shot, shot_info = portrait_mod.from_single(img, get_detector())
    photo_name = utils.save_image(shot, "students", f"student_{ts}.jpg")
    log.info("Enrolment portrait for %s: %s", roll_no, shot_info)

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
        raise HTTPException(409, f"NSRS ID '{roll_no}' is already registered")
        
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
    user: dict = Depends(auth.require_staff),
):
    """Attach an extra photo (recent selfie, another ID) to an existing student."""
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    # The same guard enroll_multiview and assign_face_to_student already apply.
    # Without it a coach can attach templates to another centre's athlete,
    # which both alters that athlete's gallery and reveals they exist.
    auth.owns_centre(user, student.get("centre_id"))
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
def remove_student(student_id: int, user: dict = Depends(auth.require_staff)):
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    # Without this a coach can delete any athlete at any centre in the country,
    # and ON DELETE CASCADE takes their templates and attendance history too.
    auth.owns_centre(user, student.get("centre_id"))
    # Gathered BEFORE the rows go, or the answer disappears with them. This
    # used to delete students.photo_path alone, so every multi-view enrolment
    # frame, every added photo and the signup capture stayed on disk after the
    # person was gone - photographs of children, kept for no reason anybody
    # could state, and still listable by a super admin because photos.student_id
    # is ON DELETE SET NULL rather than CASCADE.
    names = database.photo_files_for(student_id)
    database.forget_photo_rows(student_id)
    removed = database.delete_student(student_id)
    gone = 0
    for name in names:
        for prefix in ("students", "uploads"):
            try:
                storage.delete(prefix, name)
                gone += 1
            except Exception:      # noqa: BLE001 - a missing file is fine
                pass
    log.info("Deleted person %s: %s (%d image file(s))", student_id, removed, gone)
    return {"ok": True, "removed": removed}


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
    user: dict = Depends(auth.require_super_admin),
):
    """Process group photo for attendance.

    SUPER ADMIN ONLY. This is the pre-session route: it writes CONFIRMED
    attendance straight from a photo, with no register, no review and no
    signature from whoever took it. That is the one thing v1 set out to stop, so
    the only accounts left holding it are the ones running the bulk import
    scripts. A coach takes attendance through /api/sessions, where it is
    reviewed and signed for; an athlete never takes it for anybody.

    It used to take current_user and scope by centre, which reads like access
    control and is not - an athlete has a centre too, and could post a group
    photo to mark thirteen people present.

    detection_mode options:
    - fast:     YuNet at a higher confidence - fewer, surer boxes
    - fused:    YuNet at the calibrated 0.80 threshold (the default)
    - accurate: YuNet at a lower threshold, more recall on hard photos

    All three are the same detector at different confidences. YOLO and SCRFD
    were removed with the licence migration; naming them here outlived them.
    """
    # Stated as a statement, not only in the signature. Depends() runs on HTTP
    # dispatch; this function was also being called in-process by
    # process_attendance_video, where the dependency never executed and every
    # coach reached the confirmed-attendance writer this guard withholds.
    auth.assert_super_admin(user)

    data = await photo.read()
    try:
        img = utils.decode_image(data)
    except ValueError:
        raise HTTPException(400, "Uploaded file is not a valid image")

    day = _validated_day(date_str)
    thr = _validated_threshold(threshold)

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
    faces, det_stats = detector.detect_with_stats(img, mode=det_mode)
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
                        "match_ms": 0.0, "cascade_ms": 0.0, "annotate_ms": 0.0,
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
    # Every matched person in one query instead of one per face. A class group
    # photo matches 13, and a query each measured 8.7 ms against 1.5 ms for the
    # batch. Read before the loop because the loop also writes attendance, and
    # a read that interleaves with those writes is harder to reason about than
    # one taken up front from a single consistent snapshot.
    students_by_id = database.get_students(sid for sid, _ in match_by_face.values())
    for i, face in enumerate(faces):
        face_img = utils.crop_face(img, face)
        face_file = f"face_{ts}_{i}.jpg"
        utils.save_image(face_img, "uploads", face_file)

        if i in match_by_face:
            student_id, sim = match_by_face[i]
            student = students_by_id.get(student_id)
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
        "filtered_faces": det_stats["printed"],
        # Surfaced separately from the poster count: a printed face in frame is
        # an accident, a screen held up to the camera is someone trying to mark
        # an absent athlete present, and the operator should be told.
        "filtered_screen": det_stats["screens"],
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
            # Named for a cascade stage that was removed with GFPGAN. What
            # this window actually covers is threshold assembly and the global
            # assignment solve - which is worth reporting, but under an honest
            # name. Both keys are emitted so anything reading the old one keeps
            # working; drop cascade_ms once nothing does.
            "match_ms": round((t3 - t2) * 1000, 1),
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

    def __init__(self, data: bytes, filename: str = "frame.jpg",
                 content_type: str = "image/jpeg"):
        self._data = data
        self.filename = filename
        self.content_type = content_type

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
    user: dict = Depends(auth.require_staff),
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

    # A COACH GOES THROUGH THE REGISTER. This route used to call
    # process_attendance as a plain function, which meant that route's
    # Depends(require_super_admin) never ran: any coach reached the
    # confirmed-attendance writer and wrote straight past the draft, the review
    # and the signature - "the one thing v1 set out to stop", in its own words.
    #
    # Restricting this endpoint to admins would have taken Mark Attendance away
    # from the coaches whose landing page it is, so the capture goes where the
    # design says it should: into today's register, as drafts, for the same
    # coach to review and sign.
    #
    # Delegating to the captures route rather than reimplementing it also means
    # this path inherits its liveness handling, including the `too_far` verdict
    # for a group across a room - which this endpoint did NOT have, so it was
    # refusing every group clip on the coach's own landing page.
    if user.get("role") != "super_admin":
        day = _validated_day(date_str, back_days=config.SESSION_BACKDATE_DAYS)
        centre = auth.scope_centre(user, centre_id) or user.get("centre_id")
        if not centre:
            raise HTTPException(
                400, "This account has no centre, so it cannot take attendance")
        started = time.time()
        sess = sessions_mod.get_or_create(
            int(centre), auth.coach_student_id(user), int(user["id"]), day)
        out = await add_session_capture(
            session_id=int(sess["id"]),
            media=_MemoryUpload(data, video.filename or "clip.webm",
                                content_type="video/webm"),
            kind="video", threshold=threshold, detection_mode=detection_mode,
            latitude=latitude, longitude=longitude, accuracy_m=accuracy_m,
            user=user,
        )
        if out.get("ok") is False:
            return out
        # The Mark Attendance screen predates the register and reads
        # `recognized`, `unknown` and `timings`. Rather than leave it broken or
        # duplicate the capture logic, the register's answer is translated into
        # the shape that screen already renders - with drafted_to_register set,
        # so it can say what actually happened: these people are proposed, not
        # yet present, and the coach still signs the register.
        drafted = out.get("drafted") or []
        return {
            **out,
            "drafted_to_register": True,
            "session_id": out.get("session_id"),
            "newly_marked": out.get("newly_drafted", 0),
            "recognized_count": out.get("recognized_count", len(drafted)),
            "recognized": [
                {
                    "student_id": d.get("student_id"),
                    "name": d.get("name"),
                    "roll_no": d.get("roll_no"),
                    "similarity": d.get("similarity"),
                    "confidence": d.get("confidence"),
                    "face_url": (f"/api/uploads/{d['crop']}" if d.get("crop") else None),
                    "marked_now": True,
                }
                for d in drafted
            ],
            "unknown": [],
            "timings": {"total_ms": int((time.time() - started) * 1000)},
            "message": (
                f"{out.get('newly_drafted', 0)} added to today's register as drafts. "
                "Open Register to review and submit."
            ),
        }

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


def _landmarks_payload(f) -> list:
    """YuNet's five landmarks as [[x, y], ...], for the live overlay.

    Returned alongside the box on every path that found a face, including the
    framing rejections: a dot pattern that vanishes exactly when the app says
    "move closer" is the moment it is most useful to see.
    """
    if f.landmarks is None:
        return []
    return [[round(float(x), 1), round(float(y), 1)] for x, y in f.landmarks]


@app.post("/api/enroll/pose-check")
async def enroll_pose_check(
    request: Request,
    frame: UploadFile = File(...),
    step: str = Form(...),
    base_yaw: Optional[float] = Form(None),
    base_pitch: Optional[float] = Form(None),
    signup_token: Optional[str] = Form(None),
):
    """Live guidance for one frame of guided enrolment.

    Reachable by STAFF, or by somebody part-way through self-registration who
    has a live signup token. Both are guided by this and neither can be left
    out: a coach enrolling an athlete has a session, and an applicant recording
    their own face has no account at all - by definition, since the account is
    what they are applying for.

    Guarding it with require_staff alone made every frame of the signup capture
    403, and the frontend's failure counter turned that into "Lost connection"
    over a working camera.

    It stores nothing and answers only about the frame it was handed, so the
    bar is "invited to be here", not "who are you". Left open it would be a
    free face detector for anybody who found the URL.

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
    _pose_check_caller(request, signup_token)
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
                "face_px": round(size), "yaw": yaw, "pitch": pitch,
                "box": [round(v, 1) for v in f.box],
                "landmarks": _landmarks_payload(f)}
    # Brightness is judged before blur deliberately. A very dark frame has
    # almost no Laplacian variance, so a blur-first order diagnoses bad
    # lighting as "Hold still" and the athlete stands there holding still
    # while nothing improves.
    if q["brightness"] <= 40:
        return {"ok": False, "reason": "dark", "message": "Too dark - find better light",
                "face_px": round(size), "yaw": yaw, "pitch": pitch,
                "box": [round(v, 1) for v in f.box],
                "landmarks": _landmarks_payload(f)}
    if q["brightness"] >= 240:
        return {"ok": False, "reason": "bright", "message": "Too bright - move out of direct light",
                "face_px": round(size), "yaw": yaw, "pitch": pitch,
                "box": [round(v, 1) for v in f.box],
                "landmarks": _landmarks_payload(f)}
    if q["blur_score"] < config.MIN_BLUR_SCORE:
        return {"ok": False, "reason": "blurry", "message": "Hold still",
                "face_px": round(size), "yaw": yaw, "pitch": pitch,
                "box": [round(v, 1) for v in f.box],
                "landmarks": _landmarks_payload(f)}
    # Said BEFORE recording, because it cannot be fixed afterwards. The clip
    # gets its several angles from the turn prompts; what it cannot manufacture
    # is a level camera, and the enrolment photo comes out of these frames. A
    # phone at chest height puts the lens under the chin and every frame is an
    # up-nose shot - which is exactly the picture this was reported for.
    # Only while FRAMING, never mid-sequence. base_yaw is set once a baseline
    # exists, which is exactly when the turn prompts start asking for angles -
    # and a step that says "look up" must not be refused for looking up.
    if base_yaw is None and abs(pitch) > config.MAX_PORTRAIT_PITCH:
        return {"ok": False, "reason": "pitch",
                "message": ("Hold the phone at eye level" if pitch < 0
                            else "Lower the phone to eye level"),
                "face_px": round(size), "yaw": yaw, "pitch": pitch,
                "box": [round(v, 1) for v in f.box],
                "landmarks": _landmarks_payload(f)}

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
        "landmarks": _landmarks_payload(f),
        "frame": [img.shape[1], img.shape[0]],
    }


@app.post("/api/students/{student_id}/enroll-multiview")
async def enroll_multiview(
    request: Request,
    student_id: int,
    frames: List[UploadFile] = File(...),
    user: dict = Depends(auth.require_staff),
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
    auth.owns_centre(user, student.get("centre_id"))

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


@app.post("/api/students/register-video")
async def register_student_from_video(
    request: Request,
    video: UploadFile = File(...),
    name: str = Form(...),
    roll_no: str = Form(...),
    role: str = Form("athlete"),
    centre_id: Optional[int] = Form(None),
    gender: Optional[str] = Form(None),
    sport: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    user: dict = Depends(auth.require_staff),
):
    """Register a new person from one short clip.

    ORDER MATTERS HERE. The liveness check runs before any row is written.
    Doing it the other way - create the student, then check - would leave a
    half-registered person behind whenever a clip was refused: a roster entry
    with no templates, which can never be recognised and which nobody is
    prompted to clean up. Nothing is created unless the clip passes.

    Registration is also the moment where a wrong identity becomes permanent. A
    template built from a photograph held up to the camera lets that photograph
    mark its subject present from then on, and no later check undoes it.
    """
    data = await video.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    result = liveness.analyse(data, get_detector())
    liveness_payload = result.to_dict()

    if not result.is_live:
        log.warning(
            "Registration clip refused for '%s': %s (depth=%.5f motion=%.5f)",
            roll_no, result.verdict, result.depth_score, result.motion,
        )
        return {"ok": False, "liveness": liveness_payload, "message": result.reason}

    # The sharpest frame becomes the profile photo and the first template: face
    # size and focus drive accuracy more than anything else measured here, and
    # the first frame is often caught before the camera has settled.
    if result.best_frame is None and not result.frames:
        # A "live" verdict with no frames should be impossible now that the
        # disabled path decodes too, but indexing [0] on an empty list here was
        # a 500 once already, and this route writes roster rows about minors.
        return {"ok": False, "liveness": liveness_payload,
                "message": "Could not read any frames from the clip - record again"}
    best = result.best_frame if result.best_frame is not None else result.frames[0]
    templates, face, info = _enroll_photo_templates(best, "id")
    if face is None:
        return {
            "ok": False,
            "liveness": liveness_payload,
            "message": "No usable face in the clip - move closer and record again",
        }

    ts = utils.timestamp()
    # Across the whole clip, not just the frame liveness liked: the most
    # frontal, sharpest face is rarely the one that best proved the person was
    # three-dimensional.
    shot, shot_info = portrait_mod.choose(result.frames or [best], get_detector())
    photo_name = utils.save_image(shot if shot is not None else best,
                                  "students", f"student_{ts}.jpg")
    log.info("Registration portrait: %s", shot_info)

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
        raise HTTPException(409, f"NSRS ID '{roll_no}' is already registered")

    database.save_photo_record(
        file_path=photo_name,
        photo_type="enrollment_id",
        student_id=student_id,
        file_size=len(data),
        resolution=f"{best.shape[1]}x{best.shape[0]}",
        device_info=request.headers.get("user-agent"),
        faces_detected=info.get("faces_found", 0),
    )

    # The remaining frames go through the multi-view path, which applies the
    # duplicate-embedding gate - so near-identical frames are skipped rather
    # than filling the gallery with copies of one instant.
    extra_uploads: List[_MemoryUpload] = []
    for i, frame in enumerate(result.frames):
        if frame is best:
            continue
        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, config.CAMERA_PHOTO_QUALITY])
        if ok:
            extra_uploads.append(_MemoryUpload(buf.tobytes(), f"frame{i}_.jpg"))

    extra = {}
    if extra_uploads:
        try:
            extra = await enroll_multiview(
                request=request, student_id=student_id, frames=extra_uploads, user=user,
            )
        except HTTPException as e:
            # The person exists with a usable template already; extra views
            # failing is a degraded result, not a failed registration.
            log.warning("Extra views failed for new student %s: %s", student_id, e.detail)

    log.info("Registered %s (%s) id=%s from clip, %d + %d template(s)",
             name, roll_no, student_id, len(templates), extra.get("templates_added", 0))

    return {
        "ok": True,
        "liveness": liveness_payload,
        "student": {
            "id": student_id,
            "name": name.strip(),
            "roll_no": roll_no.strip(),
            "photo_url": f"/api/photos/{photo_name}",
            **info,
        },
        "templates": len(templates) + extra.get("templates_added", 0),
        "poses_captured": extra.get("poses_captured", []),
    }


@app.post("/api/students/{student_id}/enroll-video")
async def enroll_from_video(
    request: Request,
    student_id: int,
    video: UploadFile = File(...),
    user: dict = Depends(auth.require_staff),
):
    """Enrol an athlete from a short clip instead of a posed frame sequence.

    Two things this buys over enroll-multiview, which it otherwise reuses
    wholesale rather than reimplementing:

    Liveness. Enrolment is the one moment where a wrong identity is permanent -
    a template built from a photograph held up to the camera means that
    photograph can mark its subject present forever after. The clip is checked
    before a single template is stored, and a refusal stores nothing.

    Natural variety. Someone recording for two seconds moves without being
    told to, so the frames differ by small amounts of yaw and expression - which
    is exactly the variation max-pooled matching benefits from, and it arrives
    without asking a child to perform a sequence of head turns on cue.

    The frames go through the multi-view path unchanged, so the duplicate gate,
    the face-size floor and the pose labelling all behave identically.
    """
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Athlete not found")
    auth.owns_centre(user, student.get("centre_id"))

    data = await video.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    result = liveness.analyse(data, get_detector())
    liveness_payload = result.to_dict()

    if not result.is_live:
        log.warning(
            "Enrolment clip refused for student %s: %s (depth=%.5f motion=%.5f)",
            student_id, result.verdict, result.depth_score, result.motion,
        )
        return {
            "ok": False,
            "liveness": liveness_payload,
            "templates_added": 0,
            "poses_captured": [],
            "accepted": [],
            "rejected": [],
            "message": result.reason,
        }

    uploads: List[_MemoryUpload] = []
    for i, frame in enumerate(result.frames):
        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, config.CAMERA_PHOTO_QUALITY])
        if ok:
            # Distinct prefixes matter: the multi-view path keys seen poses by
            # label, and identical fallback labels would collapse them into one
            # entry, leaving the duplicate check comparing against a single
            # embedding instead of every view kept so far.
            uploads.append(_MemoryUpload(buf.tobytes(), f"frame{i}_.jpg"))

    if not uploads:
        raise HTTPException(400, "Could not read any frames from the clip")

    response = await enroll_multiview(
        request=request, student_id=student_id, frames=uploads, user=user,
    )
    response["liveness"] = liveness_payload
    return response



# =============================================================================
# v1 registers - sessions, captures, review
# =============================================================================
#
# These live here rather than in routes.py because a capture needs the whole
# recognition pipeline, and main.py is where that already is. The CRUD half
# could sit in routes.py, but splitting one flow across two modules to satisfy
# a filing rule makes it harder to follow, not easier.
#
# The invariant for this phase: NOTHING here writes a confirmed row. Captures
# produce drafts. Promotion happens on submit, which is phase 2.


def _session_or_404(session_id: int) -> dict:
    sess = sessions_mod.get(session_id)
    if not sess:
        raise HTTPException(404, "No such register")
    return sess


def _validated_threshold(threshold: Optional[float]) -> float:
    """The match threshold, kept inside the range it was calibrated over.

    It arrived as a plain form field on the two routes that decide who is
    recorded present, and was passed to the matcher unchecked. A caller could
    send 0.0, which makes every face in frame match its nearest gallery entry
    and writes those matches down as machine recognitions, or 2.0, which matches
    nobody and quietly produces an empty register. Neither is a setting a client
    gets to choose; the tunable exists for measurement, not for traffic.
    """
    if threshold is None:
        return config.MATCH_THRESHOLD
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        raise HTTPException(400, "Threshold must be a number")
    lo, hi = config.MATCH_THRESHOLD_MIN, config.MATCH_THRESHOLD_MAX
    if not (lo <= value <= hi):
        raise HTTPException(400, f"Threshold must be between {lo} and {hi}")
    return value


def _validated_day(date_str: Optional[str], *, back_days: Optional[int] = None) -> str:
    """A real date, not in the future, optionally within a recent window.

    `date_str` arrived as an unvalidated form field on the routes that WRITE
    attendance, so it decided which day a person was recorded present on and was
    never checked. Three things went through:

      nonsense    anything at all was stored verbatim; "banana" became a day
                  with attendance against it, and every date-keyed query then
                  quietly skipped it.
      the future  a register could be opened, filled and submitted for a date
                  that has not happened.
      the past    attendance could be written for any day in history, which is
                  the useful direction for anyone falsifying a record.

    back_days=None allows any past date - that is the bulk-import path, which is
    super-admin only and exists to load real historic registers.
    """
    if not date_str:
        return config.today_str()
    text = str(date_str).strip()
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Date must be written as YYYY-MM-DD")
    today = config.local_now().date()
    if day > today:
        raise HTTPException(400, "Attendance cannot be recorded for a future date")
    if back_days is not None and (today - day).days > back_days:
        raise HTTPException(
            400,
            f"That date is more than {back_days} day(s) ago. Ask an administrator "
            f"to record attendance for it.",
        )
    return day.isoformat()


def _person_in_scope(user: dict, student_id: int) -> dict:
    """The person, if the caller's centre may act on them. 404 otherwise.

    Scoping the COACH is not the same as scoping the PERSON. Several routes
    checked who was asking - scope_coach pins a coach to themselves - and then
    took a student_id straight off the URL without asking whether that person
    was anything to do with the caller's centre. A coach could therefore tick
    any of the ~1000 people in the database present on their own register, or
    attach them to their roster, which both alters that person's record and
    confirms to the caller that they exist.

    404 rather than 403: existence is part of what is being protected.
    """
    person = database.get_student(student_id)
    if not person:
        raise HTTPException(404, "No such person")
    if user.get("role") == "super_admin":
        return person
    if person.get("centre_id") is None or \
            int(person["centre_id"]) != int(user.get("centre_id") or -1):
        raise HTTPException(404, "No such person")
    return person


def _may_touch(user: dict, sess: dict) -> None:
    """A coach may only work on their own register."""
    if user["role"] == "super_admin":
        return
    own = auth.coach_student_id(user)
    if sess.get("coach_id") is None or int(sess["coach_id"]) != own:
        raise HTTPException(403, "You can only work on your own register")


@app.post("/api/sessions")
def open_session(
    centre_id: Optional[int] = Form(None),
    coach_id: Optional[int] = Form(None),
    date_str: Optional[str] = Form(None),
    user: dict = Depends(auth.require_staff),
):
    """Get or create today's register. Idempotent - calling it twice is safe."""
    scoped_centre = auth.scope_centre(user, centre_id)
    if scoped_centre is None:
        raise HTTPException(400, "A centre is required to open a register")
    scoped_coach = auth.scope_coach(user, coach_id)
    # A coach opens today's register, or one of the last few days if they are
    # catching up; anything older is an administrator's job. A super admin may
    # open any past date, which is what the bulk import needs.
    day = _validated_day(
        date_str,
        back_days=None if user["role"] == "super_admin" else config.SESSION_BACKDATE_DAYS,
    )
    sess = sessions_mod.get_or_create(
        scoped_centre, scoped_coach, int(user["id"]), day
    )
    return {"ok": True, "session": sess}


@app.get("/api/sessions/{session_id}")
def read_session(session_id: int, user: dict = Depends(auth.require_staff)):
    """The register: every athlete of this coach, present and absent.

    Absent athletes are returned too, deliberately. A review screen that only
    lists who was recognised cannot be used to notice who is missing, which is
    the entire reason a human looks at it.
    """
    sess = _session_or_404(session_id)
    _may_touch(user, sess)

    rows = sessions_mod.rows_of(session_id)
    coach_id = sess.get("coach_id")
    if coach_id is not None:
        roster = sessions_mod.athletes_of(int(coach_id))
    else:
        # Admin sweep: the centre's whole roster, minus anybody not approved.
        # Attendance cannot be written for them anyway, so listing them offers
        # a tick box that silently does nothing.
        roster = [s for s in database.list_students()
                  if s.get("centre_id") == sess["centre_id"]
                  and (s.get("status") or "active") == "active"]

    entries = []
    for st in roster:
        row = rows.get(int(st["id"]))
        entries.append({
            "student_id": int(st["id"]),
            "name": st["name"],
            "roll_no": st.get("roll_no"),
            "role": st.get("role", "athlete"),
            "photo_url": (f"/api/photos/{Path(st['photo_path']).name}"
                          if st.get("photo_path") else None),
            "present": row is not None,
            "status": row["status"] if row else None,
            "origin": row.get("origin") if row else None,
            "confidence": row.get("confidence") if row else None,
            "crop_url": (f"/api/uploads/{Path(row['image_path']).name}"
                         if row and row.get("image_path") else None),
            "geo_status": row.get("geo_status") if row else None,
            "distance_m": row.get("distance_m") if row else None,
        })

    # Anyone drafted who is NOT on this coach's roster - an admin sweep, or a
    # link removed after the capture. Showing them prevents a silent orphan.
    known = {e["student_id"] for e in entries}
    extra = []
    for sid, row in rows.items():
        if sid in known:
            continue
        st = database.get_student(sid)
        if not st:
            continue
        extra.append({
            "student_id": sid, "name": st["name"], "roll_no": st.get("roll_no"),
            "present": True, "status": row["status"], "origin": row.get("origin"),
            "confidence": row.get("confidence"), "off_roster": True,
        })

    return {
        "ok": True,
        "session": sess,
        "roster": entries,
        "off_roster": extra,
        "captures": sessions_mod.captures_of(session_id),
        "present_count": sum(1 for e in entries if e["present"]) + len(extra),
        "roster_count": len(entries),
    }


@app.post("/api/sessions/{session_id}/captures")
async def add_session_capture(
    session_id: int,
    media: UploadFile = File(...),
    kind: Optional[str] = Form(None),
    threshold: Optional[float] = Form(None),
    detection_mode: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    accuracy_m: Optional[float] = Form(None),
    user: dict = Depends(auth.require_staff),
):
    """Add one capture to a register. Video or photo.

    A VIDEO IS REQUIRED. A still cannot be checked for liveness at all - a
    photograph of a photograph is exactly what a still reproduces - and it used
    to be accepted, marked `not_checked`, and allowed to write drafts anyway.
    The defence was that a coach signs the register afterwards. That is true,
    and it is not the same as checking: the signature says a human was present,
    not that the people in the picture were.

    An earlier version of this docstring claimed the parallax check rejected ten
    of ten flat replays with 2.1x separation. That figure was measured on
    replays that all happened to move gently, against real clips that all
    happened to move a lot, and it did not survive a matched comparison: at the
    same motion, a waved photograph outscored a real face. The check has since
    been repaired - see backend/liveness.py - and now separates the classes with
    no overlap on 270 clips. The claim above was wrong before the fix, not after.

    A DISTANT FACE IS NOT JUDGED AT ALL. See the `too_far` branch below: this is
    the one endpoint pointed at a room rather than at arm's length, and the test
    does not reach that far.

    Capturing again ADDS to the session. Recall is 100% at 50-pixel faces and
    23% at 24 pixels, so one frame across a hall loses most of a large group -
    several captures is the normal case, not an edge case.
    """
    sess = _session_or_404(session_id)
    _may_touch(user, sess)
    if sess["status"] != "draft":
        raise HTTPException(409, "This register has already been submitted")

    data = await media.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    filename = (media.filename or "").lower()
    is_video = (kind or "").lower() == "video" or filename.endswith(
        (".webm", ".mp4", ".mkv", ".mov", ".m4v")
    )

    if not is_video:
        raise HTTPException(
            400,
            "Attendance needs a short video, not a photo. A still cannot be "
            "checked for liveness - record a few seconds, moving the phone "
            "slowly from side to side.")

    detector = get_detector()
    result = liveness.analyse(data, detector)
    verdict = result.verdict
    depth = result.depth_score

    # A ROOM IS NOT A FACE HELD AT ARM'S LENGTH, and this endpoint is the only
    # one pointed at a room. The parallax test was calibrated on faces 165-313px
    # wide; the faces in a real group photograph from this centre are 20-74px.
    # At that size the measurement is reading its own noise, so `too_far` means
    # the test could not look - not that it looked and saw a photograph. It is
    # recorded as unchecked, listed on the oversight page, and allowed through,
    # because the alternative is refusing every genuine group capture with an
    # accusation the evidence cannot support. A coach cannot move a hall closer.
    #
    # What still holds the line: a still is refused outright, a face close
    # enough to judge IS judged, and the register is signed at the end with the
    # coach's own face at arm's length, where the check works.
    if result.code == "too_far":
        verdict = "too_far"
        log.info(
            "Capture on session %s recorded unchecked: %s (face %spx, %s frames) "
            "by %s", session_id, result.code, result.face_px, result.frames_used,
            user.get("username"),
        )
    elif verdict != "live":
        # Keep the evidence. A refusal nobody can inspect is a refusal nobody
        # can appeal, and this route used to return without storing a single
        # frame - so the one failure a coach would actually report was the one
        # failure that left no trace to diagnose.
        ts_ev = utils.timestamp()
        frame_urls = []
        for i, frame in enumerate(result.frames[: config.LIVENESS_STORE_FRAMES]):
            name = f"refused_{session_id}_{ts_ev}_{i}.jpg"
            try:
                utils.save_image(frame, "uploads", name)
                frame_urls.append(f"/api/uploads/{name}")
            except Exception as e:  # noqa: BLE001 - storage must not sink the reply
                log.warning("Could not store refused frame %s: %s", name, e)
        log.warning(
            "Capture refused on session %s: %s/%s (depth=%.5f motion=%.5f "
            "face=%spx points=%s frames=%s) by %s",
            session_id, verdict, result.code, result.depth_score, result.motion,
            result.face_px, result.tracked_points, result.frames_used,
            user.get("username"),
        )
        return {
            "ok": False, "session_id": session_id,
            "message": result.reason,
            "liveness": {**result.to_dict(), "frame_urls": frame_urls},
        }

    img = result.best_frame
    if img is None:
        raise HTTPException(400, "No usable frame in that clip")

    ts = utils.timestamp()
    media_name = f"capture_{session_id}_{ts}.jpg"
    utils.save_image(img, "uploads", media_name)

    active_centre = sess["centre_id"]
    geo = centres_mod.evaluate_location(active_centre, latitude, longitude)

    thr = _validated_threshold(threshold)
    det_mode = detection_mode or config.DETECTION_MODE

    # WHOLE gallery, filtered afterwards - see sessions.route_recognised.
    gallery = database.load_gallery()
    if not gallery:
        raise HTTPException(400, "Nobody is enrolled yet")
    recognizer = get_recognizer()
    weights = {m.name: m.weight for m in recognizer.models}

    faces = detector.detect(img, mode=det_mode)
    recognised: List[dict] = []
    if faces:
        queries = recognizer.embed_faces(img, faces)
        fused, gallery_ids = fuse_scores(queries, gallery, weights)
        if fused is not None and len(gallery_ids):
            n_f, n_g = len(faces), len(gallery_ids)
            thr_matrix = np.full((n_f, n_g), float(thr), dtype=np.float64)
            for i, f in enumerate(faces):
                if min(f.width, f.height) < config.SMALL_FACE_PX:
                    thr_matrix[i, :] += config.SMALL_FACE_THRESHOLD_BUMP
            from .metaheuristics import GlobalMatchOptimizer
            pairs = GlobalMatchOptimizer.optimize_assignments(
                fused, gallery_ids, threshold=thr_matrix
            )
            by_id = database.get_students(sid for _, sid, _ in pairs)
            for face_idx, sid, sim in pairs:
                st = by_id.get(int(sid))
                if not st:
                    continue
                crop_name = f"face_{ts}_{face_idx}.jpg"
                utils.save_image(utils.crop_face(img, faces[face_idx]), "uploads", crop_name)
                recognised.append({
                    "student_id": int(sid), "name": st["name"],
                    "roll_no": st.get("roll_no"), "centre_id": st.get("centre_id"),
                    "similarity": float(sim), "crop": crop_name,
                    "confidence": utils.similarity_to_confidence(float(sim), thr),
                })

    routed = sessions_mod.route_recognised(
        sess.get("coach_id"), recognised, active_centre
    )

    capture_id = sessions_mod.add_capture(
        session_id, media_name, "video" if is_video else "photo",
        liveness_verdict=verdict, liveness_depth=depth,
        faces_detected=len(faces), recognised=len(recognised),
        latitude=latitude, longitude=longitude,
        geo_status=geo["geo_status"], distance_m=geo["distance_m"],
    )

    drafted = 0
    for r in routed["draft"]:
        if sessions_mod.draft(
            session_id, int(r["student_id"]), sess["date"], float(r["similarity"]),
            origin="recognised", image_path=r.get("crop"), capture_id=capture_id,
            centre_id=active_centre, latitude=latitude, longitude=longitude,
            accuracy_m=accuracy_m, geo_status=geo["geo_status"],
            distance_m=geo["distance_m"], marked_by=int(user["id"]),
        ):
            drafted += 1

    return {
        "ok": True,
        "session_id": session_id,
        "capture_id": capture_id,
        "kind": "video" if is_video else "photo",
        "liveness": {**result.to_dict(), "verdict": verdict},
        "unchecked": verdict == "too_far",
        "faces_detected": len(faces),
        "recognized_count": len(recognised),
        "newly_drafted": drafted,
        "drafted": routed["draft"],
        "other_coach": routed["other_coach"],
        "other_centre": routed["other_centre"],
        "unknown_count": max(0, len(faces) - len(recognised)),
        "geo": {"status": geo["geo_status"], "distance_m": geo["distance_m"]},
    }



@app.post("/api/sessions/{session_id}/submit")
async def submit_session(
    session_id: int,
    clip: UploadFile = File(...),
    attempt: int = Form(1),
    user: dict = Depends(auth.require_staff),
):
    """Close the register under the submitter's own face.

    Liveness plus a 1:1 check against that person's own templates. On pass,
    every draft is promoted in one transaction.

    On FAILURE the caller may retry. After config.VERIFY_MAX_RETRIES attempts
    the register is submitted anyway and recorded as unverified, with the score
    and the liveness verdict, for the admin dashboard. That asymmetry is
    deliberate and runs through this whole system: a genuine person refused is
    worse than a spoof let through, and an unverified register that exists and
    is flagged beats a verified register that was never taken.
    """
    sess = _session_or_404(session_id)
    _may_touch(user, sess)
    if sess["status"] != "draft":
        raise HTTPException(409, "This register has already been submitted")

    # A super admin has no person record of their own, so coach_student_id
    # raised 400 and the sweep register they had just opened and captured into
    # could never be submitted at all - opened, filled, and stuck. There is no
    # face to check against, so the register is submitted and RECORDED as
    # unverified, which is the same treatment a coach gets when the check fails
    # and lands it on the oversight page for exactly this reason.
    who = None
    if user.get("student_id"):
        who = auth.coach_student_id(user)
    elif user["role"] != "super_admin":
        who = auth.coach_student_id(user)      # raises, with the right message

    if who is None:
        out = sessions_mod.submit(session_id, False, 0.0, "no_person_record")
        log.info("Register %s submitted unverified by super admin %s "
                 "(no person record to check a face against)",
                 session_id, user["id"])
        return {
            "ok": True, "verified": False, "score": 0.0,
            "reason": "Submitted without a face check: this administrator "
                      "account has no enrolled person record.",
            **(out or {}),
        }

    data = await clip.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    detector = get_detector()
    result = liveness.analyse(data, detector)
    img = result.best_frame

    verified, score, reason = False, 0.0, ""
    if result.verdict != "live" or img is None:
        reason = result.reason or "Could not confirm this was live"
    else:
        v = sessions_mod.verify_face(img, who)
        score = float(v.get("score") or 0.0)
        if not v["ok"]:
            reason = v["reason"]
        elif score >= config.COACH_VERIFY_THRESHOLD:
            verified = True
        else:
            reason = "That face does not match your enrolled photo"

    last_attempt = int(attempt) >= config.VERIFY_MAX_RETRIES
    if not verified and not last_attempt:
        return {
            "ok": False, "verified": False, "submitted": False,
            "attempt": int(attempt), "retries_left": config.VERIFY_MAX_RETRIES - int(attempt),
            "score": round(score, 4), "liveness": result.to_dict(),
            "message": reason or "Verification failed - try again",
        }

    try:
        out = sessions_mod.submit(session_id, verified, score, result.verdict)
    except ValueError as e:
        raise HTTPException(409, str(e))

    if not verified:
        log.warning(
            "Register %s submitted UNVERIFIED by user %s (score %.4f, liveness %s)",
            session_id, user["id"], score, result.verdict,
        )
    return {
        "ok": True, "submitted": True, "verified": verified,
        "promoted": out["promoted"], "score": round(score, 4),
        "liveness": result.to_dict(),
        "message": ("Register submitted" if verified else
                    "Register submitted, but your face could not be verified - "
                    "this has been flagged for an administrator"),
    }


@app.patch("/api/sessions/{session_id}/roster/{student_id}")
def toggle_roster(
    session_id: int,
    student_id: int,
    present: bool = Form(...),
    user: dict = Depends(auth.require_staff),
):
    """Tick or untick one person by hand. Drafts only."""
    sess = _session_or_404(session_id)
    _may_touch(user, sess)
    _person_in_scope(user, student_id)
    if sess["status"] != "draft":
        raise HTTPException(409, "This register has already been submitted")
    action = sessions_mod.set_present(
        session_id, student_id, present, sess["date"],
        sess["centre_id"], int(user["id"]),
    )
    return {"ok": True, "action": action, "present": present}


@app.get("/api/coaches/{coach_id}/athletes")
def coach_roster(coach_id: int, user: dict = Depends(auth.require_staff)):
    scoped = auth.scope_coach(user, coach_id)
    return {"ok": True, "coach_id": scoped,
            "athletes": sessions_mod.athletes_of(int(scoped))}


# Declared BEFORE /athletes/{athlete_id}: FastAPI matches in order, and
# "roster" would otherwise be read as an athlete_id and fail to parse as an int.
@app.get("/api/coaches/{coach_id}/roster")
def read_roster(coach_id: int, user: dict = Depends(auth.require_staff)):
    """This coach's register roster, and who else at the centre could join it."""
    scoped = auth.scope_coach(user, coach_id)
    if scoped is None:
        raise HTTPException(400, "A coach is required")
    with pgdb.connect() as conn:
        row = conn.execute("SELECT centre_id FROM students WHERE id = ?",
                           (int(scoped),)).fetchone()
    if row is None:
        raise HTTPException(404, "No such coach")
    # Two questions, not one: may this caller act on that coach (ownership),
    # and which centre's people should the options be drawn from (the coach's).
    auth.owns_centre(user, row["centre_id"])
    centre = row["centre_id"]
    return {"ok": True, "coach_id": int(scoped),
            **sessions_mod.roster_options(int(scoped), centre)}


@app.put("/api/coaches/{coach_id}/roster")
def write_roster(
    coach_id: int,
    athlete_ids: str = Form(""),
    user: dict = Depends(auth.require_staff),
):
    """Set this coach's roster to exactly these athletes.

    Takes the whole list, not one change at a time: a coach setting up for the
    first time is ticking thirty boxes, and thirty requests is thirty chances to
    end up with a roster that is half of what they chose.
    """
    scoped = auth.scope_coach(user, coach_id)
    if scoped is None:
        raise HTTPException(400, "A coach is required")
    try:
        ids = [int(x) for x in athlete_ids.replace(" ", "").split(",") if x]
    except ValueError:
        raise HTTPException(400, "athlete_ids must be a comma-separated list of ids")
    # A coach may only add people from their own centre, checked here rather
    # than trusted from the browser.
    if user["role"] != "super_admin" and ids:
        with pgdb.connect() as conn:
            marks = ",".join("?" for _ in ids)
            outside = conn.execute(
                f"SELECT COUNT(*) FROM students WHERE id IN ({marks}) "
                "AND (centre_id IS DISTINCT FROM ?)", ids + [user["centre_id"]],
            ).fetchone()[0]
        if outside:
            raise HTTPException(403, "You can only add athletes from your own centre")
    try:
        return {"ok": True, **sessions_mod.set_roster(int(scoped), ids)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/coaches/{coach_id}/athletes/{athlete_id}")
def link_athlete(coach_id: int, athlete_id: int,
                 user: dict = Depends(auth.require_staff)):
    scoped = auth.scope_coach(user, coach_id)
    _person_in_scope(user, athlete_id)
    try:
        created = sessions_mod.link_athlete(int(scoped), athlete_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "created": created}


@app.delete("/api/coaches/{coach_id}/athletes/{athlete_id}")
def unlink_athlete(coach_id: int, athlete_id: int,
                   user: dict = Depends(auth.require_staff)):
    scoped = auth.scope_coach(user, coach_id)
    return {"ok": True, "removed": sessions_mod.unlink_athlete(int(scoped), athlete_id)}



# =============================================================================
# Athlete self-marking
# =============================================================================
#
# A self-mark creates a DRAFT in the coach's register, never a confirmed row.
# If it wrote confirmed attendance there would be two routes to a register and
# one of them would have no human check, which undoes the reason the review
# step exists. The coach still submits.


@app.get("/api/me/coaches")
def my_coaches(user: dict = Depends(auth.current_user)):
    """Which coaches this athlete trains under."""
    me = auth.scope_self(user, None)
    return {"ok": True, "student_id": me, "coaches": sessions_mod.coaches_of(me)}


@app.get("/api/me/attendance")
def my_attendance(user: dict = Depends(auth.current_user)):
    """This athlete's own confirmed history."""
    me = auth.scope_self(user, None)
    st = database.get_student(me)
    return {
        "ok": True,
        "student_id": me,
        "name": st["name"] if st else None,
        "records": database.student_attendance_history(me),
    }


@app.post("/api/me/attendance")
async def mark_myself(
    clip: UploadFile = File(...),
    coach_id: int = Form(...),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    accuracy_m: Optional[float] = Form(None),
    user: dict = Depends(auth.current_user),
):
    """Mark yourself present from a short clip.

    Liveness is MANDATORY here, with no photo path and no exceptions. A coach's
    group capture may be a photo because a person reviews and signs for it; a
    self-mark has no such witness, so the clip is the only evidence there is.

    Geo-fencing carries real weight for the same reason - an athlete marking
    themselves from home is the obvious abuse. A bad or missing fix does NOT
    reject the attempt: a genuine athlete with poor GPS should not lose their
    attendance silently. It is accepted as a draft and badged loudly, with the
    distance, in the coach's review list.
    """
    me = auth.scope_self(user, None)
    if int(coach_id) == me:
        raise HTTPException(400, "You cannot mark yourself under your own name")

    # Must actually be this athlete's coach, or anyone could post into any
    # register they can name.
    if int(coach_id) not in {int(c["id"]) for c in sessions_mod.coaches_of(me)}:
        raise HTTPException(403, "That is not one of your coaches")

    day = config.today_str()
    if sessions_mod.already_self_marked(me, int(coach_id), day):
        # The database constraint would refuse the duplicate anyway; this is so
        # the athlete gets an answer rather than a silent no-op.
        raise HTTPException(409, "You are already on today's register for this coach")

    wait = sessions_mod.self_mark_cooldown(me, int(coach_id))
    if wait:
        raise HTTPException(429, f"Too many attempts - wait {wait}s and try again")

    data = await clip.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    detector = get_detector()
    result = liveness.analyse(data, detector)
    if result.verdict != "live" or result.best_frame is None:
        sessions_mod.note_self_failure(me, int(coach_id))
        return {"ok": False, "reason": "liveness", "message": result.reason,
                "liveness": result.to_dict()}

    v = sessions_mod.verify_face(result.best_frame, me)
    score = float(v.get("score") or 0.0)
    if not v["ok"] or score < config.SELF_VERIFY_THRESHOLD:
        sessions_mod.note_self_failure(me, int(coach_id))
        return {"ok": False, "reason": "face",
                "message": v.get("reason") or "That face does not match your record",
                "score": round(score, 4), "liveness": result.to_dict()}

    coach = database.get_student(int(coach_id))
    centre_id = (coach or {}).get("centre_id")
    geo = centres_mod.evaluate_location(centre_id, latitude, longitude)

    # Opens the coach's register if they have not captured yet, so they arrive
    # to a partly-filled one rather than an empty screen.
    sess = sessions_mod.get_or_create(centre_id, int(coach_id), int(user["id"]), day)
    if sess["status"] != "draft":
        raise HTTPException(409, "Your coach has already submitted today's register")

    ts = utils.timestamp()
    frame_name = f"self_{me}_{ts}.jpg"
    utils.save_image(result.best_frame, "uploads", frame_name)

    added = sessions_mod.draft(
        sess["id"], me, day, score, origin="self_marked", image_path=frame_name,
        centre_id=centre_id, latitude=latitude, longitude=longitude,
        accuracy_m=accuracy_m, geo_status=geo["geo_status"],
        distance_m=geo["distance_m"], marked_by=int(user["id"]),
    )
    return {
        "ok": True, "added": added, "session_id": sess["id"],
        "status": "draft", "origin": "self_marked",
        "score": round(score, 4),
        "liveness": result.to_dict(),
        "geo": {"status": geo["geo_status"], "distance_m": geo["distance_m"]},
        "message": ("Marked - your coach will confirm it."
                    if geo["geo_status"] == "inside" else
                    "Marked, but you appear to be away from the centre. "
                    "Your coach will see that when they confirm it."),
    }



@app.get("/api/admin/overview")
def admin_overview(
    date_str: Optional[str] = None,
    centre_id: Optional[int] = None,
    user: dict = Depends(auth.require_super_admin),
):
    """Which registers are missing today, and which should not be trusted."""
    # The oversight page is where somebody looks at the state of the system, so
    # it is a good moment to make that state true: expire yesterday's abandoned
    # registers and forget registrations nobody decided. Rate-limited inside.
    maintenance_mod.run_due()
    return {"ok": True, **sessions_mod.admin_overview(date_str, centre_id)}



@app.get("/api/approvals")
def list_approvals(user: dict = Depends(auth.require_staff)):
    """Accounts waiting on THIS coach. A super admin sees every queue."""
    if user["role"] == "super_admin":
        return {"ok": True, "pending": sessions_mod.pending_for_coach(None)}
    who = auth.coach_student_id(user)
    return {"ok": True, "pending": sessions_mod.pending_for_coach(who)}


@app.post("/api/approvals/{user_id}")
def decide_approval(
    user_id: int,
    approve: bool = Form(...),
    guardian_name: Optional[str] = Form(None),
    guardian_consent: bool = Form(False),
    merge: bool = Form(False),
    user: dict = Depends(auth.require_staff),
):
    """Approve or reject. Approval activates, links and un-hides in one step.

    `merge` answers the duplicate question: this applicant IS the person the
    face check flagged, so fold them into that record rather than approving a
    second one. Only ever set by someone who was shown both.
    """
    if user["role"] != "super_admin":
        # Ownership is checked against chosen_coach_id, NOT against the pending
        # queue. Once an account is decided it leaves that queue, so a queue
        # check answered "that account did not choose you" for an account that
        # had chosen exactly this coach - the wrong reason, and a confusing one.
        # Resolving ownership first lets an already-decided account fall
        # through to the accurate 409 below.
        who = auth.coach_student_id(user)
        with pgdb.connect() as conn:
            row = conn.execute(
                "SELECT role, chosen_coach_id FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "No such account")
        # Checked before ownership, and stated as its own rule rather than left
        # to fall out of chosen_coach_id being NULL. Granting coach access is
        # the largest privilege escalation this app has - it hands over a whole
        # centre - so the one role allowed to grant it is named here explicitly
        # and does not depend on another column happening to be empty.
        if row["role"] != "athlete":
            raise HTTPException(
                403, "Only a super admin can approve an account that is asking "
                     "for coach access")
        if row["chosen_coach_id"] is None or int(row["chosen_coach_id"]) != who:
            raise HTTPException(403, "That account did not choose you")
    try:
        out = sessions_mod.decide(user_id, approve, int(user["id"]),
                                  guardian_name, guardian_consent, merge)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, **out}



@app.get("/api/approvals/decided")
def list_decided(limit: int = 50, user: dict = Depends(auth.require_super_admin)):
    """Accounts that were rejected, so a decision can be looked at again.

    Super admin only. A coach's own mistakes are recoverable through them, and
    a list of every rejected applicant is not something to hand to each coach.
    """
    with pgdb.connect() as conn:
        rows = conn.execute(
            "SELECT u.id AS user_id, u.username, u.full_name, u.role, u.status, "
            "       u.approved_at, u.phone, s.name AS person_name, s.roll_no, "
            "       c.name AS centre_name, a.full_name AS decided_by "
            "FROM users u "
            "LEFT JOIN students s ON s.id = u.student_id "
            "LEFT JOIN centres c ON c.id = u.centre_id "
            "LEFT JOIN users a ON a.id = u.approved_by "
            "WHERE u.status = 'rejected' ORDER BY u.approved_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return {"ok": True, "decided": [dict(r) for r in rows]}


@app.post("/api/approvals/{user_id}/reopen")
def reopen_approval(user_id: int, user: dict = Depends(auth.require_super_admin)):
    """Send a rejected application back to the queue.

    Super admin only, and deliberately so even for an athlete a coach rejected:
    undoing somebody else's decision is a different act from making one, and
    the person who made it is not always the right one to reverse it.
    """
    try:
        return {"ok": True, **sessions_mod.reopen(user_id)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/approvals/{user_id}/coach")
def reassign_approval(
    user_id: int,
    coach_id: Optional[int] = Form(None),
    user: dict = Depends(auth.require_super_admin),
):
    """Point a pending athlete at the coach who should decide them.

    For the applicant whose chosen coach was deleted, and for the one who
    picked the wrong name. Super admin only: a coach moving an applicant into
    their own queue would be approving themselves into the decision.
    """
    try:
        return {"ok": True, **sessions_mod.set_chosen_coach(user_id, coach_id)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/approvals/coaches")
def approval_coach_options(centre_id: int,
                           user: dict = Depends(auth.require_super_admin)):
    """Coaches a pending athlete at this centre could be reassigned to."""
    return {"ok": True, "coaches": signup_mod.coaches_at(centre_id)}


# =============================================================================
# Self-signup (unauthenticated)
# =============================================================================
#
# The only unauthenticated write path in the app. Every step after the first
# requires the opaque token the first returns, so nobody can post a face into
# somebody else's pending account or read a centre's coach roster uninvited.
# What comes out is INERT: it cannot sign in and its face is excluded from the
# gallery until a coach approves it.


def _pose_check_caller(request: Request, signup_token: Optional[str]) -> str:
    """Refuse anybody who is neither staff nor mid-signup. Returns which."""
    token = auth._token_from_request(request)
    if token:
        user = auth.resolve_token(token)
        if user and user.get("role") in ("coach", "super_admin"):
            return "staff"
    if signup_token:
        try:
            signup_mod.resolve_signup(signup_token)
            return "signup"
        except ValueError:
            pass
    raise HTTPException(
        403, "Sign in, or start a registration, before using the camera guide.")


def _client_ip(request: Request) -> str:
    """See auth.client_ip - one derivation, so both throttles agree."""
    return auth.client_ip(request)


@app.post("/api/signup")
def signup_start(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    centre_id: int = Form(...),
    role: str = Form("athlete"),
):
    """Start an athlete OR a coach application.

    Defaults to athlete so an older client that does not send the field keeps
    working, and signup.start whitelists the value - the role is the one thing
    an unauthenticated caller must not be able to choose freely.
    """
    try:
        return {"ok": True, **signup_mod.start(
            username, password, full_name, centre_id, _client_ip(request), role)}
    except PermissionError as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/signup/coaches")
def signup_coaches(token: str, centre_id: Optional[int] = None):  # noqa: ARG001
    """Coaches to choose from, at the applicant's OWN centre.

    Behind the token, because a centre's coach roster with photographs is not
    something to hand out to anyone who asks - but the token was only half the
    guard: centre_id came from the caller, so one self-issued token walked the
    whole country's coach lists, names and faces included. The centre is read
    from the application now. The parameter is still accepted so an older
    browser holding the previous page does not break, and is ignored.
    """
    try:
        centre = signup_mod.centre_of(token)
    except ValueError as e:
        raise HTTPException(401, str(e))
    return {"ok": True, "coaches": signup_mod.coaches_at(centre)}


@app.post("/api/signup/coach")
def signup_choose_coach(token: str = Form(...), coach_id: int = Form(...)):
    try:
        signup_mod.choose_coach(token, coach_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/signup/face")
async def signup_face(
    request: Request,
    token: str = Form(...),
    video: UploadFile = File(...),
):
    """The guided capture, into a pending account.

    Reuses the enrolment path unchanged, including its liveness requirement -
    a signup nobody is watching is exactly where a photograph of a photograph
    would be tried.

    THROTTLED, which it was not. This route is unauthenticated, decodes a video
    and runs the liveness pipeline on every call, and a token stays usable for
    its whole 45-minute life - so one token could drive unlimited decodes and
    unlimited permanent image writes. Both the token and the address are
    counted: the token stops one applicant hammering it, the address stops
    somebody minting tokens to get around that.
    """
    if signup_mod.throttled(f"face:{token}", config.SIGNUP_FACE_PER_TOKEN, 3600)             or signup_mod.throttled(f"faceip:{_client_ip(request)}",
                                    config.SIGNUP_FACE_PER_IP_HOUR, 3600):
        raise HTTPException(
            429, "Too many face captures. Wait a few minutes and try again.")
    try:
        student_id = signup_mod.student_for(token)
        applicant_role = signup_mod.role_for(token)
    except ValueError as e:
        raise HTTPException(400, str(e))

    data = await video.read()
    if not data:
        raise HTTPException(400, "Empty upload")

    detector = get_detector()
    result = liveness.analyse(data, detector)
    if result.verdict != "live":
        return {"ok": False, "message": result.reason, "liveness": result.to_dict()}

    # The enrolment pipeline per frame. enroll_multiview cannot be reused here:
    # it takes an authenticated user, and this caller has no account yet by
    # definition. _enroll_photo_templates is the same underlying path.
    best = result.best_frame if result.best_frame is not None else result.frames[0]
    templates, face, info = _enroll_photo_templates(best, "id")
    if not templates or face is None:
        return {"ok": False,
                "message": "No usable face in that clip - try again in better light",
                "liveness": result.to_dict()}

    # Asked BEFORE this face joins the gallery, and answered against people
    # who are already approved - so an applicant can never be flagged as their
    # own duplicate. Recorded, never acted on: the merge is a human's call, and
    # it is put in front of them at approval time.
    dup = sessions_mod.find_existing_person(best)
    if dup.get("student_id"):
        with pgdb.connect() as conn:
            conn.execute(
                "UPDATE users SET duplicate_of = ?, duplicate_score = ? "
                "WHERE student_id = ?",
                (int(dup["student_id"]), float(dup["score"]), student_id),
            )
        log.info("Signup for person %s resembles enrolled person %s (%.3f) - "
                 "flagged for the approver", student_id, dup["student_id"],
                 dup["score"])

    ts = utils.timestamp()
    photo_name = f"signup_{student_id}_{ts}.jpg"
    shot, shot_info = portrait_mod.choose(result.frames or [best], get_detector())
    utils.save_image(shot if shot is not None else best, "students", photo_name)
    log.info("Signup portrait for person %s: %s", student_id, shot_info)

    added = 0
    for frame in result.frames[: config.LIVENESS_STORE_FRAMES]:
        more, f2, _ = _enroll_photo_templates(frame, "live")
        if more:
            database.add_templates(student_id, more)
            added += len(more)
    database.add_templates(student_id, templates)
    added += len(templates)
    with pgdb.connect() as conn:
        conn.execute("UPDATE students SET photo_path = ? WHERE id = ?",
                     (photo_name, student_id))

    return {"ok": True, "templates": added,
            "liveness": result.to_dict(),
            "role": applicant_role,
            "message": ("Sent to a super admin for approval."
                        if applicant_role == "coach"
                        else "Sent to your coach for approval.")}



# NOT /api/centres/public: routes.py registers /centres/{centre_id} first,
# so that path matches it as centre_id="public" and answers 401 from its
# auth dependency. Under /api/signup it also sits with the rest of the flow.
@app.get("/api/signup/centres")
def public_centres():
    """Centre names for the signup form, before any account exists.

    Deliberately minimal: id and name of government centres, which is not
    personal data. The COACH list is not public - that needs a signup token,
    because a centre's coach roster with photographs is not something to hand
    to anyone who asks.
    """
    rows = centres_mod.search_centres(limit=1000)
    return {"ok": True, "centres": [{"id": r["id"], "name": r["name"]} for r in rows]}


@app.get("/api/attendance/suggest")
def suggest_for_face(
    face_url: str,
    centre_id: Optional[int] = None,
    limit: int = 5,
    user: dict = Depends(auth.require_staff),
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
    # Bounded at ten, so this is a smaller win than the attendance loop - but
    # it is the same one query instead of ten.
    suggested = database.get_students(int(gallery_ids[j]) for j in order)
    out = []
    for j in order:
        sid = int(gallery_ids[j])
        st = suggested.get(sid)
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
    user: dict = Depends(auth.require_staff),
):
    """Attribute a face the matcher missed to a known athlete, and learn from it.

    Coaches and admins only. It writes confirmed attendance AND, with learn on,
    adds an adapted template - so an athlete holding this could both mark people
    present and teach the recogniser a face of their choosing.

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
    auth.owns_centre(user, student.get("centre_id"))

    crop_name = Path(face_url).name
    if not storage.exists("uploads", crop_name):
        raise HTTPException(404, "That face crop is no longer available")

    day = date_str or config.today_str()
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
    user: dict = Depends(auth.require_staff),
):
    day = day or config.today_str()
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
def student_history(student_id: int, user: dict = Depends(auth.require_staff)):
    student = database.get_student(student_id)
    if not student:
        raise HTTPException(404, "Student not found")
    # A coach may only read the attendance record of their own centre's people.
    auth.owns_centre(user, student.get("centre_id"))
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


# Excel, LibreOffice and Sheets all treat a cell beginning = + - @ (or a
# leading tab / carriage return) as a FORMULA, not as text. Names reach this
# register from the registration form and from roster imports, and neither
# validates their content - so a person named =HYPERLINK("http://…"&A2,"Click")
# becomes a live link, and the DDE form (=cmd|'/c calc'!A1) prompts to run a
# program, in a file an administrator opens precisely because they trust it.
#
# Prefixing with an apostrophe is the standard mitigation: spreadsheets read it
# as "this is text" and do not display it. Applied to every string column
# rather than only `name`, because guessing which field can never hold a hostile
# value is how these come back.
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r", "\n")


def _csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA_LEAD) else text


@app.get("/api/attendance/export")
def export_attendance(
    day: Optional[str] = None,
    centre_id: Optional[int] = None,
    user: dict = Depends(auth.require_staff),
):
    day = day or config.today_str()
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
    writer.writerow(["NSRS ID", "NAME", "DATE", "CONFIDENCE", "MARKED AT"])
    for r in records:
        writer.writerow([
            _csv_safe(r["roll_no"]),
            _csv_safe(r["name"]),
            _csv_safe(r["date"]),
            round(utils.similarity_to_confidence(r["confidence"], config.MATCH_THRESHOLD), 4),
            _csv_safe(r["marked_at"]),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance_{day}.csv"'},
    )


# --- static files & sample images -------------------------------------------

@app.get("/api/sample-images/download")
def download_test_suite(user: dict = Depends(auth.current_user)):
    """Zip of the demo images, for anyone trying the system out.

    Authenticated, like the single-image route below it. It zips whatever .jpg
    files are sitting in samples/test_suite/, and "whatever is in that folder"
    is not a promise anybody can keep - the rest of this repo's test images are
    photographs of real athletes. The directory is empty today; the guard is
    for the day it is not.
    """
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
    centre_id: Optional[int] = None,
    user: dict = Depends(auth.require_staff),
):
    """Photo metadata, scoped to the caller's centre.

    Authenticated because those filenames are the keys the two routes below
    take: leaving this open turns "guess a filename" into "enumerate them all,
    then download every enrolment portrait".

    That was only half the guard. `current_user` admits ANY signed-in account,
    including a self-registered athlete, and the query underneath had no centre
    predicate - so the enumeration this docstring set out to prevent worked
    perfectly well for anyone with a login, across every centre in the country.
    Staff only now, and narrowed by scope_centre, which pins a coach to their
    own centre whatever they ask for.
    """
    photos = database.get_photos(
        student_id=student_id, photo_type=photo_type, limit=limit,
        centre_id=auth.scope_centre(user, centre_id),
    )
    return {"photos": photos}


def _may_read_media(user: dict, name: str) -> None:
    """Refuse a media file that does not belong to the caller's centre.

    A super admin reads anything. A coach reads what their centre owns - and
    a file no table claims is not theirs either, because an unclaimed file is
    exactly the case that used to leak.

    The exception is liveness evidence (`clip_*`, `refused_*`): frames kept so a
    coach can see WHY a capture was refused. They are never written to a table,
    so they cannot be resolved to a centre, and they are not enumerable now that
    the history route is scoped - the name carries a timestamp to the
    millisecond. Staff may fetch those; nobody else can find them.
    """
    if user.get("role") == "super_admin":
        return
    base = Path(name).name
    if base.startswith(("clip_", "refused_")):
        return
    found, centre = database.media_centre(base)
    if not found or centre is None or int(centre) != int(user.get("centre_id") or -1):
        # 404 rather than 403: whether a given filename exists is itself the
        # thing being protected.
        raise HTTPException(404, "Not found")


@app.get("/api/photos/{name}")
def student_photo(name: str, user: dict = Depends(auth.require_staff)):
    """Enrolment portraits - photographs of children. Never unauthenticated."""
    _may_read_media(user, name)
    return storage.response("students", name)


@app.get("/api/uploads/{name}")
def uploaded_file(name: str, user: dict = Depends(auth.require_staff)):
    """Group photos and the face crops taken from them."""
    _may_read_media(user, name)
    return storage.response("uploads", name)


@app.get("/api/health")
def health(response: Response):
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
            # COUNT(*), not len(list_students()). This runs on every health
            # probe - the container checks every 30s, and so does whatever sits
            # in front of it - and it was loading every student row, with their
            # photo paths, to take a length.
            n_students = database.count_students()
        except Exception:  # noqa: BLE001
            db_ok = False

    # 503 WHEN THE DATABASE IS DOWN. The body still explains what is wrong -
    # that part was right, and an operator needs the detail - but the STATUS
    # said 200, so `curl -fsS` succeeded, the Docker HEALTHCHECK passed and the
    # platform's health check passed, and an instance that could not read or
    # write anything was reported healthy and kept in rotation. A health check
    # that cannot fail is not a health check.
    if not db_ok:
        response.status_code = 503
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "storage": storage.backend_name(),
        "detector": det_label,
        "recognizer": rec_label,
        "assignment_solver": metaheuristics.assignment_solver_name(),
        "restoration": "off (GFPGAN removed - licence)",
        "threshold": config.MATCH_THRESHOLD,
        "students": n_students,
    }



@app.get("/api/analytics")
def get_analytics(
    centre_id: Optional[int] = None,
    days: int = 30,
    user: dict = Depends(auth.require_staff),
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
def get_stats(user: dict = Depends(auth.require_staff)):
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

# The MediaPipe runtime and face model, fetched by
# scripts/fetch_frontend_models.py. Mounted only when present: the fetch is
# allowed to fail (a blocked CDN at build time), and the enrolment overlay falls
# back to server-side detection, so a missing directory must not stop the app
# from starting.
#
# These are immutable, content-pinned assets - a 12 MB wasm re-validated on every
# page load would be absurd - so they keep StaticFiles' default caching rather
# than the no-cache policy /static needs for code that changes between deploys.
_VENDOR_DIR = config.FRONTEND_DIR / "vendor"
if _VENDOR_DIR.is_dir():
    app.mount("/vendor", StaticFiles(directory=_VENDOR_DIR), name="vendor")
    log.info("Vendored browser assets served from %s", _VENDOR_DIR)
else:
    log.info("No frontend/vendor - the enrolment overlay will use server-side "
             "detection. Run: python -m scripts.fetch_frontend_models")

app.mount("/static", NoCacheStatic(directory=config.FRONTEND_DIR), name="static")
