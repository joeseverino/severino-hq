# Severino HQ — homelab container image.
# Multi-stage: build wheel deps, then a slim runtime as a non-root user.

ARG PYTHON_VERSION=3.12-slim-bookworm

FROM python:${PYTHON_VERSION} AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


FROM python:${PYTHON_VERSION} AS runtime

# Non-root user. UID/GID 10001 to be predictable in volume permissions.
RUN groupadd --system --gid 10001 severino \
    && useradd  --system --uid 10001 --gid severino \
                --home /app --shell /usr/sbin/nologin severino \
    && apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings \
    SEVERINO_DATABASE_PATH=/data/severino.sqlite3 \
    SEVERINO_MEDIA_ROOT=/media \
    SEVERINO_EXPORTS_ROOT=/exports \
    DJANGO_STATIC_ROOT=/static

# Install Python deps from the build stage.
COPY --from=build /install /usr/local

WORKDIR /app
COPY . /app

# Mounted volumes; create empty so the container can boot before a host mount.
RUN mkdir -p /data /media /exports /static \
    && chown -R severino:severino /data /media /exports /static /app

USER severino
EXPOSE 8000

COPY --chown=severino:severino entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--access-log"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/accounts/login/', timeout=3).status in (200,302) else 1)"
