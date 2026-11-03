#!/usr/bin/env python
"""Django management entrypoint for Severino HQ."""

import os
import sys

lazy from django.core.management import execute_from_command_line

from hq.config.imports import lazily
lazy from hq.platform.core.attribution import command_operation, from_the_shell


def main():
    lazily()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    # Every command starts here, so this is where the shell's door binds who acted.
    with from_the_shell(command_operation(sys.argv)):
        execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
