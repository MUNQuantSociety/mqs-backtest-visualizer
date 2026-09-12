# syntax=docker/dockerfile:1

# 3.12 is what the development venv and the CI suite run (ci.yml pins the
# same). pandas==2.2.2 and numpy<=1.26.4 both ship cp312 wheels; 3.13 would
# force a numpy source build, so the Python version and those pins move together.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1

# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-privileged user that the app will run under.
# See https://docs.docker.com/go/dockerfile-user-best-practices/
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Runtime writes must work as appuser; WORKDIR itself is owned by root.
RUN install -d -o appuser -g appuser \
    /app/.artifacts /app/.strategy_store /app/data/backfill_cache

# Download dependencies as a separate step to take advantage of Docker's caching.
# Leverage a cache mount to /root/.cache/pip to speed up subsequent builds.
# Leverage a bind mount to requirements.txt to avoid having to copy them into
# this layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

# Only backend runtime sources belong in this image. Keep engine/data and the
# strategy JSON files: the engine loads them dynamically at runtime.
COPY --chown=appuser:appuser server.py ./
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser engine/ ./engine/

# Switch to the non-privileged user to run the application.
USER appuser

# Expose the port that the application listens on.
EXPOSE 8000

# Liveness for `docker compose` and for anyone running the image by hand.
# python:3.12-slim ships no curl, so the probe is the interpreter that is
# already on PATH (also as appuser). Exec form: no shell, no quoting to get
# wrong. The path is the ALB's health_check_path in MQS_AWS_INFRA so every
# layer agrees on what "healthy" means.
#
# ECS Fargate IGNORES this instruction. For a container-level check in ECS the
# task definition must carry the same command via the infra's
# `container_health_check_command`; docs/CI_CD.md gives the exact value.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

# Run the application.
# Bind 0.0.0.0 so the container is reachable from outside (ECS / compose).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
