# v1 — Coaches, athletes, and confirmed registers

> **STATUS — this is the plan as written, not a description of the build.**
>
> It is kept unedited so the reasoning behind each decision stays readable. Where
> the finished system differs, the build is right and this is history:
>
> - **Phone verification (OTP) was never shipped.** Sending SMS in India needs DLT
>   registration, which is procurement and is not done, so a code could never
>   reach anybody. The endpoints, the `otp_challenges` table and the SMS provider
>   were removed rather than left dormant. Signup is: details → choose a coach →
>   record your face. See DEVELOPMENT.md.
> - **Coach self-registration was added**, which this plan does not cover, gated
>   by a per-centre join code and approved by a super admin rather than a coach.
> - **`MATCH_THRESHOLD` stayed at 0.570**, not the 0.55 suggested here — measured
>   EER on this corpus is 0.00% at 0.570.
> - **`RATIO_TEST_THRESHOLD` was removed.** Measured over 988 assignments it
>   never fires at either polarity; see the note in `backend/config.py`.
> - **Roles are three, not two** — athlete accounts exist, and a centre scope is
>   not a role check.
>
> `V1-CHECKLIST.md` tracks what was actually built, phase by phase, with the
> verification counts.

Work plan for the next version. Every open question from the design review is
settled below; this document is the answer, not the proposal.

Read alongside [DEVELOPMENT.md](DEVELOPMENT.md) — the traps listed there
(`Row` unpacking, storage keys, startup concurrency, SQLite-vs-Postgres dialect)
all apply to this work and each has already caused a bug.

**Scope in one paragraph.** Athletes are attached to coaches rather than only to
centres. Attendance stops being written by the recogniser and becomes a register
the coach builds from one or more captures, reviews against their full roster,
and submits under their own face. Athletes get accounts and can mark themselves
present from a video, which drafts into their coach's register rather than
bypassing it. Existing accounts become super admins with an oversight dashboard.

---

## 1 · Decisions, settled

| # | Question | Decision |
|---|---|---|
| 1 | Can an athlete be under more than one coach? | **Yes.** Join table, not a single FK. |
| 2 | Photo or video capture? | **Both, by path.** Coach group capture may be a photo; athlete self-marking must be video. |
| 3 | Can one capture seed another coach's register? | **No.** Name them, don't draft them. |
| 4 | Who approves a pending athlete? | **Their chosen coach.** |
| 5 | Do athletes get a login in v1? | **Yes**, for marking their own attendance. |
| 6 | What happens when a coach leaves? | **Deferred to v2.** Links cascade; orphaned athletes are acceptable for now. |
| 7 | Does centre-wide capture survive? | **Yes**, super-admin only. |

### Two of these need a consequence spelled out

**Multi-coach breaks `UNIQUE(student_id, date)`.** An athlete under a strength
coach at 6am and a sport coach at 4pm attended *two sessions*, and both are real
attendance. Attendance therefore becomes **one row per athlete per session**, not
per day. Section 5 lists everything that counts attendance and now has to say
`COUNT(DISTINCT student_id)` or it double-counts the moment a shared athlete
appears.

**Athlete self-marking must not bypass the coach.** If a self-mark wrote a
confirmed row, there would be two routes to attendance and one of them has no
human check — which undoes the reason for building the review step. So a
self-mark creates a **draft** row in the coach's session, badged
`origin='self_marked'`, and the coach still submits. Section 4.2 covers the
geo-fence, which is the real abuse surface here.

### The liveness principle that falls out of decision 2

Coach photo capture has no liveness — a still frame cannot distinguish a group
from a photograph of one. That is acceptable **because somebody is accountable**:
the coach reviews the roster and submits under their own verified face. Athlete
self-marking has no such witness, so liveness is mandatory there.

> **Residual risk, accepted deliberately.** Coach face verification stops someone
> else using the coach's phone. It does **not** stop a dishonest coach
> photographing a laptop showing last week's group photo, confirming the roster
> themselves, and submitting under their own genuine face. The mitigations are
> the geo-fence, the stored capture, and photo-only sessions being visible to
> admins — not prevention. Worth being explicit about rather than discovering.

---

## 2 · Data model

All additive. No table dropped, no column changes meaning, recognition path
untouched.

```sql
-- ---------------------------------------------------------------- accounts
ALTER TABLE users
  ADD COLUMN status              TEXT NOT NULL DEFAULT 'active',
      -- pending | active | suspended | rejected
  ADD COLUMN phone_verified_at   TEXT,
  ADD COLUMN approved_by         INTEGER REFERENCES users(id),
  ADD COLUMN approved_at         TEXT,
  ADD COLUMN guardian_name       TEXT,
  ADD COLUMN guardian_consent_at TEXT;

-- role CHECK widens to ('super_admin','coach','athlete').
-- users.student_id already exists and is nullable; it becomes REQUIRED for
-- coach and athlete accounts. That column is the whole reason this migration
-- is additive rather than a merge.

-- ------------------------------------------------- athlete <-> coach links
CREATE TABLE coach_athletes (
  id         SERIAL PRIMARY KEY,
  coach_id   INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  athlete_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  is_primary INTEGER NOT NULL DEFAULT 0,  -- the coach who approved them
  created_at TEXT NOT NULL,
  UNIQUE (coach_id, athlete_id)
);
CREATE INDEX idx_ca_coach   ON coach_athletes(coach_id);
CREATE INDEX idx_ca_athlete ON coach_athletes(athlete_id);

-- ------------------------------------------------------------- registers
CREATE TABLE attendance_sessions (
  id                 SERIAL PRIMARY KEY,
  centre_id          INTEGER NOT NULL REFERENCES centres(id),
  coach_id           INTEGER REFERENCES students(id),   -- NULL = super-admin sweep
  opened_by          INTEGER NOT NULL REFERENCES users(id),
  date               TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'draft',     -- draft | submitted | expired
  created_at         TEXT NOT NULL,
  expires_at         TEXT NOT NULL,
  submitted_at       TEXT,
  submitter_verified INTEGER,             -- coach face check passed
  submitter_score    DOUBLE PRECISION,
  submitter_liveness TEXT,
  UNIQUE (centre_id, coach_id, date)
);

CREATE TABLE session_captures (
  id               SERIAL PRIMARY KEY,
  session_id       INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
  media_key        TEXT NOT NULL,          -- uploads/ key
  kind             TEXT NOT NULL,          -- video | photo
  liveness_verdict TEXT,                   -- live | screen | inconclusive | not_checked
  liveness_depth   DOUBLE PRECISION,
  faces_detected   INTEGER,
  recognised       INTEGER,
  latitude         DOUBLE PRECISION,
  longitude        DOUBLE PRECISION,
  geo_status       TEXT,
  distance_m       DOUBLE PRECISION,
  created_at       TEXT NOT NULL
);

-- --------------------------------------------------------------- rows
ALTER TABLE attendance
  ADD COLUMN session_id INTEGER REFERENCES attendance_sessions(id) ON DELETE CASCADE,
  ADD COLUMN capture_id INTEGER REFERENCES session_captures(id) ON DELETE SET NULL,
  ADD COLUMN status     TEXT NOT NULL DEFAULT 'confirmed',  -- draft | confirmed
  ADD COLUMN origin     TEXT;   -- recognised | self_marked | coach_added

-- REPLACE the day-scoped constraint. An athlete under two coaches legitimately
-- attends two sessions in a day, and the old constraint rejects the second.
ALTER TABLE attendance DROP CONSTRAINT attendance_student_id_date_key;
ALTER TABLE attendance ADD CONSTRAINT attendance_student_session_key
  UNIQUE (student_id, session_id);
CREATE INDEX idx_att_status_date ON attendance(status, date);

-- --------------------------------------------------------------- OTP
CREATE TABLE otp_challenges (
  id          SERIAL PRIMARY KEY,
  phone       TEXT NOT NULL,
  code_hash   TEXT NOT NULL,      -- hashed, never the code
  expires_at  TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  consumed_at TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_otp_phone ON otp_challenges(phone, created_at);
```

### Migration notes

- Existing `attendance` rows have no `session_id`. Backfill one synthetic
  `submitted` session per (centre, date) and point them at it, or leave
  `session_id` NULL and treat NULL as "pre-sessions, confirmed". Leaving it NULL
  is less work and honest — just make sure every read handles it.
- Existing rows are `status='confirmed'` by the column default. Correct.
- Geo columns stay on `attendance` for historical rows. Reads should prefer
  `session_captures` and fall back, or the Records page loses location on
  everything older than this change.
- `UPDATE users SET role='super_admin'` for the existing accounts, per the brief.
- Run the whole thing inside `pgdb.advisory_lock` if any of it happens at
  startup. See DEVELOPMENT.md §3.

---

## 3 · Roles and scoping

| | Athlete | Coach | Super admin |
|---|---|---|---|
| Own attendance history | Yes | Yes | Yes |
| Mark self present | Yes | — | — |
| Roster | Own coaches only | Own athletes; centre roster read-only | All |
| Capture and submit a register | No | Own session | Any centre |
| Approve pending accounts | No | Own queue | All |
| Centres, accounts | No | No | Full |

`auth.scope_centre` stays. Two new helpers alongside it:

- **`scope_coach(user, coach_id)`** — a coach may only act on their own session.
- **`scope_self(user, student_id)`** — an athlete may only read or write their
  own attendance.

An athlete is **not a coach with fewer buttons.** Every `role ==` comparison in
`auth.py`, `routes.py` and the frontend needs revisiting rather than defaulting.

---

## 4 · Flows

### 4.1 Coach marks a group

1. **Open** — `POST /api/sessions` gets or creates today's session for this
   coach at this centre. Idempotent.
2. **Capture** — `POST /api/sessions/{id}/captures` with a video *or* a photo.
   Video runs liveness; photo records `liveness_verdict='not_checked'`.
   Detection and embedding are unchanged.
3. **Match globally, filter after.** Score against the **whole gallery**, then
   route each result:

   | Recognised person is… | What happens |
   |---|---|
   | Under this coach | Draft row, pre-ticked, `origin='recognised'` |
   | Another coach, same centre | Named, shown as "Coach X's athlete", **not** drafted |
   | Another centre | Existing off-centre reporting, not marked |
   | Not recognised | Unknown-face card with *Who is this?*, ranked over this coach's athletes first |

   > **Do not narrow the gallery before matching.** It was tried and reversed —
   > `main.py` records that scoping first made a wrong-centre photo return zero
   > matches with no way to distinguish that from the recogniser failing.
   > Measured, wrong-centre matches are zero at every threshold tested, so the
   > wider gallery costs nothing; strangers are what the threshold defends
   > against, and shrinking the roster does not help with those.

4. **Review** — `GET /api/sessions/{id}` returns every athlete linked to this
   coach, present and absent, with toggles. Recognised ones carry their crop and
   score. Capturing again adds to the same session; it does not replace it.
5. **Submit** — `POST /api/sessions/{id}/submit` with a self-clip. Liveness plus
   a **1:1** match against the coach's own templates. On pass, promote every
   draft row to `confirmed` in one transaction and close the session.

Several captures per session is the point, not an edge case: recall is 100% at
50-pixel faces and 23% at 24 pixels, so one frame of thirty athletes across a
hall loses most of them. Left half, right half, stragglers.

### 4.2 Athlete marks themselves

1. `GET /api/me/coaches` — which coaches this athlete is under.
2. `POST /api/me/attendance` with a video clip and a `coach_id`.
3. Server: **liveness required** (no exceptions — nobody else is watching), then
   a **1:1** match against this athlete's own templates.
4. On pass, get-or-create that coach's session for today and insert a **draft**
   row with `origin='self_marked'`. If the coach has not captured yet, this
   creates the session — they open the app to a partly-filled register.
5. The coach sees self-marked athletes badged distinctly and still confirms them.

**Geo-fencing carries real weight here.** An athlete self-marking from home is
the obvious abuse, and unlike a coach's capture there is no second person in the
loop. Store the fix on the draft row and:

- `inside` — normal.
- `outside` or `no_fix` — accept the draft, **badge it loudly** in the coach's
  review list with the distance. Do not silently reject; a genuine athlete with
  a bad GPS fix should not lose their attendance without anyone seeing why.

Rate-limit self-marking to one accepted attempt per athlete per coach per day,
plus a short cooldown between failures, or it becomes a way to burn CPU.

### 4.3 Super-admin centre sweep

Unchanged in spirit. A super admin captures at any centre; the session has
`coach_id = NULL`, drafts everyone recognised at that centre regardless of coach,
and the admin submits it under their own face. This is the fallback when a
coach's phone is broken, and it is the only path that crosses coach boundaries.

### 4.4 Signup and approval

1. `POST /api/signup` — username, password, email, phone, role, centre, and
   **chosen coach** (shown by name and enrolment photo). Creates
   `users.status='pending'`.
2. `POST /api/signup/otp/send` → `POST /api/signup/otp/verify`. Codes stored
   hashed, short expiry, attempt counter, rate-limited **per number and per
   address** — the login throttle in `auth.py` is the pattern to copy, and
   without it this is an SMS bill someone else runs up.
3. `POST /api/signup/face` — the existing guided capture, unchanged. Templates
   are computed and stored.
4. **Pending state**: the account cannot sign in, and `load_gallery()` **must
   exclude templates belonging to pending accounts.** One join. Forgetting it is
   what lets an unapproved stranger be marked present.
5. `GET /api/approvals` — the chosen coach's queue.
   `POST /api/approvals/{user_id}` — approve or reject, capturing guardian name
   and consent timestamp where the athlete is a minor. On approval: status goes
   `active`, the `coach_athletes` link is created with `is_primary=1`, and the
   templates enter the gallery.

Capture the face at signup but gate the gallery on approval. Deferring capture
means a second session with the person and half never come back; adding to the
gallery at signup means an unapproved stranger can be marked present.

> **SMS is procurement, not code.** Transactional SMS to Indian numbers needs
> DLT registration with a TRAI-approved platform — entity, sender ID, and each
> template approved in advance. Days to weeks, and it needs someone with
> authority to register the organisation. **Start it now**; it blocks nothing
> else. Ship with email verification if SMS is not ready.

---

## 5 · Endpoints

### New

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/sessions` | Get or create today's session |
| `GET` | `/api/sessions/{id}` | Roster, drafts, unknowns, captures |
| `POST` | `/api/sessions/{id}/captures` | Add a video or photo capture |
| `PATCH` | `/api/sessions/{id}/roster/{student_id}` | Toggle present / absent |
| `POST` | `/api/sessions/{id}/submit` | Coach self-clip, verify, promote |
| `GET` | `/api/me/coaches` | Athlete's coaches |
| `GET` | `/api/me/attendance` | Athlete's own history |
| `POST` | `/api/me/attendance` | Athlete self-marks from a clip |
| `POST` | `/api/signup` | Create a pending account |
| `POST` | `/api/signup/otp/send` · `/verify` | Phone verification |
| `POST` | `/api/signup/face` | Guided capture for a pending account |
| `GET` | `/api/approvals` | Pending queue for the calling coach |
| `POST` | `/api/approvals/{user_id}` | Approve or reject |
| `GET` | `/api/coaches/{id}/athletes` | Roster for one coach |
| `POST` · `DELETE` | `/api/coaches/{id}/athletes/{athlete_id}` | Manage links |
| `GET` | `/api/admin/overview` | Super-admin dashboard data |

### Changed

- `POST /api/attendance/process-video` — becomes a session capture. Keep the old
  path working during migration or cut the frontend over in the same release.
- `GET /api/attendance`, `/api/attendance/export`, `/api/stats`,
  `/api/analytics` — must filter `status='confirmed'`.
- `POST /api/users` — still super-admin, still creates coaches directly. Signup
  is the athlete path; this stays for staff.

---

## 6 · What breaks — checklist

Work through these; several fail silently.

- [ ] **Every attendance count** filters `status='confirmed'`. `stats()`,
      `analytics()`, `attendance_for_day()`, CSV export, dashboard tiles. Drafts
      otherwise inflate the numbers in the direction that looks like success.
- [ ] **`stats().present_today` becomes `COUNT(DISTINCT student_id)`.** It is a
      plain `COUNT(*)` today, which double-counts a shared athlete the moment
      multi-coach lands. `analytics().trend` already uses DISTINCT — copy it.
- [ ] **`load_gallery()` excludes pending accounts.**
- [ ] **`users.role` CHECK** widened, and every `role ==` comparison reviewed.
- [ ] **Unique constraint** swapped from `(student_id, date)` to
      `(student_id, session_id)`.
- [ ] **Reads handle `session_id IS NULL`** for pre-migration rows.
- [ ] **Geo reads prefer `session_captures`, fall back to `attendance`.**
- [ ] **OTP endpoint throttled** per number and per address.
- [ ] **Self-marking rate-limited** per athlete per coach per day.
- [ ] **`scope_coach` and `scope_self`** applied on every new endpoint, the way
      `scope_centre` already is.
- [ ] **Deletion**: a person now has an account, templates, photos, attendance
      *and* coach links. `DELETE /api/students/{id}` already misses capture
      frames and backups — see DATA-HANDLING.md §3. Widen it here rather than
      after.
- [ ] **`scripts/live_test.py` fixed** (the `Row`-unpack bug). It is the harness
      behind the threshold table and currently crashes, so nothing in §7 is
      measurable until it runs.
- [ ] **CI stays green.** The import check catches broken modules; add a smoke
      assertion that draft rows do not appear in `/api/stats`.

---

## 7 · The threshold is worth revisiting

Not required for v1, but do not miss it.

`MATCH_THRESHOLD = 0.570` rests on one clause in its own comment: a false accept
is "a wrong attendance record for a minor **that nobody is prompted to check**."

With a review screen, somebody is. Every proposed match now passes under the eyes
of the person who knows these athletes by name, before it counts. That makes a
false accept cheap — the coach unticks it — where today it is permanent and
invisible. The measured table:

| Threshold | Recall | Wrong-centre | Strangers accepted |
|---|---|---|---|
| 0.55 | 45 | 0 | 1 |
| 0.57 | 44 | 0 | 0 |
| 0.60 | 39 | 0 | 0 |

0.55 recovers a genuine match and admits one impostor *pair* — which the coach
would now see and reject. **Re-measure, don't just lower it**: fix
`live_test.py`, re-run `scripts/evaluate.py --sweep`, and pick deliberately.

Two other thresholds needed, neither of which should reuse `MATCH_THRESHOLD`:

- **`COACH_VERIFY_THRESHOLD`** — 1:1 verification of the submitting coach.
- **`SELF_VERIFY_THRESHOLD`** — 1:1 verification of a self-marking athlete.

0.570 is tuned for open-set identification against a whole roster. These are 1:1
checks where you already know who the person claims to be; SFace publishes 0.363
for that case. Using the open-set number makes them far stricter than necessary,
and the failure lands on someone trying to do their job.

**When verification fails**, retry freely, then allow submission and record it as
unverified with the score and liveness verdict, surfaced on the admin dashboard.
The same asymmetry runs through this whole codebase: a genuine person refused is
worse than a spoof let through. An unverified register that exists and is flagged
beats a verified register that was never taken.

---

## 8 · Phases

Each phase is shippable on its own.

### Phase 1 — Coach mapping and the session flow
`coach_athletes`, `attendance_sessions`, `session_captures`, `attendance.status`,
the constraint swap, and the migration setting existing accounts to super admin.
Coach capture writes drafts; review screen with the full roster and toggles;
several captures accumulate. Photo and video both accepted, photo marked
`not_checked`.

**Done when:** a coach captures twice, sees every one of their athletes with the
recognised ones pre-ticked, toggles two by hand, and no row is `confirmed`
anywhere. `/api/stats` is unchanged by the drafts.

### Phase 2 — Submit with coach verification
`COACH_VERIFY_THRESHOLD`, the self-clip endpoint, transactional promotion,
unverified-submission recording.

**Done when:** submitting promotes every draft in one transaction; a failed
verification after three retries still submits, flagged; `/api/stats` moves only
after submission.

### Phase 3 — Athlete accounts and self-marking
`users.role` widened, `scope_self`, athlete login, own-history page,
`POST /api/me/attendance` with mandatory liveness, geo badging, rate limits.

**Done when:** an athlete self-marks, a draft appears in the right coach's
session badged `self_marked`, and marking from outside the geofence is accepted
but flagged with the distance.

### Phase 4 — Super-admin oversight
Sessions submitted today, coaches who have not submitted, unverified
submissions, photo-only captures, drafts about to expire, pending approvals.

**Done when:** an admin can answer "which registers are missing today" in one
screen. Build this **before** opening signup — it is how you watch the thing you
are about to open.

### Phase 5 — Pending accounts and approval
`users.status`, gallery exclusion, approval queue, guardian consent capture,
coach-athlete link creation on approval.

**Done when:** a pending account cannot sign in, its templates never match in a
capture, and approving it makes both true in one action.

### Phase 6 — Self-signup with OTP
Signup form with the coach selector, OTP send/verify, face capture into a
pending account. **Only after Phase 5** — the gate has to exist before there is
anything to gate. Start the DLT registration during Phase 1 regardless.

**Done when:** a stranger can complete signup end to end and is inert until a
coach approves them.

---

## 9 · Deferred to v2

- **Coach departure.** Links cascade; orphaned athletes appear on nobody's
  register and nobody is prompted. Acceptable for v1, needs a reassignment flow
  before a real pilot.
- **Cross-coach drafting.** One hall photo could seed three registers. Efficient,
  but it means submitting under your own face a register somebody else took.
- **Session types.** Morning and evening sessions with the same coach currently
  collide on `UNIQUE(centre_id, coach_id, date)`. Add a `slot` column when
  someone asks.
- **Guardian portal.** DATA-HANDLING.md notes that consent withdrawal is manual
  and that deletion misses capture frames and backups. Both need closing before
  real children are enrolled.
