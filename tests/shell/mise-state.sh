# Sourced by a suite that runs mise in a project of its own. mise records each
# config it trusts or loads in its state directory, and its reshim walks every
# record there, so a suite's throwaway project is trusted through the
# environment and its records are kept in a state directory of the suite's own.
# The repository's own config is trusted only in the real state, so a command
# on it runs before this is called, or in a subshell without it.
#   mise_trust <project> <state>
mise_trust() {
    MISE_TRUSTED_CONFIG_PATHS="$1"
    MISE_STATE_DIR="$2"
    export MISE_TRUSTED_CONFIG_PATHS MISE_STATE_DIR
}
