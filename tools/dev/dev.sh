#!/bin/sh
# A development server in the profile its mise task names (HQ_DEV_PROFILE):
# `secure` for `mise run dev`, `scratch` for `mise run dev:scratch`. The task's
# env says where it listens and what it opens; this refuses what env cannot,
# then serves the production ASGI path with reload.
set -eu
unset CDPATH

root=$(cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"

die() {
    echo "$*" >&2
    exit 2
}

profile=${HQ_DEV_PROFILE:?run it as mise run dev or mise run dev:scratch}
host=${UVICORN_HOST:?}
port=${usage_port:-${UVICORN_PORT:?}}
database=${SEVERINO_DATABASE_PATH:?}

# How far an address reaches. Tailscale assigns from 100.64.0.0/10.
reach() {
    case "$1" in
        127.* | ::1 | localhost) echo loopback ;;
        100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].*) echo tailnet ;;
        *) echo wide ;;
    esac
}

case "$profile" in
    secure)
        [ "$(reach "$host")" != wide ] || die "Refusing to listen on '$host': it reaches past loopback and the tailnet."
        [ "${DJANGO_DEBUG:-}" = 0 ] || die "The proxied server never runs with DEBUG."
        [ "${DJANGO_BEHIND_TLS_PROXY:-}" = 1 ] || die "The proxied server runs behind its TLS proxy."
        [ "${SEVERINO_OIDC_ENABLED:-}" = 1 ] || die "The proxied server signs in through single sign-on."
        ;;
    scratch)
        [ "$(reach "$host")" = loopback ] || die "A scratch server listens on loopback only, not '$host'."
        [ "${DJANGO_DEBUG:-}" = 1 ] || die "A scratch server runs with DEBUG on."
        case "$database" in
            "$root"/var/dev/*) ;;
            *) die "A scratch database lives under this checkout's var/dev, not $database." ;;
        esac
        ;;
    *) die "Unknown profile: $profile" ;;
esac

# The checkout and each extension's sources are watched for changes, but for
# what this server never runs: other worktrees, the environment, var/. The
# list is split on spaces, so a path with one is refused rather than
# half-watched.
case "$root${PYTHONPATH:-}" in
    *' '*) die "The checkout or PYTHONPATH holds a path with a space, which the reload list cannot carry: $root:${PYTHONPATH:-}" ;;
esac
reload_dirs="$root"
for dir in $(printf '%s' "${PYTHONPATH:-}" | tr ':' ' '); do
    reload_dirs="$reload_dirs $dir"
done
reload_skipped="$root/.claude/worktrees $root/.venv $root/var"

if [ "${usage_plan:-false}" = true ]; then
    # Before any secret is read or any file touched, so the plan holds neither.
    env | grep -E '^(DJANGO_|SEVERINO_|UVICORN_|HQ_DEV_|PYTHONPATH=)' | grep -Ev '^[A-Z_]*(SECRET_KEY|_SECRET)=' | sort
    echo "serve: $host:$port, reloading on changes to Python and templates in $reload_dirs, but for $reload_skipped"
    exit 0
fi

if [ "$profile" = secure ] \
    && [ "$(git rev-parse --path-format=absolute --git-dir)" != "$(git rev-parse --path-format=absolute --git-common-dir)" ]; then
    die "The proxied server runs from the main checkout, never a linked worktree. Use mise run dev:scratch."
fi

# A server already on this database would lose it to --fresh or a new scratch
# run, and two servers on one database answer as one estate that is two.
pidfile="$database.pid"
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    die "A server already runs on $database (pid $(cat "$pidfile")). Stop it first."
fi

state=$(dirname -- "$database")
if [ "$profile" = scratch ] && [ "${usage_keep:-false}" != true ]; then
    rm -rf -- "$state"
elif [ "${usage_fresh:-false}" = true ]; then
    rm -f -- "$database" "$database-wal" "$database-shm"
fi
mkdir -p "$state"
echo $$ > "$pidfile"

if [ "$profile" = secure ]; then
    # HQ_DEV_OP is the reading command with its arguments, such as `op`.
    # shellcheck disable=SC2086
    DJANGO_SECRET_KEY=$($HQ_DEV_OP read "${HQ_DEV_SECRET_REF:?}")
    # shellcheck disable=SC2086
    SEVERINO_OIDC_CLIENT_SECRET=$($HQ_DEV_OP read "${HQ_DEV_OIDC_SECRET_REF:?}")
    export DJANGO_SECRET_KEY SEVERINO_OIDC_CLIENT_SECRET
elif [ ! -f "$state/secret.env" ]; then
    # A throwaway key in the file the scratch tasks load, so a session
    # `mise run dev:session` mints validates here. Worth nothing past this database.
    (umask 077 && printf 'DJANGO_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" > "$state/secret.env")
    DJANGO_SECRET_KEY=$(sed -n 's/^DJANGO_SECRET_KEY=//p' "$state/secret.env")
    export DJANGO_SECRET_KEY
fi

if [ "$profile" = scratch ] && [ ! -f "$state/login" ]; then
    # A new database: migrated, its operator signed up and the data set
    # written in one process, with the settings the set is shown under.
    password=$(openssl rand -hex 12)
    DJANGO_SUPERUSER_PASSWORD=$password uv run python manage.py seed "${usage_seed:-empty}" \
        --owner operator --env-file "$state/seed.env" --verbosity 0
    (umask 077 && echo "operator $password" > "$state/login")
else
    [ -z "${usage_seed:-}" ] || die "--seed fills a new database, and --keep keeps the last one as it was seeded."
    uv run python manage.py migrate --noinput --verbosity 0
fi
if [ "$profile" = scratch ]; then
    set -a
    . "$state/seed.env"
    set +a
    echo "Sign in as: $(cat "$state/login")" >&2
fi

# Uvicorn watches with watchfiles (the dev group), the reloader that reads
# --reload-include and --reload-exclude. A template edit restarts it too: with
# DEBUG off, as `dev` runs, templates are cached until the server restarts.
set -- --reload --reload-include '*.html'
for dir in $reload_dirs; do
    set -- "$@" --reload-dir "$dir"
done
# Uvicorn reads an exclude that is no directory as a pattern, which an
# absolute path cannot be.
for dir in $reload_skipped; do
    if [ -d "$dir" ]; then
        set -- "$@" --reload-exclude "$dir"
    fi
done
exec uv run python -m uvicorn hq.config.asgi:application --host "$host" --port "$port" "$@"
