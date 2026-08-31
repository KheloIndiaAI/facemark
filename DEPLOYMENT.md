# Deploying FaceMark on AWS

Everything on AWS: static frontend on S3 behind CloudFront, backend on EC2,
both served from one hostname.

```
Route 53 ──▶ CloudFront distribution
                 ├── default    ──▶ S3 bucket      (frontend, 116 KB static)
                 └── /api/*     ──▶ EC2 instance   (Docker, FastAPI, ONNX)
                                        │
                                   ┌────┴────┬──────────┐
                                   ▼         ▼          ▼
                                EBS gp3   S3 backup  CloudWatch
                              models + DB   nightly   logs/alarms
```

One distribution with two origins terminates HTTPS, caches the frontend at the
edge, and — because the API answers on the *same* hostname — keeps the session
cookie working with no cross-site configuration. It also removes the need for an
Application Load Balancer, which would add about $20/month.

---

## Licensing

Every component permits commercial use and redistribution. This was not true
before, and deploying as a network service is exactly what made it matter:

| Component | Licence |
|---|---|
| YuNet (detection) | MIT |
| SFace (recognition) | Apache-2.0 |
| OpenCV, FastAPI, uvicorn, numpy, Pillow | Apache-2.0 / MIT / BSD |

Removed in the migration, and why:

| Removed | Reason |
|---|---|
| Ultralytics YOLO11 | **AGPL-3.0** — its network clause obliges anyone serving the software to publish the whole application's source |
| InsightFace det_10g, glintr100, w600k_r50 | **"non-commercial research purposes only"** per their model zoo |
| AdaFace IR-101 | weights trained on WebFace4M, academic research terms |
| GFPGAN | weights derive from NVIDIA StyleGAN2, not licensed for commercial use |
| torch, ultralytics, onnxruntime | pulled in only by the above |

## Sizing

Measured after the migration, not estimated:

| | Before | After |
|---|---|---|
| Memory | 1,178 MB | **122 MB** |
| Models | 1,022 MB | **37 MB** |
| Per group photo | 12–16 s | **0.6 s** |
| Accuracy (13-athlete ground truth) | 13/13 | **13/13** |
| Separation (d-prime) | 4.91 | **5.51** |
| Equal error rate | 0.10% | **0.00%** |

The swap cost no accuracy — it improved separation — and made almost any
instance viable. A `t4g.small` ($8.18/month) or even `t4g.micro` now has room to
spare, where the previous stack needed 2 GB.

## 1 · Push to GitHub

```bash
gh repo create facemark --private --source=. --push
```

`.gitignore` keeps model weights, the database and photos out. The push is
about 0.4 MB.

## 2 · Models

Nothing to host. Both download during the Docker build from the OpenCV Zoo,
total 37 MB, and are size-checked so a mirror serving an error page is rejected
rather than reaching `data/models/`.

## 3 · Launch the backend

Create the data volume first — it outlives the instance:

```bash
aws ec2 create-volume --availability-zone ap-south-1a --size 80 --volume-type gp3
```

Launch with `deploy/aws/user-data.sh` as user data, having edited `REPO_URL`
and `ADMIN_PASSWORD` at the top. Attach
the volume as `/dev/sdf`. The script formats it **only if blank**, mounts it at
`/data`, builds the image, and installs a systemd unit that restarts on failure.

Security group: inbound 443 and 80 from CloudFront's prefix list
(`com.amazonaws.global.cloudfront.origin-facing`) rather than `0.0.0.0/0`, plus
22 from your own address only.

First build takes 2–3 minutes. There is no torch to compile and only 37 MB of weights.

```bash
ssh ec2-user@<ip> 'docker logs facemark 2>&1 | grep "FIRST RUN"'
```

## 4 · Frontend on S3 + CloudFront

```bash
aws s3 mb s3://facemark-frontend
aws s3 sync frontend/ s3://facemark-frontend/ --exclude "*.bak"
```

Create a distribution with two origins:

| Behaviour | Origin | Notes |
|---|---|---|
| `/api/*` | EC2 public DNS | Cache disabled, **forward all cookies and headers** |
| `/static/*` | S3 bucket | Maps to the bucket root |
| Default `*` | S3 bucket | `index.html` as the root object |

Two settings that break things silently if missed:

- **The `/api/*` behaviour must forward cookies.** CloudFront strips them by
  default, which drops the session and returns 401 on every request after login.
- **`/static/*` must map to the bucket root.** `index.html` requests its CSS and
  JS from `/static/…`, a path that only exists as a FastAPI mount when running
  locally.

Keep the bucket private and reach it through an Origin Access Control.

## 5 · Schedule the instance (optional)

`deploy/aws/schedule.md` starts and stops the instance around training hours.
Far less compelling now: at 122 MB and 0.6 s per photo a `t4g.small` runs the
whole thing for about $8/month, so the saving is small and the app is simply
always available if you skip it.

## 6 · Before real use

```bash
# Billing alarm - cheap insurance even on a small instance.
aws cloudwatch put-metric-alarm --alarm-name facemark-spend \
  --namespace AWS/Billing --metric-name EstimatedCharges \
  --dimensions Name=Currency,Value=USD \
  --statistic Maximum --period 21600 --threshold 200 \
  --comparison-operator GreaterThanThreshold --evaluation-periods 1
```

- Sign in as `admin`, **change the password immediately**.
- Centres → **Remove demo** to clear the eight placeholder centres, then
  **Import data** for real ones.
- Enrol athletes with **Capture views** rather than a single photo. Measured on
  real data, a stale registration photo is the single largest cause of missed
  matches.
- Confirm the nightly backup is reaching S3. The SQLite file holds every
  enrolled template; losing it means re-enrolling everyone.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `FACEMARK_DATA_DIR` | `./data` | Writable root. **Must** point at the mounted volume or every rebuild wipes the database. |
| `FACEMARK_MODELS_DIR` | `$DATA_DIR/models` | Kept separate so `FACEMARK_DATA_DIR` doesn't relocate the baked-in models to an empty volume. |
| `FACEMARK_ADMIN_PASSWORD` | random | First-boot super-admin password. |
| `CORS_ORIGINS` | localhost | Only needed if a browser calls the API directly; the CloudFront behaviour makes requests same-origin. |
| `COOKIE_SECURE` | `0` | Set `1` in production. |
| `COOKIE_SAMESITE` | `lax` | Only `none` if you abandon the single-hostname setup (then `COOKIE_SECURE=1` is mandatory). |
| `DETECTION_MODE` | `fused` | `fast` \| `fused` \| `accurate`. |
| `MATCH_THRESHOLD` | `0.55` | Measured EER is 0.00% at 0.570; 0.55 trades one impostor *pair* for two extra genuine matches on small faces. |

## Operational notes

- **Two workers** are affordable at 122 MB each. Scale further with instances.
- **Cold start is seconds**, not the 20–40 s the old model loading took.
- **A GPU is no longer worth considering.** 0.6 s per photo on CPU removes the
  reason the GPU costing existed.

## Other platforms

`render.yaml` and `vercel.json` are kept for a Render + Vercel deployment.
Render **Starter** ($7/month) is now comfortable — the old stack needed
Standard at $25. `scripts/serve_public.py` opens a Cloudflare
tunnel for demos from a local machine.

## Local development

```bash
python run.py     # FastAPI serves the frontend itself, no proxy needed
```
