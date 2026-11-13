"""Human labels for stable machine names: capabilities, providers, kinds."""

lazy from django.utils.html import conditional_escape
lazy from django.utils.safestring import SafeString, mark_safe

_ACRONYMS = frozenset({"acl", "api", "dns", "hq", "id", "mcp", "npm", "nws", "ssh", "tls", "url", "vin", "vm"})


def human_label(name: str) -> str:
    """``tls.certificate`` → "TLS Certificate", ``cloudflare_dns`` → "Cloudflare DNS"."""

    words = name.replace(".", " ").replace("_", " ").split()
    return " ".join(word.upper() if word in _ACRONYMS else word.title() for word in words)


def field_label(name: str, title: str | None = None) -> str:
    """A field as a person names it: its declared title, else its name as a
    sentence (``confirm_prune`` → "Confirm prune", ``dns_name`` → "DNS name").
    The one labeler of a field, wherever its name is shown."""

    if title:
        return title
    words = name.replace(".", " ").replace("_", " ").split()
    said = " ".join(word.upper() if word in _ACRONYMS else word for word in words)
    return said[:1].upper() + said[1:]


def one(noun: str) -> str:
    """``noun`` after its indefinite article, as a command names what it acts
    on: "an asset", "a receipt"."""

    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


# What a form that changes a record it already holds is submitted by.
SAVE_CHANGES = "Save changes"


def adding(noun: str) -> str:
    """What makes one new ``noun``, on its list, its form and its command:
    "Add an asset". The one create verb of Design's one-name table."""

    return f"Add {one(noun)}"


def lower_first(text: str) -> str:
    """A label as it reads mid-sentence, leaving an acronym alone.

    "Proxy host" belongs lowercase after "Add"; "TLS certificate" does not, and
    lowering its first letter would produce "tLS certificate". A word whose second
    letter is a capital ("HQ", "TLS", "IPv6") is an acronym and keeps its case.
    """

    if text[1:2].isupper():
        return text
    return text[:1].lower() + text[1:]


def sentence(text: str) -> str:
    """A label in sentence case, as every status reads: "exited" → "Exited",
    "Changed outside HQ" as it is. The rest of the words keep their own case,
    so a name or an acronym inside it survives; an acronym first ("iOS",
    "HQ") is left alone, as ``lower_first`` leaves it, and so is a first word
    holding a digit, a version or a name ("v1.2.3", "k3s"), which reads as
    written."""

    first = text.split(maxsplit=1)[0] if text.strip() else ""
    if text[1:2].isupper() or any(character.isdigit() for character in first):
        return text
    return text[:1].upper() + text[1:]


_SIBILANT_ENDINGS = ("s", "x", "z", "ch", "sh")


def plural(noun: str) -> str:
    """Many of a noun, as English spells it: "entries", "addresses", "hosts".

    The one place a plural is derived from a singular. A noun whose plural
    this cannot reach ("person") is given whole to ``counted`` instead, or
    declared as its model's ``verbose_name_plural``.
    """

    lowered = noun.lower()
    if lowered.endswith("y") and lowered[-2:-1] not in ("", *"aeiou"):
        return f"{noun[:-1]}ies"
    if lowered.endswith(_SIBILANT_ENDINGS):
        return f"{noun}es"
    return f"{noun}s"


def human_bytes(value: int | float) -> str:
    """A byte count as people say it: ``512 B``, ``38 MB``, ``1.7 GB``."""

    amount = max(0.0, float(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit in {"B", "KB", "MB"} else f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


# The dot between two parts of one line, held to the part before it by a
# no-break space so no line starts with it or holds it alone. {% dot %} draws
# it as DOT_MARKUP, which hides the mark from a reader; so does `breakable`,
# drawing text that holds it.
DOT = "\u00a0· "
DOT_MARKUP = '&nbsp;<span aria-hidden="true">·</span> '


def _drawn(part: object) -> bool:
    return part is not None and part != ""


def dotted(*parts: object) -> str:
    """Parts of one line of text joined by ``DOT``, leaving out an empty one."""

    return DOT.join(str(part) for part in parts if _drawn(part))


def dotted_html(*parts: object) -> SafeString:
    """``dotted`` for markup: each part escaped unless it is markup already,
    joined by ``{% dot %}``'s mark."""

    return mark_safe(DOT_MARKUP.join(conditional_escape(part) for part in parts if _drawn(part)))  # noqa: S308 - each part escaped here


# What running a command does, in the words every page uses for it: the search
# results, the command's own page and the agents table.
EFFECT_LABELS = {
    "read": "Reads only",
    "remote_write": "Changes HQ",
    "infrastructure_change": "Changes a live system",
    "destructive": "Stops or deletes",
}


def effect_label(effect: str) -> str:
    return EFFECT_LABELS.get(effect, "")
