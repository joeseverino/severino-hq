# Composed production image: the host plus every admitted extension.
#
# One image carries N extensions, so deploying one never replaces another.
#
# CI verifies each signed admission independently, then merges them into one
# lock before invoking this build. The image contains only admitted wheels, and
# the runtime refuses to boot unless the lock inventory exactly matches the
# enabled set, so a wheel added here without an approval cannot start.
ARG HQ_IMAGE
FROM ${HQ_IMAGE}

# "<sha256>  <filename>" lines, one per admitted wheel.
ARG PLUGIN_WHEEL_DIGESTS
ARG PLUGIN_POLICY_SHA256
# Derived from the merged lock, never configured by hand: the enabled set and
# the approved set must be identical or the host will not start.
ARG SEVERINO_HQ_PLUGIN_REFERENCES

USER root
COPY build/composition/*.whl /tmp/plugin/
COPY --chown=severino:severino build/composition/plugin-lock.json /opt/severino/plugin-lock.json

# Each wheel installs from a hashed requirements file built from the digests
# its admission recorded, so pip refuses a wheel that differs. A wheel present
# in the build context but absent from the digest list fails the count check
# rather than being installed unverified.
RUN set -eu; \
    printf '%s\n' "${PLUGIN_WHEEL_DIGESTS}" | awk 'NF { \
        if (NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/) exit 1; \
        print "/tmp/plugin/" $2 " --hash=sha256:" $1 }' > /tmp/plugin/requirements.txt; \
    expected=$(grep -c . /tmp/plugin/requirements.txt); \
    found=$(find /tmp/plugin -maxdepth 1 -type f -name '*.whl' | wc -l); \
    test "$expected" -eq "$found"; \
    pip install --no-cache-dir --no-deps --require-hashes -r /tmp/plugin/requirements.txt; \
    rm -rf /tmp/plugin; \
    test -s /opt/severino/plugin-lock.json

ENV SEVERINO_HQ_PLUGINS=${SEVERINO_HQ_PLUGIN_REFERENCES} \
    SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=1 \
    SEVERINO_HQ_PLUGIN_LOCK=/opt/severino/plugin-lock.json \
    SEVERINO_HQ_PLUGIN_POLICY_SHA256=${PLUGIN_POLICY_SHA256}
USER severino
