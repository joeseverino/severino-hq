"""The served Inter: the release's variable font cut to what HQ prints and the
features its stylesheets ask for.

    uv run python -m tools.fonts.subset          # writes SERVED (mise run fix)
    uv run python -m tools.fonts.subset --check  # fails when SERVED is behind (lint:drift-font)

The source is Inter's own release (UPSTREAM beside this file). Both variable
axes stay: weight, cut to the 400 to 700 the @font-face declares, and optical
size, which browsers set from the font size, so headings and figures keep
their display spacing.
"""

import argparse
import io
import re
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tools/fonts/InterVariable.woff2"
SERVED = ROOT / "static/fonts/inter/inter-variable-latin.woff2"
STYLESHEETS = ROOT / "static/css"

WEIGHTS = (400, 700)

# Latin-1, the typographic punctuation HQ's copy uses on purpose (RUF001-003 in
# pyproject.toml), the minus sign amounts are written with, and the arrows and
# symbols templates draw. tools/fonts/tests holds everything HQ ships to it.
CODEPOINTS = frozenset(
    (
        *range(0x20, 0x7F),
        *range(0xA0, 0x100),
        # Zero width space: a break opportunity the stylesheets insert.
        0x200B,
        # Hyphens and dashes, curly quotes, daggers, bullet, ellipsis.
        *range(0x2010, 0x2028),
        # Per mille, single guillemets, fraction slash.
        0x2030,
        0x2039,
        0x203A,
        0x2044,
        # Euro, trade mark.
        0x20AC,
        0x2122,
        # The arrows, minus, command key, stars and check mark.
        *range(0x2190, 0x219A),
        0x2212,
        0x2318,
        0x2605,
        0x2606,
        0x2713,
    )
)

# What a browser applies to Latin text unasked: CSS Fonts 4, section 7.1
# (rlig, liga, clig, calt, locl, ccmp, mark, mkmk), and kerning, which
# font-kerning: auto turns on.
DEFAULT_FEATURES = ("rlig", "liga", "clig", "calt", "locl", "ccmp", "mark", "mkmk", "kern")

# The OpenType feature each font-variant-* value turns on (CSS Fonts 4, section 6).
# A value outside this table fails the build rather than going without.
VARIANT_FEATURES = {
    "normal": (),
    "none": (),
    "tabular-nums": ("tnum",),
    "proportional-nums": ("pnum",),
    "lining-nums": ("lnum",),
    "oldstyle-nums": ("onum",),
    "slashed-zero": ("zero",),
    "diagonal-fractions": ("frac",),
    "stacked-fractions": ("afrc",),
    "ordinal": ("ordn",),
    "small-caps": ("smcp",),
    "all-small-caps": ("smcp", "c2sc"),
    "sub": ("subs",),
    "super": ("sups",),
}

_FEATURE_SETTINGS = re.compile(r"font-feature-settings\s*:\s*([^;}]*)")
_FEATURE_TAG = re.compile(r"""["']([a-zA-Z0-9 ]{4})["']""")
_VARIANT = re.compile(r"font-variant(?:-[a-z]+)*\s*:\s*([^;}]*)")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def stylesheet_features(css: str) -> set[str]:
    """The OpenType features a stylesheet asks for, by tag."""

    css = _COMMENT.sub("", css)
    found = {tag for value in _FEATURE_SETTINGS.findall(css) for tag in _FEATURE_TAG.findall(value)}
    for value in _VARIANT.findall(css):
        for word in value.split():
            if word not in VARIANT_FEATURES:
                raise ValueError(f"font-variant value {word!r} has no feature in VARIANT_FEATURES")
            found.update(VARIANT_FEATURES[word])
    return found


def features() -> list[str]:
    """The browser's default features and every feature HQ's stylesheets ask for."""

    asked = set().union(*(stylesheet_features(path.read_text(encoding="utf-8")) for path in STYLESHEETS.rglob("*.css")))
    return sorted(set(DEFAULT_FEATURES) | asked)


def build() -> bytes:
    """The served font's bytes, the same on every run."""

    # Not lazy: the instancer reads every glyph's variations after the subset.
    font = TTFont(SOURCE, recalcTimestamp=False, lazy=False)
    options = subset.Options()
    options.layout_features = features()
    options.hinting = False
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=CODEPOINTS)
    subsetter.subset(font)
    font = instancer.instantiateVariableFont(font, {"wght": WEIGHTS})
    font.flavor = "woff2"
    served = io.BytesIO()
    font.save(served)
    return served.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the served font is not what this writes")
    check = parser.parse_args().check
    built = build()
    name = SERVED.relative_to(ROOT)
    if check:
        if SERVED.read_bytes() != built:
            print(f"{name} is behind its source; run `mise run fix` and commit the result.", file=sys.stderr)
            return 1
        print(f"{name} is current.")
        return 0
    SERVED.write_bytes(built)
    print(f"Wrote {name}, {len(built) / 1000:.1f} KB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
