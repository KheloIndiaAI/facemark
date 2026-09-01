"""Liveness from a short video: is this a person, or a photograph of one?

WHY VIDEO, AND WHY THIS SIGNAL
------------------------------
A single frame can only be judged on appearance, and appearance is exactly what
a replay reproduces. Two earlier attempts failed for that reason:

  moire + bezel   assumed a lit screen inside a dark surround. A phone held up
                  in a bright office defeats it, as one did.
  3D landmarks    MediaPipe infers z from 2D appearance, so a photo of a face
                  yields the same mesh as the face. Measured: plane-fit residual
                  0.1244 for real faces vs 0.1239 for replays - no separation.

Motion is different, because it carries information a still image does not have.
A photograph on a screen is a PLANE. Under camera or hand movement every point
on a plane maps through one homography - that is projective geometry, not a
heuristic. A real face is not planar: the nose is centimetres nearer the lens
than the ears, so no single homography fits, and the residual is the depth.

So the test is: track points across the clip, fit the best homography, and ask
how badly it fails. Near-zero means flat, which means a screen or a print.

WHAT THIS DOES NOT CATCH
------------------------
A video of a real person replayed on a screen is still a plane, so it fails the
same way and IS caught. What defeats this is a genuine 3D artefact - a mask, or
a second live person. That is a far higher bar than holding up a phone, which is
the attack this exists to stop, but it is not "solved liveness" and must not be
described as such.

The other limit is honest and structural: parallax needs motion. If the camera
and subject are both perfectly still, there is no depth information in the clip
at all, and this returns "inconclusive" rather than guessing. A caller must
decide what to do with that - it is not the same answer as "live".
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import config

log = logging.getLogger("liveness")


@dataclass
class LivenessResult:
    verdict: str                     # "live" | "screen" | "inconclusive" | "no_face"
    reason: str
    depth_score: float = 0.0         # homography residual, normalised by face width
    motion: float = 0.0              # median tracked-point displacement, same units
    frames_used: int = 0
    tracked_points: int = 0
    frames: List[np.ndarray] = field(default_factory=list)   # sampled, for storage
    best_frame: Optional[np.ndarray] = None                  # sharpest face frame

    @property
    def is_live(self) -> bool:
        return self.verdict == "live"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "depth_score": round(self.depth_score, 4),
            "motion": round(self.motion, 4),
            "frames_used": self.frames_used,
            "tracked_points": self.tracked_points,
        }


def sample_frames(data: bytes, max_frames: int = None) -> Tuple[List[np.ndarray], dict]:
    """Decode the clip and return evenly-spaced frames plus what we learned about it.

    OpenCV cannot decode from memory, so the bytes go to a temporary file. The
    browser decides the container - Chrome records WebM/VP8, Safari MP4/H.264 -
    and both are read through the same FFmpeg backend, so nothing here depends
    on which one arrived.
    """
    max_frames = max_frames or config.LIVENESS_SAMPLE_FRAMES
    info = {"frames_total": 0, "fps": 0.0, "duration_s": 0.0}

    with tempfile.NamedTemporaryFile(suffix=".video", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        cap = cv2.VideoCapture(str(tmp_path))
        if not cap.isOpened():
            return [], info

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        info["frames_total"] = total
        info["fps"] = fps
        info["duration_s"] = (total / fps) if fps > 0 else 0.0

        frames: List[np.ndarray] = []
        if total > 0:
            # Evenly spaced across the whole clip: parallax grows with the
            # distance between viewpoints, so the first and last frames are
            # worth more than any two adjacent ones.
            wanted = np.linspace(0, total - 1, min(max_frames, total)).astype(int)
            for idx in wanted:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
        else:
            # Some WebM files report no frame count. Read sequentially instead
            # of trusting the header.
            step_guard = 0
            while len(frames) < max_frames and step_guard < 600:
                ok, frame = cap.read()
                step_guard += 1
                if not ok:
                    break
                if step_guard % 4 == 1:
                    frames.append(frame)
            info["frames_total"] = step_guard
        cap.release()
        return frames, info
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _largest_face(frame: np.ndarray, detector):
    faces = detector.detect(frame, mode="fused")
    if not faces:
        return None
    return max(faces, key=lambda f: f.width * f.height)


def _depth_from_parallax(
    frames: List[np.ndarray], box
) -> Tuple[float, float, int]:
    """Return (depth_score, motion, n_points) for the region in `box`.

    depth_score is the median distance, in face-widths, by which tracked points
    disagree with the single best homography between the first frame and each
    later one. A plane gives ~0 whatever it does; a face gives more the more the
    viewpoint changes.
    """
    x1, y1, x2, y2 = [int(v) for v in box]
    fw = max(1.0, float(x2 - x1))

    first_gray = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
    # Track only the face itself. Including the background would measure the
    # room's parallax rather than the subject's, and a phone's own bezel would
    # supply beautifully planar points that are not the question.
    mask = np.zeros(first_gray.shape, dtype=np.uint8)
    mask[max(0, y1):y2, max(0, x1):x2] = 255

    pts0 = cv2.goodFeaturesToTrack(
        first_gray, maxCorners=220, qualityLevel=0.01,
        minDistance=4, mask=mask, blockSize=7,
    )
    if pts0 is None or len(pts0) < config.LIVENESS_MIN_POINTS:
        return 0.0, 0.0, 0 if pts0 is None else len(pts0)

    residuals: List[float] = []
    motions: List[float] = []
    n_used = len(pts0)

    for frame in frames[1:]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        pts1, status, _ = cv2.calcOpticalFlowPyrLK(
            first_gray, gray, pts0, None,
            winSize=(21, 21), maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        )
        if pts1 is None:
            continue
        good = status.ravel() == 1
        a, b = pts0[good], pts1[good]
        if len(a) < config.LIVENESS_MIN_POINTS:
            continue

        motions.append(float(np.median(np.linalg.norm(b - a, axis=2))) / fw)

        # RANSAC finds the dominant plane. On a photograph that plane explains
        # every point; on a face it explains one surface and leaves the rest -
        # which is exactly the quantity of interest, so the residual is measured
        # over ALL points, not just the inliers RANSAC kept.
        H, _ = cv2.findHomography(a, b, cv2.RANSAC, 3.0)
        if H is None:
            continue
        projected = cv2.perspectiveTransform(a, H)
        err = np.linalg.norm(projected - b, axis=2).ravel()
        residuals.append(float(np.median(err)) / fw)

    if not residuals:
        return 0.0, 0.0, n_used
    return float(np.median(residuals)), float(np.median(motions or [0.0])), n_used


def analyse(data: bytes, detector) -> LivenessResult:
    """Judge a short clip. Never raises for bad input - it returns a verdict."""
    if not config.LIVENESS_ENABLED:
        # Disabled means "skip the judgement", NOT "skip the decoding". Every
        # caller goes on to use result.frames and result.best_frame - the
        # attendance route encodes best_frame, registration indexes frames[0] -
        # so returning "live" with an empty result made flipping this flag off
        # crash all three video endpoints with a 500. The kill switch has to
        # leave the pipeline usable, or it is a self-destruct switch.
        frames, _ = sample_frames(data)
        if not frames:
            return LivenessResult(
                "inconclusive",
                "Could not read any frames from this clip - is it a video?",
            )
        result = LivenessResult(
            "live", "Liveness checking is disabled",
            frames_used=len(frames), frames=frames,
        )
        result.best_frame = max(
            frames,
            key=lambda f: cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var(),
        )
        return result

    if len(data) > config.LIVENESS_MAX_BYTES:
        return LivenessResult(
            "inconclusive",
            f"Clip is larger than the {config.LIVENESS_MAX_BYTES // (1024*1024)} MB limit",
        )

    frames, info = sample_frames(data)
    if len(frames) < 2:
        return LivenessResult(
            "inconclusive",
            "Could not read enough frames from this clip - is it a video?",
        )

    face = _largest_face(frames[0], detector)
    if face is None:
        # Try the middle of the clip: the first frame is often the worst, caught
        # before the camera has settled or the subject is in position.
        face = _largest_face(frames[len(frames) // 2], detector)
    if face is None:
        return LivenessResult("no_face", "No face was found in the clip",
                              frames_used=len(frames), frames=frames)

    depth, motion, n_pts = _depth_from_parallax(frames, face.box)

    result = LivenessResult(
        verdict="inconclusive", reason="", depth_score=depth, motion=motion,
        frames_used=len(frames), tracked_points=n_pts, frames=frames,
    )
    # The sharpest frame carries the most identity signal, so recognition should
    # run on that rather than on whichever frame happened to be first.
    result.best_frame = max(
        frames,
        key=lambda f: cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var(),
    )

    if n_pts < config.LIVENESS_MIN_POINTS:
        result.reason = ("Too little detail on the face to measure depth - "
                         "move closer or improve the lighting")
        return result

    if motion < config.LIVENESS_MIN_MOTION:
        # No viewpoint change means no parallax, so the clip contains no depth
        # information at all. Reporting "live" here would pass a photograph held
        # perfectly still, which is the easiest attack of the lot.
        result.reason = ("The camera and subject barely moved, so depth could not "
                         "be measured - move the phone slightly while recording")
        return result

    if depth < config.LIVENESS_MIN_DEPTH:
        result.verdict = "screen"
        result.reason = ("This looks like a photograph or a screen: everything in "
                         "frame moved as one flat surface")
        return result

    result.verdict = "live"
    result.reason = "Depth consistent with a real face"
    return result
