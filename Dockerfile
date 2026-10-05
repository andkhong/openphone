# OpenMob hosted server (Fly.io). Phones connect in with the OpenMob app; no adb, mic or OCR here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENMOB_DATA_DIR=/data \
    OPENMOB_APK=/app/OpenMob.apk \
    HF_HOME=/opt/hf

WORKDIR /app
COPY pyproject.toml README.md ./
COPY openmob ./openmob
RUN pip install ".[voice]"
# Whisper for the dashboard's mic button, baked into the image so the first use doesn't download it.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"

COPY deploy/config.yaml ./config.yaml
COPY deploy/OpenMob.apk ./OpenMob.apk

EXPOSE 8080
CMD ["openmob", "run", "--no-adb", "--quiet", "--accounts"]
