# The development servers' playbooks

What the automation worker beside `mise run dev`, and beside
`mise run dev:scratch --runner`, scans and runs. Each start commits them to a
repository beside the server's database, which the worker reads as it reads
its repository on GitHub. Every play sets its connection to local, so a run
acts on this machine alone.
