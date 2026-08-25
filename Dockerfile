# Cynea Voice Engine
#
# Two-stage build. Wheels are compiled in the builder and copied into a
# slim runtime, so the shipped image carries no compiler toolchain.
#
# Whisper is optional and off by default:
#   docker build -t cynea .                          # ~400 MB, hosted STT
#   docker build --build-arg WITH_WHISPER=1 -t cynea . # ~3 GB, local STT
#
# Local Whisper pulls torch (~2 GB) and needs ffmpeg. Unless you have a
# reason to keep audio off third-party infrastructure -- which is a real
# reason, and the point of supporting it -- the hosted path is smaller,
# faster to deploy, and needs no GPU.

# ----------------------------------------------------------------------
# Stage 1: build wheels
# ----------------------------------------------------------------------
FROM python:3.11-slim AS builder

ARG WITH_WHISPER=0

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    && if [ "$WITH_WHISPER" = "1" ]; then \
         pip wheel --no-cache-dir --wheel-dir /wheels openai-whisper==20250625 ; \
       fi

# ----------------------------------------------------------------------
# Stage 2: runtime
# ----------------------------------------------------------------------
FROM python:3.11-slim

ARG WITH_WHISPER=0

LABEL org.opencontainers.image.title="Cynea Voice Engine" \
      org.opencontainers.image.description="AI voice agents for African businesses" \
      org.opencontainers.image.source="https://github.com/Mars2390/cynea-voice-engine"

# libpq5 for psycopg2; ffmpeg only when Whisper runs locally.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && if [ "$WITH_WHISPER" = "1" ]; then \
         apt-get install -y --no-install-recommends ffmpeg ; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

# Never run the voice stack as root.
RUN useradd --create-home --shell /bin/bash cynea
WORKDIR /app

COPY --chown=cynea:cynea cynea/ ./cynea/
COPY --chown=cynea:cynea cynea_africa/ ./cynea_africa/
COPY --chown=cynea:cynea requirements.txt README.md ./

USER cynea

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# Fails until the API server exists (gap BE-1) -- that is deliberate, so an
# orchestrator does not report a container healthy when it serves nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# Import-check the engine, then serve. The import is the useful part today:
# it verifies every provider registers inside the image.
CMD ["sh", "-c", "python -c 'import cynea; print(\"providers:\", cynea.providers.registered())' && exec uvicorn cynea.api:app --host 0.0.0.0 --port ${PORT}"]
