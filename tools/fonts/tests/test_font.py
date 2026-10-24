"""The served font draws every character HQ prints and every feature its stylesheets ask for."""

import ast
import html
import re
import tempfile
import unicodedata
from pathlib import Path

from django.test import SimpleTestCase, tag
from fontTools.ttLib import TTFont

from tools.fonts.subset import ROOT, SERVED, stylesheet_features

# Unicode's Default_Ignorable_Code_Point (DerivedCoreProperties.txt, Unicode
# 18.0.0). CSS Text 3, section 4: "unsupported Default_ignorable characters must
# be ignored for text rendering", so no font is asked for one; the word joiner
# amounts are written with is one, and Inter has no glyph for it.
DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

_JS_ESCAPE = re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}|\\u([0-9a-fA-F]{4})")
_CSS_CONTENT = re.compile(r"(?<![-\w])content\s*:\s*([^;}]*)")
_CSS_STRING = re.compile(r""""((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'""")
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}) ?")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def drawn(character: str) -> bool:
    """Whether a page asks a font for this character."""

    code = ord(character)
    if code < 0x20 or 0x7F <= code < 0xA0:
        return False
    return not any(low <= code <= high for low, high in DEFAULT_IGNORABLE)


def _python_strings(path: Path) -> str:
    # String literals only: a comment is never printed.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return "".join(
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _script_text(path: Path) -> str:
    return _JS_ESCAPE.sub(
        lambda found: chr(int(found.group(1) or found.group(2), 16)), path.read_text(encoding="utf-8")
    )


def _stylesheet_content(path: Path) -> str:
    css = _CSS_COMMENT.sub("", path.read_text(encoding="utf-8"))
    # Each string decoded on its own: an escape ends with its string.
    return "".join(
        _CSS_ESCAPE.sub(lambda escape: chr(int(escape.group(1), 16)), found.group(1) or found.group(2) or "")
        for value in _CSS_CONTENT.findall(css)
        for found in _CSS_STRING.finditer(value)
    )


def _shipped(root: Path):
    """Each file HQ prints from, with the text it can print."""

    for path in sorted((root / "templates").rglob("*")):
        if path.is_file():
            yield path, html.unescape(path.read_text(encoding="utf-8"))
    for package in ("hq", "hq_sdk"):
        for path in sorted((root / package).rglob("*.py")):
            if "tests" not in path.relative_to(root).parts:
                yield path, _python_strings(path)
    for path in sorted((root / "static/js").glob("*.js")):
        yield path, _script_text(path)
    for path in sorted((root / "static/css").rglob("*.css")):
        if "vendor" not in path.relative_to(root).parts:
            yield path, _stylesheet_content(path)


def undrawn(root: Path, font: Path) -> dict[str, Path]:
    """Each character HQ prints that the font has no glyph for, with the first file that prints it."""

    cmap = TTFont(font).getBestCmap()
    missing: dict[str, Path] = {}
    for path, text in _shipped(root):
        for character in set(text):
            if drawn(character) and ord(character) not in cmap:
                missing.setdefault(character, path.relative_to(root))
    return missing


def _layout_features(font: Path) -> set[str]:
    loaded = TTFont(font)
    return {
        record.FeatureTag
        for table in ("GSUB", "GPOS")
        if table in loaded
        for record in loaded[table].table.FeatureList.FeatureRecord
    }


@tag("source", "tree")
class ServedFontTests(SimpleTestCase):
    def test_every_character_hq_prints_has_a_glyph(self):
        missing = undrawn(ROOT, SERVED)
        self.assertEqual(
            {
                f"U+{ord(character):04X} {unicodedata.name(character, '?')}": str(path)
                for character, path in missing.items()
            },
            {},
            "The served font has no glyph for these, so a page draws them in another face: add each to "
            "CODEPOINTS in tools/fonts/subset.py and run `mise run fix`, or, when Inter has none, "
            "draw it as an icon (templates/partials/_icon.html).",
        )

    def test_every_feature_the_stylesheets_ask_for_is_in_the_font(self):
        asked = set().union(
            *(stylesheet_features(path.read_text(encoding="utf-8")) for path in (ROOT / "static/css").rglob("*.css"))
        )
        self.assertLessEqual(asked, _layout_features(SERVED))

    def test_a_character_printed_anywhere_hq_prints_from_is_found(self):
        # U+22EF MIDLINE HORIZONTAL ELLIPSIS: in no Inter release, so never in the font.
        planted = {
            "templates/menu.html": "<summary>&#x22EF;</summary>",
            "hq/copy.py": '# \u2603 a comment is never printed\nMORE = "\\u22ef"\n',
            "hq/tests/test_copy.py": 'SNOW = "\u2603"\n',
            "static/js/menu.js": 'button.textContent = "\\u22EF";\n',
            "static/css/app/menu.css": '/* \u2603 */ .more::after { content: "\\22EF"; }\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in planted.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            (root / "hq_sdk").mkdir()
            missing = undrawn(root, SERVED)
        self.assertEqual(set(missing), {"\u22ef"})

    def test_each_file_kind_is_read_for_what_it_prints(self):
        cases = {
            "menu.html": ("<p>&rarr; Next&nbsp;page</p>", {"\u2192", "\u00a0"}),
            "copy.py": ('# \u2603\nA = "\\u2212"\nB = f"{A}\u2026"\n', {"\u2212", "\u2026"}),
            "menu.js": ('// \u2603\nconst a = "\\u2192", b = "\\u{2026}";\n', {"\u2192", "\u2026", "\u2603"}),
            "menu.css": (
                # An escape ends with its string, so "\00d7" and the "D" that follows are two characters.
                (
                    '/* \u2603 */ a::after { content: "\\00a0\\2197"; } b { align-content: center; }'
                    ' c::before { content: "\\00d7" / ""; } d::before { content: "Dismissed"; }'
                ),
                {"\u00a0", "\u2197", "\u00d7"},
            ),
        }
        readers = {
            ".html": lambda path: html.unescape(path.read_text()),
            ".py": _python_strings,
            ".js": _script_text,
            ".css": _stylesheet_content,
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, (text, expected) in cases.items():
                with self.subTest(name=name):
                    path = Path(directory, name)
                    path.write_text(text, encoding="utf-8")
                    found = {character for character in readers[path.suffix](path) if ord(character) > 0x7E}
                    self.assertEqual(found, expected)

    def test_an_invisible_character_asks_no_font_for_a_glyph(self):
        for character in ("\u2060", "\u00ad", "\u200b", "\ufeff", "\n"):
            with self.subTest(character=f"U+{ord(character):04X}"):
                self.assertFalse(drawn(character))
        self.assertTrue(drawn("\u2212"))


class StylesheetFeatureTests(SimpleTestCase):
    def test_a_feature_is_read_from_settings_and_variants(self):
        css = '/* font-variant-numeric: oldstyle-nums */ a { font-feature-settings: "cv11", "ss01" 1; } b { font-variant-numeric: tabular-nums slashed-zero; }'
        self.assertEqual(stylesheet_features(css), {"cv11", "ss01", "tnum", "zero"})

    def test_a_variant_with_no_known_feature_fails_rather_than_going_without(self):
        with self.assertRaises(ValueError):
            stylesheet_features("a { font-variant-east-asian: jis78; }")
