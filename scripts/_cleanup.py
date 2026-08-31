"""One-shot dev cleanup: remove smoke-test student, attendance rows and temp files."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import database

database.init_db()
with database.connect() as conn:
    n1 = conn.execute("DELETE FROM students WHERE roll_no = 'SMOKE-TEST'").rowcount
    n2 = conn.execute(
        "DELETE FROM attendance WHERE student_id NOT IN (SELECT id FROM students)"
    ).rowcount
print(f"deleted {n1} smoke-test student(s), {n2} orphan attendance row(s)")

ROOT = Path(__file__).resolve().parent.parent
for f in [
    ROOT / "probe_out.txt",
    ROOT / "server_log.txt",
    ROOT / "server_out.txt",
    ROOT / "smoke_out.txt",
    ROOT / "scripts/_validate_enhancer.py",
    ROOT / "scripts/_validate_age.py",
    ROOT / "scripts/_hf_query.py",
]:
    if f.exists():
        f.unlink()
        print("removed", f.name)
for f in (ROOT / "data/uploads").glob("validate_restored_*.jpg"):
    f.unlink()
    print("removed", f.name)
for f in (ROOT / "data/students").glob("student_*.jpg"):
    if "SMOKE" in f.stem:
        f.unlink()
print("cleanup complete")
