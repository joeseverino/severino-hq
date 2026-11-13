"""An ask says each thing once: its state's word, its sentence and the line
under a row never say the same thing twice, whoever draws it."""

from django.test import TestCase

from hq.domains.jobs.models import JobState
from hq.platform.application import asks
from hq.platform.application.asks import FAILED, IDLE, QUEUED, Standing
from hq.platform.application.tests.markup import Element, all_elements
from hq.platform.core.tests.browser.catalog import CATALOG


def _bare(said: str) -> str:
    return said.strip().rstrip(".…:").strip().casefold()


def _inside(control: Element, found: list[Element], name: str) -> list[str]:
    return [
        element.words
        for element in found
        if name in element.classes or name in element.attrs
        if any(ancestor is control for ancestor in element.ancestors)
    ]


def _notes(control: Element, found: list[Element]) -> list[Element]:
    return [
        element
        for element in found
        if "data-ask-note" in element.attrs and any(ancestor is control for ancestor in element.ancestors)
    ]


def _parted(note: Element, found: list[Element]) -> bool:
    """Whether the note says its word and its sentence as parts of their own."""

    return any(
        {"ask-word", "ask-sentence"} & element.classes and any(ancestor is note for ancestor in element.ancestors)
        for element in found
    )


class StandingWordsTests(TestCase):
    def test_a_sentence_that_only_says_its_state_s_word_is_dropped(self):
        sentences = (asks.WAITING, asks.REFRESHING, asks.REFRESHED)
        words = {*(state.label for state in JobState), *("Waiting", "Running", "Done", "Failed")}
        for word in words:
            for said in (word, f"{word}…", f"{word}.", f" {word.lower()} "):
                with self.subTest(word=word, said=said):
                    standing = Standing(QUEUED, said, word=word)
                    self.assertEqual(standing.note, "")
                    self.assertEqual(standing.as_json()["note"], "")
                    self.assertEqual(standing.label, word)
        for said in sentences:
            with self.subTest(said=said):
                standing = Standing(QUEUED, said)
                self.assertNotEqual(_bare(standing.note) or None, _bare(standing.label))

    def test_a_sentence_that_says_more_is_kept(self):
        self.assertEqual(Standing(QUEUED, "Waiting for the example worker.").note, "Waiting for the example worker.")
        self.assertEqual(Standing(FAILED, "Failed to reach example-host-01.").note, "Failed to reach example-host-01.")

    def test_idle_says_nothing(self):
        self.assertEqual(Standing(IDLE).label, "")


class CatalogAskWordsTests(TestCase):
    """Every ask the catalog draws: what its note, its row's line and its
    panel say are each said once."""

    def test_no_ask_in_the_catalog_says_one_thing_twice(self):
        checked = 0
        for name, entry in CATALOG.items():
            found = all_elements(entry.drawn(name))
            for control in (element for element in found if "data-ask" in element.attrs):
                checked += 1
                with self.subTest(entry=name):
                    said = [
                        *_inside(control, found, "ask-word"),
                        *_inside(control, found, "ask-sentence"),
                        *_inside(control, found, "data-ask-label"),
                        *(note.words for note in _notes(control, found) if not _parted(note, found)),
                    ]
                    bare = [_bare(each) for each in said if _bare(each)]
                    self.assertEqual(len(bare), len(set(bare)), f"{name} says {said}")
                    for why in _inside(control, found, "ask-why"):
                        word, _, sentence = why.partition(":")
                        self.assertTrue(sentence.strip(), f"{name}'s line under its row says only {why!r}")
                        self.assertNotEqual(_bare(word), _bare(sentence), f"{name}'s line says {why!r}")
        self.assertTrue(checked)
