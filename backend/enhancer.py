"""Image quality helpers.

GFPGAN face restoration was removed in the licence migration. Its weights derive
in part from NVIDIA's StyleGAN2, which is not licensed for commercial use, and
this application is deployed as a network service.

Nothing of value was lost. Restoration existed to pull a degraded printed ID
photo toward the domain of live photographs, and two measurements settled its
worth:

  - Degrading enrolment photos to PDF-thumbnail scale changed recall not at all
    (12/13 either way, mean score 0.580 against 0.576), so the restoration path
    was not what made low-resolution enrolment work.
  - Applying restoration to QUERY faces made results worse - 12/13 against
    13/13, and 5/22 against 6/22 - because it invents plausible detail that is
    not the person's, moving the embedding away from their template.

The real fix for a stale or degraded reference photo is a current one, which is
what guided multi-view capture provides.

What remains here is cheap, dependency-free measurement: a sharpness score used
to rank templates, and CLAHE for faces under unusual lighting.
"""
from __future__ import annotations

import logging
from typing import Optional

import cv2
import numpy as np

from . import config

log = logging.getLogger("enhancer")


def normalize_illumination(crop_bgr: np.ndarray) -> np.ndarray:
    """CLAHE on the L channel of LAB, to even out harsh lighting."""
    if not getattr(config, "CLAHE_ENABLED", False):
        return crop_bgr
    lab = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(
        clipLimit=getattr(config, "CLAHE_CLIP_LIMIT", 2.0),
        tileGridSize=(getattr(config, "CLAHE_TILE_SIZE", 8),) * 2,
    )
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def should_normalize(crop_bgr: np.ndarray) -> bool:
    mean = crop_bgr.mean()
    return mean < 60 or mean > 200


def sharpness_quality(crop_bgr: np.ndarray) -> float:
    """Perceptual sharpness in [0, 1] - variance of Laplacian, log-scaled.

    Used to rank a student's templates and to show enrolment quality. Printed
    ID scans typically land below 0.35, sharp digital photos above 0.6.
    """
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    var = cv2.Laplacian(gray, cv2.CV_64F).var()
    return float(np.clip(np.log1p(var) / np.log1p(2500.0), 0.0, 1.0))


class FaceEnhancer:
    """Retained so callers need not change. Restoration is permanently off."""

    def __init__(self):
        self.restorer = None

    @property
    def restoration_enabled(self) -> bool:
        return False

    def normalize_if_needed(self, crop_bgr: np.ndarray) -> np.ndarray:
        return normalize_illumination(crop_bgr) if should_normalize(crop_bgr) else crop_bgr

    def restore(self, img_bgr: np.ndarray, landmarks) -> Optional[np.ndarray]:
        """Always None. Callers already treat that as 'restoration unavailable'."""
        return None


_enhancer: Optional[FaceEnhancer] = None


def get_enhancer() -> FaceEnhancer:
    global _enhancer
    if _enhancer is None:
        _enhancer = FaceEnhancer()
    return _enhancer
