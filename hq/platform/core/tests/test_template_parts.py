"""What every shared part promises a page, held for each part written as its
``example``, given what its catalog entry gives it: what it does not take, or
a required option left out, is refused as the template compiles, a value
outside an option's choices as it draws, and words a page gives are never
drawn as markup."""

import re
from dataclasses import replace

from django.template import Template, TemplateSyntaxError
from django.test import SimpleTestCase
from django.utils.text import smart_split

from hq.platform.application.markup_testing import select
from hq.platform.core.template_contract import parts
from hq.platform.core.template_parts import Part
from hq.platform.core.tests.browser.catalog import CATALOG, default_name

# Words a page gives, drawn as markup only where a part fails to escape them.
HOSTILE = "<i data-hostile></i>"


def _tags() -> list[Part]:
    return [part for part in parts() if part.name]


def _opening(part: Part) -> re.Match[str]:
    found = re.search(r"\{%\s*" + re.escape(part.name) + r"(?=[\s%])(.*?)%\}", part.example, re.DOTALL)
    assert found, f"{part.name}'s example writes its tag"
    return found


def _bits(part: Part) -> list[str]:
    """What the example's own opening tag of the part gives it."""

    return list(smart_split(_opening(part)[1].strip()))


def _written(part: Part, bits: list[str]) -> str:
    """Its example, the part's opening tag giving ``bits`` instead."""

    opening = _opening(part)
    return f"{part.example[: opening.start()]}{{% {' '.join((part.name, *bits))} %}}{part.example[opening.end() :]}"


def _without(part: Part, name: str) -> list[str]:
    return [bit for bit in _bits(part) if not bit.startswith(f"{name}=")]


def _drawn(part: Part, bits: list[str]) -> str:
    entry = CATALOG[default_name(part)]
    given = replace(entry, template=_written(part, bits), context={**entry.context, "hostile": HOSTILE})
    return given.part(default_name(part))


class EveryPartTests(SimpleTestCase):
    def test_what_a_part_does_not_take_is_refused_as_the_template_compiles(self):
        for part in _tags():
            if any(name.startswith("**") for name in part.options):
                continue  # A part that gathers options says itself which it takes.
            with self.subTest(part.name), self.assertRaises(TemplateSyntaxError):
                Template(_written(part, [*_bits(part), 'unknown_option="x"']))

    def test_a_required_option_left_out_is_refused_as_the_template_compiles(self):
        for part in _tags():
            for name in part.missing(set()):
                with self.subTest(part.name, option=name), self.assertRaisesMessage(TemplateSyntaxError, name):
                    Template(_written(part, _without(part, name)))

    def test_a_value_outside_an_option_s_choices_is_refused_as_the_part_draws(self):
        for part in _tags():
            for name in (name for name, option in (part.ordered | part.options).items() if option.choices):
                refused = self.assertRaisesMessage(TemplateSyntaxError, f"{part.name} {name}= is one of")
                with self.subTest(part.name, option=name), refused:
                    _drawn(part, [*_without(part, name), f'{name}="hq-no-such-choice"'])

    def test_words_a_page_gives_are_never_drawn_as_markup(self):
        """Each option that takes words, and each word the example's own tag
        writes, given markup in their place. A part may refuse it; it never
        draws it."""

        for part in _tags():
            bits = _bits(part)
            given = {
                name: [*_without(part, name), f"{name}=hostile"]
                for name, option in part.options.items()
                if not option.choices and isinstance(option.default, str) and not name.startswith("*")
            }
            given |= {
                f"#{at}": [*bits[:at], "hostile", *bits[at + 1 :]]
                for at, bit in enumerate(bits)
                if re.fullmatch(r"(['\"]).*\1", bit)
            }
            for said, written in given.items():
                with self.subTest(part.name, given=said):
                    try:
                        html = _drawn(part, written)
                    except Exception:  # noqa: BLE001 - an option that takes no words may refuse them any way; it never draws them
                        continue
                    self.assertEqual(select(html, "data-hostile"), [])
