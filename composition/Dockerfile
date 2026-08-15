# Composed production image: the host plus every admitted extension.
#
# One image carries N extensions. Previously each extension composed its own
# image declaring a single SEVERINO_HQ_PLUGINS entry, so deploying one replaced
# the others -- shipping a second extension silently removed the first.
#
# CI verifies each signed admission independently, then merges them into one
# lock before invoking this build. The image contains only admitted wheels, and
# the runtime refuses to boot unless the lock inventory exactly matches the
# enabled set, so a wheel added here without an approval cannot start.
ARG HQ_IMAGE
FROM ${HQ_IMAGE}

# Space-separated "<sha256>  <filename>" pairs, one per admitted wheel.
ARG PLUGIN_WHEEL_DIGESTS
ARG PLUGIN_POLICY_SHA256
# Derived from the merged lock, never configured by hand: the enabled set and
# the approved set must be identical or the host will not start.
ARG SEVERINO_HQ_PLUGIN_REFERENCES

USER root
COPY build/composition/*.whl /tmp/plugin/
COPY --chown=severino:severino build/composition/plugin-lock.json /opt/severino/plugin-lock.json

# Every wheel is checked against the digest its own admission recorded. A wheel
# present in the build context but absent from the digest list fails the count
# check rather than being installed unverified.
RUN set -eu; \
    printf '%s\n' "${PLUGIN_WHEEL_DIGESTS}" > /tmp/plugin/DIGESTS; \
    expected=$(grep -c . /tmp/plugin/DIGESTS); \
    found=$(find /tmp/plugin -maxdepth 1 -type f -name '*.whl' | wc -l); \
    test "$expected" -eq "$found"; \
    cd /tmp/plugin && sha256sum -c DIGESTS; \
    for wheel in /tmp/plugin/*.whl; do \
        pip install --no-cache-dir --no-deps "$wheel"; \
    done; \
    rm -rf /tmp/plugin; \
    test -s /opt/severino/plugin-lock.json

ENV SEVERINO_HQ_PLUGINS=${SEVERINO_HQ_PLUGIN_REFERENCES} \
    SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=1 \
    SEVERINO_HQ_PLUGIN_LOCK=/opt/severino/plugin-lock.json \
    SEVERINO_HQ_PLUGIN_POLICY_SHA256=${PLUGIN_POLICY_SHA256}
USER severino
