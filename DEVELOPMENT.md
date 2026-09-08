# Development

Everything you need to run FaceMark locally, and the handful of things about it
that will otherwise cost you an afternoon.

Read [DATA-HANDLING.md](DATA-HANDLING.md) before touching anything that stores a
face. Most of the people in this system are children.

---

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
```

You need a PostgreSQL 17 database. Put its URL in `.env`:

```
DATABASE_URL=postgresql://user:password@localhost:5432/facemark
FACEMARK_TIMEZONE=Asia/Kolkata
```

```bash
python run.py                   # http://127.0.0.1:8000
```

On an empty database the first start creates a super admin and prints the
password once, at WARNING level. It is never stored in plaintext — if you lose
it, reset it with `POST /api/users/{id}/password` as another admin, or drop the
`users` table and restart.

`.env` is gitignored and must stay that way. `.env.example` is the committed
template, with empty credentials.

---

## Layout

```
backend/
  main.py          FastAPI app, recognition and register endpoints
  routes.py        auth, users, centres, people
  sessions.py      registers: sessions, captures, drafts, approvals, rosters
  signup.py        self-registration: pending accounts, coach choice, face
  maintenance.py   expiry and retention sweeps
  database.py      schema, migrations, gallery, attendance
  db.py            Postgres layer and the `?` -> `%s` shim
  auth.py          passwords, sessions, role guards
  detector.py      YuNet face detection
  recognizer.py    SFace embeddings and score fusion
  liveness.py      video parallax check
  centres.py       centre registry and geo-fencing
frontend/          vanilla JS, no build step. Edit and reload.
scripts/           tooling and evaluation harnesses
```

---

## Things that will catch you out

**The database layer is not SQLite, but the SQL looks like it.** `db.py`
translates `?` placeholders to `%s`. Write `?`.

**`Row` iterates KEYS, not values.** It subclasses `dict`, so
`a, b = row` binds column *names*. Index positionally (`row[0]`) or by name
(`row["id"]`). Nothing in the codebase unpacks a row; don't start.

**A failed statement aborts the whole transaction.** On Postgres,
`try: ... except: pass` around an INSERT does not let you carry on — every
later statement raises `InFailedSqlTransaction`. Use a SAVEPOINT; see
`sessions.get_or_create`.

**`conn.insert()` already appends `RETURNING id`.** Don't add your own.

**Route order matters.** FastAPI matches in declaration order, so a literal path
must be declared before a parameterised sibling. `/api/centres/public` was
shadowed by `/centres/{centre_id}` and answered 401; it lives at
`/api/signup/centres` now. `/api/coaches/{id}/roster` is declared before
`/api/coaches/{id}/athletes/{athlete_id}` for the same reason.

**A centre scope is not a role check.** An athlete has a `centre_id` too.
`Depends(auth.current_user)` plus `scope_centre` reads like access control and
is not — that is how an athlete could once write confirmed attendance for a
whole centre. Writes take `require_staff` or `require_super_admin`.

**The Dockerfile runs `--workers 2`.** Anything kept in a module-level dict
exists once per worker. Signup tokens used to live in one, so half of all
signups failed on both deployments. Per-request state belongs in the database;
the login and per-address throttles remain in process deliberately, and allow N
times the burst with N workers.

**Timestamps.** Stored timestamps use the centre's clock via
`config.now_stamp()` / `today_str()`. Session expiry comparisons use
`datetime.now()` on both sides — do not "fix" one side of a comparison, or
sessions stop expiring.

---

## Tests and harnesses

There is no pytest suite. Verification is a set of scripts that run against a
real database and a real server, because the failures that matter here are SQL
and HTTP failures that a mock would agree with.

```bash
python -m scripts.evaluate              # FAR/FRR/EER, rank-1, open-set rejection
python -m scripts.evaluate --sweep      # threshold calibration
python -m scripts.benchmark_detection   # detection modes and latency
python -m scripts.calibrate_liveness --live DIR --spoof DIR
```

`scripts/evaluate.py` defaults to `--sources id,restored,live` so the gallery
contains only enrolment data. Including `adapted` templates scores photos
against templates learned from those same photos and reports a meaningless
100%.

CI (`.github/workflows/ci.yml`) runs an import check, Ruff (`E9,F63,F7,F82`
blocking; `F401` reported only), and a job with a real Postgres service
asserting that a draft never reaches the attendance counts.

**Never point a harness at the live database if it writes.** `evaluate.py` and
`benchmark_detection.py` are read-only by design. The script this replaced,
`evaluate_accuracy.py`, opened with `DELETE FROM attendance` and survived only
because a later statement failed and rolled the transaction back.

---

## Liveness: the one threshold that is not settled

`LIVENESS_MIN_DEPTH = 0.006` separates a real face from a photograph held up to
the camera, and it was fitted on **two real people** and three flat portraits.
The clusters it sits between are 0.0055 (flat) and 0.0072 (real), so the margin
on the real-person side is about 1.2x. That is thin, and it is the difference
between an athlete being refused attendance and a photograph being accepted.

It cannot be improved without more clips, and the pilot is where they exist. To
re-measure there:

1. Collect ten or more clips of different real people using the normal
   registration capture, on the phones the centre actually uses. Put them in one
   directory.
2. Collect the same number of spoof clips: a printed photo, and a face on a
   phone screen, held in front of the camera. Different lighting for each.
3. Run:

   ```bash
   python -m scripts.calibrate_liveness --live LIVE_DIR --spoof SPOOF_DIR
   ```

It prints the depth and motion distributions for both sets and the separation
between them. Move `LIVENESS_MIN_DEPTH` only if the two clusters are cleanly
apart, and bias it toward the flat side as the current value does — a genuine
athlete turned away is a worse failure than a spoof let through, because the
first happens to somebody standing in front of you.

Those clips are face data. They live on disk under a gitignored path and are
deleted once the numbers are recorded; see [DATA-HANDLING.md](DATA-HANDLING.md).

---

## Sample data

```bash
python scripts/generate_samples.py
```

Downloads faces from randomuser.me and composes fabricated group photos into
`samples/`. All of it is invented and none of it is Khelo India data. It writes
to `samples/` only — it used to write enrolment images into `data/students/`,
alongside photographs of real athletes.

---

## No phone verification, and no self-service password reset

Both were removed. They depended on sending an SMS, and sending SMS to Indian
numbers requires DLT registration with a TRAI-approved platform — entity, sender
ID and every template approved in advance. That is procurement and it is not
done, so a code could never reach anybody: signup reached "we sent a code to
your phone", nothing arrived, and the registration died there.

They were briefly kept behind a switch. That was worse than removing them: a
dormant feature still has endpoints that answer, a table that exists, a config
flag to reason about and a UI branch to keep working, and none of it earns
anything.

**What replaced them.** Nothing. A signup is: details → choose a coach → record
your face. A forgotten password goes to an administrator, via
`POST /api/users/{id}/password` on the Accounts page.

**The consequence, stated plainly.** The phone number is an unverified claim —
useful for a coach ringing an athlete, worth nothing as identity. And a user
locked out has to find somebody with admin access.

**Bringing it back.** It is in git, working and tested. `git log -S require_phone_otp`
finds every piece: hashed codes salted per number, attempt counting, expiry,
per-number and per-address throttling, the `otp_challenges` table, an SMS webhook
with DLT fields, and a password reset that could not be used to discover which
usernames exist. Restore that commit rather than writing it again.

If self-service reset is wanted sooner than DLT allows, the cheaper route is a
reset **request** that appears in the coach's approvals queue — the coach knows
the athlete by sight, which is a stronger check than a text message anyway.

---

## Branches

`dev` is the working branch. `main` is the published branch.

**Never push `master`.** Its history contains `data/roster_import/` at commit
`687e494` — face crops, names and NSRS IDs of athletes, most of them minors.
`dev` and `main` are clean. Install the hook that enforces it:

```bash
git config core.hooksPath .githooks
```
