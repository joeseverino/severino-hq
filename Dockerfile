# Severino HQ: homelab container image.
# Multi-stage: build wheel deps and the controller binary, then a slim runtime
# as a non-root user.

# Every stage pins its base by digest; Dependabot bumps it on these lines.
# uv comes from its own image, as in composition/Dockerfile.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.15-slim-trixie@sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619 AS build
COPY --from=uv /uv /usr/local/bin/uv
# The cache mount and /install are different filesystems, so files are copied.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
# The export is complete and hashed: nothing resolves, and a file that differs
# from the lock is refused.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --locked --no-default-groups --no-emit-project \
        --format requirements-txt --output-file /tmp/hq-runtime.txt > /dev/null \
    && uv pip install --python /usr/local/bin/python --prefix /install \
        --require-hashes --no-deps -r /tmp/hq-runtime.txt


# The controller and the root secret renderer, one static binary each: CGO off,
# -trimpath, so the runtime image carries no Go toolchain and the binaries no
# build paths. Their Go is go.mod's go directive; the build fails if this
# pinned image drifts from it.
FROM golang:1.27.2-trixie@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e AS controller
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
# hq-host deploys only what its source repository's compose workflow signed,
# so it is told that repository here, from the source the image is labelled with.
ARG HQ_SOURCE=""
RUN go build -trimpath -ldflags='-s -w -buildid=' -o /out/hq-controller ./cmd/hq-controller \
    && go build -trimpath -ldflags='-s -w -buildid=' -o /out/hq-secrets ./cmd/hq-secrets \
    && go build -trimpath -ldflags="-s -w -buildid= -X github.com/joeseverino/severino-hq/controller/host.BuildSource=${HQ_SOURCE#https://github.com/}" \
        -o /out/hq-host ./cmd/hq-host


FROM python:3.15-slim-trixie@sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619 AS runtime

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
        sqlite3 ca-certificates openssh-client \
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
# The controller hq-host starts in a container of this image.
COPY --from=controller /out/hq-controller /usr/local/bin/hq-controller

WORKDIR /app
COPY . /app
# The renderer root runs on the host. It ships in the root-run tree, beside
# the units that name it, so the manifest below covers it: the host's copy is
# the signed image's, byte for byte, and checked daily like every script.
COPY --from=controller /out/hq-secrets /out/hq-host /app/deploy/bin/

# Mounted volumes; create empty so the container can boot before a host mount.
# The bridge socket's directory is this account's alone: a volume first mounted
# there takes that owner and mode, and HQ refuses to serve from anything wider.
RUN mkdir -p /data /media /exports /run/hq-bridge \
    && chown -R severino:severino /data /media /exports /run/hq-bridge \
    && chmod 0700 /run/hq-bridge

# The image carries what a start would otherwise make: the collected, hashed
# and compressed assets in /static, and the application's bytecode. Both are
# root's and read-only to the account HQ runs as, like the source they come
# from, so a start collects and compiles nothing.
#
# Bytecode is validated by nothing at run time (unchecked-hash): the source
# beside it cannot change in an image, and a timestamp would make the bytes
# depend on when the build ran. The hash seed is fixed for this step so they
# cannot depend on it either. Tests compile when a gate runs them.
RUN scripts/collect-assets.sh \
    && PYTHONHASHSEED=0 python -m compileall -q --invalidation-mode unchecked-hash \
        -x '/tests/' hq hq_sdk

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
