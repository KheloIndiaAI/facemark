# FaceMark

Attendance for Khelo India sports centres. A coach photographs the group, the
system recognises who is present and marks the register — with the capture
geo-tagged against the centre's location so attendance can be shown to have been
taken where it was claimed.

Detection is YOLO11s-face fused with SCRFD; recognition is a three-model
ArcFace/AdaFace ensemble matched against a multi-template gallery.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.download_models  # ~450 MB, see the note below
python run.py
```

Open **http://127.0.0.1:8000**. On first run the console prints a generated
`admin` password once — sign in with it and change it immediately from the
sidebar. Set `FACEMARK_ADMIN_PASSWORD` beforehand to choose your own.

### Models

Weights total about 1 GB and three files exceed GitHub's 100 MB limit, so they
are not in the repo. `scripts/download_models.py` fetches four of them from
public mirrors that were verified SHA-256-identical to known-good copies.
`adaface_ir101.onnx` and `gfpgan_v1.4.onnx` have no usable public mirror — host
them yourself and set `MODEL_ASSET_BASE` (see `DEPLOYMENT.md`). Both are
optional: the ensemble renormalises over whichever models load.

---

## Roles

Two roles, and the difference is enforced in SQL, not by hiding buttons.

| | Super admin | Coach |
|---|---|---|
| Centres | All, plus create/edit/import | Own centre only |
| Athletes and coaches | All centres | Own centre only |
| Attendance | All centres | Own centre only |
| Login accounts | Full management | No access |

A coach passing another centre's `centre_id` in a query string gets a 403 —
`auth.scope_centre` narrows every query server-side.

---

## How it works

### Detection

Two detectors run and their boxes are merged by weighted box fusion. A box only
one detector found must clear a higher confidence bar, because single-detector
low-confidence boxes are almost always background texture — foliage, brick,
clothing folds — rather than faces.

| Mode | What runs | Use for |
|---|---|---|
| `fast` | YOLO11s only | Large groups, speed priority |
| `fused` | YOLO11s + SCRFD + WBF | Default |
| `accurate` | YOLO with flip TTA + SCRFD | Hard photos |

### Recognition

Each face is aligned to the canonical 112×112 template, embedded by all three
models with horizontal-flip TTA, and scored by cosine similarity. Per-model
scores are max-pooled across each student's templates, then fused with
renormalised weights.

A student owns several templates — the raw enrolment photo, a GFPGAN-restored
version, an optional live photo, and any adapted from daily photos — so old and
current appearances are both represented.

### Assignment

Faces are assigned to identities by the Hungarian algorithm, so no student can
be marked present twice in one photo. Sub-threshold pairs are masked *before*
the solve: `linear_sum_assignment` always returns `min(N, M)` pairs, so an
unenrolled visitor left in the matrix would be handed an identity, and because
the solver optimises the sum it would happily move a real student onto that
stranger's face. Masking first means a face that cannot legitimately match
anyone simply goes unassigned.

### Geo-marking

The browser's location is attached to each capture and compared against the
centre's coordinates. Each record gets `inside`, `outside`, `no_fix` (browser
gave no position) or `unknown` (centre has no coordinates), with the haversine
distance. A refusal never blocks attendance.

---

## Testing

Three reusable harnesses. Run the server first for the security suite.

```bash
python -m scripts.evaluate --sweep                        # FAR/FRR/EER, d-prime, per-person
python -m scripts.robustness                              # degradation envelope
python -m scripts.security_test --password <admin-pw>     # 48 checks
```

`evaluate.py` deliberately **excludes `adapted` templates** by default. Those are
built from processed group photos, so scoring those photos against them is
self-matching — it returns similarities of 1.000 and a meaningless 100%.

Measured on 13 enrolled athletes (see the validation report for the full
caveats — 13 identities in one lighting condition is a narrow evidence base):

| | |
|---|---|
| Identification | 26/26 across two frames |
| False accepts, 16 strangers | 0 |
| Separation d′ | 4.91 |
| Equal error rate | 0.10% at threshold 0.385 |
| Security checks | 48/48 |

Robustness holds through 0.35–2.2× brightness, JPEG quality down to 15, tilt to
22°, and half-size faces. **Motion blur is the weak axis** — matches start
dropping at 5 px of blur. Across all 35 degraded variants there were no false
matches: the system misses people rather than marking the wrong one.

---

## Configuration

Everything lives in `backend/config.py`; these are the ones worth knowing.

| Setting | Default | Notes |
|---|---|---|
| `MATCH_THRESHOLD` | `0.38` | Measured 0.005 off the equal-error point. Don't move it without re-running `scripts/evaluate.py --sweep`. |
| `TTA_SCALES` | `(1.0,)` | Flip TTA only. The second warp scale cost 12 s per photo *and* lost a match. |
| `DETECTOR_CONF` / `SCRFD_CONF` | `0.25` / `0.50` | SCRFD below ~0.45 emits background boxes YOLO never confirms. |
| `CONTINUAL_LEARNING` | `True` | Gated on confidence **and** margin over the runner-up **and** face size. Confidence alone let ambiguous faces poison the gallery. |
| `GFPGAN_ENABLED` | `True` | `0` saves ~325 MB RAM. |

Environment overrides: `FACEMARK_DATA_DIR`, `FACEMARK_MODELS_DIR`,
`FACEMARK_ADMIN_PASSWORD`, `CORS_ORIGINS`, `COOKIE_SECURE`, `DETECTION_MODE`,
`ORT_THREADS`. See `DEPLOYMENT.md`.

---

## Layout

```
backend/
  main.py         attendance pipeline, enrolment, static host
  routes.py       auth, users, centres, people
  auth.py         PBKDF2 passwords, server-side sessions, role scoping
  centres.py      centre registry, search, haversine geo-fencing
  detector.py     YOLO + SCRFD + weighted box fusion
  recognizer.py   ensemble embedding and score fusion
  metaheuristics.py  Hungarian assignment
  database.py     SQLite schema and migrations
frontend/         vanilla SPA, no build step
scripts/
  download_models.py  fetch weights
  evaluate.py         biometric metrics
  robustness.py       degradation envelope
  security_test.py    security and input validation
  cleanup_gallery.py  purge adapted templates / template-less students
  archive/            broken one-off scripts, kept for reference
```

---

## Known issues

- **Legacy adapted templates.** The 40 currently in the database were written
  under the old loose gate. Measured effect: no accuracy benefit, one false
  positive, and separation d′ down from 4.91 to 3.67. Clear them with
  `python -m scripts.cleanup_gallery --apply --keep-ghosts`.
- **Latency.** About 16 s per group photo on CPU. A GPU with `onnxruntime-gpu`
  should bring that to a few seconds, but that is projected, not measured.
- **Demo centres.** First run seeds eight placeholder centres, every one flagged
  `is_demo` and prefixed `DEMO-`. They are **not** real Khelo India records.
  Replace them via Centres → Import, or delete with Centres → Remove demo.

---

## Handling biometric data

This stores face embeddings and photographs of children. Face templates are not
revocable the way a password is. Before real deployment: obtain informed consent
from guardians, publish a retention period and honour it, restrict who holds
super-admin accounts, and check your obligations under the DPDP Act 2023.
Nothing in this codebase discharges those duties.

## License

MIT.
