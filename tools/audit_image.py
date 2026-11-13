"""`mise run audit:image [IMAGE]`: what a built image's bytes are, layer by layer, read-only.

The image is saved once under var/audit/image/, and that archive is read twice:
here, layer by layer, for the bytes each layer holds under the paths that
make up HQ (the collected static trees, the code, site-packages, the stdlib,
the Go binaries), with each layer's whiteouts applied to find the filesystem a
container sees; and by dive, for the bytes a later layer leaves wasted. dive
reports no layer's paths, so the walk is ours; the wasted figure is dive's.
"""

import json
import re
import subprocess
import sys
import tarfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from django.utils.text import Truncator

from tools.perf.table import aligned

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var" / "audit" / "image"
# A reference as docker takes one: never a word a program could read as a flag.
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
SITE = "/site-packages/"
STDLIB = re.compile(r"^usr/local/lib/python3\.\d+/")
BINARIES = ("hq-controller", "hq-secrets", "hq-host")
# The trees asked about by name, each a prefix of the archive's paths.
TREES = {"/static": "static/", "/app/static": "app/static/", "/app/hq": "app/hq/"}
LARGEST_PACKAGES = 30
OTHER_GROUPS = 20
# The parts of HQ a layer's bytes are named by in its row.
SHOWN_PER_LAYER = 4
DIVE_FILES = 10
# A part's hidden bytes below this are left out of their table, though counted in its total.
SHOWN_BYTES = 50_000
SAVE_S = 900
DIVE_S = 900
WHITEOUT = ".wh."
OPAQUE = ".wh..wh..opq"


def bucket(path: str) -> str:
    """The part of the image a file's bytes count toward."""

    name = path.rsplit("/", 1)[-1]
    if name in BINARIES:
        return name
    if SITE in f"/{path}":
        return "site-packages"
    for shown, prefix in TREES.items():
        if path.startswith(prefix):
            return shown
    if STDLIB.match(path):
        return "stdlib"
    return "/" + "/".join(path.split("/")[:3]) if path.count("/") >= 3 else "/" + path.rsplit("/", 1)[0]


def package(path: str) -> str | None:
    """The distribution a site-packages file belongs to, its dist-info folded in."""

    if SITE not in f"/{path}":
        return None
    top = f"/{path}".split(SITE, 1)[1].split("/", 1)[0]
    # A module of one file: its extension's ABI tag (.cpython-315-aarch64-linux-gnu.so) or .py.
    top = re.sub(r"(\.(cpython|abi3)[^/]*)?\.(so|pyd|py)$", "", top)
    top = re.sub(r"\.(dist-info|libs|data)$", "", top)
    top = re.sub(r"-\d[^-]*$", "", top)
    return top.lower().replace("-", "_") or None


@dataclass
class Layer:
    instruction: str
    size: int = 0
    files: dict[str, int] = field(default_factory=dict)
    whiteouts: set[str] = field(default_factory=set)
    opaque: set[str] = field(default_factory=set)


def read_layer(blob: IO[bytes], instruction: str) -> Layer:
    layer = Layer(instruction)
    with tarfile.open(fileobj=blob, mode="r:*") as archive:
        for member in archive:
            path = member.name.removeprefix("./").rstrip("/")
            parent, _, name = path.rpartition("/")
            if name == OPAQUE:
                layer.opaque.add(parent)
            elif name.startswith(WHITEOUT):
                layer.whiteouts.add(f"{parent}/{name.removeprefix(WHITEOUT)}".lstrip("/"))
            elif member.isreg():
                layer.files[path] = member.size
                layer.size += member.size
    return layer


def ancestors(path: str) -> Iterator[str]:
    parts = path.split("/")
    for end in range(len(parts), 0, -1):
        yield "/".join(parts[:end])


def shown(layers: list[Layer]) -> tuple[dict[str, int], list[dict[str, int]]]:
    """The files a container sees, and each layer's bytes a later layer hides, by bucket.

    Walked from the top: a file is hidden when a later layer holds the same
    path, a whiteout of it or of a folder above it, or an opaque folder above it."""

    seen: dict[str, int] = {}
    hidden_below: set[str] = set()
    opaque_below: set[str] = set()
    hidden: list[dict[str, int]] = [{} for _ in layers]
    for index in range(len(layers) - 1, -1, -1):
        layer = layers[index]
        lost: Counter[str] = Counter()
        for path, size in layer.files.items():
            covered = path in seen or any(
                above in hidden_below or (above != path and above in opaque_below) for above in ancestors(path)
            )
            if covered:
                lost[bucket(path)] += size
            else:
                seen[path] = size
        hidden[index] = dict(lost)
        hidden_below |= layer.whiteouts
        opaque_below |= layer.opaque
    return seen, hidden


def mb(size: float) -> str:
    return f"{size / 1e6:.1f}"


def instruction(entry: dict[str, Any]) -> str:
    said = re.sub(r"\s+", " ", entry.get("created_by", "")).strip()
    said = re.sub(r"^/bin/sh -c (#\(nop\) )?", "", said).removesuffix(" # buildkit")
    return Truncator(said).chars(100, truncate="...")


def run(command: list[str], timeout: int) -> str:
    done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode:
        raise SystemExit(f"{' '.join(command[:3])} exited {done.returncode}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def layers_of(saved: Path) -> list[Layer]:
    """Each layer of the archive, bottom first, beside the instruction that made it."""

    with tarfile.open(saved) as archive:

        def member(name: str) -> IO[bytes]:
            found = archive.extractfile(name)
            if found is None:
                raise SystemExit(f"the saved image has no {name}")
            return found

        manifest = json.load(member("manifest.json"))[0]
        config = json.load(member(manifest["Config"]))
        made = [entry for entry in config.get("history", []) if not entry.get("empty_layer")]
        if len(made) != len(manifest["Layers"]):
            made = [{} for _ in manifest["Layers"]]
        return [
            read_layer(member(blob), instruction(entry)) for blob, entry in zip(manifest["Layers"], made, strict=True)
        ]


def layer_table(layers: list[Layer], hidden: list[dict[str, int]]) -> list[str]:
    rows = [["#", "MB", "hidden MB", "instruction", "its bytes"]]
    for index, layer in enumerate(layers):
        parts: Counter[str] = Counter()
        for path, size in layer.files.items():
            parts[bucket(path)] += size
        top = ", ".join(f"{name} {mb(size)}" for name, size in parts.most_common(SHOWN_PER_LAYER) if size)
        lost = sum(hidden[index].values())
        rows.append([str(index), mb(layer.size), mb(lost) if lost else "", layer.instruction, top])
    return aligned(rows, frozenset({3, 4}))


def shipped(files: dict[str, int]) -> list[str]:
    by: Counter[str] = Counter()
    for path, size in files.items():
        by[bucket(path)] += size
    named = [*TREES, "site-packages", "stdlib", *BINARIES]
    rows = [["part", "MB"]] + [[name, mb(by.pop(name, 0))] for name in named]
    rest = by.most_common()
    rows += [[name, mb(size)] for name, size in rest[:OTHER_GROUPS]]
    rows.append(
        [f"the other {max(0, len(rest) - OTHER_GROUPS)} folders", mb(sum(size for _, size in rest[OTHER_GROUPS:]))]
    )
    rows.append(["total", mb(sum(files.values()))])
    return aligned(rows)


def packages(files: dict[str, int]) -> list[str]:
    by: Counter[str] = Counter()
    for path, size in files.items():
        if name := package(path):
            by[name] += size
    rows = [["package", "MB"]] + [[name, mb(size)] for name, size in by.most_common(LARGEST_PACKAGES)]
    rows.append(
        [
            f"the other {max(0, len(by) - LARGEST_PACKAGES)}",
            mb(sum(size for _, size in by.most_common()[LARGEST_PACKAGES:])),
        ]
    )
    return aligned(rows)


def hidden_table(layers: list[Layer], hidden: list[dict[str, int]]) -> list[str]:
    """The bytes a later layer hides, by part and the layer holding them: they ship in the
    image, and no container sees them."""

    rows = [["part", "layer", "its MB", "hidden MB"]]
    for index, lost in enumerate(hidden):
        held: Counter[str] = Counter()
        for path, size in layers[index].files.items():
            held[bucket(path)] += size
        rows += [
            [part, str(index), mb(held[part]), mb(size)]
            for part, size in Counter(lost).most_common()
            if size >= SHOWN_BYTES
        ]
    total = sum(sum(lost.values()) for lost in hidden)
    if len(rows) == 1:
        return ["No layer's bytes are hidden by a later one."]
    return [*aligned(rows), f"hidden in all: {mb(total)} MB"]


def dive(saved: Path) -> list[str]:
    report = saved.with_suffix(".dive.json")
    run(["dive", "--source", "docker-archive", str(saved), "--json", str(report)], DIVE_S)
    found = json.loads(report.read_text(encoding="utf-8"))["image"]
    lines = [
        (
            f"dive: {mb(found['inefficientBytes'])} MB wasted of {mb(found['sizeBytes'])} MB, "
            f"efficiency {found['efficiencyScore']:.4f}; the files it counts most:"
        )
    ]
    files = sorted(found.get("fileReference") or [], key=lambda each: -each["sizeBytes"])[:DIVE_FILES]
    rows = [["count", "MB", "path"]] + [[str(each["count"]), mb(each["sizeBytes"]), each["file"]] for each in files]
    return lines + aligned(rows, frozenset({2}))


def main(image: str) -> int:
    if not REFERENCE.fullmatch(image):
        print(f"{image!r} is not an image reference", file=sys.stderr)
        return 2
    inspected = json.loads(run(["docker", "image", "inspect", image], 60))[0]
    OUT.mkdir(parents=True, exist_ok=True)
    saved = OUT / (re.sub(r"[^A-Za-z0-9._-]", "-", image) + ".tar")
    run(["docker", "save", "--output", str(saved), image], SAVE_S)
    try:
        layers = layers_of(saved)
        files, hidden = shown(layers)
        said = [
            (
                f"{image} {inspected['Id'][:19]}: docker image inspect's Size {mb(inspected['Size'])} MB, "
                f"{mb(sum(layer.size for layer in layers))} MB of files over {len(layers)} layers, "
                f"{mb(sum(files.values()))} MB of them seen in a container"
            ),
            "",
            *layer_table(layers, hidden),
            "",
            "What a container sees, by part:",
            *shipped(files),
            "",
            f"site-packages, the {LARGEST_PACKAGES} largest:",
            *packages(files),
            "",
            "Bytes a later layer hides:",
            *hidden_table(layers, hidden),
            "",
            *dive(saved),
        ]
    finally:
        saved.unlink(missing_ok=True)
    print("\n".join(said))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
