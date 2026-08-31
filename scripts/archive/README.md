# Archived scripts

One-off debugging scripts from earlier development. **They no longer run.**

Every one of them predates the current API and calls things that were removed or
changed:

- `live_test.py`, `test_detection_counts.py` — POST to `/api/attendance/process`
  with no authentication. Every endpoint now requires a session, so these get 401.
- `test_hungarian_perfect.py`, `test_separation.py`, `grid_search_thresholds.py`,
  `test_quality_scaling.py`, `test_adaptive_margin.py` — call
  `database.load_gallery_with_quality()` and hard-code threshold logic that has
  since moved into `config.py`.
- `clean_reenroll_and_eval.py`, `fix_templates.py` — reference `est_age`, removed
  along with the age feature.

They are kept for reference only. The maintained equivalents are:

| Instead of | Use |
|---|---|
| ad-hoc accuracy checks | `python -m scripts.evaluate --sweep` |
| threshold grid search | `python -m scripts.evaluate --sweep` (FAR/FRR/EER table) |
| degradation probing | `python -m scripts.robustness` |
| endpoint smoke tests | `python -m scripts.security_test --password <pw>` |
| gallery repair | `python -m scripts.cleanup_gallery` |
