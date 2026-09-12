"""Image decoding, annotation rendering and small shared helpers."""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import storage
from .detector import Face

# Palette (BGR-independent RGB tuples used via PIL)
GREEN = (34, 197, 94)      # recognized
AMBER = (245, 158, 11)     # unknown face
WHITE = (255, 255, 255)

_font_cache = {}


def _font(size: int):
    if size not in _font_cache:
        for name in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf"):
            try:
                _font_cache[size] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def decode_image(data: bytes) -> np.ndarray:
    """Decode uploaded bytes, raising ValueError for anything unusable.

    cv2.imdecode does NOT uniformly return None on bad input: an empty buffer
    trips an assertion inside OpenCV and raises cv2.error, which reached the
    client as a 500. Both failure modes are normalised here.
    """
    if not data:
        raise ValueError("Uploaded file is empty")
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as e:
        raise ValueError("Not a valid image file") from e
    if img is None or img.size == 0:
        raise ValueError("Not a valid image file")
    return img


def timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"


def crop_face(img_bgr: np.ndarray, face: Face, pad: float = 0.25) -> np.ndarray:
    x1, y1, x2, y2 = face.box
    w, h = x2 - x1, y2 - y1
    px, py = w * pad, h * pad
    x1 = max(int(x1 - px), 0)
    y1 = max(int(y1 - py), 0)
    x2 = min(int(x2 + px), img_bgr.shape[1])
    y2 = min(int(y2 + py), img_bgr.shape[0])
    return img_bgr[y1:y2, x1:x2]


def similarity_to_confidence(sim: float, threshold: float = 0.40) -> float:
    """Calibrate raw cosine similarity into human-readable recognition confidence (0.0 - 1.0).

    Calibrated in 512-d ArcFace space, which is NOT what it now receives:
    SFace produces 128-d embeddings with a different score distribution, so
    these constants are inherited rather than re-fitted. The displayed
    percentage is therefore indicative; MATCH_THRESHOLD governs the actual
    decision and is calibrated on this system's own data.

    Original fit:
      - At threshold boundary -> 75% match
      - 0.55 -> 88% match
      - 0.65 -> 95% match
      - 0.75+ -> 99% match
    """
    thr = float(threshold) if threshold is not None else 0.40
    if sim >= thr:
        norm = (sim - thr) / max(1.0 - thr, 1e-6)
        conf = 0.75 + 0.245 * (1.0 - np.exp(-3.5 * norm)) / (1.0 - np.exp(-3.5))
        return float(np.clip(conf, 0.75, 0.999))
    else:
        norm = max(0.0, sim) / max(thr, 1e-6)
        return float(np.clip(norm * 0.70, 0.0, 0.74))


def annotate(
    img_bgr: np.ndarray,
    faces: List[Face],
    labels: List[Optional[str]],
    confs: List[Optional[float]],
) -> np.ndarray:
    """Draw labeled boxes over detected faces (green=named student, amber=unknown)."""
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    overlay = Image.new("RGBA", pil.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    for face, label, conf in zip(faces, labels, confs):
        x1, y1, x2, y2 = [int(v) for v in face.box]
        w = x2 - x1
        color = GREEN if label else AMBER
        lw = max(2, w // 90)
        r = max(4, w // 28)

        # Translucent fill + stroked rounded rectangle
        d.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=color + (36,))
        d.rounded_rectangle([x1, y1, x2, y2], radius=r, outline=color + (255,), width=lw)

        if label:
            text = f"{label}"
            if conf is not None:
                text += f"  {conf * 100:.0f}%"
        else:
            text = "Unknown"

        fsize = max(13, min(26, w // 9))
        font = _font(fsize)
        tb = d.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad = 6
        ty = y1 - th - 2 * pad
        if ty < 0:
            ty = y2  # flip label below the box if it would clip the top
        d.rounded_rectangle(
            [x1, ty, x1 + tw + 2 * pad, ty + th + 2 * pad],
            radius=6,
            fill=color + (235,),
        )
        d.text((x1 + pad, ty + pad - tb[1]), text, font=font, fill=WHITE + (255,))

    pil = Image.alpha_composite(pil.convert("RGBA"), overlay).convert("RGB")
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def save_image(img_bgr: np.ndarray, prefix: str, name: str) -> str:
    """Encode as JPEG and hand the bytes to the configured storage backend.

    Takes (prefix, name) rather than a filesystem path because the destination
    is no longer necessarily a filesystem - under FACEMARK_STORAGE=s3 there is
    no directory to create and no path to write. `prefix` is "students" or
    "uploads"; the returned bare name is what the database stores.
    """
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError("Could not encode image as JPEG")
    return storage.put(prefix, name, buf.tobytes())
