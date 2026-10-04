# Severino HQ: homelab container image.
# Multi-stage: build wheel deps and the controller binary, then a slim runtime
# as a non-root user.

# Every stage pins its base by digest; Dependabot bumps it on these lines.
FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY scripts/dependency_config.py scripts/dependency_config.py
# Bootstrap uv from the same approved artifact hashes the lock records.
RUN python scripts/dependency_config.py uv-requirements > /tmp/uv-bootstrap.txt \
    && pip install --require-hashes --no-deps --ignore-installed --prefix=/uv-bootstrap -r /tmp/uv-bootstrap.txt \
    && /uv-bootstrap/bin/uv export --locked --no-default-groups --no-emit-project \
        --format requirements-txt --output-file /tmp/hq-runtime.txt > /dev/null \
    && pip install --require-hashes --prefix=/install -r /tmp/hq-runtime.txt


# The controller and the root secret renderer, one static binary each: CGO off,
# -trimpath, so the runtime image carries no Go toolchain and the binaries no
# build paths. Their Go is go.mod's go directive; the build fails if this
# pinned image drifts from it.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS controller
ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=readonly
WORKDIR /src
COPY controller/go.mod controller/go.sum ./
RUN want="$(sed -n 's/^go \([0-9.]*\)$/\1/p' go.mod)" \
    && case "$(go env GOVERSION)" in \
        "go${want}" | "go${want}."*) ;; \
        *) echo "$(go env GOVERSION) is not go.mod's go ${want}" >&2; exit 1 ;; \
    esac \
    && go mod download
COPY controller/ ./
RUN go build -trimpath -ldflags='-s -w -buildid=' -o /out/hq-controller ./cmd/hq-controller \
    && go build -trimpath -ldflags='-s -w -buildid=' -o /out/hq-secrets ./cmd/hq-secrets


FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS runtime

# Non-root user. UID/GID 10001 to be predictable in volume permissions.
# `apt-get upgrade` applies Debian security fixes published after the base
# image was last rebuilt; the image scan fails on any fixed HIGH/CRITICAL.
# The security archive's release date keys this layer, so a build cache
# reuses it only until Debian publishes a fix.
ARG DEBIAN_SECURITY_RELEASE=
#
# No package manager at runtime. Dependencies come from the build stage, and
# the composition installs extension wheels in a stage of its own
# (composition/Dockerfile), so pip here would only be code nothing runs:
# its vendored libraries still count against the image in a scan.
RUN python -m pip uninstall --yes pip \
    && groupadd --system --gid 10001 severino \
    && useradd  --system --uid 10001 --gid severino \
                --home /app --shell /usr/sbin/nologin severino \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        sqlite3 ca-certificates openssh-client openssl \
        certbot python3-certbot-dns-cloudflare \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    DJANGO_SETTINGS_MODULE=hq.config.settings \
    SEVERINO_DATABASE_PATH=/data/severino.sqlite3 \
    SEVERINO_MEDIA_ROOT=/media \
    SEVERINO_EXPORTS_ROOT=/exports \
    DJANGO_STATIC_ROOT=/static

# Install Python deps from the build stage.
COPY --from=build /install /usr/local
# The controller run-controller.sh starts in a container of this image.
COPY --from=controller /out/hq-controller /usr/local/bin/hq-controller

WORKDIR /app
COPY . /app
# The renderer root runs on the host. It ships in the root-run tree, beside
# the units that name it, so the manifest below covers it: the host's copy is
# the signed image's, byte for byte, and checked daily like every script.
COPY --from=controller /out/hq-secrets /app/deploy/bin/hq-secrets
# What severino-hq-sync-scripts verifies the root-run tree against on the host.
RUN sh scripts/root-tree-manifest.sh /app > /app/root-tree.sha256

# Mounted volumes; create empty so the container can boot before a host mount.
RUN mkdir -p /data /media /exports /static \
    && chown -R severino:severino /data /media /exports /static /app

USER severino
EXPOSE 8000

COPY --chown=severino:severino entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
# The proxy in front of this speaks HTTP/1.1 upstream and holds a connection
# open for 90 seconds. uvicorn's default is to hang up after 5, so a request
# arriving on a connection idle for longer finds it already closed, and the
# proxy reports what it saw:
#
#   upstream prematurely closed connection while reading response header
#
# Held above the proxy's 90s deliberately: whichever side hangs up first
# decides, and it should be the one that knows a request is not in flight.
CMD ["uvicorn", "hq.config.asgi:application", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--access-log", "--timeout-keep-alive", "120"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready/', timeout=3).status == 200 else 1)"

# Which commit this image is, and where it lives, for HQ to read about itself:
# the same two values the build stamps as OCI labels, which a container shows
# its host but not its own process. Last, so a new commit rebuilds only this.
ARG HQ_SOURCE=""
ARG HQ_REVISION=""
ENV SEVERINO_HQ_SOURCE=${HQ_SOURCE} \
    SEVERINO_HQ_REVISION=${HQ_REVISION}
