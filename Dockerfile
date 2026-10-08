# OpenMob hosted server (Fly.io). Phones connect in with the OpenMob app; no adb, mic or OCR here.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENMOB_DATA_DIR=/data \
    OPENMOB_APK=/app/OpenMob.apk

WORKDIR /app
COPY pyproject.toml README.md ./
COPY openmob ./openmob
# Public developer docs, served at /docs/{name} for the /developer console.
COPY docs/quickstart.md docs/concepts.md docs/phone-setup.md docs/api.md docs/mcp.md docs/examples.md \
     docs/webhooks.md docs/safety.md docs/conventions.md docs/errors.md docs/testing.md \
     docs/self-hosting.md ./docs/
RUN pip install ".[openai,gemini]"

COPY deploy/config.yaml ./config.yaml
COPY deploy/OpenMob.apk ./OpenMob.apk

EXPOSE 8080
CMD ["openmob", "run", "--no-adb", "--quiet", "--accounts"]
