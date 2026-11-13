"""The files whose shown text still breaks a wording rule lint:prose grew, and
how many times.

Each entry is a file to rewrite. ``plain_words.held`` fails a file with more
findings of a rule than its entry, and, when lint:prose reads the whole tree,
one with fewer, whose entry must come down with the fix. So this only falls.
``mise run lint:prose`` names both; ``plain_words --whole --census`` prints
the entries the tree holds now, ``plain_words --list <paths>`` each finding
an entry counts, at its line (``uv run python -m hq.platform.core.plain_words``),
and ``mise run audit:prose-strings`` every line of text the rules read.
"""

# The rules a file may still break by its count here. Every other rule fails
# on its first finding.
BASELINED_RULES = frozenset({"Plain.Long", "Plain.Narrate", "Plain.Person", "Plain.Parts", "Label", "Kicker"})

BASELINE: dict[tuple[str, str], int] = {
    ("templates/partials/_connection_peering.html", "Label"): 1,
}
