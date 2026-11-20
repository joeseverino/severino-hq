"""The packages a checkout's suite lives in, and what the image's copy of it leaves out."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def packages(root: Path) -> tuple[str, ...]:
    """A checkout's top-level Python packages: each directory at its root that holds an ``__init__.py``."""

    return tuple(sorted(entry.name for entry in root.iterdir() if (entry / "__init__.py").is_file()))


# The packages the suite lives in, read from the tree so a new one joins every reader at once.
SUITE = packages(ROOT)

# The image's copy of the checkout into /app, with what it leaves out.
IMAGE_COPY = re.compile(r"^COPY ((?:--exclude=\S+ )+)\. /app$", re.MULTILINE)


def image_excludes(root: Path) -> tuple[str, ...]:
    """What the image's copy of the checkout leaves out of /app: another stage's input only."""

    dockerfile = root / "Dockerfile"
    copy = IMAGE_COPY.search(dockerfile.read_text(encoding="utf-8")) if dockerfile.is_file() else None
    return tuple(re.findall(r"--exclude=(\S+)", copy[1])) if copy else ()
