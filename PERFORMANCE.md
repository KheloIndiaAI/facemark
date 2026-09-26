# Where the time goes

Investigation into "captures feel slow", 15–16 September 2026, against the
deployed instance at `at.ccki.in` (`t4g.small`, 2 vCPU, 2 GB, `ap-south-1`).

Corrected on 16 September 2026 against production as deployed at `47d4272`
(application code identical to `e2035c8`, the blink release). The first version
described an earlier build: its capture lengths, turn count, upload advice and
the "Speed" check no longer matched the app. Every statement below says how it
was checked.

**Conclusion: the server is not short of capacity, but it is not free either.**
Do not buy a GPU and do not resize the instance. The instance has headroom and
the model inference is fast. What a coach waits for is mostly the network — the
clip going up, and on a new phone the face-tracking files coming down — plus
1–2 seconds of server work per video scan that the app currently does not
report. Section 4 shows how to tell these apart on a real phone.

---

## 1 · What was measured, and how

| Check | Result | How it was checked |
|---|---|---|
| CPU credit balance, 7 days | **576.0 flat**, the maximum for the instance type | CloudWatch, 15–16 Sep. Not re-checked on 16 Sep (no AWS CLI on the checking machine). |
| CPU utilisation, 2 days | **peak 54%** of 2 vCPU | CloudWatch 5-minute maxima, 15–16 Sep. Not re-checked. A 5-minute figure smooths over short per-request spikes, so it shows headroom, not how long one scan takes. |
| Assignment solver | `hungarian (scipy)` | **Verified 16 Sep**: `/api/health`, and the container log of the `47d4272` deploy. |
| Face-tracking files served | All present, `200` | **Verified 16 Sep** by requesting each file from `at.ccki.in`. |
| Upload cap at the proxy | Caddy `max_size 30MB` | **Verified 16 Sep** in the output of deploy run `35062779321`. The app's own cap is `LIVENESS_MAX_BYTES` = 25 MB. |
| Uvicorn workers | `--workers 2` | `Dockerfile`, which production builds from. |
| Deployed front end | Same `app.js` as the repository | **Verified 16 Sep**: the served file matches `frontend/js/app.js` byte for byte once line endings are normalised. |
| Enrolled people in production | 2 | `/api/health`, 16 Sep. Matching against a gallery this size costs nothing. |

### The 20 ms figure, and what it does not cover

The first version measured detect + embed on one group photo at **20 ms**. That is
the `POST /api/attendance/process` group-photo path, and **the current app never
calls it** — no screen in the front end uploads a group photo. Every capture a
coach or athlete makes is a short **video**, and the video path does much more
than detect and embed:

| Flow | Endpoint |
|---|---|
| Take Attendance | `POST /api/attendance/scan` |
| Sign the register | `POST /api/sessions/{id}/submit` |
| Athlete marks self present | `POST /api/me/attendance` |
| Coach registers an athlete | `POST /api/students/register-video` |
| Re-record an athlete | `POST /api/students/{id}/enroll-video` |
| Self-signup face | `POST /api/signup/face` |

For one Take Attendance clip the server decodes the video, runs the liveness
check (parallax, and the blink check where that flow needs it), ranks frames to
pick the best portrait, then detects and recognises. Measured on a development
laptop, not in production:

| Stage | Time |
|---|---|
| Liveness analysis | ~485 ms |
| Portrait ranking over the sampled frames | ~650 ms |
| One face-detection pass on a 960 px frame | ~134 ms — and one scan makes about 10 of them, over the same frames |
| Embedding one face | ~11 ms |
| Matching against the gallery | ~0.1 ms |

So a video scan is **roughly 1–2 seconds of server work** on that laptop, and
likely more on the `t4g.small`'s cores. The time is repeated face detection,
not the recognition model. None of it is reported back to the phone today — see
section 5.

---

## 2 · Why a GPU is the wrong purchase

Three independent reasons, the first of which is on its own fatal.

**It would not run.** `opencv-python-headless` from PyPI has no CUDA support.
YuNet and SFace both go through OpenCV's DNN module, and using a GPU there means
compiling OpenCV from source with CUDA and cuDNN — hours of build, pinned to a
CUDA version, and ours to maintain forever. Nothing in `pip install` gets there.

**It would sit idle.** `FaceDetector._lock` is held across `detect()`, and
`SFaceRecognizer._lock` across the embed loop. Detection and embedding are
serialised within each worker process, so with `--workers 2` there are at most
two concurrent inferences whatever hardware is underneath.

**It is not where the time goes.** The heavier work on the video path — decode,
optical flow, the blink landmark model, repeated detection — does not run on a
GPU through these bindings.

A `g4dn.xlarge` is roughly $380/month against $8, and it is x86, so the image
would need rebuilding too. The original bootstrap script,
`deploy/aws/user-data.sh`, records the same conclusion. (That script predates
the current setup: production now runs Docker Compose behind Caddy from
`/opt/attendence-application`, deployed by `.github/workflows/deploy.yml`.)

**Resizing is also wrong, for now.** If concurrency ever becomes the problem —
several coaches capturing at once — the ceiling is the lock plus `--workers`,
and the answer then is `c7g.xlarge` (4 vCPU, not burstable, still ARM, no image
rebuild) with a matching worker count. Before that, the cheaper server-side win
is not detecting the same frames ten times over.

---

## 3 · The three things a person waits for

### A. First load of the face-tracking files

The first time a phone opens any capture screen it downloads the MediaPipe
runtime and face model from `/vendor/mediapipe/`. The service worker precaches
the app shell but **not `/vendor/`**, so they arrive at that moment.

Measured from production on 16 Sep, with compression as a browser receives it:

| File | On disk | Sent over the network |
|---|---|---|
| `wasm/vision_wasm_internal.wasm` | 11.8 MB | **3.6 MB** (gzip) |
| `face_landmarker.task` | 3.8 MB | **3.8 MB** (already compressed) |
| `wasm/vision_wasm_internal.js` | 323 KB | 82 KB |
| `vision_bundle.mjs` | 155 KB | 47 KB |
| **Total a phone downloads** | 16 MB | **about 7.5 MB** |

(`vision_wasm_nosimd_internal.*` is only fetched by a browser without SIMD
support, instead of the files above, never as well.)

That is about **30 s at 2 Mbps, 12 s at 5 Mbps, 6 s at 10 Mbps** — once per
device. The files are served with an `ETag` and `Last-Modified` and no
`Cache-Control`, so later visits use the browser's cache and at most make a
cheap revalidation request.

*Signature:* multi-megabyte `/vendor/mediapipe/*` requests at the start of the
flow, on a fresh device or in a private window.

*Fix:* start the download at sign-in, or add the files to the service worker
precache, so they are not fetched at the moment somebody taps a camera button;
and show a "Preparing camera…" state until they are ready.

### B. Uploading the clip

Every clip is recorded at **4 Mbps** (`videoBitsPerSecond: 4000000`,
`frontend/js/app.js`) from a camera requested at 1280×960. That bitrate is a
**deliberate choice**, not an oversight: the comment beside it records that,
left to the phone's own default, the encoder dropped quality indoors while the
head moved, exactly when the server has to find the face.

How long clips are, in the current app:

| Flow | Length | Size at 4 Mbps |
|---|---|---|
| Take Attendance, sign register, mark self present | Ends 0.5 s after the blink; at most 8 s | typically 1–2 MB, at most ~4.5 MB |
| Registering a face (guided) | Ends when both head turns are confirmed; at most 40 s | typically a few MB, at most ~20 MB |

Upload time at a phone's uplink:

| Clip | 2 Mbps | 5 Mbps | 10 Mbps |
|---|---|---|---|
| 2 MB | 8 s | 3 s | 2 s |
| 10 MB | 40 s | 16 s | 8 s |
| 20 MB | 80 s | 32 s | 16 s |

*Signature:* one long request to one of the endpoints in section 1, with most
of its time in **Request sent**, not **Waiting**.

*Fix, only after measuring:* a lower bitrate would shorten uploads, but two
things depend on detail in the clip — the face being found at all (the reason
4 Mbps was chosen) and the check for a face shown on a screen, which looks for
fine patterns that heavy compression removes. Any reduction has to be tested
against real indoor phone clips and the liveness test clips first. Do not pick
a number such as 800 kbps without that test.

### C. The guided registration sequence

Registering a face is prompt-driven: hold still, then **two** head turns (to one
side, then the other), each confirmed on the phone by its own face tracking.
Before recording starts, the phone polls `/api/enroll/pose-check` to check the
framing. **Ten seconds or so here is the design, not a fault.** If this is the
complaint, the fix is expectation — a step count and clear prompts — rather
than speed.

Take Attendance is not like this: it has no turns and stops half a second after
the blink.

---

## 4 · How to tell which, in one capture

Open the app on a laptop, DevTools → Network, throttle to **Fast 3G**, run the
flow that feels slow, and click the upload request (section 1 lists which one).
In its **Timing** tab:

| What you see | It is |
|---|---|
| Multi-MB `/vendor/mediapipe/*` requests at the start | **A** — first load |
| Most of the time in *Request sent* | **B** — upload |
| Most of the time in *Waiting (TTFB)* | **Server work** — section 1 |
| Many small `pose-check` calls before recording starts | Framing for **C** — working as designed |

The app has no on-screen "Speed" figure for these flows. The group-photo
endpoint returns `timings.total_ms`, but nothing in the current app calls it,
and the video endpoints return no timings at all. Until section 5 is done,
*Waiting* in DevTools is the server's time.

Do it **on mobile data rather than wifi**, because that is the condition the
centres are actually in — ideally on a real phone with remote debugging, since
a throttled laptop does not reproduce a phone's encoder or its camera.

---

## 5 · Instrumentation worth adding

The `timings` block exists only on the unused group-photo endpoint. The video
endpoints that every capture uses report nothing. Returning, from each of them:

- `decode_ms` — around `sample_frames()`
- `liveness_ms` — around `liveness.analyse()`, including the blink check
- `recognise_ms` — portrait ranking plus detection and matching
- `total_ms`

would make the next investigation a minute rather than a day, and would put the
server's share of every wait in the browser's own network log.

One cheap win if the video path does need speeding up: `sample_frames()` calls
`cap.read()` on every frame (`backend/liveness.py`), which demuxes *and* decodes
to BGR even for frames it then skips. Skipped frames only need `cap.grab()`;
only kept frames need `cap.retrieve()`. Worth measuring before doing.

---

## 6 · Two unrelated findings from the same session

**`scipy` was missing from a local venv**, which made the startup log say
`Assignment solver: GREEDY FALLBACK - scipy missing, results differ from
benchmarks`. Production is correct (verified above). If you see that line
locally, `pip install -r requirements.txt` — the greedy fallback is not the
Hungarian algorithm and does not produce the same assignments the accuracy
figures were measured on.

**`benchmark_detection.py` cannot run on a clean clone.** Its ground truth,
`data/eval_labels.json`, points at `data/uploads/*.jpg`, which is gitignored for
good reason — those are photographs of children. The harness ships a manifest
and no data. It also measures the group-photo path, which the app no longer
uses (section 1), so its numbers say little about capture speed.
