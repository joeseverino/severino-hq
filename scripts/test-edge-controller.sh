#!/bin/sh
set -eu

root="$(mktemp -d /tmp/severino-hq-edge-test.XXXXXX)"
trap 'rm -rf "${root}"' EXIT HUP INT TERM
cert_dir="${root}/certs"
new_dir="${root}/new"
bin_dir="${root}/bin"
mkdir -p "${cert_dir}" "${new_dir}" "${bin_dir}"

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=old.example.test \
    -keyout "${cert_dir}/privkey.pem" -out "${cert_dir}/fullchain.pem" >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=new.example.test \
    -keyout "${new_dir}/privkey.pem" -out "${new_dir}/fullchain.pem" >/dev/null 2>&1
expected_fingerprint="$(openssl x509 -in "${new_dir}/fullchain.pem" -noout -fingerprint -sha256)"
cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "${bin_dir}/docker"

tar -C "${new_dir}" -cf - fullchain.pem privkey.pem | \
    env PATH="${bin_dir}:${PATH}" SEVERINO_HQ_CADDY_CERT_DIR="${cert_dir}" \
    SEVERINO_HQ_CERT_OWNER="$(id -un)" SEVERINO_HQ_CERT_GROUP="$(id -gn)" \
    deploy/targets/severino-hq-edge-controller deploy

test "$(openssl x509 -in "${cert_dir}/fullchain.pem" -noout -fingerprint -sha256)" = \
    "${expected_fingerprint}"

# The read-only arm. Stubbed at `docker`, because what is being checked is that
# the operation is allowlisted and passes the adapted config through untouched
# -- not that Caddy adapts a Caddyfile, which is Caddy's own test to run.
cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
printf '%s' '{"apps":{"http":{"servers":{"srv0":{"routes":[]}}}}}'
EOF
chmod +x "${bin_dir}/docker"

routes="$(env PATH="${bin_dir}:${PATH}" \
    deploy/targets/severino-hq-edge-controller routes)"
test "${routes}" = '{"apps":{"http":{"servers":{"srv0":{"routes":[]}}}}}'

# And anything not named is still refused.
if env PATH="${bin_dir}:${PATH}" \
    deploy/targets/severino-hq-edge-controller rm-rf >/dev/null 2>&1; then
    echo "edge controller ran an operation it does not allowlist" >&2
    exit 1
fi

echo "Edge controller deploy, routes, and refusal all behave."
