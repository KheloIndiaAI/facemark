"""Fetch the browser-side MediaPipe runtime and face model.

    python -m scripts.fetch_frontend_models
    python -m scripts.fetch_frontend_models --list     # show plan, download nothing

WHY THESE ARE NOT COMMITTED
---------------------------
The WASM alone is 23 MB across two builds, and the model another 3.8 MB. This
repository is under 1 MB; committing them would make it roughly 28x larger
permanently, because git keeps the blobs even after a later deletion.

So they are fetched, exactly as data/models/ already is - see
scripts/download_models.py and the .gitignore note beside it - and pulled during
the Docker build. The KIRTI analyzer this mirrors does the same thing: its WASM
comes from a postinstall copy and its models from a fetch script, neither hand
committed.

WHY THE FRONTEND NEEDS A MODEL AT ALL
-------------------------------------
The enrolment overlay used to poll /api/enroll/pose-check for a face box, which
is a server round trip: five landmarks about three times a second, so the dots
stepped rather than tracked. MediaPipe's Face Landmarker runs in the browser at
video rate and returns 478 points, which is what makes the overlay feel live
rather than sampled. It is also the same library the KIRTI analyzer already
uses, so the two apps behave alike.

BOTH WASM BUILDS ARE FETCHED, and that is deliberate. MediaPipe picks the SIMD
build where the browser supports it and the nosimd build where it does not;
shipping only the first leaves older and low-end Android phones with no overlay
at all, and those are exactly the devices a sports centre has.

If the network is unavailable at build time the app still runs - the overlay
falls back to the server poll it used before, so enrolment degrades to stepped
dots rather than breaking.
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "frontend" / "vendor" / "mediapipe"

# Pinned. An unpinned "latest" would change the model and the runtime under a
# deployment without anything in the repo recording that it moved. 1.0.1 is the
# current release and the version the KIRTI analyzer already runs, so the two
# apps use the same runtime.
TASKS_VISION_VERSION = "1.0.1"
CDN = f"https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{TASKS_VISION_VERSION}"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")

# (destination name, url, expected MB). The size check is the same guard
# download_models.py uses: a mirror serving an error page is rejected rather
# than landing in vendor/ as a file that fails at runtime.
FILES = [
    ("vision_bundle.mjs",              f"{CDN}/vision_bundle.mjs",                  0.15),
    ("wasm/vision_wasm_internal.js",   f"{CDN}/wasm/vision_wasm_internal.js",       0.31),
    ("wasm/vision_wasm_internal.wasm", f"{CDN}/wasm/vision_wasm_internal.wasm",    12.0),
    ("wasm/vision_wasm_nosimd_internal.js",
     f"{CDN}/wasm/vision_wasm_nosimd_internal.js",                                  0.31),
    ("wasm/vision_wasm_nosimd_internal.wasm",
     f"{CDN}/wasm/vision_wasm_nosimd_internal.wasm",                               11.0),
    ("face_landmarker.task",           MODEL_URL,                                   3.8),
]

TOLERANCE = 0.45          # generous: CDN builds drift a little between patches


def human(mb: float) -> str:
    return f"{mb:,.1f} MB"


def _fetch(url: str, dest: Path, expect_mb: float) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "facemark/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        tmp.unlink(missing_ok=True)
        print(f"  ! {dest.name}: {e}")
        return False

    got_mb = tmp.stat().st_size / 1e6
    if expect_mb and abs(got_mb - expect_mb) > expect_mb * TOLERANCE:
        # Almost always an HTML error page saved with a .wasm name.
        print(f"  ! {dest.name}: got {human(got_mb)}, expected about "
              f"{human(expect_mb)} - refusing it")
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(dest)
    print(f"  + {dest.name}  {human(got_mb)}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show the plan, download nothing")
    ap.add_argument("--force", action="store_true", help="re-download files already present")
    args = ap.parse_args()

    print(f"MediaPipe tasks-vision {TASKS_VISION_VERSION} -> {VENDOR}")
    total = sum(mb for _, _, mb in FILES)
    if args.list:
        for name, url, mb in FILES:
            here = "present" if (VENDOR / name).exists() else "missing"
            print(f"  {name:<40} {human(mb):>10}  {here}")
        print(f"  {'total':<40} {human(total):>10}")
        return 0

    ok = failed = skipped = 0
    for name, url, mb in FILES:
        dest = VENDOR / name
        if dest.exists() and not args.force:
            print(f"  = {name} already present")
            skipped += 1
            continue
        if _fetch(url, dest, mb):
            ok += 1
        else:
            failed += 1

    print(f"\n{ok} downloaded, {skipped} already present, {failed} failed")
    if failed:
        # Not a build failure. The overlay falls back to the server poll, so a
        # blocked CDN degrades enrolment rather than breaking the image.
        print("The face overlay will fall back to server-side detection.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
