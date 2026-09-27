# shellcheck shell=sh
# The deploy checkout's git metadata belongs to the account that pulls it.
#
# A file under .git owned by anyone else (root, after a git command run through
# sudo) makes the deploy's `git pull` fail. The deploy job refuses before it
# pulls, and the daily drift check and scripts/preflight.sh ask the same
# question through these functions.

# Print the account that owns directory $1: the one the deploy pulls as.
checkout_deploy_account() {
    # The account the Actions runner service runs as: the one whose git pull
    # the deploy makes. One runner unit, and never root.
    units="$(systemctl list-units --all --plain --no-legend 'actions.runner.*.service' 2>/dev/null \
        | awk '{print $1}')"
    if [ "$(printf '%s\n' "${units}" | grep -c .)" -ne 1 ]; then
        echo "Expected one Actions runner unit, found: $(printf '%s' "${units:-none}" | tr '\n' ' ')." >&2
        return 1
    fi
    account="$(systemctl show -p User --value "${units}" 2>/dev/null)"
    case "${account}" in
        '' | root)
            echo "The Actions runner runs as '${account:-root}'; it must run as the deploy account." >&2
            return 1 ;;
    esac
    printf '%s\n' "${account}"
}

# Print up to ten paths under $1/.git not owned by $2. Fails when there is no
# .git, or when find cannot answer (an unknown account, an unreadable
# directory): an empty answer would read as "all owned correctly".
checkout_foreign_owned() {
    if [ ! -d "$1/.git" ]; then
        echo "No git checkout at $1." >&2
        return 1
    fi
    if ! foreign="$(find "$1/.git" ! -user "$2" -print)"; then
        echo "Could not check ownership under $1/.git." >&2
        return 1
    fi
    [ -z "${foreign}" ] || printf '%s\n' "${foreign}" | head -10
}

# The command that returns ownership of $1/.git to $2.
checkout_ownership_fix() {
    printf 'sudo chown -R %s: %s/.git </dev/null\n' "$2" "$1"
}
