# FaceMark

Attendance for Khelo India sports centres. A coach opens the camera, points it
at one athlete, and recording starts by itself once a face is framed and the
athlete blinks. The athlete is marked on the day's register, which the coach
reviews and then signs with their own face before it counts. Everything runs
on CPU.

Detection is YuNet, recognition is SFace, both from the OpenCV Zoo and both
permissively licensed. Liveness is two checks: motion parallax, which tells a
photograph or a phone screen from a real scene, and a blink, which proves a
live person was actually in front of the camera at the moment that matters.

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — running it on AWS, end to end
- **[DEVELOPMENT.md](DEVELOPMENT.md)** — local setup, and the traps to know before changing code
- **[DATA-HANDLING.md](DATA-HANDLING.md)** — what is stored, and the obligations that come with it
- **[IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md)** — the plan behind the coach/athlete/register model this app now runs on, kept unedited for the reasoning; its own status note says where the build differs
- **[PERFORMANCE.md](PERFORMANCE.md)** — where capture time actually goes, and why a GPU is the wrong purchase

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
landmarker (~27 MB) for the on-device mesh overlay used during enrolment, blink
detection and pose framing. Without it those fall back to server-side checks;
nothing else changes.

---

## What it does

### Taking attendance

A coach opens **Take Attendance** and holds the phone on one athlete at a
time, about an arm's length away — there is no shutter button. Recording
starts by itself once a face is framed close enough, and stops shortly after
the app sees the athlete blink. A clip that looks like a photograph or a
screen held up to the lens is refused; nothing else is. The recognised athlete
is added to today's register as a draft, or as a late addition if the register
has already been signed.

### Signing the register

Scanning only drafts a name — nobody counts as present until the coach opens
**Register**, checks the list (present and absent both, so a coach can notice
who is missing), adds anyone the camera missed with **Mark present**, and taps
**Submit register**. Submitting asks for the coach's own face and a blink: this
is the signature, a 1:1 check against the coach's own templates, and it is
what makes the day's record count. A face check that keeps failing does not
block the register forever — after a few retries it submits anyway, marked
**unverified**, and shows up on the super admin's oversight page rather than
being silently lost.

**A register left unsigned is not attendance.** The app will not open a new
day's Take Attendance or registration until yesterday's register — or the
oldest one still open, back to `SESSION_BACKDATE_DAYS` days — is submitted. A
banner on the dashboard, Take Attendance and Register all say so and link
straight to the register that needs signing.

### Registering an athlete

A coach adds a new athlete from **Directory → Register Athlete**: name, NSRS
ID, centre, sport, then a guided recording. The app tracks the face live and
asks for two head turns — one each way — each confirmed on the phone before
the clip is accepted, so the recording is only as long as the turns actually
take. If the compressed video does not show a usable face (some phones lose
detail filming from below while turning), the still photos taken at each
confirmed step are used instead. A face already on file — enrolled or still
waiting for approval — is refused with a clear reason rather than a second,
confusing account. Nothing is written unless the recording passes: a
registration that fails leaves no roster row and no templates. On success the
camera closes and a **Registration done** screen confirms it, naming how many
angles were captured and flagging it for a redo if only one was.

### Athletes marking themselves present

An athlete can self-register (details, choose a coach, record their face) and,
once their coach approves them, can **mark themselves present** if the coach
has not scanned them yet. This still requires a blink — there is no photo
fallback here, since nobody is reviewing the clip in person the way a coach's
own capture is reviewed and signed. The browser's location is attached to the
attempt and checked against the centre; a poor or missing GPS fix does not
block it, but is badged loudly on the coach's register so it is not trusted
silently. Self-marking never counts on its own — it sits on the register as
`self_marked` until the coach submits.

### Forgotten passwords

Anyone who cannot sign in can ask for a new one from the sign-in screen:
username, the password they want, and optionally how to reach them. Nothing
changes yet — the old password still works — until a super admin approves the
request under Accounts, ideally after confirming with the person directly. The
new password is stored hashed and is never shown to the admin; approving one
signs the account out everywhere. See `backend/password_reset.py`.

---

## Liveness

Two independent checks, used for different reasons.

**Parallax** tells a photograph or a phone screen from a real scene. A
photograph is a **plane**. Under camera movement every point on a plane maps
through one homography — that is projective geometry, not a heuristic. A real
face is not planar: the nose is centimetres nearer the lens than the ears, so
no single homography fits, and the residual *is* depth, measured rather than
inferred from appearance. This is what every clip is checked against, whether
or not the flow also needs a blink.

Two earlier approaches were tried and abandoned, and the reasons are worth
keeping:

| Approach | Why it failed |
|---|---|
| Moiré + bezel detection | Assumes a lit screen in a dark surround. A phone held up in a bright office defeats it. |
| MediaPipe 3D landmarks | It infers z from 2D appearance, so a photo of a face yields the same mesh. Measured: 0.1244 real vs 0.1239 replay — no separation. |

**What parallax does not catch.** A video replayed on a screen is still a
plane, so it is caught. A mask or a second live person is a genuine 3D
artefact and would pass. This stops someone holding up a photograph; it is not
solved liveness and must not be described as such.

**Parallax needs motion.** With no viewpoint change there is no depth
information in the clip at all, so the answer is `inconclusive`, not `live`.
Take Attendance only ever refuses a `screen` verdict, so an inconclusive clip
(too still, too far, too little detail) is still accepted — the coach is
standing there and chose to scan; asking for a blink on every scan would add
delay a witnessed capture does not need.

**Blink** proves a live person was in front of the camera at that moment,
using an on-device face mesh (falling back to a server-side check) against the
person's own open-eye baseline. It is required, with no way around it, for
signing the register, for enrolling a face, and for an athlete marking
themselves present — the flows where nobody but the camera is checking who is
really there. It is asked for but not required on a Take Attendance scan,
which a coach is watching in person.

---

## Roles

Three account roles, and the difference is enforced in SQL rather than by
hiding buttons.

| | Super admin | Coach | Athlete |
|---|---|---|---|
| Centres | All, plus create / edit / import | Own centre only | None |
| Athletes and coaches | All centres | Own centre only | None |
| Attendance | All centres | Own centre only | Their own, via `/api/me/*` |
| Login accounts | Full management, plus password-reset approval | No access | No access |
| Photographs | All centres | Own centre only | None |

An athlete sees their own record and their own attendance through `/api/me/*`
and nothing else. That is enforced on the routes: the roster, the day's
register, the CSV export, the centre statistics and every media route are
staff-only, not merely hidden from the athlete's navigation.

A coach passing another centre's `centre_id` in a query string gets a 403 —
`auth.scope_centre` narrows every query server-side.

Separately, enrolled **people** have a role of `athlete` or `coach`; both are
in the recognition gallery.

Passwords are PBKDF2-HMAC-SHA256, 600,000 iterations, per-user salt. Sessions
are opaque server-side tokens, so logout and deactivation take effect
immediately. Login is throttled two ways — per account (8 failures, then a
15-minute lock, shared across workers via the database) and per address (30
attempts per 5 minutes, in-process). **Both checks run before any hashing**,
which is the point: 600k PBKDF2 rounds with unlimited attempts is a way to
saturate every worker from one laptop. Requesting a password reset is
throttled the same way, before its own hashing.

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

**Printed faces are rejected**, on every call — a registration photo taken in
front of a banner carrying a printed portrait, or a group photo used by one of
the legacy still-photo endpoints (see [Known issues](#known-issues)), detects
the poster as a face otherwise. A vinyl print under bright light loses colour
saturation and gains brightness in a way real skin does not. All three
conditions must hold before a face is dropped — any one alone can legitimately
describe a badly-lit real face. Calibrated on 68 faces across four photos: it
fired on the one poster and none of the 67 real faces. That is a single poster
example, so treat it as a working filter, not a validated rate.

### Recognition — SFace

Apache-2.0, 128-dimensional, L2-normalised, with alignment handled by OpenCV's
own `alignCrop` from the detector's five landmarks. This is what every match
in the app runs on: a Take Attendance scan against the coach's roster, a
duplicate-face check during registration, and the 1:1 check that verifies a
coach's own face when they sign a register.

A person owns several templates — one per view from guided registration, plus
any added later from a photo. Matching max-pools over all of them, so an older
reference and a current one are both represented.

**Threshold is 0.570.** SFace publishes 0.363 for 1:1 verification, but
attendance matching is open-set — most faces the camera sees belong to nobody
enrolled — and at 0.363 six strangers were accepted in testing. 0.570 is the
measured equal-error point and the lowest value that admits no stranger. It
costs some genuine matches that fall just short, which a coach then adds by
hand rather than the app guessing; a stranger marked present would instead be
a false attendance record for a minor that nobody is prompted to check.

Faces narrower than 32 px must clear a bar 0.05 higher, because a small face
carries a weaker identity signal.

### Assignment, continual learning, and geo-fencing — where they actually run

Three pieces of the pipeline exist and are tested, but only run through the
still-photo group endpoints (`POST /api/attendance/process`,
`/process-video`), which the current frontend never calls — see
[Known issues](#known-issues). They are worth knowing about rather than
presenting as live behaviour:

- **Hungarian assignment** (`linear_sum_assignment`) solves a many-faces-to-
  many-identities match for a single group photo, masking sub-threshold pairs
  before the solve so an unenrolled visitor cannot be handed someone else's
  identity. Take Attendance never needs it: it matches one face at a time
  against the roster, which is a threshold comparison, not an assignment
  problem.
- **Continual learning** stores a high-confidence group-photo match as a new
  `adapted` template (three gates: confidence ≥ 0.62, a margin of ≥ 0.20 over
  the runner-up, a face at least 60 px), so the gallery would track how people
  actually look at a centre over time. Nothing in the live flow calls it.
- **Geo-fencing** (haversine distance against the centre's coordinates) is
  live, but only for an athlete's self-mark — the one capture nobody is
  watching in person. Take Attendance and register signing do not collect a
  location at all.

`assignment_solver_name()` is still reported at startup and in `/api/health`
because the code and its tests remain in the repository, correct and ready if
the group-photo path is ever reconnected to the UI.

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
| `TRUSTED_PROXY_HOPS` | `0` | Number of proxies in front of the app. Set correctly or every login/signup/reset throttle shares one bucket (or none). |

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

These measure the detector, recognizer and liveness check directly, which is
the same code every live flow calls — the evidence below does not depend on
which endpoint is in front of it.

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

This is the finding the product acts on — it is why a scan too far away is
refused before it ever reaches matching, rather than returned as a low-quality
guess.

**Liveness thresholds** were re-measured on real people through real browser VP8
encoding after synthetic clips proved misleading. Earlier numbers were
withdrawn: they came from clips whose motion ranges barely overlapped —
photographs that happened to move gently, real faces that happened to move a
lot — and comparing two classes over different parts of the motion range
measures the mismatch, not the classes. Re-measured against a matched corpus
(150 real clips, 150 photograph clips built to the same frame count and the
same measured motion):

| | depth |
|---|---|
| Photographs | 0.00060 – 0.00197 |
| Real faces | 0.00313 – 0.256 (median 0.0111) |

`LIVENESS_MIN_DEPTH` is **0.0025**, inside that gap: no photograph accepted and
no real person refused across 270 judged clips. See the calibration note in
`backend/config.py`.

Two regimes remain unmeasured and are reported as *unmeasurable* rather than
judged: a face closer than `LIVENESS_MIN_FACE_PX` cannot be told from a
photograph at all, so a face too close or too far is **not** liveness-checked
either way.

**Caveats that matter.** The identification numbers come from roughly 13
enrolled athletes in one lighting condition. `data/eval_labels.json` has three
photos, one of which is a second frame of the same burst — a repeatability
check, not an independent sample. At that N, an "equal error rate of 0.00%" is
not a measurable quantity, whatever the sweep prints.

The defensible claim is: *this identified a small number of known athletes under
one set of conditions and rejected the strangers it was shown.* That is a
promising pilot result, not a validated accuracy figure.

---

## Layout

```
backend/
  main.py           video + photo pipelines, enrolment, static host
  routes.py         auth, users, centres, people, password-reset requests
  auth.py           PBKDF2 passwords, sessions, role scoping, login throttling
  password_reset.py forgotten-password requests, approved by a super admin
  sessions.py       registers, drafts, submission, self-mark, reminders
  signup.py         self-registration for athletes and coaches
  blink.py          blink liveness from a short clip's landmark track
  portrait.py       ranks a clip's frames to pick the best one to match on
  db.py             PostgreSQL pool, placeholder translation, advisory locks
  database.py       schema, gallery, attendance, analytics
  storage.py        switchable photo storage (local | S3)
  liveness.py       parallax depth from a short clip
  detector.py       YuNet, quality gates, printed-face rejection
  recognizer.py     SFace embedding and score fusion
  metaheuristics.py Hungarian assignment (group-photo path only - see above)
  centres.py        centre registry, search, haversine geo-fencing
  maintenance.py    scheduled cleanup: expired sessions, stale drafts
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
  each one states its own status.
- **`/api/health` returns 503 when the database is unreachable**, with a body
  explaining what is wrong (`"database": "unreachable"`), rather than 200 with
  `status: degraded`. A health check that always returns 200 is not a health
  check — it let an instance that could neither read nor write stay in
  rotation.
- **The still-photo group endpoints remain**
  (`POST /api/attendance/process`, `/process-video`, `POST /api/students`),
  unauthenticated against liveness by nature, and **the current frontend never
  calls them.** `scripts/enroll_from_pdf.py` and `scripts/bulk_assign.py`
  still do. Three pieces of the pipeline only run through this path: the
  Hungarian assignment solver, continual learning from daily group photos, and
  geo-fencing on attendance (as opposed to self-mark, which is live) — see
  [How it works](#how-it-works).

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
