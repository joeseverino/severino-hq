"""perf:report reads its tools' output as those tools print it."""

from django.test import SimpleTestCase

from tools.perf import parse

# `footprint -f bytes --noCategories -p <pid> -p <pid>` on macOS, as printed.
FOOTPRINT_TWO = """\
======================================================================
zsh [18913]: 64-bit    Footprint: 1900952 B (16384 bytes per page)
======================================================================

Auxiliary data:
    phys_footprint: 1900952 B
    phys_footprint_peak: 1950104 B

======================================================================
zsh [17645]: 64-bit    Footprint: 1900000 B (16384 bytes per page)
======================================================================

Auxiliary data:
    phys_footprint: 1900000 B
    phys_footprint_peak: 1950104 B

======================================================================
Summary Footprint: 3800952 B
======================================================================
"""


class ServerTimingTests(SimpleTestCase):
    def test_reads_the_app_metric_hq_sends(self):
        self.assertEqual(parse.server_time("app;dur=6.33"), 6.33)

    def test_reads_it_among_other_metrics_and_parameters(self):
        header = 'sql_time;dur=3.1;desc="SQL 4 queries", app;desc="view";dur=20.5, cache;dur=0.2'
        self.assertEqual(parse.server_time(header), 20.5)
        self.assertEqual(parse.server_time(header, "sql_time"), 3.1)

    def test_no_metric_or_no_duration_is_none(self):
        for header in ("", "cache;dur=1", "app", "app;desc=x", "application;dur=4"):
            with self.subTest(header=header):
                self.assertIsNone(parse.server_time(header))


class PercentileTests(SimpleTestCase):
    def test_median_and_nearest_rank_p95(self):
        values = [float(value) for value in range(20, 0, -1)]
        self.assertEqual(parse.percentiles(values), (10.5, 19.0))

    def test_one_value_is_both(self):
        self.assertEqual(parse.percentiles([4.2]), (4.2, 4.2))


class MemoryTests(SimpleTestCase):
    def test_footprint_sums_each_process_once(self):
        """The summary line repeats the processes' sum; counting it too would double it."""

        self.assertEqual(parse.footprint_bytes(FOOTPRINT_TWO), 1900952 + 1900000)

    def test_footprint_that_found_no_process_is_refused(self):
        with self.assertRaises(ValueError):
            parse.footprint_bytes("footprint: no process matching 123")

    def test_docker_memory_in_either_unit_system(self):
        self.assertEqual(parse.docker_memory_bytes("2.746MiB / 7.774GiB\n"), round(2.746 * 1024**2))
        self.assertEqual(parse.docker_memory_bytes("512kB / 1GB"), 512_000)
        self.assertEqual(parse.docker_memory_bytes("0B / 0B"), 0)

    def test_docker_without_a_usage_is_refused(self):
        for output in ("", "--", "Error response from daemon: No such container"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse.docker_memory_bytes(output)


class CookieTests(SimpleTestCase):
    def test_the_line_dev_session_ends_on(self):
        for output in ("sessionid=k3y\n", "Loaded 2 plugins\nsessionid=k3y\n"):
            with self.subTest(output=output):
                self.assertEqual(parse.cookie(output), ("sessionid", "k3y"))

    def test_anything_else_is_refused(self):
        for output in (
            "",
            "CommandError: dev_session runs only with DEBUG on\n",
            "[dev:session] sessionid=k3y\n",
            "sessionid=\n",
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse.cookie(output)


class ApiRefusalTests(SimpleTestCase):
    def test_the_refusal_hq_raises_is_read_from_a_log(self):
        log = (
            '  File "/tmp/checkout/hq/platform/application/plugins.py", line 141, in _validate_manifest\n'
            "django.core.exceptions.ImproperlyConfigured: Plugin 'example.ledger' requires API 5; HQ supports 4.\n"
        )
        self.assertEqual(parse.api_refusal(log), "Plugin 'example.ledger' requires API 5; HQ supports 4.")

    def test_another_failure_is_no_refusal(self):
        for log in (
            "",
            "OSError: [Errno 48] Address already in use",
            "ImproperlyConfigured: Invalid HQ plugin id 'x'.",
        ):
            with self.subTest(log=log):
                self.assertIsNone(parse.api_refusal(log))


# A page as HQ draws a list: a grouped table whose rows each hold back a line
# beneath, a chart's table of values, and tables outside main or no data table.
LIST_PAGE = """\
<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/s.css"></head><body>
<nav><table class="data-table"><tbody><tr><td>menu</td></tr></tbody></table></nav>
<main>
  <table class="data-table queue-table">
    <thead><tr><th>Name</th></tr></thead>
    <tbody>
      <tr class="row-group"><th>Open</th></tr>
      <tr><td>one<br><img src="/a.png"></td></tr>
      <tr class="row-detail" hidden><td>held</td></tr>
      <tr data-attention-item><td><input type="checkbox">two</td></tr>
      <tr class="row-detail" hidden><td>held</td></tr>
    </tbody>
  </table>
  <table class="calendar"><tbody><tr><td>1</td></tr></tbody></table>
  <details><table class="data-table compact-table"><tbody><tr><th>Mon</th><td>3</td></tr></tbody></table></details>
</main>
</body></html>
"""


class DataRowTests(SimpleTestCase):
    def test_counts_the_records_in_main_s_data_tables(self):
        self.assertEqual(parse.data_rows(LIST_PAGE), 3)

    def test_a_page_without_one_holds_none(self):
        self.assertEqual(parse.data_rows("<main><p>Nothing here yet.</p></main>"), 0)
