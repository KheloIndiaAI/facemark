"""Fetch model weights at build time.

The weights total about 1 GB and three of them exceed GitHub's 100 MB per-file
limit, so they cannot be committed to the repo. This script pulls each one into
data/models/ during the Docker build instead.

TWO CLASSES OF MODEL
--------------------
PUBLIC   Four models have stable public mirrors whose bytes were verified to
         match this project's known-good local copies exactly. They download
         with no setup.

PRIVATE  adaface_ir101.onnx and gfpgan_v1.4.onnx have no public ONNX mirror at
         the right size - the candidates found were either a different variant
         (89 MB rather than 249 MB) or PyTorch .pth rather than ONNX. Upload
         your existing local copies as assets on a GitHub Release of your own
         repo and point MODEL_ASSET_BASE at it. Release assets allow files up
         to 2 GB, do not count against repo size, and are free.

             gh release create models v1 --title "Model weights" --notes "" \
                 data/models/adaface_ir101.onnx data/models/gfpgan_v1.4.onnx

         then set, in Render's environment:

             MODEL_ASSET_BASE=https://github.com/<user>/<repo>/releases/download/models

BOTH PRIVATE MODELS ARE OPTIONAL. Without them the app still runs: the
recognizer ensemble renormalises its weights over whichever members loaded, and
ID-photo restoration disables itself. Accuracy on degraded ID photos drops,
which is why fetching them is worth the one-time upload.

    python -m scripts.download_models                 # everything available
    python -m scripts.download_models --profile lite  # smallest working set
    python -m scripts.download_models --list          # show plan, download nothing
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get(
    "FACEMARK_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
MODELS_DIR = DATA_DIR / "models"

# Base URL for models served from your own GitHub Release (see module docstring).
ASSET_BASE = os.environ.get("MODEL_ASSET_BASE", "").rstrip("/")

# name -> (url, expected MB, profiles, required)
#
# Sizes below were confirmed against this project's working local copies with a
# ranged GET, so a mirror that silently swapped in a different variant will be
# caught by the size check rather than by a confusing runtime failure.
# Every model here permits commercial use. Sizes are verified on download so a
# mirror that silently serves an HTML error page or a different variant is
# rejected rather than reaching data/models/.
PUBLIC = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        0.23, {"full", "lite"}, True,      # MIT
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx",
        36.9, {"full", "lite"}, True,      # Apache-2.0
    ),
}

# Nothing is served from a private release any more - both models are public and
# permissively licensed.
PRIVATE: dict = {}

FALLBACKS: dict = {}


def human(mb: float) -> str:
    return f"{mb:,.1f} MB"


def _fetch(url: str, dest: Path, expect_mb: float) -> bool:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "facemark-setup"})
        with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("content-length") or 0)
            done = 0
            while chunk := r.read(1024 * 512):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r         {100 * done / total:5.1f}%  "
                          f"{human(done / 1048576)}", end="", flush=True)
            if total:
                print()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"\n         failed: {e}")
        tmp.unlink(missing_ok=True)
        return False

    got = tmp.stat().st_size / 1048576
    # A wrong-sized file is nearly always an HTML error page served as 200, or a
    # different model variant. Either way it must not reach data/models/.
    if got < expect_mb * 0.9 or got > expect_mb * 1.1:
        print(f"         size mismatch: got {human(got)}, expected ~{human(expect_mb)}")
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(dest)
    print(f"         ok ({human(got)})")
    return True


def ensure(name: str, url: str, expect_mb: float, force: bool) -> bool:
    dest = MODELS_DIR / name
    if dest.exists() and not force:
        have = dest.stat().st_size / 1048576
        if have >= expect_mb * 0.9:
            print(f"  [skip] {name}  ({human(have)} already present)")
            return True
        print(f"  [redo] {name}  (short: {human(have)})")

    print(f"  [get ] {name}  ({human(expect_mb)})")
    if _fetch(url, dest, expect_mb):
        return True
    if name in FALLBACKS:
        print(f"         retrying from mirror")
        return _fetch(FALLBACKS[name], dest, expect_mb)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=("full", "lite"), default="full",
                    help="both profiles are the same now: 37 MB total")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any OPTIONAL model is missing too")
    args = ap.parse_args()

    plan = []
    for name, (url, mb, profiles, required) in PUBLIC.items():
        if args.profile in profiles:
            plan.append((name, url, mb, required, "public"))
    for name, (mb, profiles) in PRIVATE.items():
        if args.profile not in profiles:
            continue
        if ASSET_BASE:
            plan.append((name, f"{ASSET_BASE}/{name}", mb, False, "release"))
        else:
            print(f"  [note] {name} skipped - MODEL_ASSET_BASE is not set. "
                  f"See the docstring in this file.")

    total = sum(p[2] for p in plan)
    print(f"\nProfile '{args.profile}': {len(plan)} model(s), about {human(total)}")
    print(f"Target: {MODELS_DIR}\n")
    if args.list:
        for name, url, mb, required, src in plan:
            tag = "required" if required else "optional"
            print(f"  {human(mb):>12}  {name:<22} [{tag}, {src}]")
            print(f"                {url}")
        return 0

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    missing_required, missing_optional = [], []
    for name, url, mb, required, _src in plan:
        if not ensure(name, url, mb, args.force):
            (missing_required if required else missing_optional).append(name)

    print()
    if missing_required:
        print(f"ERROR: required model(s) unavailable: {', '.join(missing_required)}")
        print("The app cannot detect or recognise faces without these.")
        return 1
    if missing_optional:
        print(f"WARNING: optional model(s) unavailable: {', '.join(missing_optional)}")
        print("The app will start. The recognizer ensemble renormalises over the "
              "members that loaded and restoration disables itself, but accuracy "
              "on degraded ID photos will be lower.")
        if args.strict:
            return 1

    have = sorted(p.name for p in MODELS_DIR.glob("*") if p.suffix in (".onnx", ".pt"))
    print(f"Ready: {len(have)} model(s) in {MODELS_DIR}")
    for h in have:
        print(f"  {human((MODELS_DIR / h).stat().st_size / 1048576):>12}  {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
