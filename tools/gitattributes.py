"""The root .gitattributes, written from the derived files the tasks declare.

Each file a task's ``vars.writes`` names is marked linguist-generated. git
reads attributes only from tracked files, so the file is committed and
lint:drift-gitattributes holds it.
"""

from hq.platform.core.tests.task import usage
from tests.mise_tasks import ROOT, mise_tasks, writes
from tools.derived import hold

PATH = ROOT / ".gitattributes"
HEADER = """\
# Written by `mise run fix` from each task's vars.writes in mise-tasks/*.toml,
# the one declaration of a derived file (docs/Development.md, Derived files).
# An attribute of hand-written files goes in a .gitattributes beside them.
"""


def declared() -> list[str]:
    """Every file, or git glob of files, some task declares it writes whole."""

    return sorted({path for task in mise_tasks().values() for path in writes(task)})


def render() -> str:
    # Each path from the root: a pattern without a slash would match a file of
    # that name in any directory.
    return HEADER + "".join(f"/{path} linguist-generated\n" for path in declared())


if __name__ == "__main__":
    raise SystemExit(hold(PATH, render().encode(), "lint:drift-gitattributes", fix=usage().switch("fix")))
