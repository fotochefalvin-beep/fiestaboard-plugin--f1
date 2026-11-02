# syntax=docker/dockerfile:1
#
# F1 Live Standings sidecar (FastF1 live timing -> plugin `f1`).
# Build context: the REPOSITORY ROOT:
#
#   docker compose -f docker-compose.sidecar.yml build
#   # or: docker build -f sidecar/Dockerfile .
#
# - Dependencies come only from sidecar/pyproject.toml + uv.lock
#   (`uv sync --frozen --no-dev`); there is no requirements.txt (FiestaBoard's
#   install check scans the repo for one).
# - secrets/ never enters the build context (.dockerignore); the secrets are
#   mounted at runtime by the compose file.

# python:3.14-slim-trixie (multi-arch, includes linux/arm64), pinned 2026-10-04.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

# --- build: venv ----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

# uv from PyPI, the index the dependencies come from anyway.
RUN pip install --no-cache-dir --root-user-action=ignore uv==0.12.21

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3

WORKDIR /app/sidecar

# Dependencies first, so code changes don't reinstall them.
COPY sidecar/pyproject.toml sidecar/uv.lock ./
RUN uv sync --frozen --no-dev

# --- runtime --------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ARG UID=10001
ARG GID=10001
RUN groupadd --system --gid "${GID}" sidecar \
 && useradd --system --uid "${UID}" --gid "${GID}" --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin sidecar \
 && mkdir -p /data \
 && chown "${UID}:${GID}" /data

WORKDIR /app/sidecar

COPY --from=build /app/sidecar/.venv /app/sidecar/.venv
COPY sidecar/sidecar ./sidecar

# Read-only root filesystem: no .pyc writes; the heartbeat, FastF1's cache
# and matplotlib's config live on the /tmp tmpfs. The F1 token lives under
# XDG_DATA_HOME=/data (a named volume, so restarts don't sign in again).
# FastF1 ignores XDG_CACHE_HOME and defaults to ~/.fastf1, but the sidecar
# user has no home (/nonexistent): FASTF1_CACHE points it at the tmpfs.
ENV PATH=/app/sidecar/.venv/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEARTBEAT_FILE=/tmp/heartbeat \
    XDG_DATA_HOME=/data \
    XDG_CACHE_HOME=/tmp/cache \
    FASTF1_CACHE=/tmp/fastf1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    FASTF1_AUTH_METHOD=password

USER sidecar:sidecar

# The healthcheck (heartbeat older than 60 s -> unhealthy) is defined in
# docker-compose.sidecar.yml.

CMD ["python", "-m", "sidecar"]
