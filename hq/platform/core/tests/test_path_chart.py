"""Spatial drawings share the chart frame, legend and empty state."""

from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, text, with_class
from hq.platform.application.path_chart import PathChart, PathMarker, PathScale, PathSegment


class PathChartTests(SimpleTestCase):
    def test_geometry_and_readings_stay_in_the_shared_frame(self):
        tooltip = 'An "example" & reading.'
        chart = PathChart(
            title="Example path",
            description="The example path.",
            segments=(PathSegment("M 1 2 L 3 4", 2, tooltip),),
            markers=(PathMarker(1, 2, "Start"),),
            scale=PathScale(10.5, 20.5, 30.5, "100 m"),
            bands=((2, "An ordered band"),),
            width=400,
            height=200,
            note="An example reading.",
        )
        html = render_to_string("partials/_chart.html", {"chart": chart})
        (drawing,) = elements(html, "svg")
        self.assertEqual(drawing.attrs["viewbox"], "0 0 400 200")
        self.assertEqual(drawing.attrs["aria-label"], chart.description)
        (path,) = elements(html, "path")
        self.assertEqual(path.attrs["d"], chart.segments[0].path)
        self.assertEqual(path.attrs["data-tip"], tooltip)
        (scale,) = elements(html, "line")
        self.assertEqual(scale.attrs["x2"], "41.0")
        self.assertEqual(len(with_class(html, "chart-legend")), 1)
        self.assertIn("An ordered band", text(html))
        self.assertIn(chart.note, text(html))

    def test_an_empty_path_has_one_empty_state(self):
        chart = PathChart(title="Example path", description="The example path.", segments=(), width=400, height=200)
        html = render_to_string("partials/_chart.html", {"chart": chart})
        self.assertEqual(elements(html, "svg"), [])
        self.assertEqual(text(html).count("No path recorded."), 1)

    def test_the_page_frame_owns_the_printed_footer(self):
        html = Template(
            "{% page_main %}<p>An example page.</p>{% print_footer %}Printed example.{% endpage_main %}"
        ).render(Context())
        (main,) = elements(html, "main")
        (footer,) = with_class(html, "page-print-footer")
        self.assertEqual(footer.tag, "footer")
        self.assertEqual(footer.words, "Printed example.")
        self.assertIn(main, footer.ancestors)
