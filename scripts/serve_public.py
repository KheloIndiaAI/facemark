"""Run FaceMark and expose it on the public internet through a Cloudflare Tunnel.

Starts the API, opens a tunnel, prints the public URL, and optionally rewrites
vercel.json so the Vercel frontend proxies to this run's tunnel.

    python -m scripts.serve_public                 # tunnel only
    python -m scripts.serve_public --update-vercel # also patch vercel.json
    python -m scripts.serve_public --deploy        # patch and redeploy to Vercel

WHY THE URL CHANGES
-------------------
A Cloudflare *quick* tunnel needs no account and no domain, which is what makes
it free, but it issues a fresh random hostname on every start. Cloudflare's
*named* tunnels keep a fixed hostname, but they require a domain on your
Cloudflare account. --deploy exists so the changing hostname costs one command
rather than a manual edit.

Ctrl-C stops both processes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# cloudflared installs outside PATH for the current shell on Windows.
CLOUDFLARED_HINTS = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
]


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for p in CLOUDFLARED_HINTS:
        if Path(p).exists():
            return p
    return None


def wait_for_api(port: int, timeout: int = 240) -> bool:
    """Block until the API answers. Model loading takes 20-40 s from cold."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=3
            ) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(2)
    return False


def update_vercel(url: str) -> bool:
    cfg_path = ROOT / "vercel.json"
    if not cfg_path.exists():
        print("  vercel.json not found - skipping")
        return False
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for rw in cfg.get("rewrites", []):
        if rw.get("source", "").startswith("/api/"):
            rw["destination"] = f"{url}/api/:path*"
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            print(f"  vercel.json -> {url}/api/:path*")
            return True
    print("  no /api rewrite found in vercel.json")
    return False


def deploy_vercel() -> None:
    vercel = shutil.which("vercel") or shutil.which("vercel.cmd")
    if not vercel:
        print("  vercel CLI not found. Install with:  npm i -g vercel")
        print("  Then run:  vercel --prod")
        return
    print("  deploying to Vercel...")
    subprocess.run([vercel, "--prod", "--yes"], cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--update-vercel", action="store_true",
                    help="rewrite vercel.json with this run's tunnel URL")
    ap.add_argument("--deploy", action="store_true",
                    help="rewrite vercel.json and redeploy to Vercel")
    ap.add_argument("--no-server", action="store_true",
                    help="tunnel only; assumes the API is already running")
    args = ap.parse_args()

    cloudflared = find_cloudflared()
    if not cloudflared:
        print("cloudflared is not installed. Install it with:")
        print("    winget install --id Cloudflare.cloudflared")
        return 1

    procs = []
    try:
        if not args.no_server:
            print(f"Starting FaceMark on port {args.port} ...")
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.main:app",
                 "--host", "127.0.0.1", "--port", str(args.port)],
                cwd=ROOT,
            ))

        print("Waiting for the API (models take 20-40 s to load) ...")
        if not wait_for_api(args.port):
            print("API did not come up in time.")
            return 1
        print("API is up.\n")

        print("Opening Cloudflare Tunnel ...")
        tunnel = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://localhost:{args.port}",
             "--no-autoupdate"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        procs.append(tunnel)

        public_url = None
        deadline = time.time() + 60
        while time.time() < deadline and public_url is None:
            line = tunnel.stdout.readline()
            if not line:
                if tunnel.poll() is not None:
                    break
                continue
            m = URL_RE.search(line)
            if m:
                public_url = m.group(0)

        if not public_url:
            print("Could not obtain a tunnel URL.")
            return 1

        # Keep draining the tunnel's output or its pipe buffer fills and blocks it.
        threading.Thread(
            target=lambda: [None for _ in tunnel.stdout], daemon=True
        ).start()

        bar = "=" * 64
        print(f"\n{bar}")
        print(f"  PUBLIC URL   {public_url}")
        print(f"  Local        http://127.0.0.1:{args.port}")
        print(f"{bar}\n")
        print("  The full app - frontend and API - is reachable at the public URL.")
        print("  It stays up while this window is open and this PC is awake.\n")

        if args.update_vercel or args.deploy:
            update_vercel(public_url)
            if args.deploy:
                deploy_vercel()
            print()

        print("Ctrl-C to stop.\n")
        while True:
            time.sleep(1)
            for p in procs:
                if p.poll() is not None:
                    print("A child process exited; shutting down.")
                    return 1

    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    finally:
        for p in procs:
            try:
                if p.poll() is None:
                    p.send_signal(signal.SIGTERM)
                    try:
                        p.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        p.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
