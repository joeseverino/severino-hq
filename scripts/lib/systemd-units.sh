# shellcheck shell=sh
# The systemd files this repository ships, derived in one place.
#
# The installer that puts them on the host and the check that says whether the
# host still has them must agree on what "them" is. Each once had its own idea:
# the installer a list written by hand, the check three unit names. Neither saw
# the files added after it was written, so those reached the host once or never
# and their drift was reported by nothing.

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

# Install shipped file $3 from directory $1 into systemd directory $2, root-owned
# and not writable by anyone else, creating its drop-in directory if it has one.
units_install() {
    case "$3" in
        */*) install -d -o root -g root -m 0755 "$2/${3%/*}" ;;
    esac
    install -o root -g root -m 0644 "$1/$3" "$2/$3"
}

# Print each file shipped in $1 whose copy in $2 is missing or not byte-identical.
#
# Only the shipped set is compared. A drop-in the host adds beside a shipped one
# (its own naming, a credential mount another repository installs) is the
# host's to own, and reporting it would train whoever reads this to ignore it.
units_drifted() {
    shipped="$(units_shipped "$1")" || return 1
    for f in ${shipped}; do
        cmp -s "$1/${f}" "$2/${f}" || printf '%s\n' "${f}"
    done
}
