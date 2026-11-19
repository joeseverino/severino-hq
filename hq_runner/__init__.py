"""HQ's automation worker: it claims a run of a reviewed procedure over the
bridge, holds its lease while ansible-runner runs it, and reports a redacted
outcome.

It imports nothing of HQ and ships in its own image, so the web process holds
no playbook, inventory or machine credential (docs/Architecture.md, "The
automation worker").
"""
