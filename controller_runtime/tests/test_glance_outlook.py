"""What the dashboard says about the hours ahead and the containers' share."""

from __future__ import annotations

from django.test import SimpleTestCase

from controller_runtime.glance import _ahead, _alert, _container_metrics, _outlook


def _period(hour: int, temperature: int, chance: int | None = 0, forecast: str = "Clear"):
    return {
        "startTime": f"2026-09-30T{hour:02d}:00:00-05:00",
        "temperature": temperature,
        "shortForecast": forecast,
        "probabilityOfPrecipitation": {"value": chance},
    }


class OutlookTests(SimpleTestCase):
    def test_the_range_and_the_first_hour_rain_is_likely(self):
        periods = [_period(14, 83), _period(15, 86, 20), _period(16, 84, 60, "Showers"), _period(17, 71, 80)]

        self.assertEqual(
            _outlook(periods),
            [
                {"label": "Range", "value": "71–86°", "detail": "next 4 hours"},
                {"label": "Rain", "value": "4 PM · 60%", "detail": "Showers"},
            ],
        )

    def test_rain_already_falling_is_now(self):
        self.assertEqual(_outlook([_period(9, 60, 70, "Rain")])[1]["value"], "Now · 70%")

    def test_no_rain_worth_naming_says_nothing_about_it(self):
        labels = [item["label"] for item in _outlook([_period(0, 60, None), _period(12, 70, 29)])]
        self.assertEqual(labels, ["Range"])

    def test_the_hours_ahead_read_in_the_points_own_time(self):
        self.assertEqual(
            _ahead([_period(0, 60), _period(12, 70, 40, "Showers")]),
            [
                {"time": "12 AM", "temperature": "60°", "forecast": "Clear", "precipitation": "0%"},
                {"time": "12 PM", "temperature": "70°", "forecast": "Showers", "precipitation": "40%"},
            ],
        )

    def test_an_alert_is_named_with_how_many_more(self):
        features = [
            {"properties": {"event": "Heat Advisory", "headline": "Until 8 PM"}},
            {"properties": {"event": "Air Quality Alert"}},
        ]
        self.assertEqual(
            _alert(features),
            [{"label": "Alerts", "value": "Heat Advisory +1", "detail": "Until 8 PM"}],
        )
        self.assertEqual(_alert([]), [])


class ContainerShareTests(SimpleTestCase):
    def test_cpu_is_a_share_of_the_machine_and_memory_of_its_total(self):
        metrics = {
            item["label"]: item
            for item in _container_metrics(12, 80.0, 8, 4 * 1024**3, 16 * 1024**3, 0)
        }
        self.assertEqual(metrics["Containers"]["value"], "12")
        self.assertEqual(metrics["CPU"]["value"], "10%")
        self.assertEqual(metrics["Memory"]["value"], "25%")
        self.assertIn("of", metrics["Memory"]["detail"])
