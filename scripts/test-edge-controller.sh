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

# A deploy that should be refused, and must leave the installed pair alone.
# Only matched pairs were ever exercised here, so the guard that rejects an
# unmatched one was never run by a test.
refuse() { # refuse <name> <dir>
    # Existence, not size: one case is deliberately zero bytes. A fixture that
    # failed to generate would otherwise tar nothing, and `set -e` would end the
    # run with no indication of which case was even being tried.
    if [ ! -e "$2/fullchain.pem" ] || [ ! -e "$2/privkey.pem" ]; then
        echo "FAIL $1: the fixture was not generated." >&2
        exit 1
    fi
    if tar -C "$2" -cf - fullchain.pem privkey.pem | \
        env PATH="${bin_dir}:${PATH}" SEVERINO_HQ_CADDY_CERT_DIR="${cert_dir}" \
        SEVERINO_HQ_CERT_OWNER="$(id -un)" SEVERINO_HQ_CERT_GROUP="$(id -gn)" \
        deploy/targets/severino-hq-edge-controller deploy >/dev/null 2>&1
    then
        echo "FAIL $1: the deploy was accepted." >&2
        exit 1
    fi
    if [ "$(openssl x509 -in "${cert_dir}/fullchain.pem" -noout -fingerprint -sha256)" \
        != "${expected_fingerprint}" ]; then
        echo "FAIL $1: the installed certificate was replaced." >&2
        exit 1
    fi
    echo "ok   $1"
}

# A key that belongs to a different certificate.
bad_dir="${root}/mismatched"; mkdir -p "${bad_dir}"
cp "${new_dir}/fullchain.pem" "${bad_dir}/fullchain.pem"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=other.example.test \
    -keyout "${bad_dir}/privkey.pem" -out "${bad_dir}/unused.pem" >/dev/null 2>&1
rm -f "${bad_dir}/unused.pem"
refuse "a key that does not match the certificate is refused" "${bad_dir}"

# Files that are not PEM at all. Both openssl stages fail, and a pipeline would
# have hashed empty input on each side and compared them equal.
junk_dir="${root}/junk"; mkdir -p "${junk_dir}"
printf 'not a certificate\n' >"${junk_dir}/fullchain.pem"
printf 'not a key\n' >"${junk_dir}/privkey.pem"
refuse "unparseable input is refused" "${junk_dir}"

# Zero-byte files.
empty_dir="${root}/empty"; mkdir -p "${empty_dir}"
: >"${empty_dir}/fullchain.pem"; : >"${empty_dir}/privkey.pem"
refuse "empty input is refused" "${empty_dir}"

# An expired certificate parses and matches its key, so only the expiry check
# stands between it and a reload.
#
# Backdating a certificate needs `-not_before`, which OpenSSL gained in 3.5.
# Where that is unavailable the case is skipped out loud rather than silently:
# the point of this suite is that a check which cannot run must not look like
# one that passed.
old_dir="${root}/expired"; mkdir -p "${old_dir}"
if openssl req -x509 -help 2>&1 | grep -q: '-not_before'; then
    openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=expired.example.test \
        -keyout "${old_dir}/privkey.pem" -out "${old_dir}/fullchain.pem" \
        -not_before 20200101000000Z -not_after 20200102000000Z >/dev/null 2>&1
    refuse "an expired certificate is refused" "${old_dir}"
else
    echo "skip an expired certificate is refused (openssl $(openssl version \
        | awk '{print $2}') has no -not_before; needs 3.5+)"
fi

# The read-only arm. Stubbed at `docker`, because what is being checked is that
# the operation is allowlisted and passes the adapted config through untouched
# not that Caddy adapts a Caddyfile, which is Caddy's own test to run.
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
