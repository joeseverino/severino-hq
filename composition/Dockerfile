# Composed production image: the host plus every admitted extension.
#
# One image carries N extensions, so deploying one never replaces another.
#
# CI verifies each signed admission independently, then merges them into one
# lock before invoking this build. The image contains only admitted wheels, and
# the runtime refuses to boot unless the lock inventory exactly matches the
# enabled set, so a wheel added here without an approval cannot start.

# The host image, by digest: compose.yml passes it after verifying its
# signature. The default has no shell or users, so a build without it fails.
ARG HQ_IMAGE=scratch
# uv at the release mise.toml pins, installed by mise from mise.lock, which
# refuses a download whose checksum differs from it (as the host image's
# tools stage does, with the same mise).
FROM python:3.15-slim-trixie@sha256:044d5bd747976e21f8bc4d746c3479b19a84bb8ae6261b802acdd26b29036c93 AS tools
COPY --from=ghcr.io/jdx/mise:2026.10.18@sha256:fc31de7b93d195613deb600d42c65a164b0205716ade38132aadeabac3838248 /usr/local/bin/mise /usr/local/bin/mise
ENV MISE_TRUSTED_CONFIG_PATHS=/tools
WORKDIR /tools
COPY mise.toml mise.lock ./
RUN mise install uv \
    && install -D "$(mise which uv)" /opt/bin/uv

# The installer uses the same Python ABI as HQ.
FROM python:3.15-slim-trixie@sha256:044d5bd747976e21f8bc4d746c3479b19a84bb8ae6261b802acdd26b29036c93 AS installer-base
COPY --from=tools /opt/bin/uv /usr/local/bin/uv
# Every wheel an installer of this base installs carries its bytecode, as the
# host's own packages do: the image is read-only to the account HQ runs as.
ENV UV_COMPILE_BYTECODE=1

FROM installer-base AS installer

# The wheels, and "<sha256>  <filename>" lines, one per admitted wheel. A
# file, not a build argument: the build log prints each RUN with its arguments
# expanded, and the log is public. The directory rather than a *.whl pattern,
# so a composition of no extensions copies as plainly as one of several.
COPY build/composition/ /tmp/plugin/

# Each wheel installs from a hashed requirements file built from the digests
# its admission recorded, so uv refuses a wheel that differs. A wheel present
# in the build context but absent from the digest list fails the count check
# rather than being installed unverified. No wheels and no digests is the host
# alone, and installs nothing.
RUN set -eu; \
    awk 'NF { \
        if (NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/) exit 1; \
        print "/tmp/plugin/" $2 " --hash=sha256:" $1 }' /tmp/plugin/digests > /tmp/plugin/requirements.txt; \
    expected=$(grep -c . /tmp/plugin/requirements.txt || true); \
    found=$(find /tmp/plugin -maxdepth 1 -type f -name '*.whl' | wc -l); \
    test "$expected" -eq "$found"; \
    mkdir -p /install; \
    if [ "$expected" -gt 0 ]; then \
      uv pip install --quiet --python /usr/local/bin/python --prefix /install \
        --no-cache --no-deps --require-hashes -r /tmp/plugin/requirements.txt; \
    fi

FROM ${HQ_IMAGE}
# Derived from the merged lock, never configured by hand: the enabled set and
# the approved set must be identical or the host will not start.
ARG PLUGIN_POLICY_SHA256
ARG SEVERINO_HQ_PLUGIN_REFERENCES
# The commit on main this composition releases. The host image may have been
# built for the pull request that proved the same tree, so the release's own
# commit is stated here rather than inherited.
ARG HQ_REVISION
LABEL org.opencontainers.image.revision=${HQ_REVISION}
USER root
COPY --from=installer /install /usr/local
COPY --chown=severino:severino build/composition/plugin-lock.json /opt/severino/plugin-lock.json
# The extensions this composition removed on purpose. Only a deploy reads it:
# production refuses an image that drops an extension it runs unless it is
# named here (controller/host/extensions.go).
COPY build/composition/plugin-retired.json /opt/severino/plugin-retired.json
RUN test -s /opt/severino/plugin-lock.json \
    && test -s /opt/severino/plugin-retired.json \
    && ! command -v pip \
    && ! python -c 'import pip'

ENV SEVERINO_HQ_PLUGINS=${SEVERINO_HQ_PLUGIN_REFERENCES} \
    SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=1 \
    SEVERINO_HQ_PLUGIN_LOCK=/opt/severino/plugin-lock.json \
    SEVERINO_HQ_PLUGIN_POLICY_SHA256=${PLUGIN_POLICY_SHA256} \
    SEVERINO_HQ_REVISION=${HQ_REVISION}
# The extensions bring assets of their own, so the tree the host image
# collected is collected again with them, under the settings set above. The
# key exists for this step alone: collecting signs nothing.
RUN DJANGO_SECRET_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" \
    python manage.py collectstatic --noinput --clear --verbosity 0
USER severino
