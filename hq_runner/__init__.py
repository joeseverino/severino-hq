"""HQ's automation worker: it claims a run of a reviewed procedure from HQ over
the bridge, holds the run's lease while ansible-runner runs it, and reports a
bounded, redacted outcome.

It imports nothing of HQ (lint:imports) and ships in an image of its own, never
the web image: the web process holds no playbook, inventory or machine
credential. How a run is claimed, held and reported: docs/Architecture.md,
"The automation worker".
"""
