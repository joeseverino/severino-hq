"""Checks on what HQ and its extensions show people: their words and markup.

An em dash in interface text is the most recognisable mark of generated prose,
and it spreads by copying. This check reads every template and every string
literal in HQ and in each extension checked out beside it, and reports any em
dash outside comments and docstrings. A value that is missing is shown with
the `or_empty` filter or ``MISSING``, never a literal dash.

Counts get the same treatment. A plural built by hand, Django's ``pluralize``
or ``"s" if n != 1 else ""``, agrees the noun and forgets the verb ("1 need
output"). ``counted`` takes the whole phrase for one and for many instead.

A warning, not an error: an error would stop HQ starting over punctuation. The
gates that enforce it run ``manage.py check --tag interface --fail-level WARNING``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import checks

EM_DASH = chr(0x2014)  # built, so this file holds no literal one
_TEMPLATE_COMMENT = re.compile(
    r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.DOTALL
)
_SKIPPED = frozenset(
    {"tests", "migrations", "staticfiles", "node_modules", "var", "data", "docs", "deploy"}
)


def _roots() -> list[Path]:
    """HQ's checkout, then each extension that is not installed as a package."""
    base = Path(settings.BASE_DIR).resolve()
    roots = [base]
    for config in apps.get_app_configs():
        path = Path(config.path).resolve()
        if "site-packages" in path.parts or path.is_relative_to(base):
            continue
        roots.append(path)
    return roots


def _files(root: Path, suffix: str):
    for path in sorted(root.rglob(f"*{suffix}")):
        parts = path.relative_to(root).parts
        if any(part in _SKIPPED or part.startswith(".") for part in parts):
            continue
        if path.name.startswith("test"):
            continue
        yield path


def _docstrings(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _read(path: Path) -> str | None:
    """A file's text, or None when it cannot be read. A check never stops startup."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _template_lines():
    """Every template line outside comments, as (path, number, text)."""
    for root in _roots():
        for path in _files(root, ".html"):
            source = _read(path)
            if source is None:
                continue
            text = _TEMPLATE_COMMENT.sub(
                lambda match: "\n" * match.group().count("\n"), source
            )
            for number, line in enumerate(text.splitlines(), 1):
                yield path, number, line


def _python_trees():
    for root in _roots():
        for path in _files(root, ".py"):
            source = _read(path)
            if source is None:
                continue
            try:
                yield path, ast.parse(source)
            except (SyntaxError, ValueError):
                continue


def em_dashes() -> list[tuple[Path, int]]:
    found = [(path, number) for path, number, line in _template_lines() if EM_DASH in line]
    for path, tree in _python_trees():
        skip = _docstrings(tree)
        found.extend(
            (path, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and EM_DASH in node.value
            and id(node) not in skip
        )
    return found


_PLURALIZE = re.compile(r"\|\s*pluralize\b")
# "account(s)": a plural hedged in brackets. Only flagged where a value is
# interpolated (an f-string, a template), since a plain string such as a CSV
# column named "Duration(s)" is data, not a count.
_BRACKETED = re.compile(r"(?:^|[a-z]|\}\})\((?:s|es|ies)\)")
_SUFFIXES = frozenset({"s", "es"})


def _is_suffix_guess(node: ast.AST) -> bool:
    """``"s" if n != 1 else ""`` and its mirror: a plural guessed from a letter."""
    if not isinstance(node, ast.IfExp):
        return False
    branches = {node.body, node.orelse}
    values = {b.value for b in branches if isinstance(b, ast.Constant)}
    return len(values) == 2 and "" in values and bool(values & _SUFFIXES)


def _is_bracketed_plural(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) and any(
        isinstance(part, ast.Constant) and _BRACKETED.search(str(part.value))
        for part in node.values
    )


def hand_plurals() -> list[tuple[Path, int]]:
    found = [
        (path, number)
        for path, number, line in _template_lines()
        if _PLURALIZE.search(line) or _BRACKETED.search(line)
    ]
    for path, tree in _python_trees():
        found.extend(
            (path, node.lineno)
            for node in ast.walk(tree)
            if _is_suffix_guess(node) or _is_bracketed_plural(node)
        )
    return found


_FORM_TAG = re.compile(r"<(/?)form\b", re.IGNORECASE)


def nested_forms() -> list[tuple[Path, int]]:
    """Forms opened inside an open form, in one template's own markup.

    HTML has no nested forms: the parser drops the inner opening tag and takes
    the inner closing tag as the end of the outer form, so every control after
    it silently belongs to no form. A save button that saves nothing.
    """
    found, depth, current = [], 0, None
    for path, number, line in _template_lines():
        if path != current:
            current, depth = path, 0
        for match in _FORM_TAG.finditer(line):
            if match.group(1):
                depth = max(depth - 1, 0)
            else:
                depth += 1
                if depth > 1:
                    found.append((path, number))
    return found


@checks.register("interface")
def no_nested_forms(app_configs=None, **kwargs):
    return [
        checks.Warning(
            f"Form opened inside another form at {path}:{line}.",
            hint="Close the outer form first, or point the control at a form elsewhere with form=\"id\".",
            id="hq.W102",
        )
        for path, line in nested_forms()
    ]


@checks.register("interface")
def no_em_dashes(app_configs=None, **kwargs):
    return [
        checks.Warning(
            f"Em dash in interface text at {path}:{line}.",
            hint="Rewrite the sentence. For a missing value use `or_empty` or MISSING.",
            id="hq.W100",
        )
        for path, line in em_dashes()
    ]


@checks.register("interface")
def no_hand_plurals(app_configs=None, **kwargs):
    return [
        checks.Warning(
            f"Plural built by hand at {path}:{line}.",
            hint=(
                "Use counted(n, one, many) or {{ n|counted:\"one,many\" }} so the "
                "noun and its verb agree for one and for many."
            ),
            id="hq.W101",
        )
        for path, line in hand_plurals()
    ]


@checks.register(checks.Tags.security, deploy=True)
def deployment_identity_is_set(app_configs=None, **kwargs):
    """A deployment names its own host and sign-in issuer; the defaults are for development."""
    problems = []
    if settings.SEVERINO_SITE_HOST == "localhost":
        problems.append(
            checks.Warning(
                "The site host resolves to localhost.",
                hint="Set DJANGO_CSRF_TRUSTED_ORIGINS or DJANGO_ALLOWED_HOSTS to the host "
                "this deployment is reached at, or SEVERINO_SITE_HOST to override.",
                id="hq.W111",
            )
        )
    if not settings.OIDC_ISSUER:
        problems.append(
            checks.Warning(
                "SEVERINO_OIDC_ISSUER is unset, so every sign-in is refused.",
                hint="Set it to the identity provider's issuer URL.",
                id="hq.W112",
            )
        )
    return problems


@checks.register(checks.Tags.security, deploy=True)
def static_live_needs_debug(app_configs=None, **kwargs):
    """Serving static files from the source trees is a development mode only."""
    if getattr(settings, "STATIC_LIVE", False) and not settings.DEBUG:
        return [
            checks.Error(
                "STATIC_LIVE is on while DEBUG is off.",
                hint="Unset DJANGO_WHITENOISE_AUTOREFRESH, or turn DEBUG on for local development.",
                id="hq.E110",
            )
        ]
    return []
