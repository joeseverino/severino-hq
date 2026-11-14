"""perf reads its tools' output as those tools print it."""

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
    def test_median_and_interpolated_p95(self):
        values = [float(value) for value in range(20, 0, -1)]
        # 1 to 20: the median halfway between 10 and 11, the 95th a twentieth of the way from 19 to 20.
        self.assertEqual(parse.percentiles(values), (10.5, 19.05))

    def test_one_value_is_both(self):
        self.assertEqual(parse.percentiles([4.2]), (4.2, 4.2))


class MemoryTests(SimpleTestCase):
    def test_footprint_sums_each_process_once(self):
        """The summary line repeats the processes' sum; counting it too would double it."""

        self.assertEqual(parse.memory_bytes(FOOTPRINT_TWO), 1900952 + 1900000)

    def test_footprint_that_found_no_process_is_refused(self):
        with self.assertRaises(ValueError):
            parse.memory_bytes("footprint: no process matching 123")

    def test_a_linux_process_s_memory_is_its_proportional_set_and_swapped_share(self):
        # /proc/<pid>/smaps_rollup as proc(5) prints it.
        rollup = (
            "00400000-ffffffffff601000 ---p 00000000 00:00 0                  [rollup]\n"
            "Rss:                 884 kB\nPss:                 385 kB\nPss_Anon:            204 kB\n"
            "Pss_File:            181 kB\nPss_Shmem:             0 kB\nShared_Clean:        696 kB\n"
            "Private_Dirty:       128 kB\nAnonymous:           204 kB\nSwap:                 40 kB\n"
            "SwapPss:              12 kB\nLocked:                0 kB\n"
        )
        self.assertEqual(parse.memory_bytes(rollup), (385 + 12) * 1024)

    def test_a_rollup_naming_no_pss_is_refused(self):
        with self.assertRaises(ValueError):
            parse.memory_bytes("")

    def test_a_container_s_anonymous_memory_is_read_from_its_memory_stat(self):
        # The first lines of a cgroup v2 memory.stat, as a container's reads.
        stat = "anon 102043648\nfile 4096\nkernel 5259264\nkernel_stack 49152\npagetables 446464\n"
        self.assertEqual(parse.container_anon_bytes(stat), 102043648)

    def test_a_memory_stat_without_anonymous_memory_is_refused(self):
        for output in ("", "file 4096\n", "cat: /sys/fs/cgroup/memory.stat: No such file or directory"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse.container_anon_bytes(output)


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


# The rows of LIST_PAGE a record draws: each record, the line it holds back, and the chart's value.
RECORD_ROWS = (
    '<tr><td>one<br><img src="/a.png"></td></tr>',
    '<tr class="row-detail" hidden><td>held</td></tr>',
    '<tr data-attention-item><td><input type="checkbox">two</td></tr>',
    '<tr class="row-detail" hidden><td>held</td></tr>',
    "<tr><th>Mon</th><td>3</td></tr>",
)


class DataRowTests(SimpleTestCase):
    def test_counts_the_records_in_main_s_data_tables(self):
        self.assertEqual(parse.data_rows(LIST_PAGE).count, 3)

    def test_measures_the_html_of_the_rows_records_draw_and_no_other(self):
        self.assertEqual(parse.data_rows(LIST_PAGE).html_bytes, sum(len(row.encode()) for row in RECORD_ROWS))

    def test_a_row_s_bytes_are_its_encoded_text(self):
        page = '<main><table class="data-table"><tbody>\n<tr><td>caf\u00e9</td></tr></tbody></table></main>'
        self.assertEqual(parse.data_rows(page).html_bytes, len("<tr><td>caf\u00e9</td></tr>".encode()))

    def test_a_page_without_one_holds_none(self):
        self.assertEqual(parse.data_rows("<main><p>Nothing here yet.</p></main>"), (0, 0))
