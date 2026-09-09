"""Pick the frame somebody is looking at the camera in, and crop to their face.

The enrolment photo was whatever frame the liveness check happened to like best,
stored whole. On the Athlete Directory that produced a wall of wide shots -
people mid-turn, looking at the floor, half out of frame, with two other faces
in the background. A coach scanning that grid to find one athlete cannot use it,
and it is the picture a person sees of themselves in the system.

Two jobs, both cheap because the detector already measures what is needed:

  CHOOSE   `FaceQualityAssessor.assess` gives yaw, pitch and a blur score per
           detection. The best portrait is the most frontal one, with sharpness
           and size breaking ties - not the largest face, which is whoever
           leaned closest to the lens.

  CROP     A square around the face with room for hair and chin, clamped to the
           frame. Square because the grid shows squares, and a crop the UI then
           has to letterbox or cut is a crop that decided nothing.

Recognition is untouched. Templates are still built from the full frames; this
only decides the single JPEG a human looks at.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .detector import FaceQualityAssessor

log = logging.getLogger(__name__)

# How much of the box width to add around the face. 0.45 keeps the top of the
# head and the chin: tighter than this and people are cropped at the eyebrows,
# which reads as a mugshot rather than a portrait.
MARGIN = 0.45

# Below this the crop is upscaled rather than shown small, so the card is not a
# blurry postage stamp on a high-DPI screen.
MIN_SIDE = 320


def _score(quality: dict, face) -> float:
    """Higher is a better portrait.

    Frontality dominates: a sharp photo of the side of someone's head is not
    the picture you want next to their name. Blur and size only separate faces
    that are already looking at the camera.
    """
    yaw = abs(float(quality.get("yaw") or 0.0))
    pitch = abs(float(quality.get("pitch") or 0.0))
    if not quality.get("pose_reliable"):
        # No real landmarks, so the angles carry no information - see
        # detector.estimate_landmarks. Rank on sharpness alone, below anything
        # whose pose is actually known.
        yaw = pitch = 45.0
    blur = float(quality.get("blur_score") or 0.0)
    area = float(face.width * face.height)
    return (-2.0 * yaw) + (-1.0 * pitch) + (12.0 * min(blur, 400.0) / 400.0) \
        + (4.0 * min(area, 200_000.0) / 200_000.0)


def crop_face(img: np.ndarray, face) -> np.ndarray:
    """A square crop around one face, clamped to the image."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = face.box
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    # Capped to the frame's short edge first. Without this, a face in a narrow
    # image asked for a square bigger than the picture, and the clamping below
    # produced a tall sliver - 181x649 on one test - which is not a portrait
    # and looks wrong in a grid of squares.
    side = min(max(x2 - x1, y2 - y1) * (1.0 + 2 * MARGIN), float(min(w, h)))
    # Nudge upward: a face sits above the centre of a head-and-shoulders frame,
    # and centring on the box alone cuts the hair and leaves chest.
    cy -= side * 0.06
    half = side / 2.0
    left, top = int(round(cx - half)), int(round(cy - half))
    right, bottom = int(round(cx + half)), int(round(cy + half))

    # Clamp by SHIFTING before shrinking, so a face near an edge keeps its size
    # instead of being sliced into a rectangle.
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > w:
        left -= right - w
        right = w
    if bottom > h:
        top -= bottom - h
        bottom = h
    left, top = max(0, left), max(0, top)
    right, bottom = min(w, right), min(h, bottom)
    if right - left < 8 or bottom - top < 8:
        return img
    return img[top:bottom, left:right]


def choose(frames: Sequence[np.ndarray], detector) -> Tuple[Optional[np.ndarray], dict]:
    """The best portrait across these frames, cropped. (None, info) if no face.

    Falls back to the first frame uncropped rather than returning nothing: an
    enrolment that worked should not lose its photograph because the portrait
    step was fussy.
    """
    import cv2  # noqa: F401  (cv2 is already a hard dependency; imported for resize)

    best = None
    best_score = float("-inf")
    info = {"considered": 0, "with_face": 0}

    for frame in frames:
        if frame is None:
            continue
        info["considered"] += 1
        try:
            faces = detector.detect(frame)
        except Exception as e:                      # noqa: BLE001
            log.warning("Portrait: detection failed on a frame: %s", e)
            continue
        if not faces:
            continue
        info["with_face"] += 1
        # The subject is the largest face; other people in shot are background.
        face = max(faces, key=lambda f: f.width * f.height)
        q = FaceQualityAssessor.assess(frame, face)
        sc = _score(q, face)
        if sc > best_score:
            best_score = sc
            best = (frame, face, q)

    if best is None:
        first = next((f for f in frames if f is not None), None)
        info["fallback"] = "no face found; stored the frame whole"
        return first, info

    frame, face, q = best
    crop = crop_face(frame, face)
    if crop.shape[0] < MIN_SIDE:
        scale = MIN_SIDE / max(1, crop.shape[0])
        crop = cv2.resize(crop, (int(crop.shape[1] * scale), MIN_SIDE),
                          interpolation=cv2.INTER_CUBIC)
    info.update({"yaw": round(float(q.get("yaw") or 0.0), 1),
                 "pitch": round(float(q.get("pitch") or 0.0), 1),
                 "blur": round(float(q.get("blur_score") or 0.0), 1),
                 "score": round(best_score, 2)})
    return crop, info


def from_single(img: np.ndarray, detector) -> Tuple[np.ndarray, dict]:
    """Crop one already-chosen image to its subject. Never returns None."""
    out, info = choose([img], detector)
    return (out if out is not None else img), info
