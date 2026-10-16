# shellcheck shell=sh
# The systemd files this repository ships, derived in one place.
#
# hq-host derives the same set (Shipped in controller/host/units.go); the
# preflight asks this one from the operator's machine, where no hq-host runs.

# Print, relative to $1 and sorted, every file under it that systemd would read:
# a unit file at the top level, or a `.conf` drop-in one directory down. That is
# systemd's own rule, not a list, so a unit added to the repository is in the set
# without anything here changing. Templates (`*.example`) are left out by the
# same rule: a person copies one into place after replacing its values.
units_shipped() {
    if [ ! -d "$1" ]; then
        # An empty answer would read as "nothing is shipped", which installs
        # nothing and reports no drift. Missing is not the same as none.
        echo "No unit directory at $1." >&2
        return 1
    fi
    (cd "$1" && find . -type f) | sed 's|^\./||' | while IFS= read -r f; do
        case "${f}" in
            */*/*) ;;
            *.d/*.conf) printf '%s\n' "${f}" ;;
            */*) ;;
            *.service | *.socket | *.mount | *.automount | *.swap | *.target | \
                *.path | *.timer | *.slice) printf '%s\n' "${f}" ;;
        esac
    done | LC_ALL=C sort
}
