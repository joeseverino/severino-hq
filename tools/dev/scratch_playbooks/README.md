# The scratch server's playbooks

What the automation worker beside `mise run dev:scratch --runner` scans and
runs. Each start commits them to a repository under the scratch state, which
the worker reads as it reads its repository on GitHub. Every play sets its
connection to local, so a run acts on this machine alone.
