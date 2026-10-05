# OpenMob hosted server (Fly.io). Phones connect in with the OpenMob app; no adb, mic or OCR here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENMOB_DATA_DIR=/data \
    OPENMOB_APK=/app/OpenMob.apk

WORKDIR /app
COPY pyproject.toml README.md ./
COPY openmob ./openmob
# No speech model on the server: the dashboard and the phone app use their built-in speech recognition.
RUN pip install .

COPY deploy/config.yaml ./config.yaml
COPY deploy/OpenMob.apk ./OpenMob.apk

EXPOSE 8080
CMD ["openmob", "run", "--no-adb", "--quiet", "--accounts"]
