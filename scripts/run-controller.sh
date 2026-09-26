#!/bin/sh
# Execute the controller in a short-lived, isolated container made from the
# exact running HQ image. The web container never receives provider secrets,
# deployment identities, ACME state, or certificate private keys.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"
readonly env_file="${controller_env}"
readonly mode="${1:-}"
readonly container="${HQ_CONTAINER:-severino-hq}"
readonly acme_dir="${app_dir}/secrets/acme"
readonly app_env="${app_dir}/secrets/severino_hq_env"

if [ "$(id -u)" -ne 0 ]; then
    echo "run-controller.sh must run as root." >&2
    exit 1
fi
controller_require_environment
if [ ! -s "${app_env}" ]; then
    echo "Controller application environment is missing." >&2
    exit 1
fi

install -d -o root -g root -m 0700 "${acme_dir}"
# The whole tree, every run, and not only the directory. Certbot saves a renewal
# by copying the previous key's owner onto the new one, and the controller runs
# as 10001 without CAP_CHOWN, so a single file left with another group fails the
# save: after the CA has issued. Declared here, where root owns the step, so
# anything that re-owned the tree in between is put back before it matters.
# -h: symlinks themselves, never what they point at.
chown -R -h 10001:10001 "${acme_dir}"
# Everything this run hands the container is staged in one directory on the
# controller's secret mount (the renderer's tmpfs, kept out of swap) and
# removed when the run ends. A run
# killed before its trap fired leaves a directory; the next run clears those.
find "${controller_runtime_dir}" -mindepth 1 -maxdepth 1 -type d -name 'run.*' \
    -mmin +120 -exec rm -rf {} +
run_dir="$(mktemp -d "${controller_runtime_dir}/run.XXXXXX")"
trap 'rm -rf "${run_dir}"' EXIT
trap 'exit 1' HUP INT TERM
runtime_app_env="${run_dir}/env"
runtime_ssh_dir="${run_dir}/ssh"
# The roots this host added to its own trust store, as one bundle. Public roots
# are always trusted; these only add to them, and a host with none mounts none.
ca_file="${run_dir}/ca.pem"
for root_cert in /usr/local/share/ca-certificates/*.crt; do
    [ -f "${root_cert}" ] && cat "${root_cert}"
done > "${ca_file}"
chmod 0444 "${ca_file}"
runtime_tailnet="${run_dir}/tailnet.json"
runtime_tailnet_lock="${run_dir}/tailnet-lock.json"
runtime_firewall="${run_dir}/firewall.json"
install -o root -g root -m 0400 "${app_env}" "${runtime_app_env}"
chown 10001:10001 "${runtime_app_env}"
# The identities refresh-secrets.sh rendered, copied under the shared lock so
# the run holds one generation even if a refresh replaces it meanwhile.
install -d -m 0700 "${runtime_ssh_dir}"
controller_ssh_lock shared
if [ -d "${controller_runtime_dir}/ssh" ]; then
    cp -a "${controller_runtime_dir}/ssh/." "${runtime_ssh_dir}/"
fi
exec 8>&-
chown -R 10001:10001 "${runtime_ssh_dir}"
image="$(docker inspect --format '{{.Config.Image}}' "${container}")"
data_volume="$(
    docker inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
        "${container}"
)"
if [ -z "${image}" ] || [ -z "${data_volume}" ]; then
    echo "Could not resolve the deployed image or HQ data volume." >&2
    exit 1
fi

set -a
# Values are shell-quoted by render-controller-env.sh.
# shellcheck disable=SC1090
. "${env_file}"
set +a

# Labelled with a nonce for this run, and handed the same nonce, so the sweep
# can tell which of the containers it finds is itself. A fixed label is not
# enough: any container can set it and drop out of the sweep.
run_nonce="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
if [ -z "${run_nonce}" ]; then
    echo "Could not generate a run nonce." >&2
    exit 1
fi
set -- run --rm --network host --user 10001:10001 --cap-drop ALL \
    --no-healthcheck \
    --label severino-hq.role=controller \
    --label "severino-hq.run=${run_nonce}" \
    --env "HQ_CONTROLLER_RUN=${run_nonce}" \
    --security-opt no-new-privileges:true \
    --entrypoint python \
    --mount "type=volume,source=${data_volume},target=/data" \
    --mount "type=bind,source=${runtime_app_env},target=/run/secrets/severino_hq_env,readonly" \
    --mount "type=bind,source=${runtime_ssh_dir},target=/run/secrets/controller-ssh,readonly" \
    --mount "type=bind,source=${acme_dir},target=/var/lib/severino-hq/acme" \
    --env HQ_IN_PROCESS=1 \
    --env HQ_CONTROLLER_SSH_DIR=/run/secrets/controller-ssh \
    --env HQ_ACME_DIR=/var/lib/severino-hq/acme
if [ -s "${ca_file}" ]; then
    set -- "$@" \
        --mount "type=bind,source=${ca_file},target=/run/secrets/severino_controller_ca.pem,readonly" \
        --env HQ_CONTROLLER_CA_FILE=/run/secrets/severino_controller_ca.pem
fi

# The 1Password CLI, lent to the controller for the one run. A certificate that
# records its facts in a password manager reaches it through `op`, a binary
# rather than the HTTP call every other provider here makes.
#
# Lent rather than built into the image, because the image is shared with the
# web container: baking `op` in would hand a vault-reading tool to the one
# process that faces the internet, to serve a provider that runs nowhere near
# it. It also keeps a third-party binary out of a public image.
#
# The host's own copy is already trusted to render this machine's secrets, so
# it carries no provenance this machine had not already accepted. Statically
# linked, so the container's libc is not part of the bargain; read-only; and
# beside `--cap-drop ALL` and `no-new-privileges`, which leave its setgid bit
# inert.
#
# Missing, the mount is simply absent. Every other provider is unaffected and
# the 1Password one reports a publication it could not write, which is what a
# machine without the tool should say.
if op_binary="$(command -v op 2>/dev/null)"; then
    set -- "$@" \
        --mount "type=bind,source=${op_binary},target=/usr/bin/op,readonly"
fi

# The tailnet, read from the daemon this machine is already a peer of rather
# than from Tailscale's API, so there is no credential for the controller to
# hold.
#
# The answer is fetched here and passed in as a file. The socket itself is not
# mounted: the daemon's local API is read *and* write, with no read-only mode,
# so handing it to the container would let the process that holds every
# provider credential log this machine off the tailnet. It only ever needed the
# reading. Fetched as root, mounted read-only, owned by nobody the container
# can become.
#
# Written only when the daemon answers. A missing file is a controller that
# reports the tailnet as unreadable, which is what a machine that is not on one
# should say.
if [ -S /var/run/tailscale/tailscaled.sock ] \
    && curl -fsS --max-time 10 \
        --unix-socket /var/run/tailscale/tailscaled.sock \
        -H "Host: local-tailscaled.sock" \
        http://local-tailscaled.sock/localapi/v0/status \
        -o "${runtime_tailnet}" 2>/dev/null; then
    chown 10001:10001 "${runtime_tailnet}"
    chmod 0400 "${runtime_tailnet}"
    set -- "$@" \
        --mount "type=bind,source=${runtime_tailnet},target=/run/severino-hq/tailnet.json,readonly" \
        --env SEVERINO_TAILNET_STATUS=/run/severino-hq/tailnet.json

    # Tailnet lock, from the same socket and on the same terms. A separate
    # reading because it is a separate endpoint, and separately optional: a
    # tailnet without lock enabled answers it perfectly well, and a daemon too
    # old to know it should cost the sweep nothing.
    if curl -fsS --max-time 10 \
        --unix-socket /var/run/tailscale/tailscaled.sock \
        -H "Host: local-tailscaled.sock" \
        http://local-tailscaled.sock/localapi/v0/tka/status \
        -o "${runtime_tailnet_lock}" 2>/dev/null; then
        chown 10001:10001 "${runtime_tailnet_lock}"
        chmod 0400 "${runtime_tailnet_lock}"
        set -- "$@" \
            --mount "type=bind,source=${runtime_tailnet_lock},target=/run/severino-hq/tailnet-lock.json,readonly" \
            --env SEVERINO_TAILNET_LOCK=/run/severino-hq/tailnet-lock.json
    fi
fi

# Whether the firewall requires this machine's port to be reached over the
# tailnet interface, rather than merely from an address that claims to be on it.
# A source address is a field in a packet; an interface is where the packet
# actually arrived, and only the second is something a sender cannot assert.
#
# Distilled here rather than mounted: the answer is one boolean and the rule
# behind it, where the full ruleset is a map of every way into this machine, and
# the container asking the question holds every provider credential. Same terms
# as the tailnet socket above: root reads, the container receives a reading.
#
# Absent when nft is missing or the table is not there, which is a controller
# that reports the binding as unobserved. That is the honest answer on a host
# that does not run this firewall, and it is not the same as reporting it open.
if command -v nft >/dev/null 2>&1 \
    && firewall_chain="$(nft list chain inet host_filter input 2>/dev/null)"; then
    bound=false
    guarded=false
    case "${firewall_chain}" in
        *'iifname "tailscale0"'*'dport'*'accept'*) bound=true ;;
    esac
    case "${firewall_chain}" in
        *'iifname != "tailscale0"'*'drop'*) guarded=true ;;
    esac
    printf '{"record":"interface-binding","interface":"tailscale0",' \
        > "${runtime_firewall}"
    printf '"accept_requires_interface":%s,"foreign_interface_dropped":%s,' \
        "${bound}" "${guarded}" >> "${runtime_firewall}"
    printf '"read_at":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        >> "${runtime_firewall}"
    chown 10001:10001 "${runtime_firewall}"
    chmod 0400 "${runtime_firewall}"
    set -- "$@" \
        --mount "type=bind,source=${runtime_firewall},target=/run/severino-hq/firewall.json,readonly" \
        --env SEVERINO_HOST_FIREWALL=/run/severino-hq/firewall.json
fi

# Forward what the renderer produced, rather than recomputing the same names
# from a registry. The registry holds the shape a connection can take; which
# connections exist is the vault's to say, so a list rebuilt here is a second
# answer to a question this file cannot see.
#
# The names only: `--env NAME` passes the value already sourced above, so no
# secret reaches the process table.
while IFS= read -r env_name; do
    [ -n "${env_name}" ] || continue
    set -- "$@" --env "${env_name}"
done <<EOF
$(sed -nE 's/^([A-Z][A-Z0-9_]*)=.*/\1/p' "${env_file}")
EOF
set -- "$@" "${image}" -m controller_runtime.worker
if [ "${mode}" = "--apply" ]; then
    set -- "$@" --apply
fi

docker "$@"
