"""Face detection with OpenCV YuNet.

LICENSING - the reason this module looks the way it does.
This software is deployed as a network service, so every component must permit
commercial use and redistribution without obliging us to publish the whole
application. That rules out the two most obvious choices:

    Ultralytics YOLO   AGPL-3.0. Its network clause obliges anyone serving the
                       software over a network to offer users the complete
                       corresponding source of the entire application.
    InsightFace SCRFD  "ALL models are available for non-commercial research
                       purposes only" per their model zoo.

YuNet is MIT-licensed and ships inside OpenCV (Apache-2.0), so nothing here
carries an obligation beyond attribution.

It is also dramatically lighter - 227 KB against roughly 1 GB - and measured on
this project's own data it detects the same faces: 13 on the ground-truth photo
where the previous stack also found 13, in 0.5 s rather than 13 s.

YuNet returns five real landmarks with every detection, which additionally
fixes a latent bug: the old pipeline fell back to landmarks estimated from the
bounding box, and because that template is symmetric the yaw estimate was
always exactly 0.0 (measured mean 0.0, sd 0.0 across 13 photos).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import config

log = logging.getLogger("detector")

# Where the 5 landmarks sit inside a tight face box, as fractions. Only used for
# a crop that was handed to us without detection metadata.
_LM_FRAC = np.array(
    [
        [0.34, 0.46], [0.66, 0.46],   # eyes
        [0.50, 0.64],                 # nose
        [0.39, 0.82], [0.61, 0.82],   # mouth corners
    ],
    dtype=np.float32,
)


@dataclass
class Face:
    box: Tuple[float, float, float, float]          # x1, y1, x2, y2 (pixels)
    conf: float
    landmarks: Optional[np.ndarray] = None          # (5, 2) xy, real from YuNet
    source: str = "yunet"
    quality: Optional[dict] = None
    raw: Optional[np.ndarray] = None                # YuNet's 15-value row, for alignCrop

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


def estimate_landmarks(box) -> np.ndarray:
    """Approximate landmarks for a bare crop with no detection behind it.

    A last resort. The points are symmetric by construction, so anything derived
    from them - yaw especially - carries no information about the actual pose.
    """
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    return np.stack([x1 + w * _LM_FRAC[:, 0], y1 + h * _LM_FRAC[:, 1]], axis=1).astype(np.float32)


def looks_printed(img_bgr: np.ndarray, box) -> bool:
    """True when a detection looks like a printed portrait rather than a person.

    Sports centres photograph groups in front of banners carrying printed
    portraits, and a detector finds those exactly as it finds real faces. A
    print under bright light is desaturated and bright compared with skin lit by
    the same source.

    All three conditions must agree. Each alone describes plenty of real faces -
    an overexposed face is bright, a shaded one desaturated - so requiring all
    three is what keeps a real athlete from being dropped. Measured across 68
    detections in four photos it fired on the one poster and none of the 67
    real faces.
    """
    if not getattr(config, "REJECT_PRINTED_FACES", False):
        return False
    x1, y1, x2, y2 = [max(0, int(v)) for v in box]
    crop = img_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    return bool(
        sat.mean() < config.PRINT_MAX_SATURATION
        and val.mean() > config.PRINT_MIN_BRIGHTNESS
        and sat.std() < config.PRINT_MAX_SAT_STDDEV
    )


class FaceQualityAssessor:
    """Blur, exposure and pose for one detection."""

    @staticmethod
    def assess(img_bgr, face) -> dict:
        x1, y1, x2, y2 = [int(v) for v in face.box]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img_bgr.shape[1], x2), min(img_bgr.shape[0], y2)
        crop = img_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return {"blur_score": 0, "brightness": 0, "yaw": 0.0, "pitch": 0.0,
                    "pose_reliable": False, "resolution": 0, "is_usable": False,
                    "quality_penalty": 1.0}

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(crop.mean())

        # YuNet always supplies real landmarks, so pose is measurable here in a
        # way it was not before. The 0.95 constant is the nose's vertical offset
        # below the eye line on a frontal face, in eye-width units; the previous
        # 0.35 put a frontal face at +36 degrees (measured mean +36.4, sd 2.3).
        yaw = pitch = 0.0
        pose_reliable = face.landmarks is not None and face.source.startswith("yunet")
        if face.landmarks is not None and face.landmarks.shape == (5, 2):
            lm = face.landmarks
            eye_center = (lm[0] + lm[1]) / 2
            eye_dist = float(np.linalg.norm(lm[1] - lm[0]))
            if eye_dist > 1e-3:
                yaw = float(np.degrees(np.arctan2(((lm[2][0] - eye_center[0]) / eye_dist) * 2, 1.0)))
                # 0.550 is the measured nose-below-eyes ratio for a
                # camera-facing face (median over 79 faces in the five test
                # photographs, sd 0.059). The previous 0.95 was never
                # calibrated and reported -38 degrees for people looking
                # straight at the lens, which made pitch unusable as an
                # absolute angle.
                pitch = float(np.degrees(np.arctan2((((lm[2][1] - eye_center[1]) / eye_dist) - 0.550) * 2, 1.0)))

        resolution = min(face.width, face.height)
        is_usable = (blur > config.MIN_BLUR_SCORE and 40 < brightness < 240
                     and (not pose_reliable or abs(yaw) < config.MAX_YAW_ANGLE))

        penalty = 0.0
        if not is_usable:
            if blur <= config.MIN_BLUR_SCORE:
                penalty += 0.05
            if brightness <= 40 or brightness >= 240:
                penalty += 0.03
            if pose_reliable and abs(yaw) >= config.MAX_YAW_ANGLE:
                penalty += 0.05

        return {"blur_score": blur, "brightness": brightness, "yaw": yaw, "pitch": pitch,
                "pose_reliable": pose_reliable, "resolution": resolution,
                "is_usable": is_usable, "quality_penalty": penalty}


class FaceDetector:
    """YuNet, wrapped so the rest of the app is unchanged by the swap."""

    def __init__(self, mode: Optional[str] = None):
        self._lock = threading.Lock()
        self.mode = (mode or config.DETECTION_MODE).lower()
        self.last_filtered_printed = 0
        self._model_path = config.MODELS_DIR / config.YUNET_MODEL
        if not self._model_path.exists():
            raise FileNotFoundError(
                f"YuNet model missing: {self._model_path}. "
                "Run: python -m scripts.download_models"
            )
        self._detector = None      # created per image; input size is baked in
        log.info("Detector ready: YuNet (%s)", config.YUNET_MODEL)

    @property
    def backend_label(self) -> str:
        return f"yunet ({self.mode})"

    def _score_for(self, mode: str) -> float:
        # Modes trade recall against false detections rather than swapping
        # architectures, since there is only one detector now.
        return {
            "fast": config.YUNET_SCORE_FAST,
            "fused": config.YUNET_SCORE,
            "accurate": config.YUNET_SCORE_ACCURATE,
        }.get(mode, config.YUNET_SCORE)

    def detect(self, img_bgr: np.ndarray, mode: Optional[str] = None) -> List[Face]:
        mode = (mode or self.mode).lower()
        h, w = img_bgr.shape[:2]
        score = self._score_for(mode)

        with self._lock:
            det = cv2.FaceDetectorYN.create(
                str(self._model_path), "", (w, h), score, config.YUNET_NMS,
                config.MAX_FACES_PER_IMAGE,
            )
            det.setInputSize((w, h))
            _, rows = det.detect(img_bgr)

        faces: List[Face] = []
        printed = 0
        for row in (rows if rows is not None else []):
            x, y, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            if min(bw, bh) < config.MIN_FACE_SIZE:
                continue
            ar = bh / (bw + 1e-5)
            if ar < 0.50 or ar > 2.20:
                continue                      # slivers are never faces
            box = (x, y, x + bw, y + bh)
            if looks_printed(img_bgr, box):
                printed += 1
                continue
            f = Face(
                box=box,
                conf=float(row[14]),
                landmarks=np.array(row[4:14], dtype=np.float32).reshape(5, 2),
                source="yunet",
                raw=np.asarray(row, dtype=np.float32),
            )
            f.quality = FaceQualityAssessor.assess(img_bgr, f)
            faces.append(f)

        if printed:
            log.info("Filtered %d printed/poster face(s) from the photo.", printed)
        self.last_filtered_printed = printed
        faces.sort(key=lambda f: f.box[0])
        return faces


_detector: Optional[FaceDetector] = None


def get_detector() -> FaceDetector:
    global _detector
    if _detector is None:
        _detector = FaceDetector()
    return _detector
