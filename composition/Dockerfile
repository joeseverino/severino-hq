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
# The installer uses the same Python ABI as HQ. uv is pinned to the toolchain
# version and to the Linux amd64 image digest used by the composition runner.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:d46db4c7b7f2e75ff80aeef95da4c2c6ff1ad399c13ab85067db4430b7c3b9c3 AS uv
FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS installer-base
COPY --from=uv /uv /usr/local/bin/uv

FROM installer-base AS installer

# The wheels, and "<sha256>  <filename>" lines, one per admitted wheel. A
# file, not a build argument: the build log prints each RUN with its arguments
# expanded, and the log is public.
COPY build/composition/*.whl build/composition/digests /tmp/plugin/

# Each wheel installs from a hashed requirements file built from the digests
# its admission recorded, so uv refuses a wheel that differs. A wheel present
# in the build context but absent from the digest list fails the count check
# rather than being installed unverified.
RUN set -eu; \
    awk 'NF { \
        if (NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/) exit 1; \
        print "/tmp/plugin/" $2 " --hash=sha256:" $1 }' /tmp/plugin/digests > /tmp/plugin/requirements.txt; \
    expected=$(grep -c . /tmp/plugin/requirements.txt); \
    found=$(find /tmp/plugin -maxdepth 1 -type f -name '*.whl' | wc -l); \
    test "$expected" -eq "$found"; \
    uv pip install --quiet --python /usr/local/bin/python --prefix /install \
      --no-cache --no-deps --require-hashes -r /tmp/plugin/requirements.txt

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
RUN test -s /opt/severino/plugin-lock.json \
    && ! command -v pip \
    && ! python -c 'import pip'

ENV SEVERINO_HQ_PLUGINS=${SEVERINO_HQ_PLUGIN_REFERENCES} \
    SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=1 \
    SEVERINO_HQ_PLUGIN_LOCK=/opt/severino/plugin-lock.json \
    SEVERINO_HQ_PLUGIN_POLICY_SHA256=${PLUGIN_POLICY_SHA256} \
    SEVERINO_HQ_REVISION=${HQ_REVISION}
USER severino
