#!/usr/bin/env python
"""Django management entrypoint for Severino HQ."""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Activate your virtualenv and run `uv sync --locked --no-default-groups`."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
