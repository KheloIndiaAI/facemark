# FaceMark

Attendance for Khelo India sports centres. A coach records a two-second clip of
the group; the system checks the clip shows a live scene rather than a screen,
recognises who is present, and marks the register — geo-tagged against the
centre's location, so attendance can be shown to have been taken where it was
claimed.

Detection is YuNet, recognition is SFace, both from the OpenCV Zoo and both
permissively licensed. Liveness comes from motion parallax rather than
appearance. Everything runs on CPU; a group clip takes well under a second.

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — running it on AWS, end to end
- **[DEVELOPMENT.md](DEVELOPMENT.md)** — local setup, and the traps to know before changing code
- **[DATA-HANDLING.md](DATA-HANDLING.md)** — what is stored, and the obligations that come with it
- **[IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md)** — the next version: athletes attached to coaches, attendance as a reviewed register, athlete self-marking

---

## Quick start

You need **PostgreSQL**. There is deliberately no SQLite fallback — see
[Database](#database).

```bash
docker run -d --name attendance-db \
  -e POSTGRES_DB=attendance -e POSTGRES_USER=attendance \
  -e POSTGRES_PASSWORD=attendance \
  -p 5432:5432 postgres:16-alpine

python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.download_models        # 37 MB, once
cp .env.example .env                     # then set DATABASE_URL
python run.py
```

Open **http://127.0.0.1:8000**. On first run the console prints a generated
`admin` password once; sign in and change it from the sidebar. Set
`FACEMARK_ADMIN_PASSWORD` beforehand to choose your own.

`http://localhost` counts as a secure context, so the camera works there. It
will **not** work over `http://<your-lan-ip>:8000` — browsers refuse
`getUserMedia` outside a secure context, so testing on a phone needs HTTPS.

Optionally, `python -m scripts.fetch_frontend_models` downloads MediaPipe's face
landmarker (~27 MB) for the on-device mesh overlay during enrolment. Without it
the overlay falls back to server-drawn boxes; nothing else changes.

---

## What it does

### Marking attendance

The coach picks a centre, points the rear camera at the group, and records a
**two-second clip**. There is no photo-upload path, deliberately: a still frame
cannot distinguish a group from a photograph of a group.

The clip is checked for liveness, the sharpest frame is pulled out, faces are
detected and embedded, and identities are assigned. Every recognised person at
the selected centre is marked present. People recognised from *other* centres
are named but **not** marked — they were not at this centre's session, and a row
saying otherwise would be false.

The result screen shows the liveness verdict, photo quality with advice, the
geo-fence status, who was recognised with match confidence, and every face that
was not. Each unknown face has a **Who is this?** button that ranks the roster by
similarity and lets the coach confirm in one click — which also stores that crop
as a new template, so the same person matches unaided next time.

### Enrolling someone

Enrolment is a guided video recording, not a photo. The app shows a live mesh
over the face, then prompts in sequence:

> Hold still, looking at the camera → turn LEFT → turn RIGHT → tilt UP → tilt DOWN

Each prompt is **verified before advancing**, using the same pose endpoint that
drives the framing hints — so the clip is as long as the turns take (about
three seconds at best, with a 45-second backstop) rather than a fixed timer that
cannot guarantee the motion actually happened.

One template is stored per distinct view. Views that land too close to one
already captured are rejected by embedding similarity rather than by geometry,
because yaw cannot be measured reliably from five landmarks.

**Nothing is written unless the clip passes liveness.** A registration that
fails leaves no roster row and no templates — the whole thing is one request so
a partial failure cannot strand a person with no face data.

### Liveness

A photograph is a **plane**. Under camera movement every point on a plane maps
through one homography — that is projective geometry, not a heuristic. A real
face is not planar: the nose is centimetres nearer the lens than the ears, so no
single homography fits, and the residual *is* depth, measured rather than
inferred from appearance.

Two earlier approaches were tried and abandoned, and the reasons are worth
keeping:

| Approach | Why it failed |
|---|---|
| Moiré + bezel detection | Assumes a lit screen in a dark surround. A phone held up in a bright office defeats it. |
| MediaPipe 3D landmarks | It infers z from 2D appearance, so a photo of a face yields the same mesh. Measured: 0.1244 real vs 0.1239 replay — no separation. |

**What this does not catch.** A video replayed on a screen is still a plane, so
it is caught. A mask or a second live person is a genuine 3D artefact and would
pass. This stops someone holding up a phone; it is not solved liveness and must
not be described as such.

**Parallax needs motion.** With no viewpoint change there is no depth
information in the clip at all, so the answer is `inconclusive` — not `live`.
That is a third outcome the UI surfaces honestly, because reporting "live" there
would pass a photograph held perfectly still.

### Geo-marking

The browser's position is attached to each capture and compared against the
centre's coordinates by haversine distance. Each record gets `inside`,
`outside`, `no_fix` (browser gave no position) or `unknown` (no coordinates on
file), with the distance in metres. **A refusal never blocks attendance** — it
is recorded as unverified, not rejected.

---

## Roles

Three account roles, and the difference is enforced in SQL rather than by
hiding buttons.

| | Super admin | Coach | Athlete |
|---|---|---|---|
| Centres | All, plus create / edit / import | Own centre only | None |
| Athletes and coaches | All centres | Own centre only | None |
| Attendance | All centres | Own centre only | Their own, via `/api/me/*` |
| Login accounts | Full management | No access | No access |
| Photographs | All centres | Own centre only | None |

An athlete sees their own record and their own attendance through `/api/me/*`
and nothing else. That is enforced on the routes: the roster, the day's
register, the CSV export, the centre statistics and every media route are
staff-only, not merely hidden from the athlete's navigation.

A coach passing another centre's `centre_id` in a query string gets a 403 —
`auth.scope_centre` narrows every query server-side. The centre selector on the
Mark page is not even rendered for a coach, because they are pinned regardless.

Separately, enrolled **people** have a role of `athlete` or `coach`; both are in
the recognition gallery, and the result screen counts them separately.

Passwords are PBKDF2-HMAC-SHA256, 600,000 iterations, per-user salt. Sessions
are opaque server-side tokens, so logout and deactivation take effect
immediately. Login is throttled two ways — per account (8 failures, then a
15-minute lock, shared across workers via the database) and per address (30
attempts per 5 minutes, in-process). **Both checks run before any hashing**,
which is the point: 600k PBKDF2 rounds with unlimited attempts is a way to
saturate every worker from one laptop.

---

## How it works

### Detection — YuNet

MIT licensed, 227 KB, shipping inside OpenCV. Three modes trade recall against
false positives: `fast` (score 0.85), `fused` (0.80, the default) and `accurate`
(0.70).

The 0.80 default was measured, not guessed. Across 81 detections in five test
photographs, real faces never scored below **0.892** — including 21-pixel faces,
so confidence is not merely tracking size — while a hand resting on a shoulder,
detected as a face, sat at **0.632**. The face count is identical anywhere from
0.70 to 0.89 and only collapses at 0.92 where real faces start dropping.

**Printed faces are rejected.** Group photos at sports centres are routinely
taken in front of a banner carrying a printed portrait, which detects as a face
and appears as a phantom attendee. A vinyl print under bright light loses colour
saturation and gains brightness in a way real skin does not. All three
conditions must hold before a face is dropped — any one alone can legitimately
describe a badly-lit real face. Calibrated on 68 faces across four photos: it
fired on the one poster and none of the 67 real faces. That is a single poster
example, so check the `filtered_faces` count in the response rather than
assuming it is right.

### Recognition — SFace

Apache-2.0, 128-dimensional, L2-normalised, with alignment handled by OpenCV's
own `alignCrop` from the detector's five landmarks.

A person owns several templates — one per view from guided capture, plus any
confirmed through *Who is this?*, plus any adapted from daily clips. Matching
max-pools over all of them, so an older reference and a current one are both
represented.

**Threshold is 0.570.** SFace publishes 0.363 for 1:1 verification, but
attendance is open-set — most faces in a group belong to nobody enrolled — and
at 0.363 six strangers were accepted. 0.570 is the measured equal-error point
and the lowest value that admits no stranger. It costs one genuine match in 45;
that person appears under *Unregistered* and is confirmed in one click, whereas
a stranger marked present is a false attendance record for a minor that nobody
is prompted to check.

Faces narrower than 32 px must clear a bar 0.05 higher, because a small face
carries a weaker identity signal.

### Assignment — Hungarian

Faces are assigned to identities by `linear_sum_assignment`, so nobody is marked
present twice from one clip.

Sub-threshold pairs are masked **before** the solve, not filtered after. The
solver always returns `min(N, M)` pairs, so an unenrolled visitor left in the
matrix would be handed an identity — and because it optimises the *sum*, it will
happily move a real student onto that stranger's face when doing so buys a
fraction of a point elsewhere, turning one stranger into two errors. Masking
first means a face that cannot legitimately match anyone simply goes unassigned.

### Continual learning

A high-confidence match can be stored as a new `adapted` template, so the
gallery tracks how people actually look at this centre. Three gates, all
required: confidence ≥ 0.62, a margin of ≥ 0.20 over the runner-up, and a face
at least 60 px. Confidence alone is not enough — a face scoring 0.65 against the
right person and 0.60 against the wrong one is ambiguous, and a template learned
from it raises impostor scores for everyone thereafter.

---

## Database

**PostgreSQL, required.** `DATABASE_URL` must be set; the app refuses to start
without it. A fallback that silently engaged would let a deployment come up
writing to a container-local file that vanishes on the next deploy, which is
exactly the failure the migration removed.

`backend/db.py` is a thin shim rather than an ORM: it rewrites SQLite's `?`
placeholders to `%s`, and provides a row type addressable by both name and
position, because the codebase uses both. See [DEVELOPMENT.md](DEVELOPMENT.md)
for the sharp edges — there are two, and both are silent.

Migrating an existing SQLite install:

```bash
python -m scripts.migrate_to_postgres --dry-run
python -m scripts.migrate_to_postgres --photos
```

It preserves row ids so foreign keys stay valid, resets the `SERIAL` sequences
afterwards, and deliberately does not carry sessions over.

## Photo storage

`FACEMARK_STORAGE=local` keeps photos under `data/`; `FACEMARK_STORAGE=s3` puts
them in any S3-compatible bucket — AWS, Cloudflare R2, MinIO. Two prefixes:
`students/` for enrolment portraits, `uploads/` for capture frames, annotated
output and face crops.

Photos are **streamed through the application**, not redirected to a presigned
URL, so the session check gates every photograph of a minor.

---

## Configuration

`.env.example` lists every variable with commentary. The ones worth knowing:

| Setting | Default | Notes |
|---|---|---|
| `DATABASE_URL` | *(none)* | Required. No fallback. |
| `FACEMARK_STORAGE` | `local` | `local` or `s3`. |
| `S3_REGION` | `auto` | Correct for R2, **wrong for AWS** — set the real region. |
| `FACEMARK_TIMEZONE` | `Asia/Kolkata` | Decides what "today" means. Not cosmetic: a 5 AM session on a UTC server files under yesterday. |
| `COOKIE_SECURE` | `0` | `1` in production, and only with working TLS. |
| `DETECTION_MODE` | `fused` | `fast` \| `fused` \| `accurate`. |

Recognition thresholds live in `backend/config.py`, each next to the measurement
that produced it. `MATCH_THRESHOLD` is not an environment variable — moving it
should mean re-running the evaluation, not editing a deploy config.

---

## Testing and evidence

```bash
python -m scripts.evaluate --sweep                      # FAR/FRR/EER, d-prime, per-person
python -m scripts.robustness                            # degradation envelope
python -m scripts.security_test --password <admin-pw>   # ~40 checks, needs a running server
python -m scripts.calibrate_liveness --live DIR --spoof DIR
```

`evaluate.py` excludes `adapted` templates by default. Those are built from
processed capture frames, so scoring those frames against them is self-matching
— it returns similarities of 1.000 and a meaningless 100%.

### What the evidence actually supports

Read this before quoting a number.

**Face size is the measured driver of accuracy**, obtained by shrinking one
photo and re-testing the same people:

| Median face width | Recall |
|---|---|
| 50 px | 100% |
| 42 px | 92% |
| 32 px | 77% |
| 24 px | 23% |

This is the finding the product acts on — it is why the result screen rates
photo quality and tells a coach at capture time when a register should not be
trusted.

**Liveness thresholds** were re-measured on real people through real browser VP8
encoding after synthetic clips proved misleading:

**Those numbers were withdrawn.** They came from clips whose motion ranges
barely overlapped — photographs that happened to move gently, real faces that
happened to move a lot — and comparing two classes over different parts of the
motion range measures the mismatch, not the classes. Re-measured against a
matched corpus (150 real clips, 150 photograph clips built to the same frame
count and the same measured motion):

| | depth |
|---|---|
| Photographs | 0.00060 – 0.00197 |
| Real faces | 0.00313 – 0.256 (median 0.0111) |

`LIVENESS_MIN_DEPTH` is **0.0025**, inside that gap: no photograph accepted and
no real person refused across 270 judged clips. The old figure only separated
the classes because the comparison was unfair — measured fairly, the previous
algorithm had no separation at any motion, and a photograph waved hard scored
ABOVE a real face. See the calibration note in `backend/config.py`.

Two regimes remain unmeasured and are reported as *unmeasurable* rather than
judged: a face closer than `LIVENESS_MIN_FACE_PX` cannot be told from a
photograph at all, so a group across a hall is **not** liveness-checked.

**Caveats that matter.** The identification numbers come from roughly 13
enrolled athletes in one lighting condition. `data/eval_labels.json` has three
photos, one of which is a second frame of the same burst — a repeatability
check, not an independent sample. At that N, an "equal error rate of 0.00%" is
not a measurable quantity, whatever the sweep prints. And `scripts/live_test.py`
— the harness whose threshold table justifies `MATCH_THRESHOLD` — has since
been repaired (the Row-unpack bug it died on is fixed), so that table is
reproducible again — the sample-size point above still stands, though.
Read the script's own docstring before running it.

The defensible claim is: *this identified a small number of known athletes under
one set of conditions and rejected the strangers it was shown.* That is a
promising pilot result, not a validated accuracy figure.

---

## Layout

```
backend/
  main.py           video + photo pipelines, enrolment, static host
  routes.py         auth, users, centres, people
  auth.py           PBKDF2 passwords, sessions, role scoping, login throttling
  db.py             PostgreSQL pool, placeholder translation, advisory locks
  database.py       schema, gallery, attendance, analytics
  storage.py        switchable photo storage (local | S3)
  liveness.py       parallax depth from a short clip
  detector.py       YuNet, quality gates, printed-face rejection
  recognizer.py     SFace embedding and score fusion
  metaheuristics.py Hungarian assignment
  centres.py        centre registry, search, haversine geo-fencing
frontend/           vanilla SPA, no build step, installable PWA
scripts/            models, migration, evaluation, calibration  (see DEVELOPMENT.md)
.github/workflows/  CI on every push, deploy to EC2 on dev
```

---

## Known issues

- **Demo centres.** First run seeds eight placeholders, each flagged `is_demo`
  and prefixed `DEMO-`. They are **not** real Khelo India records. Replace via
  Centres → Import, or delete with Centres → Remove demo.
- **Several scripts are stale or broken**, including two that are destructive.
  Read the docstring at the top of anything in `scripts/` before running it —
  each one states its own status. (This used to link to a
  `DEVELOPMENT.md#script-status` section that does not exist, on the line
  telling operators to check before running destructive scripts.)
- **`/api/health` returns 503 when the database is unreachable**, with a body
  explaining what is wrong. It used to answer 200 with `status: degraded`, so
  the container's own HEALTHCHECK and the platform's both passed while the
  instance could neither read nor write. The paragraph below describes the old
  behaviour: it
  reports `"database": "unreachable"` in the body. That is deliberate, so an
  operator can see *why*, but it means container health checks do not catch a
  database outage.
- **The still-photo endpoints remain** (`POST /api/attendance/process`,
  `POST /api/students`) and are unauthenticated against liveness by nature. The
  frontend no longer calls them; `scripts/enroll_from_pdf.py` and
  `scripts/bulk_assign.py` still do.

---

## Handling biometric data

This stores face embeddings and photographs of children. A face template is not
revocable the way a password is. Before any real deployment, read
**[DATA-HANDLING.md](DATA-HANDLING.md)** — obtaining informed consent from
guardians, publishing and enforcing a retention period, restricting super-admin
accounts, and checking your obligations under the DPDP Act 2023 are all
prerequisites, and nothing in this codebase discharges them.

## Licence

MIT. Every model and dependency permits commercial use — see
[DEPLOYMENT.md](DEPLOYMENT.md) for the full licence table and what was removed
to get there.
