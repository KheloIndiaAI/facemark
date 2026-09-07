# v1 build checklist

Tracks execution of [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md).

Status key: `[ ]` not started · `[~]` in progress · `[x]` done and verified ·
`[!]` blocked or deferred (reason given)

Every `[x]` means **verified**, not merely written — the evidence is named in
the Notes column. "Compiles" is not evidence.

---

## Progress

| Phase | Scope | Status |
|---|---|---|
| 1 | Coach mapping and the session flow | `[x]` **done** |
| 2 | Submit with coach verification | `[x]` **done** |
| 3 | Athlete accounts and self-marking | `[x]` **done** |
| 4 | Super-admin oversight | `[x]` **done** |
| 5 | Pending accounts and approval | `[x]` **done** |
| 6 | Self-signup with OTP | `[x]` **done** (6.9 blocked: procurement) |

---

## Phase 1 — Coach mapping and the session flow

**Done when:** a coach captures twice, sees every one of their athletes with the
recognised ones pre-ticked, toggles two by hand, and no row is `confirmed`
anywhere. `/api/stats` is unchanged by the drafts.

### Schema
| # | Item | Status | Notes |
|---|---|---|---|
| 1.1 | `coach_athletes` table + 2 indexes | `[x]` | verify_p1_schema 20/20 |
| 1.2 | `attendance_sessions` table | `[x]` | verify_p1_schema 20/20 |
| 1.3 | `session_captures` table | `[x]` | verify_p1_schema 20/20 |
| 1.4 | `attendance`: `session_id`, `capture_id`, `status`, `origin` | `[x]` | verify_p1_schema 20/20 |
| 1.5 | Swap `UNIQUE(student_id,date)` → `UNIQUE(student_id,session_id)` | `[x]` | verify_p1_schema 20/20; partial index keeps legacy day-dedup |
| 1.6 | `idx_att_status_date` | `[x]` | verify_p1_schema 20/20 |
| 1.7 | Existing accounts → `super_admin` | `[x]` | verify_p1_schema 20/20 |
| 1.8 | All of it inside the startup advisory lock | `[x]` | verify_p1_schema 20/20 |
| 1.9 | Migration is re-runnable (idempotent) | `[x]` | verify_p1_schema 20/20 (init_db x3) |

### Endpoints
| # | Item | Status | Notes |
|---|---|---|---|
| 1.10 | `POST /api/sessions` get-or-create, idempotent | `[x]` | verify_phase1 27/27 |
| 1.11 | `GET /api/sessions/{id}` roster + drafts + unknowns + captures | `[x]` | verify_phase1 27/27 |
| 1.12 | `POST /api/sessions/{id}/captures` video **or** photo | `[x]` | verify_phase1 27/27 |
| 1.13 | Photo capture records `liveness_verdict='not_checked'` | `[x]` | verify_phase1 27/27 |
| 1.14 | `PATCH /api/sessions/{id}/roster/{student_id}` toggle | `[x]` | verify_phase1 27/27 |
| 1.15 | `GET /api/coaches/{id}/athletes` | `[x]` | route registered; scope_coach |
| 1.16 | `POST`/`DELETE /api/coaches/{id}/athletes/{athlete_id}` | `[x]` | route registered; scope_coach |
| 1.17 | Match against the **whole** gallery, route after | `[x]` | verify_phase1 27/27; whole gallery, routed after |
| 1.18 | Other coach's athlete: named, **not** drafted | `[x]` | verify_phase1 27/27 (10 named, 0 drafted) |
| 1.19 | Several captures accumulate into one session | `[x]` | verify_phase1 27/27 (2nd capture drafted 0) |
| 1.20 | `scope_coach` on every session endpoint | `[x]` | verify_phase1 27/27; 403 for another coach's register |

### Frontend
| # | Item | Status | Notes |
|---|---|---|---|
| 1.21 | Review screen: full roster, present + absent, toggles | `[x]` | driven in browser (3 athletes, absent shown) |
| 1.22 | Recognised rows show crop and score | `[x]` | driven in browser |
| 1.23 | "Capture again" adds to the same session | `[x]` | driven in browser |

---

## Phase 2 — Submit with coach verification

**Done when:** submitting promotes every draft in one transaction; a failed
verification after three retries still submits, flagged; `/api/stats` moves only
after submission.

| # | Item | Status | Notes |
|---|---|---|---|
| 2.1 | `COACH_VERIFY_THRESHOLD` (not `MATCH_THRESHOLD`) | `[x]` | 0.363 (SFace 1:1), separate from MATCH_THRESHOLD |
| 2.2 | `POST /api/sessions/{id}/submit` self-clip | `[x]` | verify_phase2 20/20 |
| 2.3 | Liveness + **1:1** match vs the coach's own templates | `[x]` | self 0.796 vs impostor 0.295, threshold 0.363 |
| 2.4 | Promotion of all drafts in **one transaction** | `[x]` | one UPDATE; 3 promoted atomically |
| 2.5 | Failed verification still submits, recorded unverified | `[x]` | submits on attempt 3, verified=0 |
| 2.6 | `submitter_verified/score/liveness` stored | `[x]` | verified/score/liveness all stored |
| 2.7 | Session closes on submit | `[x]` | status submitted; resubmit + toggle both 409 |

---

## Phase 3 — Athlete accounts and self-marking

**Done when:** an athlete self-marks, a draft appears in the right coach's
session badged `self_marked`, and marking from outside the geofence is accepted
but flagged with the distance.

| # | Item | Status | Notes |
|---|---|---|---|
| 3.1 | `users.role` CHECK widened to include `athlete` | `[x]` | CHECK widened + migration for existing DBs |
| 3.2 | `scope_self` helper | `[x]` | verify_phase3 26/26 |
| 3.3 | Athlete login works; sees only own data | `[x]` | lands on /me; /api/users -> 403 |
| 3.4 | `GET /api/me/coaches` | `[x]` | verify_phase3 26/26 |
| 3.5 | `GET /api/me/attendance` own history | `[x]` | verify_phase3 26/26 |
| 3.6 | `POST /api/me/attendance` — liveness **mandatory** | `[x]` | non-video refused, nothing written |
| 3.7 | 1:1 match vs athlete's own templates (`SELF_VERIFY_THRESHOLD`) | `[x]` | self 0.80 vs impostor 0.295, threshold 0.363 |
| 3.8 | Draft into the coach's session, `origin='self_marked'` | `[x]` | draft, origin=self_marked, right coach |
| 3.9 | Creates the session if the coach has not captured yet | `[x]` | session created by the self-mark |
| 3.10 | Geo `outside`/`no_fix` accepted but badged with distance | `[x]` | geo outside accepted, distance stored |
| 3.11 | Rate limit: one accepted per athlete per coach per day | `[x]` | second mark -> 409 |
| 3.12 | Cooldown between failures | `[x]` | immediate retry -> 429 |
| 3.13 | Athlete own-history page in the frontend | `[x]` | athlete page driven in browser |

---

## Phase 4 — Super-admin oversight

**Done when:** an admin can answer "which registers are missing today" in one
screen. Built **before** signup opens.

| # | Item | Status | Notes |
|---|---|---|---|
| 4.1 | `GET /api/admin/overview` | `[x]` | verify_phase4 13/13; super-admin only (coach 403) |
| 4.2 | Sessions submitted today | `[x]` | lists both submitted registers |
| 4.3 | Coaches who have **not** submitted | `[x]` | LEFT JOIN so a coach with no session appears |
| 4.4 | Unverified submissions | `[x]` | unverified listed, verified not flagged |
| 4.5 | Photo-only captures | `[x]` | photo/not_checked captures listed |
| 4.6 | Drafts about to expire | `[x]` | expiring derived from expires_at |
| 4.7 | Pending approvals count | `[x]` | pending count (users.status added early - see 5.1) |
| 4.8 | Admin dashboard screen | `[x]` | driven in browser; 'Registers missing 1' tile |

---

## Phase 5 — Pending accounts and approval

**Done when:** a pending account cannot sign in, its templates never match in a
capture, and approving it makes both true in one action.

| # | Item | Status | Notes |
|---|---|---|---|
| 5.1 | `users.status` + `approved_by/at`, guardian columns | `[x]` | columns added in phase 4 (4.7 needed the count); behaviour in phase 5 | |
| 5.2 | Pending account **cannot sign in** | `[x]` | 403 with the real reason, checked before the password hash |
| 5.3 | **`load_gallery()` excludes pending** (one join) | `[x]` | NOT EXISTS join; pending person NOT drafted from a real capture |
| 5.4 | `GET /api/approvals` — the coach's own queue | `[x]` | queue scoped by chosen_coach_id; another coach sees nothing |
| 5.5 | `POST /api/approvals/{user_id}` approve / reject | `[x]` | verify_phase5 21/21; 409 on a second decision |
| 5.6 | Guardian name + consent timestamp for minors | `[x]` | guardian name + consent timestamp stored |
| 5.7 | Approval creates `coach_athletes` link `is_primary=1` | `[x]` | link created is_primary=1 in the same transaction |
| 5.8 | Approval puts templates into the gallery in the same action | `[x]` | gallery is a query, so approval un-hides atomically |

---

## Phase 6 — Self-signup with OTP

**Done when:** a stranger can complete signup end to end and is inert until a
coach approves them. **Only after Phase 5.**

| # | Item | Status | Notes |
|---|---|---|---|
| 6.1 | `otp_challenges` table + index | `[x]` | otp_challenges + idx_otp_phone |
| 6.2 | `POST /api/signup` creates `status='pending'` | `[x]` | verify_phase6 26/26; status=pending, login 403 |
| 6.3 | Coach selector shows name + enrolment photo | `[x]` | name + photo_url, behind the signup token |
| 6.4 | `POST /api/signup/otp/send` — codes stored **hashed** | `[x]` | sha256 salted with the number; 64 hex, never returned |
| 6.5 | `POST /api/signup/otp/verify` — expiry + attempt counter | `[x]` | expiry, attempt counter incremented before compare, single use |
| 6.6 | Throttled **per number and per address** | `[x]` | per-number and per-address, plus a resend cooldown |
| 6.7 | `POST /api/signup/face` guided capture into pending account | `[x]` | guided capture into the pending person; 7 templates |
| 6.8 | Signup screens in the frontend | `[x]` | full flow driven in browser to the face step |
| 6.9 | DLT/SMS registration started (procurement, not code) | `[!]` | Not mine to do — needs someone with authority to register the org. Ship with email verification if not ready. |

---

## Cross-cutting — "what breaks" (plan §6)

Several of these fail **silently**. Each needs its own check.

| # | Item | Status | Notes |
|---|---|---|---|
| C1 | `stats()` filters `status='confirmed'` | `[x]` | verify_phase1 27/27 (stats unmoved) |
| C2 | `analytics()` filters `status='confirmed'` | `[x]` | 5 analytics clauses filtered |
| C3 | `attendance_for_day()` filters `status='confirmed'` | `[x]` | verify_phase1 27/27 (day register empty) |
| C4 | CSV export filters `status='confirmed'` | `[x]` | verify_phase1 27/27 (CSV 0 rows) |
| C5 | Dashboard tiles filter `status='confirmed'` | `[x]` | verify_phase1 27/27 |
| C6 | `stats().present_today` → `COUNT(DISTINCT student_id)` | `[x]` | COUNT(DISTINCT student_id) |
| C7 | `load_gallery()` excludes pending accounts | `[x]` | Phase | verify_phase5 21/21 |
| C8 | `users.role` CHECK widened; every `role ==` reviewed | `[x]` | roleLabel/roleShort; nav gated; landing route fixed |
| C9 | Unique constraint swapped | `[x]` | verify_p1_schema 20/20 |
| C10 | Every read handles `session_id IS NULL` (pre-migration rows) | `[x]` | legacy rows still read; suites 16/16 12/12 23/23 |
| C11 | Geo reads prefer `session_captures`, fall back to `attendance` | `[ ]` | |
| C12 | OTP throttled per number **and** per address | `[x]` | Phase | per number AND per address |
| C13 | Self-marking rate-limited per athlete/coach/day | `[ ]` | Phase 3 |
| C14 | `scope_coach` / `scope_self` on every new endpoint | `[x]` | verify_phase1 27/2 | scope_self on every /api/me route |
| C15 | `DELETE /api/students/{id}` widened: account, links, captures | `[ ]` | |
| C16 | `scripts/live_test.py` `Row`-unpack bug fixed | `[ ]` | Blocks §7 measurement |
| C17 | CI green + smoke assertion that drafts never reach `/api/stats` | `[ ]` | |

---

## Threshold work (plan §7) — not required for v1

| # | Item | Status | Notes |
|---|---|---|---|
| T1 | Fix `live_test.py`, re-run `evaluate.py --sweep` | `[ ]` | Same as C16 |
| T2 | Re-measure and pick `MATCH_THRESHOLD` deliberately | `[ ]` | Do **not** just lower it |
| T3 | `COACH_VERIFY_THRESHOLD` chosen (1:1, ~0.363 published) | `[x]` | Phase | 0.363; measured separation 0.80 vs 0.30 |
| T4 | `SELF_VERIFY_THRESHOLD` chosen (1:1) | `[x]` | Phase | 0.363 (SFace 1:1) |

---

## Known gaps in the plan's own references

| Item | Note |
|---|---|
| `DEVELOPMENT.md` | Referenced by the plan; **not present in the repo**. Its traps (`Row` unpacking yields keys, storage keys, startup concurrency, `?`→`%s` dialect) are known from prior work and are respected regardless. |
| `DATA-HANDLING.md` | Referenced by §6 (deletion) and §9 (consent withdrawal); **not present in the repo**. |

---

## Deferred to v2 (plan §9) — deliberately not built

- Coach departure / reassignment flow.
- Cross-coach drafting from one hall photo.
- Session types (morning/evening `slot` column).
- Guardian portal; consent withdrawal; deletion of capture frames and backups.
