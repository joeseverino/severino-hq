# shellcheck shell=sh
# The automation worker's environment on this machine, sourced from the
# checkout's root in one line:
#   . ./tools/lib/runner-environment.sh
# It is var/runner/venv, the runner group's, which smoke:runner and the worker
# beside a development server share, so the checkout's .venv keeps the default
# groups CI tests with. The collections hq_runner/collections.yml pins install
# into it, where the worker looks for them (hq_runner/ansible.py, COLLECTIONS),
# as the image's runner-build stage installs them; a pin already installed is
# left as it is.
runner_venv=var/runner/venv
runner_collections=$runner_venv/share/ansible/collections
UV_PROJECT_ENVIRONMENT=$runner_venv uv sync --inexact --group runner --quiet
ANSIBLE_HOME=var/runner/ansible ANSIBLE_COLLECTIONS_PATH=$runner_collections \
    "$runner_venv/bin/ansible-galaxy" collection install \
    --requirements-file hq_runner/collections.yml --collections-path "$runner_collections" > /dev/null
