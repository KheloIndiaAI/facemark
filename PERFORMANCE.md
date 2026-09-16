# Where the time goes

Investigation into "captures feel slow", 15–16 September 2026, against the
deployed instance at `at.ccki.in` (`t4g.small`, 2 vCPU, 2 GB, `ap-south-1`).

**Conclusion: it is not the server.** Do not buy a GPU and do not resize the
instance. Every server-side hypothesis was measured and eliminated. What is left
is client-side, and this document says how to tell the three remaining
candidates apart in one look at DevTools.

---

## 1 · What was measured

| Check | Result | Verdict |
|---|---|---|
| CPU credit balance, 7 days | **576.0 flat** — the maximum for this instance type, never drawn down | Not throttled. The burstable ceiling was never approached. |
| CPU utilisation, 2 days | **peak 54%** of 2 vCPU, 5-minute maxima | Roughly half the machine idle at the busiest recorded moment. |
| Detect + embed, one group photo | **20 ms** (detect 11.6 ms, embed 8.3 ms) | The recognition pipeline is not what anyone is waiting for. |
| Assignment solver in production | `hungarian (scipy)` | Correct. Not the greedy fallback. |
| On-device face mesh on the server | `frontend/vendor/mediapipe/` fully populated | Present, so the enrolment overlay is not silently falling back to server-side detection. |

Reproduce the latency number:

```bash
python -m scripts.benchmark_detection --repeat 5
```

That needs a photo path in `data/eval_labels.json` that actually exists —
`data/uploads/` is gitignored, so a clean clone has no input. Pull one:

```bash
source ~/.attendence-env
aws s3 ls "s3://${BUCKET}/uploads/" | grep group_ | tail -3
aws s3 cp "s3://${BUCKET}/uploads/<one of those>" data/uploads/
```

Check the two production lines:

```bash
cd /opt/attendence-application
docker compose logs app | grep -i "Assignment solver"
docker compose exec app ls -la /app/frontend/vendor/mediapipe/
```

---

## 2 · Why a GPU is the wrong purchase

Three independent reasons, the first of which is on its own fatal.

**It would not run.** `opencv-python-headless` from PyPI has no CUDA support.
YuNet and SFace both go through OpenCV's DNN module, and using a GPU there means
compiling OpenCV from source with CUDA and cuDNN — hours of build, pinned to a
CUDA version, and ours to maintain forever. Nothing in `pip install` gets there.

**It would sit idle.** `FaceDetector._lock` is held across `det.detect()`, and
`SFaceRecognizer._lock` across the embed loop. Detection and embedding are
serialised within each worker process, so with `--workers 2` there are exactly
two concurrent inferences whatever hardware is underneath.

**It is not where the time goes.** The models are 227 KB and 37 MB. The heavier
work in the video path — decode, forward and backward optical flow, Laplacian
variance, the tflite landmark passes in `blink.py` — is not GPU-accelerated
through these bindings.

A `g4dn.xlarge` is roughly $380/month against $8, and it is x86, so the image
would need rebuilding too. `deploy/aws/user-data.sh` already records the same
conclusion for the same reasons.

**Resizing is also wrong, for now.** Peak 54% of two cores means there is
headroom, not a shortage. If concurrency ever becomes the problem — several
coaches capturing at once — the ceiling is the mutex plus `--workers`, and the
answer then is `c7g.xlarge` (4 vCPU, not burstable, still ARM, no image rebuild)
with a matching worker count. Not today.

---

## 3 · The three remaining candidates

All client-side. Each has a distinct signature in DevTools.

### A. First load of the face mesh

`frontend/vendor/mediapipe/` is about **16 MB** that a phone downloads the first
time it opens any capture screen — the WASM runtime plus the 3.8 MB
`face_landmarker.task`. The service worker precaches the app shell but **not
`/vendor/`**, so it arrives on demand.

Over mobile data that is 25–60 seconds, once per device, exactly when somebody
taps Register. Second time it is cached and instant.

*Signature:* a cluster of multi-megabyte `/vendor/mediapipe/*` requests at the
start of the flow, on a fresh device or in a private window.

*Fix:* add them to the service worker precache so they arrive with the app
rather than at the worst moment; or accept the one-off cost and show a
"preparing camera" state instead of an apparently frozen screen.

### B. Clip upload

`LIVENESS_MAX_BYTES` is 25 MB and the proxy cap was raised from 12 MB to 30 MB,
so clips land in the 10–30 MB range. At a phone's uplink:

| Clip | 2 Mbps | 5 Mbps | 10 Mbps |
|---|---|---|---|
| 10 MB | 40 s | 16 s | 8 s |
| 25 MB | 100 s | 40 s | 20 s |

*Signature:* one long `process-video` or `register-video` request with most of
the time in **Request sent**, not in **Waiting**.

*Fix, in the browser, not on AWS:*

- Set `videoBitsPerSecond` on the `MediaRecorder`. It is likely unset, so the
  browser picks a generous default. 800 kbps is ample.
- Constrain `getUserMedia` to 720p. Liveness measures parallax in *face-widths*
  and is therefore scale-invariant — 1080p costs triple the bytes and buys
  nothing.
- Revisit the 45-second enrolment ceiling, which at a high bitrate is where the
  25 MB clips come from.

A 2-second 720p clip at 800 kbps is about 200 KB.

### C. The guided sequence itself

Registration is prompt-driven: a 3-second floor, then four head turns each
verified by `/api/enroll/pose-check` before advancing. **10–20 seconds there is
the design, not a fault.** If this is the complaint, the fix is expectation —
progress, a step count, telling people what is happening — rather than speed.

Attendance capture is a fixed 2 seconds, so a long wait there is never this.

---

## 4 · How to tell which, in one capture

Open the app on a laptop, DevTools → Network, throttle to **Fast 3G**, run the
flow that feels slow. Then:

| What you see | It is |
|---|---|
| Multi-MB `/vendor/mediapipe/*` at the start | **A** — first load |
| One request, most time in *Request sent* | **B** — upload |
| Many small `pose-check` calls spread over 15 s with gaps | **C** — working as designed |

Also compare the wall-clock wait against the **Speed** tile on the result
screen. That tile is `timings.total_ms`, the server's own processing time. If
the wait is 20 seconds and Speed says 800 ms, 19 of those seconds are network.

Do it **on mobile data rather than wifi**, because that is the condition the
centres are actually in.

---

## 5 · Instrumentation worth adding

The `timings` block covers detect, embed, match and annotate. It does **not**
cover liveness or blink, which are the largest server-side costs on the video
path. Two more fields would make the next investigation a minute rather than a
day:

- `liveness_ms` — around `liveness.analyse()`
- `decode_ms` — around `sample_frames()`

And one cheap win if the enrolment path ever does need speeding up:
`sample_frames()` calls `cap.read()` on every frame, which demuxes *and* decodes
to BGR. Frames being discarded only need `cap.grab()`; only kept frames need
`cap.retrieve()`. On a 45-second clip that is most of the decode cost. Worth
measuring before doing, now that we know how to measure.

---

## 6 · Two unrelated findings from the same session

**`scipy` was missing from a local venv**, which made the startup log say
`Assignment solver: GREEDY FALLBACK - scipy missing, results differ from
benchmarks`. Production is correct. If you see that line locally,
`pip install -r requirements.txt` — the greedy fallback is not the Hungarian
algorithm and does not produce the same assignments the accuracy figures were
measured on. The warning itself is a good addition; it made a silent divergence
visible.

**`benchmark_detection.py` cannot run on a clean clone.** Its ground truth,
`data/eval_labels.json`, points at `data/uploads/*.jpg`, which is gitignored for
good reason — those are photographs of children. The harness ships a manifest
and no data. Worth either committing a synthetic group photo for it, or having
it fail with a message that says where to get one.
