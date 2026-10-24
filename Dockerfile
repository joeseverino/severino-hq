# Severino HQ: homelab container image.
# Multi-stage: build wheel deps and the controller binary, then a slim runtime
# as a non-root user.

# Every stage pins its base by digest; Renovate bumps it on these lines.

# The build tools, Go and uv, at the releases mise.toml pins: mise installs
# each from mise.lock and refuses a download whose checksum differs from it.
# mise is the release min_version names, from its image by digest
# (checks:toolchain holds the tag to min_version).
FROM python:3.15-slim-trixie@sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619 AS tools
COPY --from=ghcr.io/jdx/mise:2026.10.18@sha256:fc31de7b93d195613deb600d42c65a164b0205716ade38132aadeabac3838248 /usr/local/bin/mise /usr/local/bin/mise
ENV MISE_TRUSTED_CONFIG_PATHS=/tools
WORKDIR /tools
COPY mise.toml mise.lock ./
RUN mise install go uv \
    && cp -a "$(mise where go)" /opt/go \
    && install -D "$(mise which uv)" /opt/bin/uv


FROM python:3.15-slim-trixie@sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619 AS build
COPY --from=tools /opt/bin/uv /usr/local/bin/uv
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
# HQ is English only (LANGUAGE_CODE, no LocaleMiddleware), so Django's other
# locales are deleted. The locale package and `en` stay: Django reads formats
# and LANG_INFO from them.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --locked --no-default-groups --no-emit-project \
        --format requirements-txt --output-file /tmp/hq-runtime.txt > /dev/null \
    && uv pip install --python /usr/local/bin/python --prefix /install \
        --require-hashes --no-deps -r /tmp/hq-runtime.txt \
    && find /install/lib -type d -path '*/site-packages/django/*locale/*' -prune ! -name en -exec rm -rf {} +


# The controller and the root secret renderer, one static binary each: CGO off,
# -trimpath, so the runtime image carries no Go toolchain and the binaries no
# build paths. Their Go is mise's (checks:toolchain holds it to go.mod).
FROM tools AS controller
ENV PATH=/opt/go/bin:$PATH \
    CGO_ENABLED=0 \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=readonly
WORKDIR /src
COPY controller/go.mod controller/go.sum ./
RUN go mod download
COPY controller/ ./
# hq-host deploys only what its source repository's compose workflow signed,
# so it is told that repository here, from the source the image is labeled with.
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
#
# The settings refuse to load without a signing key. Collecting signs nothing,
# so the key exists for this step alone. The storage writes every asset
# readable by the serving account (STORAGES in hq/config/settings.py), which
# image:verify:assets holds.
RUN DJANGO_SECRET_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" \
        python manage.py collectstatic --noinput --clear --verbosity 0 \
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

# The probe is the static controller binary already in the image: no
# interpreter or shell per check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/hq-controller", "ready", "http://127.0.0.1:8000/health/ready/"]

# Which commit this image is, and where it lives, for HQ to read about itself:
# the same two values the build stamps as OCI labels, which a container shows
# its host but not its own process. Last, so a new commit rebuilds only this.
ARG HQ_SOURCE=""
ARG HQ_REVISION=""
ENV SEVERINO_HQ_SOURCE=${HQ_SOURCE} \
    SEVERINO_HQ_REVISION=${HQ_REVISION}
