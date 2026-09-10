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

That only holds while the tracking is real. The first version matched frame 0
against every later frame directly, which is a displacement Lucas-Kanade cannot
follow across a three-second clip; the residual then measured the tracker
failing rather than the subject's shape, and failure looks exactly like depth.
Measured on a matched corpus, photographs waved hard scored ABOVE real faces,
and no threshold separated the two classes at all. Points are now followed frame
to frame and checked by tracking them back again, which is what makes the number
mean what the paragraph above says it means.

WHAT THIS DOES NOT CATCH
------------------------
A video of a real person replayed on a screen is still a plane, so it fails the
same way and IS caught. What defeats this is a genuine 3D artefact - a mask, or
a second live person. That is a far higher bar than holding up a phone, which is
the attack this exists to stop, but it is not "solved liveness" and must not be
described as such.

Two limits are structural rather than incidental, and both return
"inconclusive" - which is NOT the same answer as "live", and not the same as
"screen" either:

  no motion   parallax needs a viewpoint change. Camera and subject both still
              means the clip contains no depth information whatsoever.
  too far     parallax across a face falls with the square of distance while the
              tracker's error stays fixed in pixels. Beyond roughly arm's
              length the measurement is reading its own noise. A group across a
              hall is far outside what this can judge, and saying so is the
              honest answer - see LIVENESS_MIN_FACE_PX.
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
    # WHY, in a form a caller can branch on. The verdict alone lumps together
    # "this is flat" with "this could not be measured", and those two deserve
    # opposite treatment: one is an accusation, the other is an admission. A
    # caller that has to match on English prose to tell them apart will get it
    # wrong the first time the wording changes.
    #   ok | flat | too_far | no_motion | no_detail | unreadable | no_face
    code: str = ""
    depth_score: float = 0.0         # homography residual, normalised by face width
    motion: float = 0.0              # largest tracked-point displacement, same units
    frames_used: int = 0
    tracked_points: int = 0
    face_px: int = 0                 # width of the face judged, in pixels
    frames: List[np.ndarray] = field(default_factory=list)   # sampled, for storage
    best_frame: Optional[np.ndarray] = None                  # sharpest face frame

    @property
    def is_live(self) -> bool:
        return self.verdict == "live"

    @property
    def measurable(self) -> bool:
        """Did the test actually get to look? Distinct from what it concluded."""
        return self.code in ("ok", "flat")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "code": self.code,
            "depth_score": round(self.depth_score, 4),
            "motion": round(self.motion, 4),
            "frames_used": self.frames_used,
            "tracked_points": self.tracked_points,
            "face_px": self.face_px,
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


_LK = dict(
    winSize=(21, 21), maxLevel=3,
    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
)


def _depth_from_parallax(
    frames: List[np.ndarray], box
) -> Tuple[float, float, int, bool]:
    """Return (depth_score, motion, n_points, measured) for the region in `box`.

    depth_score is the median distance, in face-widths, by which tracked points
    disagree with the single best homography between their first-frame position
    and their current one. A plane gives ~0 whatever it does; a face gives more
    the more the viewpoint changes.

    TWO THINGS MAKE THAT NUMBER MEAN WHAT IT CLAIMS, and the first version had
    neither.

    Follow points to the NEXT frame, not to the last one. Matching frame 0
    against frame 17 directly asks Lucas-Kanade for a displacement far outside
    the window it searches, so points settle on whatever texture is nearby and
    the residual measures the tracker giving up. Measured: photographs waved
    hard scored up to 1.15, well above real faces, because a bigger mess is a
    bigger residual and a bigger residual read as "more alive". Stepping frame
    to frame keeps every displacement inside what the tracker can do, while the
    comparison stays first-frame against current, which is where the parallax
    is.

    Then check the tracking instead of trusting it. Each point is tracked on and
    back again, and any point that does not return to where it started never
    followed anything. This is the standard forward-backward test, and it is
    what separates the classes: with it, photographs top out at 0.00197 while
    real faces start at 0.0031; without it there is no threshold that tells them
    apart at all.

    `measured` is False when too few points survived to say anything. That is
    not evidence of a photograph - it is the absence of evidence, and the caller
    must not turn it into an accusation.
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
        return 0.0, 0.0, 0 if pts0 is None else len(pts0), False

    origin = pts0.copy()      # where each surviving point sat in frame 0
    cur = pts0.copy()
    prev_gray = first_gray
    residuals: List[float] = []
    motions: List[float] = []

    for frame in frames[1:]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        nxt, st_fwd, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, cur, None, **_LK)
        if nxt is None:
            break
        back, st_bwd, _ = cv2.calcOpticalFlowPyrLK(gray, prev_gray, nxt, None, **_LK)
        if back is None:
            break
        round_trip = np.linalg.norm(back - cur, axis=2).ravel()
        good = (
            (st_fwd.ravel() == 1)
            & (st_bwd.ravel() == 1)
            & (round_trip <= config.LIVENESS_FB_MAX_PX)
        )
        if int(good.sum()) < config.LIVENESS_MIN_POINTS:
            break                     # tracking is lost; stop rather than guess
        origin, cur, prev_gray = origin[good], nxt[good], gray

        motions.append(float(np.median(np.linalg.norm(cur - origin, axis=2))) / fw)

        # RANSAC finds the dominant plane. On a photograph that plane explains
        # every point; on a face it explains one surface and leaves the rest -
        # which is exactly the quantity of interest, so the residual is measured
        # over ALL surviving points, not just the inliers RANSAC kept.
        H, _ = cv2.findHomography(origin, cur, cv2.RANSAC, 3.0)
        if H is None:
            continue
        projected = cv2.perspectiveTransform(origin, H)
        err = np.linalg.norm(projected - cur, axis=2).ravel()
        residuals.append(float(np.median(err)) / fw)

    if not residuals:
        return 0.0, 0.0, len(origin), False
    # Motion is the LARGEST viewpoint change the clip achieved - the question it
    # answers is "was there ever enough baseline to see depth", not "how much on
    # average". Depth stays a median, so one bad frame pair cannot carry it.
    return float(np.median(residuals)), float(np.max(motions)), len(origin), True


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
                code="unreadable",
            )
        result = LivenessResult(
            "live", "Liveness checking is disabled",
            code="ok", frames_used=len(frames), frames=frames,
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
            code="unreadable",
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
            code="unreadable",
        )

    # WHICH frame the face was found in matters, because the tracking below
    # starts from that frame. The fallback used to find the face in the middle
    # of the clip and then hand its box to a search that always began at frame
    # 0 - so on exactly the clips the fallback exists to rescue, the mask sat
    # over a region of frame 0 that had never been checked for a face, and
    # frequently held none. Whatever was under it was tracked instead, and a
    # region with no depth reads as flat: a real person, refused as a
    # photograph. That is the failure this whole check was rewritten to stop,
    # surviving in the one branch nobody measured.
    start = 0
    face = _largest_face(frames[0], detector)
    if face is None:
        # The first frame is often the worst - caught before the camera has
        # settled or the subject is in position.
        start = len(frames) // 2
        face = _largest_face(frames[start], detector)
    if face is None:
        return LivenessResult("no_face", "No face was found in the clip",
                              code="no_face", frames_used=len(frames), frames=frames)
    # Track from the frame the face is actually in, to the end of the clip.
    track_frames = frames[start:]
    if len(track_frames) < 2:
        # The face only appears in the last frame, so there is no second
        # viewpoint to compare it against.
        track_frames = frames

    face_px = int(face.box[2] - face.box[0])
    result = LivenessResult(
        verdict="inconclusive", reason="", frames_used=len(frames),
        face_px=face_px, frames=frames,
    )
    # The sharpest frame carries the most identity signal, so recognition should
    # run on that rather than on whichever frame happened to be first.
    result.best_frame = max(
        frames,
        key=lambda f: cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var(),
    )

    if face_px < config.LIVENESS_MIN_FACE_PX:
        # Out of range, and saying so is the only honest answer. Parallax across
        # a face shrinks with the square of distance while the tracker's error
        # does not, so past a certain distance the measurement is reading its
        # own noise. Every threshold here was set on faces 165-313px wide; the
        # faces in a real group photograph from this centre are 20-74px. A test
        # calibrated at arm's length cannot convict somebody standing across a
        # hall, and must not pretend otherwise.
        result.code = "too_far"
        result.reason = (
            f"The face is too far away to check for depth ({face_px}px across; "
            f"{config.LIVENESS_MIN_FACE_PX}px is the closest this can judge from)"
        )
        return result

    depth, motion, n_pts, measured = _depth_from_parallax(track_frames, face.box)
    result.depth_score, result.motion, result.tracked_points = depth, motion, n_pts

    if n_pts < config.LIVENESS_MIN_POINTS:
        result.code = "no_detail"
        result.reason = ("Too little detail on the face to measure depth - "
                         "move closer or improve the lighting")
        return result

    if not measured:
        # Points were found but could not be followed: usually the phone swept
        # too fast, or the clip is too compressed to track. Nothing was measured,
        # so nothing is concluded.
        result.code = "no_detail"
        result.reason = ("The camera moved too fast to follow the face - record "
                         "again, moving the phone more slowly")
        return result

    if motion < config.LIVENESS_MIN_MOTION:
        # No viewpoint change means no parallax, so the clip contains no depth
        # information at all. Reporting "live" here would pass a photograph held
        # perfectly still, which is the easiest attack of the lot.
        result.code = "no_motion"
        result.reason = ("The camera and subject barely moved, so depth could not "
                         "be measured - record again, moving the phone slowly "
                         "from side to side")
        return result

    if depth < config.LIVENESS_MIN_DEPTH:
        result.verdict = "screen"
        result.code = "flat"
        result.reason = ("This looks like a photograph or a screen: everything in "
                         "frame moved as one flat surface")
        return result

    result.verdict = "live"
    result.code = "ok"
    result.reason = "Depth consistent with a real face"
    return result
