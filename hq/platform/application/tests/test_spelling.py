"""Everything HQ and each installed extension writes is in American English."""

import importlib.util
import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.plugins import installed_plugin_apps

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
# What `mise run fix` writes; its source carries the words.
GENERATED = {"docs/Connections.md", "hq_sdk/contract.json"}


def checked_files(root: Path = ROOT) -> list[Path]:
    found = []
    for path in source_files(root, "*"):
        relative = path.relative_to(root).as_posix()
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


def british_spellings(root: Path) -> list[str]:
    """Each British spelling under ``root``, as ``path:line: word``."""

    found = []
    for path in checked_files(root):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            found.extend(
                f"{path.relative_to(root)}:{number}: {hit.group(0)}" for hit in PATTERN.finditer(EXTERNAL.sub("", line))
            )
    return found


def extension_roots() -> list[Path]:
    """Each installed extension's project, read whole as this checkout is.

    That is the nearest directory above its package that holds a
    ``pyproject.toml``, or the package alone when that directory is this
    checkout (a fixture) or the package is installed into an environment.
    """

    roots = set()
    for app in installed_plugin_apps():
        spec = importlib.util.find_spec(app.split(".")[0])
        for location in (spec.submodule_search_locations or ()) if spec else ():
            package = Path(location).resolve()
            project = next((d for d in package.parents if (d / "pyproject.toml").is_file()), ROOT)
            installed = "site-packages" in package.parts
            roots.add(package if installed or project == ROOT or ROOT.is_relative_to(project) else project)
    return sorted(roots)


@tag("source", "tree")
class AmericanSpellingTests(SimpleTestCase):
    def test_the_tree_has_walked_files(self):
        self.assertGreater(len(checked_files()), 500)

    def test_no_british_spelling_is_written(self):
        self.assertEqual(british_spellings(ROOT), [], "British spelling; HQ writes American English")


@tag("source", "tree")
class ExtensionSpellingTests(SimpleTestCase):
    """An extension's words are held as the host's are, by the extension check
    (tools/pipeline/check-plugin.sh) and wherever one is installed."""

    def test_no_installed_extension_writes_british_spelling(self):
        roots = extension_roots()
        if not roots:
            self.skipTest("no extension is installed")
        found = [f"{root.name}/{place}" for root in roots for place in british_spellings(root)]
        self.assertEqual(found, [], "British spelling; HQ and its extensions write American English")
