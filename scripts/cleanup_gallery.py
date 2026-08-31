"""Repair the gallery: drop poisoned adapted templates and never-enrolled students.

Two kinds of junk accumulate in attendance.db and both degrade matching:

1. `adapted` templates written by continual learning under the old, loose gate
   (confidence only, no margin, faces as small as 35px). Measured on the sample
   data they raised impostor similarity for every student - one stranger scored
   0.53 against an enrolled student with them in place, 0.45 without - and they
   flipped two top-1 matches to the wrong person.
2. Students with zero templates (test rows whose enrollment photos are gone).
   They can never be matched, so they only distort the roster and the stats.

Run with --apply to actually write; the default is a dry run.

    python -m scripts.cleanup_gallery              # report only
    python -m scripts.cleanup_gallery --apply
    python -m scripts.cleanup_gallery --apply --keep-ghosts
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--keep-adapted", action="store_true", help="do not drop adapted templates")
    ap.add_argument("--keep-ghosts", action="store_true", help="do not drop template-less students")
    args = ap.parse_args()

    with database.connect() as conn:
        adapted = conn.execute(
            "SELECT COUNT(*) FROM templates WHERE source = 'adapted'"
        ).fetchone()[0]
        ghosts = conn.execute(
            "SELECT id, name, roll_no FROM students s "
            "WHERE NOT EXISTS (SELECT 1 FROM templates t WHERE t.student_id = s.id)"
        ).fetchall()

    print(f"adapted templates      : {adapted}")
    print(f"students with 0 templates: {len(ghosts)}")
    for g in ghosts:
        print(f"    #{g['id']:<4} {g['name']} ({g['roll_no']})")

    if not args.apply:
        print("\nDry run - nothing written. Re-run with --apply to make these changes.")
        return 0

    backup = config.DB_PATH.with_suffix(".db.bak")
    shutil.copy2(config.DB_PATH, backup)
    print(f"\nBacked up database to {backup}")

    with database.connect() as conn:
        if not args.keep_adapted:
            n = conn.execute("DELETE FROM templates WHERE source = 'adapted'").rowcount
            print(f"deleted {n} adapted template(s)")
        if not args.keep_ghosts:
            n = conn.execute(
                "DELETE FROM students WHERE id IN (%s)" % ",".join("?" * len(ghosts)),
                [g["id"] for g in ghosts],
            ).rowcount if ghosts else 0
            print(f"deleted {n} student(s) with no templates")
        conn.commit()

    print("\nDone. Restart the server so it reloads the gallery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
