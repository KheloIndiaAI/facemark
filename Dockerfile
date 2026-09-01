FROM python:3.11-slim

# The image is small because every model is small: YuNet (MIT, 227 KB) and
# SFace (Apache-2.0, 37 MB), both loaded through OpenCV's own DNN module. There
# is no torch, no ultralytics and no onnxruntime - the previous stack pulled in
# roughly 1.7 GB for those, and could not be deployed anyway, because YOLO is
# AGPL-3.0 and the InsightFace weights are non-commercial research only.

# libgl / libglib are OpenCV's runtime shared libraries. Without them
# `import cv2` fails with an ImportError that names neither package.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so this layer caches across code-only pushes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Models next, in their own layer, so editing code never re-downloads them.
COPY scripts/download_models.py scripts/
RUN python -m scripts.download_models && ls -lh /app/data/models

# The browser-side face model and MediaPipe runtime, same reasoning: 27 MB that
# would otherwise be committed. Its own layer so it is not re-fetched on a code
# change. It does NOT fail the build if the CDN is unreachable - the enrolment
# overlay falls back to server-side detection, which is how it worked before.
COPY scripts/fetch_frontend_models.py scripts/
RUN python -m scripts.fetch_frontend_models && ls -lh /app/frontend/vendor/mediapipe || true

# Application code last: the layer that actually changes between deploys.
COPY backend/ backend/
COPY frontend/ frontend/
COPY run.py .
COPY scripts/ scripts/

ENV FACEMARK_MODELS_DIR=/app/data/models \
    PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

# Startup is seconds now rather than the 20-40s the old model loading took.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/api/health || exit 1

# Two workers are affordable now that the models total 37 MB rather than 1 GB.
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 2 --timeout-keep-alive 75
