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
| 5.1 | `users.status` + `approved_by/at`, guardian columns | `[x]` |  |
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
| 6.9 | DLT/SMS registration started (procurement, not code) | `[~]` | Deferred. Phone verification is SWITCHED OFF (`require_phone_otp()`) so signup completes without it; comes back on by itself when a provider is set. Not mine to do — needs someone with authority to register the org. Ship with email verification if not ready. |

---

## Phase 7 — Coach self-registration

**Done when:** a coach can register themselves the way an athlete can, and the
account is inert until a **super admin** — never another coach — approves it.

| # | Item | Status | Notes |
|---|---|---|---|
| 7.1 | `POST /api/signup` accepts `role=coach` | `[x]` | whitelisted to athlete\|coach; super_admin/admin refused 400 |
| 7.2 | Coach application skips the coach picker | `[x]` | server returns `needs_coach:false`; browser goes step 1 -> OTP |
| 7.3 | OTP and face capture identical to the athlete path | `[x]` | same endpoints, same liveness gate; 7 templates stored |
| 7.4 | Pending coach cannot sign in, face not in gallery | `[x]` | login 403; absent from `load_gallery()` |
| 7.5 | Pending coach not offered in the athlete coach picker | `[x]` | `coaches_at()` excludes non-active; posting the id directly refused |
| 7.6 | A coach application never enters a coach's queue | `[x]` | `pending_for_coach` filters `role='athlete'` too |
| 7.7 | A coach cannot approve one by posting the id | `[x]` | 403 "Only a super admin can approve..."; still pending after |
| 7.8 | Super admin approval activates it and links nothing | `[x]` | `linked:false`; nobody's athlete; can open own register |
| 7.9 | Approving a coach needs a typed confirmation | `[x]` | must type APPROVE; 'yes' sent no request (verified in browser) |

Verified by `verify_coach_signup.py` — 41/41, athlete path re-run 26/26.

---

## Phase 8 — Loopholes found after the coach-registration audit

Each was reproduced before it was fixed. Verified by `verify_matchable.py`
(22/22) and `verify_fixes.py` (54/54).

| # | Item | Status | Notes |
|---|---|---|---|
| 8.1 | Rejecting no longer makes a face matchable | `[x]` | exclusion was `status='pending'` only; reproduced rejected -> IN gallery |
| 8.2 | Deactivating an account removes the face too | `[x]` | `MATCHABLE` covers `is_active=0` and any non-active status |
| 8.3 | Deleting an abandoned signup does not re-arm it | `[x]` | state now on `students.status`, so it outlives the account |
| 8.4 | `load_gallery_with_quality` filters too | `[x]` | it had no exclusion at all; unused, so never a live hole |
| 8.5 | Startup re-derives person status from accounts | `[x]` | `_sync_person_status`, only ever moves AWAY from active |
| 8.6 | A pending person cannot be ticked on by hand | `[x]` | `draft()` refuses; admin-sweep roster excludes them |
| 8.7 | A duplicate signup is flagged, not created | `[x]` | face matched against the gallery at signup; `duplicate_of` |
| 8.8 | Approver can merge instead of duplicating | `[x]` | templates move, duplicate row deleted, link on the survivor |
| 8.9 | A shared phone is surfaced, not blocked | `[x]` | families share numbers; shown as a count, no refusal |
| 8.10 | A signup token dies with the decision | `[x]` | reproduced: token worked after approval for the rest of 45 min |
| 8.11 | Rejection is recoverable | `[x]` | `/api/approvals/{id}/reopen`, super admin only |
| 8.12 | The Accounts page shows the decision | `[x]` | `list_users` did not even select `status` |
| 8.13 | A rejected login is told so | `[x]` | was "not active", which reads as "wait" |
| 8.14 | Coach registration needs the centre's code | `[x]` | `centres.coach_join_code`, rotatable, never leaked to a coach |
| 8.15 | Orphaned applicants are flagged and reassignable | `[x]` | chosen coach deleted -> flag, oversight tile, super-admin reassign |
| 8.16 | `RATIO_TEST_THRESHOLD` removed, with evidence | `[x]` | measured 988 assignments: 0 flagged at either polarity; see config.py |

**Not fixed, and still open:** the register roster is driven entirely by
`coach_athletes`, which only self-signup approval writes. On the current data
that is 0 of 34 athletes linked, so a coach opens an empty register. The
link/unlink endpoints exist and have no UI.

---

## Phase 9 — Full-audit remediation

Every item reproduced before it was fixed, and re-tested after.

| # | Item | Status | Notes |
|---|---|---|---|
| 9.1 | An athlete cannot write confirmed attendance | `[x]` | reproduced 13 rows; `process` -> super_admin, `assign` -> staff |
| 9.2 | `mark_attendance` refuses a non-active person | `[x]` | same guard `draft()` had; enforced at the write, not per route |
| 9.3 | 14 more write routes require staff | `[x]` | enrol, delete, edit, sessions, captures, roster links |
| 9.4 | An athlete can do 0 of 8 probed actions | `[x]` | was 4 of 10 |
| 9.5 | Signup survives `--workers 2` | `[x]` | tokens in `signup_tokens`; 4/4 full flows across two workers |
| 9.6 | Per-number OTP limit is shared across workers | `[x]` | counted from `otp_challenges`, not a per-worker dict |
| 9.7 | Coach photos render during signup | `[x]` | inlined thumbnails; the /api/photos URL always 401'd |
| 9.8 | Draft registers expire | `[x]` | `maintenance.expire_drafts`; submitted registers untouched |
| 9.9 | Undecided and refused registrations are forgotten | `[x]` | 30 days, templates included; refuses anyone with attendance |
| 9.10 | Sweeps run without a scheduler | `[x]` | startup, register open, oversight page; 15-min rate limit |
| 9.11 | A coach can fill their own register | `[x]` | GET/PUT `/api/coaches/{id}/roster`; **the pilot blocker** |
| 9.12 | SMS provider, and no false claim of delivery | `[x]` | webhook + DLT fields; `sent:false` when unconfigured |
| 9.13 | Self-service password reset | `[x]` | code to the verified phone; no account enumeration |
| 9.14 | Liveness recalibration documented | `[!]` | still n=2. Cannot improve without pilot clips; procedure written |
| 9.15 | Sample generator stops writing into `data/students/` | `[x]` | writes to `samples/` only |
| 9.16 | `master` cannot be pushed by accident | `[x]` | `.githooks/pre-push`; verified it refuses and lets `dev` through |
| 9.17 | `DEVELOPMENT.md` and `DATA-HANDLING.md` exist | `[x]` | retention, roles, consent, and the traps |
| 9.18 | Built PDF untracked | `[x]` | plus `*.pdf` and `.backup_*/` ignored |

**Verification:** matchable 22/22, maintenance 18/18, roster 19/19, reset 17/17,
multi-worker 22/22, athlete-powers 0 of 8 allowed, earlier fixes 54/54, coach
signup 41/41, athlete signup 27/27, schema 20/20, phase 1 27/27, phase 4 13/13,
phase 5 21/21, API 23/23, and the 220-photo corpus still rejects no real face.

---

## Phase 10 — Phone verification deferred

**Decision:** OTP is off until DLT registration exists. A verification step that
cannot deliver a code is a wall, not a check.

| # | Item | Status | Notes |
|---|---|---|---|
| 10.1 | `config.require_phone_otp()` switches it in one place | `[x]` | follows SMS config; `FACEMARK_REQUIRE_OTP` forces either way |
| 10.2 | Signup completes with no code | `[x]` | verify_phase6 20/20 with it off, 27/27 with it on |
| 10.3 | Asking for a code while off is REFUSED | `[x]` | 400, and no challenge row is created |
| 10.4 | The face step no longer demands a verified phone | `[x]` | that gate refused every signup for an impossible step |
| 10.5 | The browser skips the step because the SERVER said so | `[x]` | `needs_otp` in the start response, not a second copy of the config |
| 10.6 | Password reset unavailable, and says so | `[x]` | 503 identically for every username; no enumeration |
| 10.7 | The sign-in screen offers no link that would fail | `[x]` | `GET /api/config`; "ask your coach or an administrator" instead |
| 10.8 | The phone field stops promising a text | `[x]` | "So your coach can reach you." |
| 10.9 | Docs describe the flow that is running | `[x]` | DEVELOPMENT.md and DATA-HANDLING.md, including what it costs |

**What it costs:** the phone number is an unverified claim. **What it does not
cost:** approval still gates sign-in, gallery membership and every attendance
write — see DATA-HANDLING.md, "What approval is doing".

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
| C7 | `load_gallery()` excludes pending accounts | `[x]` | verify_phase5 21/21 |
| C8 | `users.role` CHECK widened; every `role ==` reviewed | `[x]` | roleLabel/roleShort; nav gated; landing route fixed |
| C9 | Unique constraint swapped | `[x]` | verify_p1_schema 20/20 |
| C10 | Every read handles `session_id IS NULL` (pre-migration rows) | `[x]` | legacy rows keep geo + day-dedup; suites 23/23 16/16 12/12 |
| C11 | Geo reads prefer `session_captures`, fall back to `attendance` | `[x]` | geo denormalised onto the attendance row at draft time, so one read serves both eras (281 legacy rows still carry it) |
| C12 | OTP throttled per number **and** per address | `[x]` | per number AND per address |
| C13 | Self-marking rate-limited per athlete/coach/day | `[x]` | one per athlete/coach/day + failure cooldown |
| C14 | `scope_coach` / `scope_self` on every new endpoint | `[x]` | scope_self on every /api/me route |
| C15 | `DELETE /api/students/{id}` widened: account, links, captures | `[x]` | 6/6: account, links both ways, session detached, other athletes' attendance survives |
| C16 | `scripts/live_test.py` `Row`-unpack bug fixed | `[x]` | Row indexed not unpacked; harness runs, 79 faces |
| C17 | CI green + smoke assertion that drafts never reach `/api/stats` | `[x]` | CI job on a real Postgres; asserts drafts move nothing AND submission promotes |

---

## Threshold work (plan §7) — not required for v1

| # | Item | Status | Notes |
|---|---|---|---|
| T1 | Fix `live_test.py`, re-run `evaluate.py --sweep` | `[x]` | live_test fixed; evaluate.py --sweep re-run |
| T2 | Re-measure and pick `MATCH_THRESHOLD` deliberately | `[x]` | KEEP 0.570 - EER 0.00%, zero-FAR point rejects 0% of genuine. Lowering buys nothing. |
| T3 | `COACH_VERIFY_THRESHOLD` chosen (1:1, ~0.363 published) | `[x]` | 0.363; measured separation 0.80 vs 0.30 |
| T4 | `SELF_VERIFY_THRESHOLD` chosen (1:1) | `[x]` | 0.363 (SFace 1:1) |

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
