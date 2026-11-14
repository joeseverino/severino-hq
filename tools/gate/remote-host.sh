#!/bin/sh
# The second machine's half of a gate run there, sent over SSH by
# tools/gate/remote.py (docs/Development.md, Gates on a second machine).
#   prepare <checkout> <tree> <commit> [<extension checkout> <tree> <commit>]
#     makes each tree's worktree, from the commit pushed as refs/gate/<tree>,
#     and lays the archive on its input over it; prints the worktree and $HOME.
#   run <tree> <extension tree> <packages> <command...>
#     runs the command in the tree's worktree, the extension set composed from
#     the extension tree's packages, until it ends or the input closes; fails
#     a run that left the worktree holding a tree other than the one sent.
#   back <tree>
#     archives what the last run wrote there, and fast's pass and map of it.
# A worktree untouched for a day is removed as the next one is made.
set -eu
step=$1
shift
gates="${XDG_CACHE_HOME:-$HOME/.cache}/hq-gates"
# mise trusts the worktrees where they live and keeps its records in this
# machine's own state, which holds the heavy turn every run here shares.
MISE_TRUSTED_CONFIG_PATHS=$gates
export MISE_TRUSTED_CONFIG_PATHS
# Files are made as the image's build, CI's runner and the Mac make them; an
# Ubuntu login's 002 would leave every written file group-writable.
umask 022

# A detached worktree of <commit> at <worktree>, holding the gitignored files
# .worktreeinclude names as the checkout has them, as Claude Code makes one.
# One made before holds exactly <commit> again, whatever an earlier run wrote
# there: the tree a run judges is the tree sent. Ignored files stay (the
# environment, caches, the .worktreeinclude copies, var/).
worktree() { # <checkout> <tree> <commit> <worktree>
    if [ -d "$4" ]; then
        git -C "$4" reset --quiet --hard "$3"
        git -C "$4" clean -d --force --quiet
    else
        git -C "$1" worktree add --quiet --detach "$4" "$3"
        sed -n 's|/$||; /^[^#]/p' "$1/.worktreeinclude" 2>/dev/null | while IFS= read -r kept; do
            [ ! -e "$1/$kept" ] || cp -R "$1/$kept" "$4/$kept"
        done
    fi
    touch "$4"
    # The worktree holds the commit now; a ref left behind would keep every tree ever sent.
    for ref in "$2" "$2-base" "$2-at"; do
        git -C "$1" update-ref -d "refs/gate/$ref"
    done
}

case "$step" in
    prepare)
        mkdir -p "$gates" "$1/.git/severino"
        find "$gates" -path "$gates/*" -prune -type d -mtime +0 -exec rm -rf {} +
        git -C "$1" worktree prune
        worktree "$1" "$2" "$3" "$gates/$2"
        if [ "$#" -gt 3 ]; then
            git -C "$4" worktree prune
            worktree "$4" "$5" "$6" "$gates/ext-$5"
        fi
        # Every dependency group, as a checkout there has them once its browser gates have run.
        if [ -f "$gates/$2/uv.lock" ] && [ ! -d "$gates/$2/.venv" ]; then
            (cd "$gates/$2" && mise exec -- uv sync --locked --all-groups --quiet)
        fi
        tar -xf - -C "$gates/$2"
        printf "%s\n%s\n" "$gates/$2" "$HOME"
        ;;
    run)
        cd "$gates/$1"
        sent=$1 extension=$2 packages=$3
        shift 3
        mkdir -p var
        : >var/remote-stamp
        if [ -n "$extension" ]; then
            joined=""
            IFS=:
            for package in $packages; do
                joined="${joined:+$joined:}$gates/ext-$extension/$package"
            done
            unset IFS
            HQ_LOCAL_PYTHONPATH=$joined
            export HQ_LOCAL_PYTHONPATH
        fi
        # Its own session, so a closed input stops every process it started.
        # Command lines in full, as the sending machine prints them: with no
        # terminal, mise here cuts them at 80 columns.
        exec 3<&0
        HQ_GATE_HERE=1 HQ_GATE_SENT=$sent MISE_TASK_SHOW_FULL_CMD=1 PYTHONUNBUFFERED=1 setsid "$@" </dev/null &
        run=$!
        { cat >/dev/null; kill -s TERM -- "-$run"; } <&3 >/dev/null 2>&1 &
        watcher=$!
        status=0
        wait "$run" || status=$?
        kill "$watcher" 2>/dev/null || true
        # A run judges the tree it was sent and only checks it, so one that
        # left the worktree holding another tree fails, naming what changed.
        index=$(mktemp)
        rm -f "$index"
        [ ! -f "$(git rev-parse --git-path index)" ] || cp "$(git rev-parse --git-path index)" "$index"
        GIT_INDEX_FILE=$index git add --all
        left=$(GIT_INDEX_FILE=$index git write-tree)
        rm -f "$index"
        if [ "$left" != "$sent" ]; then
            echo "The run changed the tree it was sent, ${sent}, to ${left}, writing:"
            git diff --name-status "$sent" "$left"
            [ "$status" -ne 0 ] || status=1
        fi
        exit "$status"
        ;;
    back)
        cd "$gates/$1"
        severino="$(git rev-parse --path-format=absolute --git-common-dir)/severino"
        rm -rf var/back
        for kept in "fast-passes/$1" "fast-maps/$1.json"; do
            if [ -n "$(find "$severino/$kept" -newer var/remote-stamp 2>/dev/null)" ]; then
                mkdir -p "var/back/${kept%/*}"
                cp "$severino/$kept" "var/back/$kept"
            fi
        done
        find var/gate var/look var/back -type f -newer var/remote-stamp >var/remote-written 2>/dev/null || true
        tar -cf - -T var/remote-written
        ;;
    *)
        echo "remote-host.sh: no step $step" >&2
        exit 64
        ;;
esac
