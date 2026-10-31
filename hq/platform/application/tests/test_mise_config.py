"""Every image that installs from mise.toml carries the files its templates read.

mise.toml takes Go's release from controller/go.mod through read_file, and mise
refuses the whole file, every tool, when that read fails.
"""

import fnmatch
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]


def mise_reads() -> list[str]:
    """The files mise.toml's templates read, relative to it: mise loads no part of it without them."""
    return re.findall(r'read_file\(path="([^"]+)"\)', (ROOT / "mise.toml").read_text())


def image_files() -> list[str]:
    return subprocess.run(
        ["git", "ls-files", "-z", "--", "*Dockerfile"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")[:-1]


def instructions(text: str) -> list[list[tuple[str, list[str]]]]:
    """A Dockerfile's stages, each its instructions as (keyword, arguments)."""
    stages: list[list[tuple[str, list[str]]]] = [[]]
    for line in re.sub(r"\\\n", " ", text).splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        keyword = words[0].upper()
        if keyword == "FROM":
            stages.append([])
        stages[-1].append((keyword, words[1:]))
    return stages


def landed(arguments: list[str]) -> dict[str, str]:
    """Where a COPY from the build context puts each source, by its source."""
    if any(word.startswith("--from=") for word in arguments):
        return {}
    *sources, dest = [word for word in arguments if not word.startswith("--")]
    into = dest.endswith("/") or len(sources) > 1
    return {
        source: posixpath.normpath(posixpath.join(dest, posixpath.basename(source)) if into else dest)
        for source in sources
    }


def context_admits(ignore: list[str], path: str) -> bool:
    """Whether a .dockerignore leaves ``path`` in the build context: its last matching line decides."""
    admitted = True
    for raw in ignore:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.removeprefix("!").strip("/")
        if fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern + "/"):
            admitted = line.startswith("!")
    return admitted


def stage_copies(stage: list[tuple[str, list[str]]]) -> tuple[str | None, dict[str, str]]:
    """Where a stage puts mise.toml, and what it has copied by the first RUN after that."""
    copies: dict[str, str] = {}
    for keyword, arguments in stage:
        if keyword == "COPY":
            copies |= landed(arguments)
        elif keyword == "RUN" and "mise.toml" in copies:
            break
    config = copies.get("mise.toml")
    return (posixpath.dirname(config) if config else None), copies


def unreadable_config(image: str) -> tuple[int, list[str]]:
    """How many of an image's stages copy mise.toml, and what they lack of the files it reads."""
    path = ROOT / image
    ignore_file = path.with_name(path.name + ".dockerignore")
    ignore = (ignore_file if ignore_file.exists() else ROOT / ".dockerignore").read_text().splitlines()
    configs, found = 0, []
    for stage in instructions(path.read_text()):
        config_dir, copies = stage_copies(stage)
        if config_dir is None:
            continue
        configs += 1
        for read in mise_reads():
            if copies.get(read) != posixpath.normpath(posixpath.join(config_dir, read)):
                found.append(f"{image} copies mise.toml without {read} beside it, before mise runs")
            if not context_admits(ignore, read):
                found.append(f"{image}'s build context leaves out {read}, which mise.toml reads")
    return configs, found


@tag("source")
class MiseConfigTravelsTests(SimpleTestCase):
    """A stage that installs from mise.toml carries go.mod beside it, and its build context admits it."""

    @tag("tree")
    def test_every_image_that_copies_mise_toml_copies_what_it_reads(self):
        self.assertIn("controller/go.mod", mise_reads())
        configs = 0
        for image in image_files():
            with self.subTest(image=image):
                copied, found = unreadable_config(image)
                configs += copied
                self.assertEqual(found, [])
        self.assertGreater(configs, 1)

    def test_a_stage_without_go_mod_or_a_context_without_it_fails(self):
        dockerfile = "FROM base AS tools\nWORKDIR /tools\nCOPY mise.toml mise.lock ./\n{go_mod}RUN mise install uv\n"
        cases = {
            "no copy": (dockerfile.format(go_mod=""), "*\n!mise.toml\n!controller/go.mod\n"),
            "elsewhere": (dockerfile.format(go_mod="COPY controller/go.mod ./\n"), "*\n!mise.toml\n!controller/\n"),
            "after the run": (
                dockerfile.format(go_mod="") + "COPY controller/go.mod controller/\n",
                "*\n!mise.toml\n!controller/go.mod\n",
            ),
            "ignored": (dockerfile.format(go_mod="COPY controller/go.mod controller/\n"), "*\n!mise.toml\n"),
        }
        passing = (dockerfile.format(go_mod="COPY controller/go.mod controller/\n"), "*\n!mise.toml\n!controller/\n")
        with tempfile.TemporaryDirectory() as folder, mock.patch(f"{__name__}.ROOT", Path(folder)):
            (Path(folder) / "mise.toml").write_text("go = '{{ read_file(path=\"controller/go.mod\") }}'\n")
            for name, (text, ignore) in {"passing": passing, **cases}.items():
                with self.subTest(case=name):
                    (Path(folder) / "Dockerfile").write_text(text)
                    (Path(folder) / "Dockerfile.dockerignore").write_text(ignore)
                    copied, found = unreadable_config("Dockerfile")
                    self.assertEqual(copied, 1)
                    self.assertEqual(bool(found), name != "passing")


def passed_build_args(image: str) -> dict[str, set[str]]:
    """The values a workflow's ``docker build -f <image>`` writes out for each ARG."""
    values: dict[str, set[str]] = {}
    for workflow in sorted((ROOT / ".github").rglob("*.yml")):
        text = re.sub(r"\\\n\s*", " ", workflow.read_text())
        for command in re.findall(r"docker build [^\n]*", text):
            if f"-f {image} " in command:
                for name, value in re.findall(r"--build-arg (\w+)=([^\s\"'$]+)", command):
                    values.setdefault(name, set()).add(value)
    return values


def context_sources(image: str, passed: dict[str, set[str]]) -> set[str]:
    """Every path an image's stages copy from the build context, each ARG read as its default and as each value passed."""
    sources: set[str] = set()
    for stage in instructions((ROOT / image).read_text()):
        args: dict[str, set[str]] = {}
        for keyword, arguments in stage:
            if keyword == "ARG" and arguments:
                name, _, default = arguments[0].partition("=")
                args[name] = ({default} if default else set()) | passed.get(name, set())
            elif keyword == "COPY" and not any(word.startswith("--from=") for word in arguments):
                for source in [word for word in arguments if not word.startswith("--")][:-1]:
                    for path in expand(source, args):
                        sources.add(posixpath.normpath(path).removeprefix("./"))
    return {source for source in sources if source != "." and "$" not in source}


def expand(source: str, args: dict[str, set[str]]) -> set[str]:
    match = re.search(r"\$\{?(\w+)\}?", source)
    if not match or match.group(1) not in args:
        return {source}
    return {path for value in args[match.group(1)] for path in expand(source.replace(match.group(0), value, 1), args)}


@tag("source")
class BuildContextTests(SimpleTestCase):
    """A path an image copies from its context is one its .dockerignore admits, whatever value a workflow gives its ARG."""

    @tag("tree")
    def test_every_path_an_image_copies_is_in_its_build_context(self):
        checked = 0
        for image in image_files():
            path = ROOT / image
            ignore_file = path.with_name(path.name + ".dockerignore")
            ignore = (ignore_file if ignore_file.exists() else ROOT / ".dockerignore").read_text().splitlines()
            for source in sorted(context_sources(image, passed_build_args(image))):
                checked += 1
                with self.subTest(image=image, source=source):
                    self.assertTrue(
                        context_admits(ignore, source), f"{image} copies {source}, which its build context leaves out"
                    )
        self.assertGreater(checked, 1)

    @tag("tree")
    def test_a_staged_directory_a_workflow_passes_that_the_context_leaves_out_fails(self):
        dockerfile = "FROM base\nARG STAGED=build/composition\nCOPY ${STAGED}/ /tmp/plugin/\n"
        workflow = (
            'run: |\n  docker build -f Dockerfile --build-arg STAGED=build/candidate \\\n    --build-arg IMAGE="$X" .\n'
        )
        with tempfile.TemporaryDirectory() as folder, mock.patch(f"{__name__}.ROOT", Path(folder)):
            (Path(folder) / ".github").mkdir()
            (Path(folder) / ".github" / "ci.yml").write_text(workflow)
            (Path(folder) / "Dockerfile").write_text(dockerfile)
            sources = context_sources("Dockerfile", passed_build_args("Dockerfile"))
            self.assertEqual(sources, {"build/composition", "build/candidate"})
            self.assertFalse(context_admits(["*", "!build/composition/"], "build/candidate"))
            self.assertTrue(context_admits(["*", "!build/composition/", "!build/candidate/"], "build/candidate"))
