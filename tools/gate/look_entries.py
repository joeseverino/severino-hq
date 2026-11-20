"""The catalog entries fast:look draws: those the sheets and templates it is
handed reach (hq/platform/core/tests/look_reach.py), less each file that
reaches every entry, whose look is the whole catalog and so CI's Browser job.

Prints each entry on a line of its own, and why on stderr."""

import os
import sys


def main(paths: list[str]) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    import django

    django.setup()
    from django.test.utils import setup_test_environment

    # The test environment announces each template a page renders, which a template's reach reads.
    setup_test_environment()
    from hq.platform.core.tests import look_reach

    every = len(look_reach.drawings())
    entries: list[str] = []
    for path in paths:
        reach = look_reach.reach(path)
        if every and len(reach.entries) == every:
            print(f"fast:look: {path} reaches every catalog entry, so CI's Browser job looks at it", file=sys.stderr)
            continue
        print(f"fast:look: {reach.said}", file=sys.stderr)
        entries += reach.entries
    print("\n".join(dict.fromkeys(entries)))


if __name__ == "__main__":
    main(sys.argv[1:])
