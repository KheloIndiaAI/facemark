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
| 1 | Coach mapping and the session flow | `[ ]` |
| 2 | Submit with coach verification | `[ ]` |
| 3 | Athlete accounts and self-marking | `[ ]` |
| 4 | Super-admin oversight | `[ ]` |
| 5 | Pending accounts and approval | `[ ]` |
| 6 | Self-signup with OTP | `[ ]` |

---

## Phase 1 — Coach mapping and the session flow

**Done when:** a coach captures twice, sees every one of their athletes with the
recognised ones pre-ticked, toggles two by hand, and no row is `confirmed`
anywhere. `/api/stats` is unchanged by the drafts.

### Schema
| # | Item | Status | Notes |
|---|---|---|---|
| 1.1 | `coach_athletes` table + 2 indexes | `[ ]` | |
| 1.2 | `attendance_sessions` table | `[ ]` | |
| 1.3 | `session_captures` table | `[ ]` | |
| 1.4 | `attendance`: `session_id`, `capture_id`, `status`, `origin` | `[ ]` | |
| 1.5 | Swap `UNIQUE(student_id,date)` → `UNIQUE(student_id,session_id)` | `[ ]` | |
| 1.6 | `idx_att_status_date` | `[ ]` | |
| 1.7 | Existing accounts → `super_admin` | `[ ]` | |
| 1.8 | All of it inside the startup advisory lock | `[ ]` | |
| 1.9 | Migration is re-runnable (idempotent) | `[ ]` | |

### Endpoints
| # | Item | Status | Notes |
|---|---|---|---|
| 1.10 | `POST /api/sessions` get-or-create, idempotent | `[ ]` | |
| 1.11 | `GET /api/sessions/{id}` roster + drafts + unknowns + captures | `[ ]` | |
| 1.12 | `POST /api/sessions/{id}/captures` video **or** photo | `[ ]` | |
| 1.13 | Photo capture records `liveness_verdict='not_checked'` | `[ ]` | |
| 1.14 | `PATCH /api/sessions/{id}/roster/{student_id}` toggle | `[ ]` | |
| 1.15 | `GET /api/coaches/{id}/athletes` | `[ ]` | |
| 1.16 | `POST`/`DELETE /api/coaches/{id}/athletes/{athlete_id}` | `[ ]` | |
| 1.17 | Match against the **whole** gallery, route after | `[ ]` | |
| 1.18 | Other coach's athlete: named, **not** drafted | `[ ]` | |
| 1.19 | Several captures accumulate into one session | `[ ]` | |
| 1.20 | `scope_coach` on every session endpoint | `[ ]` | |

### Frontend
| # | Item | Status | Notes |
|---|---|---|---|
| 1.21 | Review screen: full roster, present + absent, toggles | `[ ]` | |
| 1.22 | Recognised rows show crop and score | `[ ]` | |
| 1.23 | "Capture again" adds to the same session | `[ ]` | |

---

## Phase 2 — Submit with coach verification

**Done when:** submitting promotes every draft in one transaction; a failed
verification after three retries still submits, flagged; `/api/stats` moves only
after submission.

| # | Item | Status | Notes |
|---|---|---|---|
| 2.1 | `COACH_VERIFY_THRESHOLD` (not `MATCH_THRESHOLD`) | `[ ]` | |
| 2.2 | `POST /api/sessions/{id}/submit` self-clip | `[ ]` | |
| 2.3 | Liveness + **1:1** match vs the coach's own templates | `[ ]` | |
| 2.4 | Promotion of all drafts in **one transaction** | `[ ]` | |
| 2.5 | Failed verification still submits, recorded unverified | `[ ]` | |
| 2.6 | `submitter_verified/score/liveness` stored | `[ ]` | |
| 2.7 | Session closes on submit | `[ ]` | |

---

## Phase 3 — Athlete accounts and self-marking

**Done when:** an athlete self-marks, a draft appears in the right coach's
session badged `self_marked`, and marking from outside the geofence is accepted
but flagged with the distance.

| # | Item | Status | Notes |
|---|---|---|---|
| 3.1 | `users.role` CHECK widened to include `athlete` | `[ ]` | |
| 3.2 | `scope_self` helper | `[ ]` | |
| 3.3 | Athlete login works; sees only own data | `[ ]` | |
| 3.4 | `GET /api/me/coaches` | `[ ]` | |
| 3.5 | `GET /api/me/attendance` own history | `[ ]` | |
| 3.6 | `POST /api/me/attendance` — liveness **mandatory** | `[ ]` | |
| 3.7 | 1:1 match vs athlete's own templates (`SELF_VERIFY_THRESHOLD`) | `[ ]` | |
| 3.8 | Draft into the coach's session, `origin='self_marked'` | `[ ]` | |
| 3.9 | Creates the session if the coach has not captured yet | `[ ]` | |
| 3.10 | Geo `outside`/`no_fix` accepted but badged with distance | `[ ]` | |
| 3.11 | Rate limit: one accepted per athlete per coach per day | `[ ]` | |
| 3.12 | Cooldown between failures | `[ ]` | |
| 3.13 | Athlete own-history page in the frontend | `[ ]` | |

---

## Phase 4 — Super-admin oversight

**Done when:** an admin can answer "which registers are missing today" in one
screen. Built **before** signup opens.

| # | Item | Status | Notes |
|---|---|---|---|
| 4.1 | `GET /api/admin/overview` | `[ ]` | |
| 4.2 | Sessions submitted today | `[ ]` | |
| 4.3 | Coaches who have **not** submitted | `[ ]` | |
| 4.4 | Unverified submissions | `[ ]` | |
| 4.5 | Photo-only captures | `[ ]` | |
| 4.6 | Drafts about to expire | `[ ]` | |
| 4.7 | Pending approvals count | `[ ]` | |
| 4.8 | Admin dashboard screen | `[ ]` | |

---

## Phase 5 — Pending accounts and approval

**Done when:** a pending account cannot sign in, its templates never match in a
capture, and approving it makes both true in one action.

| # | Item | Status | Notes |
|---|---|---|---|
| 5.1 | `users.status` + `approved_by/at`, guardian columns | `[ ]` | |
| 5.2 | Pending account **cannot sign in** | `[ ]` | |
| 5.3 | **`load_gallery()` excludes pending** (one join) | `[ ]` | |
| 5.4 | `GET /api/approvals` — the coach's own queue | `[ ]` | |
| 5.5 | `POST /api/approvals/{user_id}` approve / reject | `[ ]` | |
| 5.6 | Guardian name + consent timestamp for minors | `[ ]` | |
| 5.7 | Approval creates `coach_athletes` link `is_primary=1` | `[ ]` | |
| 5.8 | Approval puts templates into the gallery in the same action | `[ ]` | |

---

## Phase 6 — Self-signup with OTP

**Done when:** a stranger can complete signup end to end and is inert until a
coach approves them. **Only after Phase 5.**

| # | Item | Status | Notes |
|---|---|---|---|
| 6.1 | `otp_challenges` table + index | `[ ]` | |
| 6.2 | `POST /api/signup` creates `status='pending'` | `[ ]` | |
| 6.3 | Coach selector shows name + enrolment photo | `[ ]` | |
| 6.4 | `POST /api/signup/otp/send` — codes stored **hashed** | `[ ]` | |
| 6.5 | `POST /api/signup/otp/verify` — expiry + attempt counter | `[ ]` | |
| 6.6 | Throttled **per number and per address** | `[ ]` | |
| 6.7 | `POST /api/signup/face` guided capture into pending account | `[ ]` | |
| 6.8 | Signup screens in the frontend | `[ ]` | |
| 6.9 | DLT/SMS registration started (procurement, not code) | `[!]` | Not mine to do — needs someone with authority to register the org. Ship with email verification if not ready. |

---

## Cross-cutting — "what breaks" (plan §6)

Several of these fail **silently**. Each needs its own check.

| # | Item | Status | Notes |
|---|---|---|---|
| C1 | `stats()` filters `status='confirmed'` | `[ ]` | |
| C2 | `analytics()` filters `status='confirmed'` | `[ ]` | |
| C3 | `attendance_for_day()` filters `status='confirmed'` | `[ ]` | |
| C4 | CSV export filters `status='confirmed'` | `[ ]` | |
| C5 | Dashboard tiles filter `status='confirmed'` | `[ ]` | |
| C6 | `stats().present_today` → `COUNT(DISTINCT student_id)` | `[ ]` | |
| C7 | `load_gallery()` excludes pending accounts | `[ ]` | Phase 5 |
| C8 | `users.role` CHECK widened; every `role ==` reviewed | `[ ]` | |
| C9 | Unique constraint swapped | `[ ]` | |
| C10 | Every read handles `session_id IS NULL` (pre-migration rows) | `[ ]` | |
| C11 | Geo reads prefer `session_captures`, fall back to `attendance` | `[ ]` | |
| C12 | OTP throttled per number **and** per address | `[ ]` | Phase 6 |
| C13 | Self-marking rate-limited per athlete/coach/day | `[ ]` | Phase 3 |
| C14 | `scope_coach` / `scope_self` on every new endpoint | `[ ]` | |
| C15 | `DELETE /api/students/{id}` widened: account, links, captures | `[ ]` | |
| C16 | `scripts/live_test.py` `Row`-unpack bug fixed | `[ ]` | Blocks §7 measurement |
| C17 | CI green + smoke assertion that drafts never reach `/api/stats` | `[ ]` | |

---

## Threshold work (plan §7) — not required for v1

| # | Item | Status | Notes |
|---|---|---|---|
| T1 | Fix `live_test.py`, re-run `evaluate.py --sweep` | `[ ]` | Same as C16 |
| T2 | Re-measure and pick `MATCH_THRESHOLD` deliberately | `[ ]` | Do **not** just lower it |
| T3 | `COACH_VERIFY_THRESHOLD` chosen (1:1, ~0.363 published) | `[ ]` | Phase 2 |
| T4 | `SELF_VERIFY_THRESHOLD` chosen (1:1) | `[ ]` | Phase 3 |

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
