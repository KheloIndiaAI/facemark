"""Entry point: python run.py  (starts the API + web UI)

The port comes from PORT so the harness can assign one, matching what the
Dockerfile already does. Hardcoding 8000 meant a second instance died on a
bind error instead of simply starting elsewhere.
"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print()
    print("=" * 62)
    print("  FaceMark - AI Attendance System")
    print("  YuNet detection (MIT) + SFace recognition (Apache-2.0)")
    print(f"  Opening: http://{host}:{port}")
    print("=" * 62)
    uvicorn.run("backend.main:app", host=host, port=port, log_level="info")
