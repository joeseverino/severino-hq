"""Where HQ's source and its public demo are, each named once for every page,
header and check that links to them."""

REPOSITORY = "https://github.com/joeseverino/severino-hq"
DEMO = "https://demo.jseverino.com/"


def in_repository(path: str) -> str:
    """The address of ``path`` on the repository's main branch."""

    return f"{REPOSITORY}/blob/main/{path}"
