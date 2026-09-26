# Archived scripts

One-off debugging scripts from earlier development. **None of them runs.**

They are kept because several carry reasoning worth not repeating — threshold
sweeps, separation measurements, the Hungarian assignment experiments — but the
code around that reasoning is dead.

Why each fails, accurately:

- `live_test.py`, `test_detection_counts.py` — these *do* authenticate (they log
  in and carry the session). They fail because the photographs they open live
  under `data/uploads/`, which is gitignored, so a clean checkout has no input.
- `test_hungarian_perfect.py`, `test_separation.py`, `grid_search_thresholds.py`,
  `test_quality_scaling.py`, `test_adaptive_margin.py` — hard-code threshold
  logic that has since moved into `config.py`, mutate settings that no longer
  exist (`RATIO_TEST_THRESHOLD` and friends), and read the same gitignored
  photographs.
- `clean_reenroll_and_eval.py`, `fix_templates.py` — import `FFHQ_LANDMARKS`
  from `backend.enhancer` and reference `est_age`, both removed with GFPGAN and
  the age feature.

None has a `sys.path` insert, so they cannot be run as `python scripts/archive/X.py`
even after the above is fixed.

The maintained equivalents:

| Instead of | Use |
|---|---|
| ad-hoc accuracy checks | `python -m scripts.evaluate --sweep` |
| threshold grid search | `python -m scripts.evaluate --sweep` |
| degradation probing | `python -m scripts.robustness` |
| endpoint smoke tests | `python -m scripts.security_test --password <pw>` |
| liveness thresholds | `python -m scripts.calibrate_liveness --live DIR --spoof DIR` |

Gallery repair via `scripts/cleanup_gallery.py` is **currently broken on
`--apply`**: it backs up `config.DB_PATH`, which is the dead SQLite file, and
then calls `conn.commit()`, which `Conn` does not expose. Dry-run works.
