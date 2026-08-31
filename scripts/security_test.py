"""Security, input-validation and concurrency tests against a running server.

Covers the failure modes that matter for a system holding biometric data and
attendance records for minors: authentication boundaries, tenant isolation
between centres, path traversal on the file-serving routes, injection through
search parameters, stored XSS via names, and malformed uploads.

Start the API first, then:

    python -m scripts.security_test --password <admin password>
"""
from __future__ import annotations

import argparse
import concurrent.futures
import io
import os
import sys
import time

os.environ.pop("REQUESTS_CA_BUNDLE", None)
os.environ.pop("CURL_CA_BUNDLE", None)

import requests  # noqa: E402

PASS, FAIL = 0, 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f"   {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}" + (f"   {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("-" * 70)
    print(f"  {title}")
    print("-" * 70)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--password", required=True)
    ap.add_argument("--coach-user", default="coach1")
    ap.add_argument("--coach-pass", default="coachpass1")
    args = ap.parse_args()
    B = args.base.rstrip("/") + "/api"

    print("=" * 70)
    print("  SECURITY AND INPUT VALIDATION")
    print("=" * 70)

    # ---------------------------------------------------------------- auth
    section("Authentication boundary")
    anon = requests.Session()
    for ep in ["/stats", "/students", "/centres", "/people", "/attendance", "/users"]:
        r = anon.get(B + ep)
        check(f"anonymous GET {ep} blocked", r.status_code == 401, f"got {r.status_code}")
    r = anon.post(B + "/attendance/process", files={"photo": ("x.jpg", b"\xff\xd8\xff", "image/jpeg")})
    check("anonymous attendance upload blocked", r.status_code == 401, f"got {r.status_code}")

    check("login rejects wrong password",
          requests.post(B + "/auth/login", data={"username": "admin", "password": "nope"}).status_code == 401)
    check("login rejects unknown user",
          requests.post(B + "/auth/login", data={"username": "nobody", "password": "x"}).status_code == 401)

    r1 = requests.post(B + "/auth/login", data={"username": "admin", "password": "wrong"})
    r2 = requests.post(B + "/auth/login", data={"username": "definitely-not-a-user", "password": "wrong"})
    check("no user enumeration via error text",
          r1.text == r2.text, "identical response for bad password and unknown user")

    admin = requests.Session()
    r = admin.post(B + "/auth/login", data={"username": "admin", "password": args.password})
    check("admin login succeeds", r.status_code == 200, f"got {r.status_code}")
    if r.status_code != 200:
        print("\nCannot continue without an admin session.")
        return 1
    check("session cookie is httpOnly",
          "httponly" in r.headers.get("set-cookie", "").lower())

    # ------------------------------------------------------------- sessions
    section("Session handling")
    tmp = requests.Session()
    tmp.post(B + "/auth/login", data={"username": "admin", "password": args.password})
    check("session works before logout", tmp.get(B + "/stats").status_code == 200)
    tmp.post(B + "/auth/logout")
    check("session revoked after logout", tmp.get(B + "/stats").status_code == 401)

    forged = requests.Session()
    forged.cookies.set("facemark_token", "a" * 43)
    check("forged token rejected", forged.get(B + "/stats").status_code == 401)

    # -------------------------------------------------------- tenant isolation
    section("Centre isolation (coach role)")
    coach = requests.Session()
    rc = coach.post(B + "/auth/login",
                    data={"username": args.coach_user, "password": args.coach_pass})
    if rc.status_code == 200:
        me = coach.get(B + "/auth/me").json()["user"]
        own = me["centre_id"]
        check("coach blocked from user management", coach.get(B + "/users").status_code == 403)
        check("coach blocked from creating centres",
              coach.post(B + "/centres", data={"code": "X", "name": "Y"}).status_code == 403)
        others = [c["id"] for c in admin.get(B + "/centres").json()["centres"] if c["id"] != own]
        if others:
            check("coach blocked from another centre's detail",
                  coach.get(f"{B}/centres/{others[0]}").status_code == 403)
            check("coach cannot widen scope via query param",
                  coach.get(B + "/people", params={"centre_id": others[0]}).status_code == 403)
            check("coach cannot read another centre's attendance",
                  coach.get(B + "/attendance", params={"centre_id": others[0]}).status_code == 403)
        check("coach sees only its own centre",
              coach.get(B + "/centres").json()["count"] == 1)
    else:
        print(f"  SKIP  coach account unavailable ({rc.status_code})")

    # ------------------------------------------------------- path traversal
    section("Path traversal on file routes")
    for probe in ["../../backend/config.py", "..%2f..%2fbackend%2fconfig.py",
                  "....//....//backend//config.py", "/etc/passwd",
                  "..\\..\\backend\\config.py"]:
        for route in ["/photos/", "/uploads/"]:
            r = admin.get(B + route + probe)
            leaked = r.status_code == 200 and (b"import" in r.content[:4000]
                                               or b"root:" in r.content[:4000])
            check(f"traversal blocked {route}{probe[:26]}", not leaked,
                  f"status {r.status_code}")

    # ------------------------------------------------------------- injection
    section("SQL injection through search and filters")
    baseline = admin.get(B + "/centres").json()["count"]
    for payload in ["' OR '1'='1", "'; DROP TABLE centres;--", "%' OR 1=1--",
                    "\" OR \"\"=\"", "1' UNION SELECT null,null--"]:
        r = admin.get(B + "/centres", params={"q": payload})
        ok = r.status_code == 200 and r.json()["count"] <= baseline
        check(f"parameterised: {payload[:24]}", ok,
              f"{r.json().get('count') if r.status_code == 200 else r.status_code} results")
    check("centres table intact after injection attempts",
          admin.get(B + "/centres").json()["count"] == baseline,
          f"{baseline} centres still present")

    # ------------------------------------------------------------------ xss
    section("Stored XSS through names")
    xss = "<img src=x onerror=alert(1)>"
    r = admin.post(B + "/centres", data={"code": f"XSSTEST{int(time.time())}", "name": xss})
    if r.status_code == 200:
        cid = r.json()["centre_id"]
        got = admin.get(f"{B}/centres/{cid}").json()
        check("payload stored verbatim, not executed server-side", got["name"] == xss,
              "frontend escapes via Charts.esc on render")
        admin.delete(f"{B}/centres/{cid}")
        check("test centre cleaned up",
              admin.get(f"{B}/centres/{cid}").status_code == 404)
    else:
        print(f"  SKIP  could not create test centre ({r.status_code})")

    # ---------------------------------------------------------- bad uploads
    section("Malformed and hostile uploads")
    cases = [
        ("empty file", b""),
        ("plain text", b"this is not an image at all"),
        ("html disguised as jpg", b"<html><script>alert(1)</script></html>"),
        ("truncated jpeg header", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
        ("zip bomb header", b"PK\x03\x04" + b"\x00" * 200),
        ("null bytes", b"\x00" * 5000),
    ]
    for name, blob in cases:
        r = admin.post(B + "/attendance/process",
                       files={"photo": ("evil.jpg", blob, "image/jpeg")})
        check(f"rejected cleanly: {name}", r.status_code in (400, 422),
              f"got {r.status_code}")

    # a valid but enormous image
    try:
        import cv2
        import numpy as np
        big = np.random.randint(0, 255, (4000, 6000, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", big, [cv2.IMWRITE_JPEG_QUALITY, 70])
        t = time.time()
        r = admin.post(B + "/attendance/process",
                       files={"photo": ("big.jpg", buf.tobytes(), "image/jpeg")},
                       timeout=400)
        check("24 MP image handled without error", r.status_code == 200,
              f"{r.status_code} in {time.time()-t:.0f}s, {len(buf.tobytes())//1024} KB")
    except Exception as e:  # noqa: BLE001
        check("24 MP image handled without error", False, str(e)[:60])

    # ------------------------------------------------------------ concurrency
    section("Concurrency")
    def hit(_):
        s = requests.Session()
        s.post(B + "/auth/login", data={"username": "admin", "password": args.password})
        return s.get(B + "/stats").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        codes = list(ex.map(hit, range(10)))
    check("10 concurrent logins all succeed", all(c == 200 for c in codes),
          f"codes {sorted(set(codes))}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda _: admin.get(B + "/centres").status_code, range(24)))
    check("24 concurrent reads all succeed", all(c == 200 for c in codes))

    # --------------------------------------------------------------- summary
    print()
    print("=" * 70)
    print(f"  {PASS} passed, {FAIL} failed")
    if FAILURES:
        for f in FAILURES:
            print(f"    - {f}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
