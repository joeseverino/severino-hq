"""`mise run audit:hand-rolled`: the page-class check's count, read-only.

The numbers are the ones test_host_templates_draw_only_the_shared_parts_classes
reads, from the same reader (hand_rolled_classes) held against the same table
(HAND_ROLLED); nothing here recounts. The reader learns which templates the
component catalog draws from Django's template_rendered signal, which fires
only once the test environment is set up, so main sets it up as the test
runner does.
"""

import os
import warnings
from collections import defaultdict

import django
from django.test.utils import setup_test_environment


def report(found: set[tuple[str, str]], held: dict[str, str]) -> tuple[list[str], bool]:
    """The report's lines, and whether found and held agree."""

    pairs = {(name, word) for name, words in held.items() for word in words.split()}
    by_template: dict[str, list[str]] = defaultdict(list)
    for name, word in sorted(found):
        by_template[name].append(word)
    totals = (
        f"{len({word for _, word in found})} classes, {len(by_template)} templates, "
        f"{len(found)} template-and-class pairs"
    )
    per_template = [
        f"templates/{name}\t{len(words)}\t{' '.join(f'.{word}' for word in words)}"
        for name, words in by_template.items()
    ]
    new = [f"hand-rolled, not in HAND_ROLLED: templates/{name}: .{word}" for name, word in sorted(found - pairs)]
    stale = [
        f"no longer hand-rolled: delete it from HAND_ROLLED: templates/{name}: .{word}"
        for name, word in sorted(pairs - found)
    ]
    return [totals, *per_template, *new, *stale], not (new or stale)


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()
    setup_test_environment()
    # Imported once settings are set up, which the test module needs at import.
    from hq.platform.application.tests.test_architecture_styles import (  # noqa: PLC0415
        HAND_ROLLED,
        hand_rolled_classes,
    )

    # The catalog draws its parts with no request, which csrf_token warns of.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        found = hand_rolled_classes()
    lines, agrees = report(found, HAND_ROLLED)
    print("\n".join(lines))
    return 0 if agrees else 1


if __name__ == "__main__":
    raise SystemExit(main())
