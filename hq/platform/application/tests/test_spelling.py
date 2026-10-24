"""Everything HQ writes is in American English."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import source_files

ROOT = Path(__file__).resolve().parents[4]

# British stems with the American spelling beside each. A stem that is also a
# correct American word is left out ("cancelled" is a GitHub status value and
# accepted in both; CSS keeps `grey` aliases in its named colors).
BRITISH = {
    r"colour": "color",
    r"behaviour": "behavior",
    r"centre": "center",
    r"catalogue": "catalog",
    r"neighbour": "neighbor",
    r"honour": "honor",
    r"favour": "favor",
    r"rigour": "rigor",
    r"humour": "humor",
    r"harbour": "harbor",
    r"flavour": "flavor",
    r"\bwhilst\b": "while",
    r"\bamongst\b": "among",
    r"\blearnt\b": "learned",
    r"analys(?:e|es|ed|ing)\b": "analyze",
    r"judgement": "judgment",
    r"licence": "license",
    r"artefact": "artifact",
    r"defence": "defense",
    r"labell": "label",
    r"modell": "model",
    r"(?<![a-z])grey": "gray",
    r"(?:recogn|organ|normal|summar|serial|author|initial(?!ism)|material|memo|categor|capital|item|parameter|plural|canonical)is(?:e|es|ed|er|ing|ation|ations|able|ably)\b": "-ize",
}
PATTERN = re.compile("|".join(f"(?:{stem})" for stem in BRITISH), re.IGNORECASE)

# Not HQ's own words: HTML's attribute, CSS's named colors, a code
# generator's option.
EXTERNAL = re.compile(
    r"aria-labelledby|(?:dark|light|dim|slate|darkslate|lightslate)grey|ToCamelCaseWithInitialisms|additional-initialisms",
    re.IGNORECASE,
)

TEXT = {
    ".py",
    ".md",
    ".html",
    ".css",
    ".js",
    ".sh",
    ".toml",
    ".yml",
    ".yaml",
    ".go",
    ".txt",
    ".json",
    ".example",
    ".cfg",
}
NOT_OURS = ("static/vendor/", "controller/api/vendor/")
# What `mise run regen` writes; its source carries the words.
GENERATED = {"docs/Connections.md", "hq_sdk/contract.json"}


def checked_files() -> list[Path]:
    found = []
    for path in source_files(ROOT, "*"):
        relative = path.relative_to(ROOT).as_posix()
        if (
            not path.is_file()
            or path.suffix not in TEXT
            or path == Path(__file__).resolve()
            or relative in GENERATED
            or relative.startswith(NOT_OURS)
            or "/migrations/" in relative
            or relative.endswith((".gen.go", ".openapi.json", ".lock"))
        ):
            continue
        found.append(path)
    return found


@tag("source", "tree")
class AmericanSpellingTests(SimpleTestCase):
    def test_the_tree_has_walked_files(self):
        self.assertGreater(len(checked_files()), 500)

    def test_no_british_spelling_is_written(self):
        found = []
        for path in checked_files():
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                found.extend(
                    f"{path.relative_to(ROOT)}:{number}: {hit.group(0)}"
                    for hit in PATTERN.finditer(EXTERNAL.sub("", line))
                )
        self.assertEqual(found, [], "British spelling; HQ writes American English")
