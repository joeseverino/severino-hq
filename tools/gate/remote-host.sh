#!/bin/sh
# The second machine's half of a gate run there, sent over SSH by
# tools/gate/remote.py (docs/Development.md, Gates on a second machine).
#   stage <id>
#     keeps the archive on its input (files no commit holds) for run <id>.
#   run <id> <key> <checkout> <tree> <commit> <extension checkout> <commit> <packages> <command...>
#     takes the first free worktree of the sending checkout's (<key>-1,
#     <key>-2, ...), holding it until the run ends, so a run never shares one;
#     resets it to the commit pushed as refs/gate/<id>, with what run <id>
#     staged laid over it and the extension commit's beside it when one is named
#     (- names none); prints the worktree and $HOME on its first line; runs the
#     command there, the extension set composed from the extension commit's
#     packages, until it ends or the input closes; fails a run that left the
#     worktree holding a tree other than the one sent; and keeps what the run
#     wrote, with fast's pass and map of it, for back <id>.
#   back <id>
#     prints, then removes, what run <id> kept.
#   room <checkout>
#     runs the Python on its input (tools/gate/room.py) in <checkout>, which
#     prints this machine's free tokens of its heavy turn and its capacity.
# A worktree reused for each tree its checkout sends keeps its environment and
# caches warm; one untouched for a day is removed as the next run starts.
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
worktree() { # <checkout> <id> <commit> <worktree>
    if [ -d "$4" ]; then
        git -C "$4" reset --quiet --hard "$3"
        git -C "$4" clean -d --force --quiet
    else
        git -C "$1" worktree prune
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
    stage)
        mkdir -p "$gates/staged"
        cat >"$gates/staged/$1.tar"
        ;;
    run)
        id=$1 key=$2 checkout=$3 sent=$4 commit=$5 extension_checkout=$6 extension_commit=$7 packages=$8
        shift 8
        mkdir -p "$gates/back" "$gates/staged" "$checkout/.git/severino"
        find "$gates" -path "$gates/*" -prune -type d ! -name staged ! -name back -mtime +0 -exec rm -rf {} +
        find "$gates/staged" "$gates/back" -type f -mtime +0 -exec rm -f {} +
        # The first free worktree, its lock held on descriptor 9 until this
        # shell ends; the kernel frees it however the run ends.
        n=1
        while :; do
            slot="$gates/$key-$n"
            exec 9>"$slot.lock"
            if flock -n 9; then break; fi
            n=$((n + 1))
        done
        worktree "$checkout" "$id" "$commit" "$slot"
        if [ "$extension_checkout" != - ]; then
            worktree "$extension_checkout" "$id" "$extension_commit" "$slot.ext"
        fi
        cd "$slot"
        # Every dependency group, as a checkout there has them once its
        # browser gates have run; quick once the environment holds them.
        [ ! -f uv.lock ] || mise exec -- uv sync --locked --all-groups --quiet
        if [ -f "$gates/staged/$id.tar" ]; then
            tar -xf "$gates/staged/$id.tar"
            rm -f "$gates/staged/$id.tar"
        fi
        printf '%s %s\n' "$slot" "$HOME"
        mkdir -p var
        : >var/remote-stamp
        if [ "$extension_checkout" != - ]; then
            joined=""
            IFS=:
            for package in $packages; do
                joined="${joined:+$joined:}$slot.ext/$package"
            done
            unset IFS
            HQ_LOCAL_PYTHONPATH=$joined
            export HQ_LOCAL_PYTHONPATH
        fi
        # Its own session, so a closed input stops every process it started,
        # none of which holds the worktree's lock. Command lines in full, as
        # the sending machine prints them: with no terminal, mise here cuts
        # them at 80 columns.
        exec 3<&0
        HQ_GATE_HERE=1 HQ_GATE_SENT=$sent MISE_TASK_SHOW_FULL_CMD=1 PYTHONUNBUFFERED=1 setsid "$@" </dev/null 9>&- &
        run=$!
        { cat >/dev/null; kill -s TERM -- "-$run"; } <&3 >/dev/null 2>&1 9>&- &
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
        # What the run wrote, and fast's pass and map of the tree, while the
        # worktree is still this run's.
        severino="$(git rev-parse --path-format=absolute --git-common-dir)/severino"
        rm -rf var/back
        for kept in "fast-passes/$sent" "fast-maps/$sent.json"; do
            if [ -n "$(find "$severino/$kept" -newer var/remote-stamp 2>/dev/null)" ]; then
                mkdir -p "var/back/${kept%/*}"
                cp "$severino/$kept" "var/back/$kept"
            fi
        done
        find var/gate var/look var/back -type f -newer var/remote-stamp >var/remote-written 2>/dev/null || true
        tar -cf "$gates/back/$id.tar" -T var/remote-written || status=$((status ? status : 1))
        exit "$status"
        ;;
    back)
        cat "$gates/back/$1.tar" 2>/dev/null || true
        rm -f "$gates/back/$1.tar"
        ;;
    room)
        cd "$1"
        exec python3 -I -
        ;;
    *)
        echo "remote-host.sh: no step $step" >&2
        exit 64
        ;;
esac
