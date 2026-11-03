"""What dev:look's --eval returns names, for each element in it, the shot
file whose part of the page holds the element's box: the band, where the
shot is cut in bands."""

from django.test import SimpleTestCase

from hq.platform.core.tests import look


def _cut(height, band):
    """The shot files of a page ``height`` tall, cut as the look cuts it."""

    bands = look._bands(height, band)
    return [(f"page.band-{number:02}.png", top, tall) for number, (top, tall) in enumerate(bands, start=1)] or [
        ("page.png", 0, height)
    ]


def _box(top, bottom, *, shown=True):
    return {"named": "section#example", "top": top, "bottom": bottom, "shown": shown}


class ElementBandTests(SimpleTestCase):
    def placed(self, boxes, bands):
        returned = {"found": [{look._ELEMENT: place} for place in range(len(boxes))], "title": "Example"}
        return look._placed(returned, lambda: boxes, bands)

    def test_each_element_is_named_with_the_band_that_holds_its_box(self):
        bands = _cut(3000, 800)
        boxes = [_box(1700, 1750), _box(700, 900), _box(10, 10)]

        placed = self.placed(boxes, bands)

        self.assertEqual(placed["title"], "Example")
        said = placed["found"]
        self.assertTrue(said[0].endswith("in page.band-03.png"), said[0])
        self.assertIn("page.band-01.png to page.band-02.png", said[1])
        self.assertTrue(said[2].endswith("in page.band-01.png"), said[2])
        self.assertTrue(all(line.startswith("section#example") for line in said))

    def test_a_page_one_band_holds_names_its_shot(self):
        [said] = self.placed([_box(100, 200)], _cut(600, 800))["found"]

        self.assertTrue(said.endswith("in page.png"), said)

    def test_an_element_not_shown_or_past_the_shot_names_no_file(self):
        bands = _cut(3000, 800)
        said = self.placed([_box(0, 0, shown=False), _box(40_000, 40_100)], bands)["found"]

        for line in said:
            self.assertNotIn(".png", line)

    def test_a_value_without_an_element_reads_nothing_from_the_page(self):
        def unread():
            raise AssertionError("the page was asked for boxes")

        self.assertEqual(look._placed(["Example", {"a": 1}], unread, _cut(600, 800)), ["Example", {"a": 1}])
