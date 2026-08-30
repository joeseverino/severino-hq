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

# The write arm, stubbed at `docker` again. What is checked here is the
# transaction: a good file is installed, and a reload that fails puts the
# previous one back rather than leaving the edge serving nothing.
routes_dir="${root}/routes"
mkdir -p "${routes_dir}"
printf 'old.example.test {\n\trespond "old" 200\n}\n' >"${routes_dir}/hq-routes.caddy"

cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "${bin_dir}/docker"

printf 'new.example.test {\n\trespond "new" 200\n}\n' | \
    env PATH="${bin_dir}:${PATH}" \
    SEVERINO_HQ_CADDY_ROUTES="${routes_dir}/hq-routes.caddy" \
    SEVERINO_HQ_CERT_OWNER="$(id -un)" SEVERINO_HQ_CERT_GROUP="$(id -gn)" \
    deploy/targets/severino-hq-edge-controller routes:write
grep -q "new.example.test" "${routes_dir}/hq-routes.caddy"

# Now a docker that fails the reload. The routes must come back.
cat >"${bin_dir}/docker" <<'EOF'
#!/bin/sh
for arg in "$@"; do
    [ "$arg" = "reload" ] && exit 1
done
exit 0
EOF
chmod +x "${bin_dir}/docker"

printf 'broken.example.test {\n\trespond "broken" 200\n}\n' | \
    env PATH="${bin_dir}:${PATH}" \
    SEVERINO_HQ_CADDY_ROUTES="${routes_dir}/hq-routes.caddy" \
    SEVERINO_HQ_CERT_OWNER="$(id -un)" SEVERINO_HQ_CERT_GROUP="$(id -gn)" \
    deploy/targets/severino-hq-edge-controller routes:write && {
        echo "a failed reload reported success" >&2; exit 1; }
grep -q "new.example.test" "${routes_dir}/hq-routes.caddy" || {
    echo "a failed reload did not restore the previous routes" >&2; exit 1; }

echo "Edge controller route writes install, and roll back when the reload fails."
