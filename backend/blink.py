"""Did the eyes blink? Proof of a live face without moving the phone.

WHY A BLINK
-----------
Depth from parallax (liveness.py) needs the camera or the head to move, and
asking people to move the phone was the thing that kept failing: "barely
moved", "moved too fast", over and over. A blink needs no movement at all, and
a photograph cannot do it.

HOW IT IS MEASURED
------------------
Eye openness is the eye aspect ratio (EAR): the gap between the eyelids over
the width of the eye, from MediaPipe's 478-point face mesh, averaged over both
eyes. It does not depend on distance or head tilt. The mesh model is the SAME
one the browser runs for the tracking dots - read straight out of the vendored
face_landmarker.task and run through OpenCV's own TFLite importer, so there is
no new dependency and the phone and the server measure the same thing.

A blink is the eyes measured open, then closed below CLOSED_RATIO of their open
reading, then open again above OPEN_RATIO, within MAX_CLOSED_S. Relative to the
person's own open eyes, never a fixed number, because resting eye shape varies
between people. frontend/js/app.js applies the same rule live.

WHAT A BLINK DOES NOT CATCH
---------------------------
A VIDEO of the person replayed on a screen blinks. Parallax catches that replay
only when there is movement to measure, and liveness.analyse still refuses a
clip that moved and was flat. A replay held perfectly still is the gap this
trade opens - see liveness.analyse.

FAILS OPEN TO THE OLD BEHAVIOUR
-------------------------------
If the model file is missing (the Docker build does not fail when the MediaPipe
download does) this reports checked=False, and liveness falls back to depth
alone, exactly as before this module existed.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import config

log = logging.getLogger("blink")

TASK_PATH = (Path(__file__).resolve().parent.parent
             / "frontend" / "vendor" / "mediapipe" / "face_landmarker.task")
MODEL_MEMBER = "face_landmarks_detector.tflite"

# MediaPipe mesh indices, per eye: corner, two upper lid points, corner, two
# lower lid points. The same indices as EAR_EYES in app.js.
EAR_EYES = ((33, 160, 158, 133, 153, 144), (362, 385, 387, 263, 373, 380))

CLOSED_RATIO = 0.60        # closed below this fraction of the open reading
OPEN_RATIO = 0.80          # open again above this fraction
MAX_CLOSED_S = 2.0         # longer than this is eyes shut, not a blink
# Searched either side of the phone's reported blink. The phone reports the
# moment the eyes REOPENED, so the closing sits a few hundred ms before it, and
# the recording starts up to ~200ms after the camera does. 0.6s covers both;
# every extra 0.1s is another ~6 landmark passes on every attendance.
WINDOW_S = 0.6
MAX_FRAMES = 60            # landmark passes per clip, whatever its length
MIN_FACE_PX = 80           # below this the eyelids are too few pixels to judge
INPUT = 256                # the mesh model's input size

_net = None
_tried = False
_lock = threading.Lock()


def _load():
    """The mesh model, loaded once; None when it is not available."""
    global _net, _tried
    with _lock:
        if _tried:
            return _net
        _tried = True
        if not TASK_PATH.exists():
            log.warning("Blink check unavailable: %s is missing - liveness uses depth only",
                        TASK_PATH)
            return None
        try:
            raw = zipfile.ZipFile(TASK_PATH).read(MODEL_MEMBER)
            _net = cv2.dnn.readNetFromTFLite(np.frombuffer(raw, np.uint8))
            log.info("Blink check ready (%s from %s)", MODEL_MEMBER, TASK_PATH.name)
        except Exception as e:                       # noqa: BLE001
            log.warning("Blink check unavailable: could not load the mesh model: %s", e)
            _net = None
        return _net


def available() -> bool:
    return _load() is not None


def _crop(frame: np.ndarray, face) -> np.ndarray:
    """The face, eyes level, 1.5x its box, at the model's input size."""
    x1, y1, x2, y2 = face.box
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * 1.5
    angle = 0.0
    if face.landmarks is not None and len(face.landmarks) >= 2:
        (rx, ry), (lx, ly) = face.landmarks[0], face.landmarks[1]
        angle = float(np.degrees(np.arctan2(ly - ry, lx - rx)))
    m = cv2.getRotationMatrix2D((cx, cy), angle, INPUT / max(side, 1.0))
    m[0, 2] += INPUT / 2.0 - cx
    m[1, 2] += INPUT / 2.0 - cy
    return cv2.warpAffine(frame, m, (INPUT, INPUT))


def eye_aspect(points: np.ndarray) -> Optional[float]:
    """EAR averaged over both eyes, from (478, 2+) mesh points."""
    total = 0.0
    for p1, p2, p3, p4, p5, p6 in EAR_EYES:
        width = float(np.linalg.norm(points[p1, :2] - points[p4, :2]))
        if width <= 1e-6:
            return None
        gap = (float(np.linalg.norm(points[p2, :2] - points[p6, :2]))
               + float(np.linalg.norm(points[p3, :2] - points[p5, :2])))
        total += gap / (2.0 * width)
    return total / len(EAR_EYES)


def _ear_of(net, crop: np.ndarray) -> Optional[float]:
    blob = np.ascontiguousarray(
        crop[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)[None] / 255.0)
    net.setInput(blob)
    # By name: OpenCV's newer graph engine exposes only the face-presence score
    # as an unconnected output, while the 1434 mesh values are "Identity".
    out = net.forward("Identity")
    if out.size != 1434:
        return None
    return eye_aspect(out.reshape(478, 3))


def find_blink(ears: List[Optional[float]], times: List[float]) -> Optional[float]:
    """Time (s) the eyes closed for a blink, or None. See the module docstring.

    The open reading is the 90th percentile of the whole window: open eyes are
    most of any clip, so it is not dragged down by the blink it is judging.
    """
    valid = [e for e in ears if e is not None and np.isfinite(e)]
    if len(valid) < 4:
        return None
    base = float(np.percentile(valid, 90))
    if base <= 0:
        return None
    seen_open = False
    closed_at = None
    for e, t in zip(ears, times):
        if e is None or not np.isfinite(e):
            continue
        if e >= base * OPEN_RATIO:
            if closed_at is not None and seen_open and t - closed_at <= MAX_CLOSED_S:
                return closed_at
            closed_at = None
            seen_open = True
        elif e < base * CLOSED_RATIO and seen_open and closed_at is None:
            closed_at = t
    return None


def _face_crops(data: bytes, detector, blink_at_ms: Optional[float],
                rotation: Optional[int]) -> Tuple[List[np.ndarray], List[float]]:
    """Face crops with their times: around the reported blink when there is one,
    otherwise spread over the whole clip."""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    crops: List[np.ndarray] = []
    times: List[float] = []
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return [], []
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not 1.0 <= fps <= 120.0:
            fps = 30.0
        lo = hi = None
        if blink_at_ms is not None:
            lo, hi = blink_at_ms / 1000.0 - WINDOW_S, blink_at_ms / 1000.0 + WINDOW_S
        # Around a reported blink, every frame up to ~30fps; otherwise a stride
        # that doubles whenever the kept frames overflow, like sample_frames.
        stride = max(1, int(round(fps / 30.0))) if lo is not None else 1
        face = None
        idx = 0
        last_ms = -1.0
        while idx < 1800:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
            t = ms / 1000.0 if ms > last_ms else idx / fps
            last_ms = max(last_ms, ms)
            i = idx
            idx += 1
            if lo is not None and not (lo <= t <= hi):
                continue
            if i % stride:
                continue
            if rotation is not None:
                frame = cv2.rotate(frame, rotation)
            if frame.shape[1] > 640:
                s = 640.0 / frame.shape[1]
                frame = cv2.resize(frame, (640, int(round(frame.shape[0] * s))))
            # The face barely moves in a clip held still for a blink, so it is
            # found again only every few kept frames.
            if face is None or len(crops) % 6 == 0:
                faces = detector.detect_robust(frame, config.CLIP_DETECTION_MODE)
                if faces:
                    face = max(faces, key=lambda f: f.width * f.height)
            if face is None or min(face.width, face.height) < MIN_FACE_PX:
                continue
            crops.append(_crop(frame, face))
            times.append(t)
            if lo is None and len(crops) > MAX_FRAMES * 2:
                crops, times = crops[::2], times[::2]
                stride *= 2
        cap.release()
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if len(crops) > MAX_FRAMES:
        keep = np.linspace(0, len(crops) - 1, MAX_FRAMES).astype(int)
        crops = [crops[k] for k in keep]
        times = [times[k] for k in keep]
    return crops, times


def detect(data: bytes, detector, blink_at_ms: Optional[float] = None,
           rotation: Optional[str] = None) -> dict:
    """Look for a blink in the clip. Never raises.

    `blink_at_ms` is when the phone saw the blink, from the start of the
    recording. It only narrows WHERE to look - the blink must still be in the
    video - so a page reporting a blink that did not happen gains nothing.
    `rotation` is liveness.upright's name for how the clip was turned upright.
    """
    out = {"checked": False, "blinked": False, "frames": 0}
    net = _load()
    if net is None:
        out["reason"] = "model unavailable"
        return out
    try:
        from .liveness import _ROTATIONS
        rot = next((code for code, name in _ROTATIONS if name == rotation), None)
        crops, times = _face_crops(data, detector, blink_at_ms, rot)
        if len(crops) < 4 and blink_at_ms is not None:
            # The reported time did not line up with the video's own clock.
            crops, times = _face_crops(data, detector, None, rot)
        if len(crops) < 4:
            out["reason"] = "too few face frames"
            return out
        with _lock:
            ears = [_ear_of(net, c) for c in crops]
        out.update(checked=True, frames=len(crops))
        valid = [e for e in ears if e is not None]
        if valid:
            out["ear_open"] = round(float(np.percentile(valid, 90)), 3)
            out["ear_min"] = round(float(min(valid)), 3)
        at = find_blink(ears, times)
        if at is not None:
            out.update(blinked=True, at_s=round(float(at), 2))
    except Exception as e:                           # noqa: BLE001
        log.warning("Blink check failed on a clip: %s", e)
        out["reason"] = "error"
    return out
