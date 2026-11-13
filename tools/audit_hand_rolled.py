"""`mise run audit:hand-rolled`: the page-class check's count, read-only; with
--lower, `mise run fix`'s step that drops from the baseline each pair the
check no longer finds.

The numbers are the ones test_host_templates_draw_only_the_shared_parts_classes
reads, from the same reader held against the same baseline
(hq/platform/application/tests/hand_rolled.py); nothing here recounts. The
reader learns which templates the component catalog draws from Django's
template_rendered signal, which fires only once the test environment is set
up, so main sets it up as the test runner does.
"""

import argparse
import os
import warnings
from collections import defaultdict

import django
from django.test.utils import setup_test_environment


def report(found: set[tuple[str, str]], held: set[tuple[str, str]]) -> tuple[list[str], bool]:
    """The report's lines, and whether found and held agree."""

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
    new = [f"hand-rolled, not in the baseline: templates/{name}: .{word}" for name, word in sorted(found - held)]
    stale = [
        f"no longer hand-rolled: mise run fix drops it: templates/{name}: .{word}"
        for name, word in sorted(held - found)
    ]
    return [totals, *per_template, *new, *stale], not (new or stale)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lower", action="store_true", help="drop each pair no longer found from the baseline")
    lower = parser.parse_args().lower
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()
    setup_test_environment()
    # Imported once settings are set up, which the reader's imports need.
    from hq.platform.application.tests import hand_rolled  # noqa: PLC0415

    # The catalog draws its parts with no request, which csrf_token warns of.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        found = hand_rolled.hand_rolled_classes()
    text = hand_rolled.BASELINE.read_text(encoding="utf-8")
    if lower:
        hand_rolled.BASELINE.write_text(hand_rolled.lowered(text, found), encoding="utf-8")
        for name, word in sorted(hand_rolled.held(text) - found):
            print(f"fix:hand-rolled: dropped templates/{name}: .{word}")
        return 0
    lines, agrees = report(found, hand_rolled.held(text))
    print("\n".join(lines))
    return 0 if agrees else 1


if __name__ == "__main__":
    raise SystemExit(main())
