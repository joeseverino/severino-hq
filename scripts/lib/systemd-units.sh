# shellcheck shell=sh
# The systemd files this repository ships, derived in one place.
#
# The installer that puts them on the host and the check that says whether the
# host still has them agree on what "them" is by asking these functions; no list
# is kept anywhere, so a file added to deploy/systemd is in both at once.

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

# Print each unit file in $2 that carries this repository's prefix and is not
# shipped in $1: what an earlier release installed and this one has dropped.
# Left in place it would keep running what the release no longer ships. Top
# level only; drop-ins beside a shipped unit are the host's to own.
units_retired() {
    shipped="$(units_shipped "$1")" || return 1
    for installed in "$2"/severino-hq-*; do
        [ -f "${installed}" ] || continue
        f="${installed##*/}"
        case "${f}" in
            *.service | *.socket | *.mount | *.automount | *.swap | *.target | \
                *.path | *.timer | *.slice) ;;
            *) continue ;;
        esac
        printf '%s\n' "${shipped}" | grep -Fxq -- "${f}" || printf '%s\n' "${f}"
    done
}

# Print, sorted, every unit of $1 that systemd can be asked about by name: each
# shipped unit that is not a template, and each instance of a shipped template
# that a shipped file starts (a timer's or path's `Unit=`, a unit's
# `OnFailure=`). A template has no state of its own, only its instances do, and
# the files that start them are where their names are written.
#
# A name is kept only when it is made of the characters a unit name is made of,
# so nothing a file holds reaches a command line as anything else.
units_reported() {
    shipped="$(units_shipped "$1")" || return 1
    {
        for f in ${shipped}; do
            case "${f}" in
                */* | *@.*) ;;
                *) printf '%s\n' "${f}" ;;
            esac
        done
        for f in ${shipped}; do
            sed -n -e 's/^Unit=//p' -e 's/^OnFailure=//p' "$1/${f}"
        done | tr -s '[:blank:]' '\n' | while IFS= read -r started; do
            case "${started}" in
                *[!A-Za-z0-9:_.@-]* | *@.* | @*) continue ;;
                *@*) ;;
                *) continue ;;
            esac
            template="${started%%@*}@.${started##*.}"
            printf '%s\n' "${shipped}" | grep -Fxq -- "${template}" &&
                printf '%s\n' "${started}"
        done
    } | LC_ALL=C sort -u
}

# What is asked of systemd about each reported unit, as `systemctl show` names
# the properties. States, the last run's result and exit, and instants: no
# property here holds a command line, an environment or a path. The controller
# keeps exactly these (hostUnitProperties in controller/providers/host_units.go).
#
# Id is the unit's name. LoadState says whether systemd holds a configuration
# for it and UnitFileState whether it is enabled. ActiveState and SubState are
# its state now. Result, ExecMainCode and ExecMainStatus are how the last run
# ended; InactiveExitTimestamp and InactiveEnterTimestamp are when it started
# and ended. ConditionResult and ConditionTimestamp tell a start that was
# skipped from one that ran. LastTriggerUSec and NextElapseUSecRealtime are when
# a timer last fired and is next due, and Unit is what it starts.
readonly units_properties='Id,LoadState,UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,InactiveExitTimestamp,InactiveEnterTimestamp,ConditionResult,ConditionTimestamp,LastTriggerUSec,NextElapseUSecRealtime,Unit'

# Print what systemd says of every reported unit of $1: one block of
# Property=value lines per unit, an empty line between blocks. A unit that is
# not installed is answered too, as not-found, so its absence is a fact and not
# a gap. Instants are seconds since the epoch, and in UTC where a systemctl
# prints them as dates.
units_state() {
    reported="$(units_reported "$1")" || return 1
    [ -n "${reported}" ] || return 1
    # shellcheck disable=SC2086  # the unit list is meant to split
    TZ=UTC systemctl show --timestamp=unix --property="${units_properties}" -- ${reported}
}
