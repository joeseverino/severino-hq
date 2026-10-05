#!/bin/sh
# Execute the controller in a short-lived, isolated container made from the
# exact running HQ image. The web container never receives provider secrets,
# deployment identities, ACME state, or certificate private keys; the
# controller's container never receives HQ's database or its application
# environment, and reaches HQ only through the bridge socket.

set -eu

readonly app_dir="${SEVERINO_HQ_APP_DIR:-/opt/apps/severino-hq}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=scripts/lib/controller-env.sh
. "${script_dir}/lib/controller-env.sh"
# shellcheck source=scripts/lib/systemd-units.sh
. "${script_dir}/lib/systemd-units.sh"
readonly mode="${1:-}"
readonly container="${HQ_CONTAINER:-severino-hq}"
readonly acme_dir="${app_dir}/secrets/acme"

if [ "$(id -u)" -ne 0 ]; then
    echo "run-controller.sh must run as root." >&2
    exit 1
fi
controller_require_connections

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
runtime_ssh_dir="${run_dir}/ssh"
runtime_connections="${run_dir}/connections.json"
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
# The connections document and the identities hq-secrets rendered, copied
# under the shared lock so the run holds one generation even if a refresh
# replaces it meanwhile. The document is the controller account's own private
# file, which is the only kind the controller reads.
install -d -m 0700 "${runtime_ssh_dir}"
controller_ssh_lock shared
controller_require_connections
install -o root -g root -m 0400 "${controller_connections}" "${runtime_connections}"
if [ -d "${controller_runtime_dir}/ssh" ]; then
    cp -a "${controller_runtime_dir}/ssh/." "${runtime_ssh_dir}/"
fi
exec 8>&-
chown 10001:10001 "${runtime_connections}"
chown -R 10001:10001 "${runtime_ssh_dir}"
image="$(docker inspect --format '{{.Config.Image}}' "${container}")"
if [ -z "${image}" ]; then
    echo "Could not resolve the deployed image." >&2
    exit 1
fi
# Which repository delivers this image, from its standard OCI source label.
# Blank for an image built without it; delivery then has nothing to follow.
source_label="$(docker inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.source"}}' "${image}" 2>/dev/null || true)"
case "${source_label}" in
    https://github.com/*/*) source_repository="${source_label#https://github.com/}" ;;
    *) source_repository="" ;;
esac
case "${source_repository}" in
    */*/* | *[!A-Za-z0-9_./-]*) source_repository="" ;;
esac

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
    --tmpfs /tmp:size=64m,noexec,nosuid,nodev \
    --entrypoint /usr/local/bin/hq-controller \
    --mount "type=bind,source=${runtime_connections},target=/run/secrets/controller-connections.json,readonly" \
    --mount "type=bind,source=${runtime_ssh_dir},target=/run/secrets/controller-ssh,readonly" \
    --mount "type=bind,source=${acme_dir},target=/var/lib/severino-hq/acme" \
    --env HQ_CONTROLLER_CONNECTIONS=/run/secrets/controller-connections.json \
    --env HQ_CONTROLLER_SSH_DIR=/run/secrets/controller-ssh \
    --env HQ_ACME_DIR=/var/lib/severino-hq/acme \
    --env "HQ_CONTROLLER_IMAGE=${image}" \
    --env "SEVERINO_HQ_SOURCE_REPOSITORY=${source_repository}"
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
# The host's own copy is the operator's install, so it carries no provenance
# this machine had not already accepted. Statically
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

# HQ's bridge: the Unix socket the running web process serves the controller
# contract on, and the only way the controller reaches HQ. The path is the web
# container's own setting and its directory is the volume mounted there, so
# both are read from that container and neither is restated here. Mounted
# read-only: the controller connects to the socket, and can neither replace it
# nor leave anything beside it. A web container that serves no bridge stops the
# run; nothing else is tried in its place.
bridge_socket="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${container}" \
    | sed -n 's/^SEVERINO_BRIDGE_SOCKET=//p' | tail -n 1)"
case "${bridge_socket}" in
    *[!A-Za-z0-9_./-]* | *//* | */./* | */../* | */)
        echo "HQ's bridge socket is not a plain path." >&2
        exit 1 ;;
    /*/*) ;;
    *)
        echo "HQ serves no controller bridge: ${container} names no socket." >&2
        exit 1 ;;
esac
bridge_dir="${bridge_socket%/*}"
bridge_volume="$(docker inspect --format \
    '{{range .Mounts}}{{if eq .Type "volume"}}{{.Destination}} {{.Name}}{{println}}{{end}}{{end}}' \
    "${container}" | awk -v dir="${bridge_dir}" '$1 == dir { print $2 }')"
case "${bridge_volume}" in
    "" | *[!A-Za-z0-9_.-]*)
        echo "HQ's bridge directory is not a volume of ${container}." >&2
        exit 1 ;;
esac
set -- "$@" \
    --mount "type=volume,source=${bridge_volume},target=${bridge_dir},readonly" \
    --env "SEVERINO_BRIDGE_SOCKET=${bridge_socket}"

# What each secret renderer on this machine says about its own runs, so HQ can
# tell fresh secrets from a renderer that keeps failing while the files it left
# keep everything running. A status document holds times, versions, counts and
# short words, and no secret (docs/SECRETS.md). It sits in the root-only
# directory beside the secrets, so a copy is mounted and never the directory.
#
# One line per renderer, name=document. The name is a fixed word chosen here
# and is all HQ is told of where a document came from. Every name is passed
# whether or not its document exists: a renderer that left none is a finding
# in HQ, where an unnamed one would be silence.
render_status=""
while IFS='=' read -r renderer_name renderer_document; do
    renderer_target="/run/severino-hq/render-status/${renderer_name}.json"
    render_status="${render_status}${render_status:+,}${renderer_name}=${renderer_target}"
    if [ -f "${renderer_document}" ] && [ ! -L "${renderer_document}" ]; then
        renderer_copy="${run_dir}/render-status-${renderer_name}.json"
        install -o 10001 -g 10001 -m 0400 "${renderer_document}" "${renderer_copy}"
        set -- "$@" \
            --mount "type=bind,source=${renderer_copy},target=${renderer_target},readonly"
    fi
done <<EOF
hq=${controller_runtime_dir}/status.json
EOF
set -- "$@" --env "SEVERINO_RENDER_STATUS=${render_status}"

# The state of the units this repository ships, as systemd holds it, so a unit
# that failed, a timer that stopped and a unit that was never installed reach
# HQ instead of staying in this machine's journal. The units are the ones under
# deploy/systemd beside this script, in the tree root owns, and the question
# is a fixed list of properties (scripts/lib/systemd-units.sh): states,
# results and instants, and nothing a unit runs or is given.
#
# Asked here and passed in as a file, on the same terms as the readings above:
# the manager's socket is read and write, so the container is handed the
# answer and never the socket. The reading is named whether or not systemd
# answered, so a machine where it did not is a reading HQ could not take, and
# says so.
runtime_units="${run_dir}/units"
if units_state "${script_dir}/../deploy/systemd" > "${runtime_units}" 2>/dev/null \
    && [ -s "${runtime_units}" ]; then
    chown 10001:10001 "${runtime_units}"
    chmod 0400 "${runtime_units}"
    set -- "$@" \
        --mount "type=bind,source=${runtime_units},target=/run/severino-hq/units,readonly"
fi
set -- "$@" --env SEVERINO_HOST_UNITS=/run/severino-hq/units

# No connection reaches the container's environment: Docker writes a
# container's resolved environment to disk and shows it in `docker inspect`.
# The controller reads them from the document mounted above, by the path
# passed in HQ_CONTROLLER_CONNECTIONS. Every --env here is a path, a name or a
# nonce.
set -- "$@" "${image}"
if [ "${mode}" = "--apply" ]; then
    set -- "$@" --apply
fi

docker "$@"
