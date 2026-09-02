"""Central configuration: paths, model selection, fusion modes and thresholds.

Optimized for best accuracy/latency balance:
Every model permits commercial use, which the previous stack did not:
- YuNet (MIT) for detection, replacing YOLO (AGPL-3.0)
- SFace (Apache-2.0) for recognition, replacing InsightFace and AdaFace weights
  that were restricted to non-commercial research
"""
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Read ROOT_DIR/.env into the environment, if it exists.

    Hand-rolled rather than adding python-dotenv: it is fifteen lines, and this
    project keeps its dependency list short and its licences auditable.

    A real environment variable always wins. That ordering matters because
    production sets DATABASE_URL and the S3 credentials through the platform,
    and a stray .env copied into an image must not silently override them.
    """
    path = ROOT_DIR / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# FACEMARK_DATA_DIR lets a deployment point every writable path at a mounted
# disk. On Render the container filesystem is replaced on each deploy, so the
# SQLite database, enrolment photos and uploads must live on the persistent
# volume or they are lost on the next push.
DATA_DIR = Path(os.environ.get("FACEMARK_DATA_DIR") or (ROOT_DIR / "data"))

# Models are configured SEPARATELY from user data and deliberately so. In the
# container they are baked into the image (immutable, no cold-start download),
# while DATA_DIR points at a mounted disk. Folding models into DATA_DIR would
# make FACEMARK_DATA_DIR silently relocate them to an empty volume.
MODELS_DIR = Path(os.environ.get("FACEMARK_MODELS_DIR") or (DATA_DIR / "models"))
UPLOADS_DIR = DATA_DIR / "uploads"
STUDENTS_DIR = DATA_DIR / "students"
FRONTEND_DIR = ROOT_DIR / "frontend"
SAMPLES_DIR = ROOT_DIR / "samples"

# The SQLite file this project used before the move to PostgreSQL. Nothing
# reads it at runtime any more; it is kept so scripts/migrate_to_postgres.py can
# find the old data, and so an existing install is not silently orphaned.
LEGACY_SQLITE_PATH = DATA_DIR / "attendance.db"
DB_PATH = LEGACY_SQLITE_PATH          # retained for older scripts

# --- Database ---------------------------------------------------------------
# PostgreSQL, required. There is deliberately no SQLite fallback: a fallback
# that silently engages when DATABASE_URL is missing would let a deployment
# come up writing to a container-local file that vanishes on the next deploy,
# which is exactly the failure this migration exists to remove.
#
#   postgresql://user:password@host:5432/facemark
#
# Managed providers hand out URLs starting `postgres://`; libpq accepts both
# spellings, so no rewriting is needed.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# FastAPI runs sync endpoints in a thread pool, so several requests hold a
# connection at once. The dashboard alone issues eight small counts per load.
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))

# --- Photo storage ----------------------------------------------------------
# "local" keeps photos under DATA_DIR, where they have always lived; "s3" puts
# them in an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO). The switch is
# read once at startup by backend/storage.py.
#
# S3 matters for the same reason DATABASE_URL does: on a platform that replaces
# the container each deploy, a photo written to local disk is gone next push.
STORAGE_BACKEND = os.environ.get("FACEMARK_STORAGE", "local").strip().lower()

S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
# Set for Cloudflare R2 / MinIO; leave empty to talk to AWS S3.
#   R2: https://<account-id>.r2.cloudflarestorage.com
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "").strip()
S3_REGION = os.environ.get("S3_REGION", "auto").strip()
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "").strip()
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "").strip()
# Optional key prefix, so one bucket can hold several environments.
S3_PREFIX = os.environ.get("S3_PREFIX", "").strip()

for _d in (DATA_DIR, UPLOADS_DIR, STUDENTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# --- Detector (face detection) ---------------------------------------------
# YuNet, MIT licensed, shipping inside OpenCV (Apache-2.0). Chosen over YOLO
# (AGPL-3.0, whose network clause would oblige publishing this whole
# application) and SCRFD (InsightFace: non-commercial research only). 227 KB.
YUNET_MODEL = "face_detection_yunet_2023mar.onnx"
# A hand resting on a shoulder was detected as a face at 0.632 and shown as
# "Unknown". Measured over 81 detections in the five test photographs, real
# faces never score below 0.892 - including 21px faces in the strangers photo,
# so confidence is not merely tracking face size - while that hand sat at
# 0.632. Sweeping the threshold gives an identical face count everywhere from
# 0.70 to 0.89, and only collapses at 0.92 where real faces start dropping:
#
#   score   delhi_a  delhi_b  delhi_c  strangers  wl_group
#   0.60      13       14*      14*       15         26      * the hand
#   0.70-0.89 13       13       13        15         26
#   0.92      10        9        9         3         21      real faces lost
#
# 0.80 is the middle of that plateau: 0.168 above the hand, 0.092 below the
# weakest real face. Accurate mode keeps a lower bar for genuinely hard photos
# but no longer reaches down to where non-faces live.
YUNET_SCORE = 0.80             # detection confidence for the default mode
YUNET_SCORE_FAST = 0.85        # fewer, surer boxes
YUNET_SCORE_ACCURATE = 0.70    # more recall on hard photos
YUNET_NMS = 0.30
DETECTION_MODE = os.environ.get("DETECTION_MODE", "fused")
MIN_FACE_SIZE = 20             # px; below this a face carries no identity signal

# --- Recognizer (face recognition) -----------------------------------------
# SFace, Apache-2.0, from the OpenCV Zoo. Replaces the ArcFace/AdaFace ensemble,
# whose weights were all restricted to non-commercial research. Measured on this
# project's data the swap costs nothing: 13/13 on both ground-truth frames,
# 6/22 on the weightlifting centre and 0 false accepts on the strangers control
# - identical to the previous stack - at 0.5s per photo instead of 13s.
SFACE_MODEL = "face_recognition_sface_2021dec.onnx"
EMBED_SIZE = 112               # SFace's own aligner produces 112x112

# SFace publishes 0.363 for 1:1 verification. Attendance is open-set, and at
# 0.363 six strangers were accepted. Measured on this project's data the equal
# error rate is 0.00% at 0.570, and 0.570 is also the zero-FAR point.
#
# 0.570 rather than the earlier 0.55, because recognition is now global.
#
# The old value was chosen while the gallery was scoped to one centre, which
# kept the impostor space small enough that a marginal accept lost the
# Hungarian assignment to the right person anyway. Matching against every
# centre removes that cushion, so the threshold has to stand on its own.
#
# Measured on the five real photographs (scripts/live_test.py, 81 faces):
#
#   thr    recall   wrong-centre   strangers accepted
#   0.55      45          0                1
#   0.57      44          0                0
#   0.60      39          0                0
#
# 0.570 is the measured equal-error point and is the lowest value that admits
# no stranger. It costs one genuine match out of 45; that person appears under
# "Unregistered" and can be confirmed in one click, whereas a stranger marked
# present is a wrong attendance record for a minor that nobody is prompted to
# check. Wrong-centre matches are zero at every threshold tested, so the wider
# gallery is not what the threshold is defending against - strangers are.
MATCH_THRESHOLD = 0.570
SMALL_FACE_PX = 32             # faces narrower than this clear a higher bar
SMALL_FACE_THRESHOLD_BUMP = 0.05

# --- Enrollment -------------------------------------------------------------
# GFPGAN restoration was removed with the licence migration: its weights derive
# in part from NVIDIA StyleGAN2, which is not licensed for commercial use. It
# was only ever used to bridge printed ID photos toward the live-photo domain,
# and guided multi-view capture addresses that far more directly by taking
# current photographs in the room where attendance happens.
MIN_ENROLL_FACE_SIZE = 48      # px

# --- Multi-view enrolment ---------------------------------------------------
# The useful half of a Face ID style enrolment ceremony. A phone's TrueDepth
# camera builds an actual depth map; a browser gets RGB frames and nothing else,
# so depth is not available. What IS available - and what actually helps a 2D
# recognizer - is several views of the face across poses, captured today in the
# room where attendance is taken.
#
# Poses are bucketed by yaw and pitch so the app can tell whether the person
# genuinely turned their head or just held still through the whole sequence.
MULTIVIEW_ENROLMENT = True
MULTIVIEW_YAW_TURN = 12.0      # degrees of yaw before a view counts as turned
MULTIVIEW_PITCH_TURN = 10.0    # degrees of pitch before it counts as up/down
MULTIVIEW_MIN_POSES = 2        # fewer than this and the capture is just one photo
MULTIVIEW_MIN_FACE_PX = 90     # a selfie at arm's length gives far more than this
# Two views this alike carry the same information, so the second is not stored.
# Geometry cannot decide this reliably - yaw is unmeasurable without real
# landmarks - but the embeddings answer it directly.
MULTIVIEW_DUPLICATE_SIM = 0.97

# --- Photo quality advice ---------------------------------------------------
# Face pixel size is the dominant driver of recognition accuracy, measured on
# this system's own data by shrinking one photo and re-testing the same people:
#     50px median faces -> 100% recall
#     42px             ->  92%
#     32px             ->  77%
#     24px             ->  23%
# Below GOOD_FACE_PX results degrade quickly, so the app says so at capture time
# rather than letting a coach trust a register built from an unusable photo.
GOOD_FACE_PX = 45      # comfortable
FAIR_FACE_PX = 35      # workable, expect misses
# Under this, the photo is not worth trusting for attendance.
POOR_FACE_PX = 28

# --- Printed / poster face rejection ----------------------------------------
# Group photos at sports centres are routinely taken in front of a banner that
# carries a printed portrait. Those prints are detected as faces and appear as
# phantom "Unknown" attendees.
#
# A vinyl print under bright light loses colour saturation and gains brightness
# in a way real skin under the same light does not. All THREE conditions must
# hold before a face is rejected: any single one of them can legitimately
# describe a real, badly-lit face, so requiring agreement keeps a genuine
# athlete from being silently dropped.
#
# Calibrated on 68 detected faces across four photos (indoor and outdoor): it
# fired on the one poster and on none of the 67 real faces. That is a single
# poster example, so treat the thresholds as provisional and check the
# `filtered_faces` count in the response rather than assuming it is right.
REJECT_PRINTED_FACES = True
PRINT_MAX_SATURATION = 70.0    # real faces here measured 95-147
PRINT_MIN_BRIGHTNESS = 170.0   # real faces here measured 74-167
PRINT_MAX_SAT_STDDEV = 35.0    # real faces here measured 39-57

# --- Screen / replay rejection ----------------------------------------------
# A face displayed on a phone or laptop screen is detected exactly as a real
# one, so holding up a photograph of an absent athlete marks them present. That
# is attendance fraud that leaves no trace anywhere in the record - the row
# looks identical to an honest one.
#
# Re-photographing a screen leaves two traces a live face does not:
#
#   moire    the camera's sensor grid beats against the screen's pixel grid,
#            adding a near-periodic interference pattern. Real skin and fabric
#            are broadband - their spectrum falls off smoothly - so periodic
#            energy shows up as isolated spikes that natural texture lacks.
#   framing  the lit screen sits inside a dark bezel, so the ring around the
#            face is markedly darker than the face. A real room rarely frames
#            someone that way.
#
# BOTH must hold, following looks_printed()'s rule: each alone describes plenty
# of real faces - a striped shirt carries periodic energy, a spotlit face
# outshines its surroundings - so requiring agreement is what keeps a genuine
# athlete from being refused attendance.
#
# Measured over 267 real faces from this deployment's own photographs (192
# group photos and enrolment portraits, faces >= 60px):
#
#             median    p99     max
#   moire      22.74   30.50   30.50
#   bezel       0.83    1.13    1.47
#
# 38.0 sits 24% above the highest moire peak any real face produced, and only
# ONE of the 267 cleared the bezel condition at all - that face scored 19.45 on
# moire, less than half the threshold. So no face in the existing corpus is
# rejected by this test, which is the property that matters: a genuine athlete
# refused attendance is a worse failure than a spoof that gets through.
#
# The false-reject side was calibrated; the true-CATCH side never was, because
# no stored photograph is a picture of a screen. That gap is what the disabling
# note below records - the check was measured on the half that could not fail.
#
# DISABLED. It was demonstrated failing on a phone held up in a bright office:
# the bezel condition assumes a lit screen inside a DARK surround, and a lit
# room defeats it, after which the moire half cannot fire on its own. A check
# that has never caught anything is worse than no check, because it reads as
# protection that is not there. Liveness now comes from video parallax instead -
# see backend/liveness.py. Kept rather than deleted because the measurements in
# the comment above are real and worth not repeating.
REJECT_SCREEN_FACES = False
SCREEN_MAX_MOIRE_PEAK = 38.0     # real faces here measured 5.06 - 30.50
SCREEN_MIN_BEZEL_RATIO = 1.35    # real faces here measured 0.43 - 1.47
SCREEN_MIN_FACE_PX = 60          # below this the spectrum is too coarse to judge

# --- Liveness from video -----------------------------------------------------
# A photograph is a plane, and under camera motion every point on a plane maps
# through ONE homography. A real face does not: the nose is nearer the lens than
# the ears, so the best-fitting homography leaves a residual, and that residual
# is depth measured from parallax rather than guessed from appearance.
#
# LIVENESS_MIN_DEPTH is the residual, in face-widths, below which the subject is
# treated as flat. LIVENESS_MIN_MOTION is the guard that makes the test honest:
# with no viewpoint change there is no parallax and therefore no evidence either
# way, so the clip is called inconclusive rather than live. Without that guard a
# photograph held perfectly still would sail through.
#
# CALIBRATION STATUS, 2026-09-02: 0.010 was set from synthetic clips only - a
# photo warped through known homographies (faithful for the flat side) and
# deliberate large head turns stitched from guided-enrolment frames (which
# overstated the live side: a big, deliberate pose swing is not what "look at
# the camera and move your head a little" produces).
#
# That gap was real. A user reported genuine registrations being refused as
# "screen". Reproduced by measuring REAL people - not synthetic warps - with
# ordinary small motion, encoded through actual browser VP8 compression at a
# realistic phone bitrate, the same pipeline production traffic uses:
#
#                                                  depth    motion
#   flat photo, real VP8, 3 portraits x 2 bitrates 0.0038-0.0055   0.17-0.21
#   real face, genuine small natural motion (n=2)  0.0072-0.0097   0.098-0.099
#   real face, deliberate large pose change        0.17 -0.32      0.20-0.37
#
# 0.010 sat ABOVE both real small-motion measurements - it was rejecting
# ordinary people by construction, not as an edge case. 0.006 sits between the
# two clusters, biased toward the flat side on purpose: the margin above the
# threshold to the nearest real measurement (0.0072, ~1.2x) is wider than the
# margin below it to the nearest flat measurement (0.0055, ~1.09x), because a
# genuine person refused attendance is a worse failure than a spoof let
# through. The margin is still thin - two people, one capture pipeline - and
# needs more real clips before it can be called settled.
# See scripts/calibrate_liveness.py to re-measure on your own devices.
LIVENESS_ENABLED = True
LIVENESS_SAMPLE_FRAMES = 18      # was 12; more samples for the longer clip below
LIVENESS_MIN_POINTS = 25         # fewer trackable corners than this cannot judge
LIVENESS_MIN_MOTION = 0.004      # median displacement in face-widths
LIVENESS_MIN_DEPTH = 0.006       # measured - see calibration note above
LIVENESS_MAX_BYTES = 25 * 1024 * 1024
# Registration records for 10s (config below); attendance stays at 2s. The
# ceiling needs headroom above the longer of the two, not to equal it exactly -
# encoding jitter can make an intended 10s recording land a little over.
LIVENESS_MAX_SECONDS = 14
# Frames kept per clip. They are the evidence behind a refusal, so a coach
# can see WHY a capture was rejected rather than being told only that it was.
LIVENESS_STORE_FRAMES = 6

# --- Stage-2 cascade verification -------------------------------------------
# Disabled: it re-scored a GFPGAN-restored crop, and GFPGAN is gone. Measured
# separately, restoring query faces made accuracy worse anyway (12/13 against
# 13/13, and 5/22 against 6/22) because it hallucinates detail that is not the
# person's.
CASCADE_VERIFY = False
CASCADE_MATCH_BAND = 0.08
CASCADE_UNKNOWN_MARGIN = 0.10
CASCADE_THRESHOLD_OFFSET = -0.02

# --- Continual Learning (multi-template adaptation) -------------------------
CONTINUAL_LEARNING = True      # learn from daily group photos via NEW templates
CONTINUAL_MIN_CONF = 0.62      # high confidence required before storing an adapted template
CONTINUAL_MIN_MARGIN = 0.20    # the match must also beat the runner-up student by this much.
                               # Without a margin gate, templates learned from ambiguous faces
                               # raise impostor scores for everyone and the gallery degrades.
CONTINUAL_MIN_FACE_PX = 60     # only learn from large, well-resolved faces
CONTINUAL_MAX_TEMPLATES = 4    # max adapted templates per (student, model)
CONTINUAL_SIMILARITY_SKIP = 0.96  # skip storing a template this similar to an existing one

# --- Performance ------------------------------------------------------------
# ONNX Runtime intra-op threads: half of CPU cores (avoids oversubscription)
ORT_THREADS = max(1, (os.cpu_count() or 4) // 2)
# Enable all graph optimizations + disable logging noise
ORT_GRAPH_OPT = True
WARMUP_ON_START = True         # pre-run models once to avoid first-request JIT lag

# --- Attendance ------------------------------------------------------------
ATTENDANCE_DATE_FORMAT = "%Y-%m-%d"

# --- Advanced tuning (expert) ----------------------------------------------
# TTA (test-time augmentation) for detection - enables horizontal flip
DETECTOR_TTA = True            # set True for "accurate" mode
# Face alignment quality threshold (0-1)
ALIGN_QUALITY_THRESH = 0.3
# Max faces to process per image (prevents OOM on huge groups)
MAX_FACES_PER_IMAGE = 500      # Upgraded from 50 to support massive lecture halls

# --- v3.0: Feature-level fusion --------------------------------------------
FEATURE_FUSION = False             # Use pure score-level ensemble fusion for clean open-set rejection
PCA_WHITEN = False                 # embedding denoising via PCA whitening
PCA_COMPONENTS = 480               # retained dimensions (of 512 per model)

# --- v3.0: Quality-aware matching ------------------------------------------
# Quality-weighted pooling was removed: it multiplied cosine similarities by a
# quality factor, which shifts every score away from the calibrated threshold.
# Quality now influences matching only through detection filtering and the
# continual-learning gates.
FACE_QUALITY_GATE = True           # pre-filter low-quality detections
MIN_BLUR_SCORE = 20.0              # Laplacian variance threshold for usability
MAX_YAW_ANGLE = 65.0               # degrees; reject extreme profile faces
QUALITY_PENALTY_WEIGHT = 0.15      # confidence penalty multiplier for low-quality faces

# --- v3.0: Adaptive tiled detection ----------------------------------------
TILED_DETECTION = False            # Disabled to prevent tile-boundary cuts and sub-crop duplicates (YOLO11s at 1280px handles full image directly)
TILE_SIZE = 1280                   # tile dimension in pixels
TILE_OVERLAP = 0.15                # fractional overlap between tiles

# --- v3.0: Ratio test (ambiguous match rejection) --------------------------
RATIO_TEST = True
RATIO_TEST_THRESHOLD = 0.88        # best/2nd-best similarity ratio; above = ambiguous match (rejected as unknown)

# --- v3.0: Platt calibration (raw similarity -> probability) ---------------
PLATT_CALIBRATION = False          # keep pure cosine similarity for matching decisions

# --- v3.0: Illumination normalization -------------------------------------
CLAHE_ENABLED = True               # CLAHE on abnormally lit faces before embedding
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE_SIZE = 8

# --- v3.0: Camera capture -------------------------------------------------
CAMERA_PHOTO_QUALITY = 92         # JPEG quality for camera captures
CAMERA_MAX_RESOLUTION = 1920      # max dimension for camera photos


# --- Deployment -------------------------------------------------------------
# Browser origins allowed to call the API directly, comma-separated.
#
# This backend serves its own frontend, so requests normally arrive same-origin
# and never consult this list; it exists for local development and for a
# frontend hosted on a different domain.
#
# "*" is NOT valid, whatever convenience suggests. Session auth rides a
# credentialed cookie, and browsers reject a wildcard origin alongside
# credentials - the request fails with no useful error, so following the old
# advice here produced a CORS setup that looked configured and was broken.
# List the origins explicitly.
#
# And if you do set this for genuine cross-site use, set COOKIE_SECURE=1 and
# COOKIE_SAMESITE=none too. Without them the cookie is refused on a cross-site
# request, so the login appears to succeed and every call after it returns 401.
CORS_ORIGINS = [
    o.strip() for o in os.environ.get(
        "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(",") if o.strip()
]

# Cookies must be Secure + SameSite=None to survive a cross-site request. Behind
# the Vercel proxy the request is same-origin, so the stricter Lax default holds.
# --- Login throttling --------------------------------------------------------
# Two separate problems, one guard.
#
# Brute force: nothing limited attempts, so a weak password fell to a script.
#
# CPU exhaustion: verifying a password is 600,000 PBKDF2 rounds, which is
# correct for storage and is also, with unlimited attempts, a way to saturate
# every worker from one laptop. Both checks below run BEFORE any hashing, which
# is the point - a guard that hashes first would still burn the CPU it exists to
# protect.
#
# The per-account lock lives in the database so it is shared by every worker and
# survives a restart. The per-address window is in-process, so N workers allow
# N times the burst; that is a deliberate trade against giving an unauthenticated
# caller a way to write rows.
LOGIN_MAX_FAILURES = 8           # per account before it locks
LOGIN_LOCKOUT_SECONDS = 900      # 15 minutes, then the count resets on success
LOGIN_IP_MAX_ATTEMPTS = 30       # per address within the window below
LOGIN_IP_WINDOW_SECONDS = 300

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") in ("1", "true", "True")
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "lax")
