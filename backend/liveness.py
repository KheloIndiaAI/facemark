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
    """Decode the clip and return evenly-spaced frames plus what we learned.

    OpenCV cannot decode from memory, so the bytes go to a temporary file. The
    browser decides the container - Chrome records WebM/VP9, Safari MP4/H.264 -
    and both go through the same FFmpeg backend.

    READ SEQUENTIALLY, NEVER SEEK. The previous version trusted
    CAP_PROP_FRAME_COUNT and then seeked to evenly spaced indices. A file from
    MediaRecorder is a LIVE stream: it carries no Cues index and usually no
    duration, so the frame count is a guess derived from a header that is not
    there. Where that guess comes back wrong, every seek lands past the end,
    every read fails, and a clip that decodes perfectly well yields zero frames
    - surfacing to the person as "could not read enough frames from this clip",
    with no way to tell that from a codec the box genuinely cannot decode.
    Sequential reading needs no index and is what streamed WebM supports.

    Memory stays bounded by halving: once twice the wanted number of frames is
    held, every other one is dropped and the stride doubles. The kept frames
    still span the WHOLE clip, which is what the parallax check needs - depth
    grows with the distance between viewpoints, so the first and last frames
    matter more than any adjacent pair.
    """
    max_frames = max_frames or config.LIVENESS_SAMPLE_FRAMES
    info = {"frames_total": 0, "fps": 0.0, "duration_s": 0.0,
            "opened": False, "bytes": len(data), "decoded": 0}

    # A real extension, not ".video". FFmpeg probes content rather than trusting
    # the name, but some builds consult it first, and there is no reason to hand
    # the demuxer a name it has never seen.
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        cap = cv2.VideoCapture(str(tmp_path))
        info["opened"] = bool(cap.isOpened())
        if not cap.isOpened():
            return [], info

        # Recorded for diagnostics only - nothing below depends on them being
        # right, which is the entire point.
        reported = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        info["frames_total"] = reported
        info["fps"] = fps
        info["duration_s"] = (reported / fps) if fps > 0 else 0.0

        frames: List[np.ndarray] = []
        stride = 1
        seen = 0
        # 1800 frames is about two minutes at 15fps - far beyond any clip this
        # accepts, and a guard against a corrupt file that never reports EOF.
        while seen < 1800:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if seen % stride == 0:
                frames.append(frame)
                if len(frames) > max_frames * 2:
                    frames = frames[::2]      # keep the span, halve the count
                    stride *= 2
            seen += 1
        cap.release()
        info["decoded"] = seen

        # Trim to the requested count, still spread across the whole clip.
        if len(frames) > max_frames:
            idx = np.linspace(0, len(frames) - 1, max_frames).astype(int)
            frames = [frames[i] for i in idx]
        return frames, info
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
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
        # Three genuinely different faults used to share one message, which
        # made the difference between "this box cannot decode the format" and
        # "the clip really was empty" invisible from the outside. Say which.
        if not info.get("opened"):
            detail = ("the server could not open it - the video format may not "
                      "be supported on this server")
        elif not info.get("decoded"):
            detail = ("the server opened it but decoded no frames - the codec "
                      "is likely unsupported on this server")
        else:
            detail = "it was too short - record for a few seconds"
        log.warning(
            "Liveness could not use the clip: %s (bytes=%s opened=%s decoded=%s "
            "reported_count=%s fps=%.2f)",
            detail, info.get("bytes"), info.get("opened"), info.get("decoded"),
            info.get("frames_total"), info.get("fps") or 0.0,
        )
        return LivenessResult(
            "inconclusive",
            f"Could not read enough frames from this clip - {detail}.",
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
