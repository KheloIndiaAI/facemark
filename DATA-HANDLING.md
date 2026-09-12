# Data handling

FaceMark holds biometric data about children. Almost everyone in the system is
an athlete at a Khelo India centre, and most of them are minors. This document
says what is stored, where, who can reach it, and when it is destroyed — so that
those are decisions somebody made rather than accidents of implementation.

If you change any of it, change this file in the same commit.

---

## What is stored

| Data | Where | Why it exists |
|---|---|---|
| Face templates — 128 floats per image, SFace | `templates` table, Postgres | The only thing recognition compares against. Not reversible into a photograph, but still biometric data about an identified person. |
| Enrolment photographs | `data/students/` (or S3 `students/`) | Shown to a coach so they can confirm the system means the right person. |
| Group captures and face crops | `data/uploads/` (or S3 `uploads/`) | Evidence for a register: it must be possible to see why somebody was marked present. |
| Name, NSRS ID, gender, sport, centre | `students` table | The roster. |
| Phone number | `students.phone`, `users.phone` | **No longer collected at signup.** The column remains, and an admin enrolling somebody by hand can still fill it. Never verified, so where it exists it is a claim, not identity. |
| Guardian name and consent timestamp | `users.guardian_name`, `guardian_consent_at` | Recorded by the coach at approval, for athletes under 18. |
| Attendance rows | `attendance` table | The record the centre exists to produce. |
| Account credentials | `users.password_hash` | PBKDF2-HMAC-SHA256, 600k iterations, per-user salt. No plaintext is ever written. |

**Not stored:** raw images inside the database, plaintext passwords, or
location history beyond the single geo fix attached to a capture. One-time
codes are not stored at all any more — phone verification was removed and the
`otp_challenges` table with it.

---

## Who can reach it

Enforced server-side, on every request. Hiding a button is not access control.

- **Athlete** — their own attendance and their own record, through `/api/me/*`.
  Nothing else. They cannot enrol anybody, delete anybody, open a register,
  capture a group, or read any other person's record, photograph or attendance.

  One exception, stated because it is a real one: an athlete may mark
  **themselves** present, through `POST /api/me/attendance`, under their own
  face and against a coach who already coaches them. That writes a DRAFT into
  that coach's register, never confirmed attendance — the coach still reviews
  and signs it. This section previously said they could not write attendance
  "for any person including themselves", which was not true of that route.
- **Coach** — one centre. Every query is narrowed to their `centre_id`, and
  register actions to their own `coach_id`. They approve the athletes who chose
  them, and nobody else.
- **Super admin** — every centre, account management, centre management, and
  approval of coach registrations.

Photographs (`/api/photos/*`) and captures (`/api/uploads/*`) require a session.
There is no unauthenticated route that returns an image of a person. The one
place a coach's photograph appears before sign-in — the coach picker during
athlete signup — is a downscaled thumbnail inlined into a response that is
itself gated by a signup token.

---

## Consent

Guardian consent is recorded at **approval**, not at signup: the coach is the
person who knows whether an athlete is a minor, and they are asked for the
guardian's name when they approve the account. It is stored on the account
(`guardian_name`, `guardian_consent_at`).

This is a record that consent was obtained, not a substitute for obtaining it.
Consent for a minor's biometric data is collected by the centre, in whatever
form the programme requires; the app records that it happened and when.

---

## What approval is doing

There is no phone verification, so **approval is the only thing standing
between a stranger and the register.** It is worth being explicit about what it covers:

- A pending account cannot sign in.
- Its face is excluded from the recognition gallery, so it cannot be recognised
  in any capture.
- Attendance cannot be written for it by any route — both writes refuse a
  non-active person, so it cannot be ticked present by hand either.
- An athlete application reaches only the coach it chose; a coach application
  reaches only a super admin.

So a coach approving somebody should be looking at the face on the screen and
the person in front of them. There is no phone number and no centre code to lean on — the face, and knowing who is supposed to be there, is the check.

---

## Retention

Nothing used to be deleted, ever. That is now bounded.

| What | Kept for | Set by |
|---|---|---|
| A registration nobody decided | 30 days from signup | `FACEMARK_PENDING_TTL_DAYS` |
| A registration that was refused | 30 days from the decision | `FACEMARK_REJECTED_TTL_DAYS` |
| Registers nobody submitted | 18 hours, then expired and their drafts deleted | `sessions.SESSION_TTL_HOURS` |
| Enrolled people and their attendance | Indefinitely, until deleted by an admin | — |

Purging a registration deletes the **person, their face templates, their
stored photographs and their account together**.

The photographs are recent: the sweep used to delete the rows and leave every
image on disk — the enrolment frames, the signup capture, anything added later
— which is the opposite of what a retention limit is for. Deleting a person
through `DELETE /api/students/{id}` removes their images too, and for the same
reason. Half a deletion is worse than none: a `students` row with no
account is a name nobody can explain, and templates with no person are a face
the gallery cannot name.

The sweep is deliberately narrow. It never touches somebody an admin enrolled,
and it refuses to delete anybody who has attendance against their name — if an
unapproved person somehow acquired attendance, that is evidence of a bug and
destroying it would destroy the evidence. It logs and skips instead.

Sweeps run on startup, when a register is opened, and when the oversight page is
loaded, rate-limited to once every 15 minutes per worker. There is no scheduler
to forget to deploy.

**Deleting a person** (`DELETE /api/students/{id}`, staff only) removes their
templates, attendance, coach links and account. Registers they opened as a coach
survive with `coach_id` set to NULL — other athletes' attendance must not be
lost because their coach left.

---

## Who is matchable

A face enters the recognition gallery only while **both** are true:

- the person's `students.status` is `active`, and
- no account attached to them is pending, rejected, suspended or deactivated.

The person half lives on the `students` row so that it survives the account
being deleted. Keying it only on the account meant that deleting an abandoned
signup silently re-armed its face.

The same rule is enforced again at both writes — `sessions.draft()` and
`database.mark_attendance()` — so a person who should not be matchable also
cannot be ticked present by hand.

---

## Rules for anyone working on this

1. **Never commit face data.** `data/testset/`, `data/roster_import/` and
   `*.pdf` are gitignored. Roster PDFs and their extracted crops carry real
   names, NSRS IDs and photographs.
2. **Never push `master`.** Its history contains `data/roster_import/` at commit
   `687e494`. `dev` and `main` are clean and verified so. A `pre-push` hook in
   `.githooks/` refuses it; install with
   `git config core.hooksPath .githooks`.
3. **Never put a code, token or password in a log line** once a delivery
   provider is configured. Phone numbers in logs are redacted to the last four
   digits.
4. **Never add an unauthenticated route that returns an image of a person.**
5. **A centre scope is not a role check.** An athlete has a `centre_id` too.
   Guard writes with `require_staff` or `require_super_admin`, then narrow by
   centre.
6. **`.env` is never committed.** Only `.env.example`, with empty values.

---

## Deployment

- Postgres holds everything except image files. Back it up: there is no other
  copy of the templates, and losing them means re-enrolling every athlete in
  person.
- On AWS, `DATABASE_URL` and the admin password come from SSM Parameter Store,
  never from `user-data.sh`.
- S3 storage uses an IAM instance role. `S3_ACCESS_KEY_ID` stays empty — there
  are no long-lived keys to leak.
