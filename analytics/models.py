"""What the site was actually asked for, and how it answered.

Two tables and one rule: Cloudflare is asked once, per day, per breakdown, and
every surface downstream derives from what that produced. Pages, writeups,
referrers and the dashboard tile are projections of ``RumDaily`` -- none of
them queries Cloudflare, and none of them keeps its own copy.

Why store at all, when the API is right there. Three reasons, and the first is
the weakest: retention is 184 days on this plan, so history beyond that exists
only if HQ keeps it. The second is the quota -- 300 queries per five minutes,
account-wide -- which a page rendering on every operator visit would spend on
re-asking the same settled question about last month. The third is the one that
actually decides it: a reading is evidence. A page that fetches live can only
say what is true now, and cannot say what was true in June, which is the
question anyone looking at a traffic page is really asking.

Units are normalised at ingest, once. Cloudflare reports timings in
microseconds and reports "no data" as -1; both of those are provider details
and neither survives into this table. Timings are milliseconds, absence is
NULL, and no template has to know that Cloudflare ever said otherwise.
"""

from __future__ import annotations

from django.db import models


class AnalyticsSite(models.Model):
    """One Web Analytics site, as the credential reports it.

    Derived, never declared. The account can hold sites that describe nothing
    -- Cloudflare keeps one after whatever it measured goes away -- so the sync
    admits only sites with a ruleset bound to a hostname. That filter is the
    whole membership rule; there is no list of site tags anywhere in HQ, which
    is what keeps a new site a matter of enabling it at Cloudflare.
    """

    site_tag = models.CharField(max_length=64)
    host = models.CharField(max_length=255)
    # Which credential observed it. Two accounts would mean two connections,
    # and a reading has to say which one it came from or it cannot be refreshed.
    connection_ref = models.CharField(max_length=100, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    observed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("host",)
        constraints = (
            models.UniqueConstraint(
                fields=("connection_ref", "site_tag"),
                name="analytics_site_ref_tag_unique",
            ),
        )

    def __str__(self) -> str:
        return self.host or self.site_tag


class RumDaily(models.Model):
    """One day of one breakdown: the single grain everything else derives from.

    A row is (site, date, dimension, value) -- so ``/contact/`` on 12 August is
    one row, and so is ``google.com`` on 12 August. Storing breakdowns as rows
    of one table rather than as columns of several is what lets a new breakdown
    be a new ``Dimension`` member and a sync loop entry, instead of a migration,
    a model, a read model and a template each time.

    The cost is honest and worth naming: dimensions cannot be crossed. This
    answers "which pages" and "which referrers", never "which referrers sent
    traffic to which page". Cloudflare's own free view does not cross them
    either, and a table that could would be a different shape than this one --
    added beside it when something actually needs it, not anticipated here.
    """

    class Dimension(models.TextChoices):
        PATH = "path", "Page"
        REFERRER = "referrer", "Referrer"
        COUNTRY = "country", "Country"
        DEVICE = "device", "Device"
        BROWSER = "browser", "Browser"
        OS = "os", "Operating system"

    site = models.ForeignKey(
        AnalyticsSite, on_delete=models.CASCADE, related_name="daily"
    )
    date = models.DateField()
    dimension = models.CharField(max_length=16, choices=Dimension.choices)
    # A path keeps its leading slash and trailing slash exactly as the browser
    # reported it, because that is what joins to a published URL. Normalising
    # here would mean two rows that differ only by a slash silently merging,
    # and the site genuinely serves one of those and 404s the other.
    value = models.CharField(max_length=512)

    # Cloudflare's ``count`` is already extrapolated from the sample, so this is
    # an estimate and is stored as one. ``sample_interval`` is kept beside it so
    # a surface can say how much to trust the number rather than presenting a
    # figure derived from two beacons as though it were a census.
    pageviews = models.PositiveIntegerField(default=0)
    visits = models.PositiveIntegerField(default=0)
    sample_interval = models.PositiveIntegerField(default=1)

    observed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-date", "-pageviews")
        constraints = (
            models.UniqueConstraint(
                fields=("site", "date", "dimension", "value"),
                name="analytics_rumdaily_unique_grain",
            ),
        )
        indexes = (
            # The three shapes every read model asks for: one breakdown over a
            # window, one path's history, and the window itself.
            models.Index(fields=("site", "dimension", "-date")),
            models.Index(fields=("dimension", "value", "-date")),
            models.Index(
                fields=("site", "dimension", "value", "-date"),
                name="analytics_rum_loc_date_idx",
            ),
        )

    def __str__(self) -> str:
        return f"{self.date} {self.dimension}={self.value}"


class VitalsDaily(models.Model):
    """One day of Core Web Vitals for a site.

    Site-wide rather than per-path, deliberately. The vitals dataset is sampled
    the same way the pageload one is, and splitting a day's handful of beacons
    across twenty paths produces twenty numbers that each rest on nothing. A
    path dimension can be added when there is traffic to carry it; until then
    the honest grain is the whole site.

    Every timing is milliseconds and nullable. A percentile Cloudflare has no
    data for arrives as -1, which is absence wearing a number's clothes -- it is
    stored as NULL so that averaging a column cannot quietly produce a negative
    load time.
    """

    site = models.ForeignKey(
        AnalyticsSite, on_delete=models.CASCADE, related_name="vitals"
    )
    date = models.DateField()

    largest_contentful_paint_ms = models.PositiveIntegerField(null=True, blank=True)
    interaction_to_next_paint_ms = models.PositiveIntegerField(null=True, blank=True)
    first_contentful_paint_ms = models.PositiveIntegerField(null=True, blank=True)
    time_to_first_byte_ms = models.PositiveIntegerField(null=True, blank=True)
    # Unitless, and small. Four decimal places is more than the metric resolves.
    cumulative_layout_shift = models.DecimalField(
        max_digits=6, decimal_places=4, null=True, blank=True
    )

    # The pass-rate buckets, which are what "Core Web Vitals" actually means:
    # a metric passes when 75% of samples are good. Stored as counts rather than
    # as a computed rate so a window of days can be summed before dividing --
    # averaging daily percentages across days of unequal traffic is wrong, and
    # storing the rate would make it the only thing possible.
    lcp_good = models.PositiveIntegerField(default=0)
    lcp_needs_improvement = models.PositiveIntegerField(default=0)
    lcp_poor = models.PositiveIntegerField(default=0)
    inp_good = models.PositiveIntegerField(default=0)
    inp_needs_improvement = models.PositiveIntegerField(default=0)
    inp_poor = models.PositiveIntegerField(default=0)
    cls_good = models.PositiveIntegerField(default=0)
    cls_needs_improvement = models.PositiveIntegerField(default=0)
    cls_poor = models.PositiveIntegerField(default=0)

    sample_interval = models.PositiveIntegerField(default=1)
    observed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-date",)
        constraints = (
            models.UniqueConstraint(
                fields=("site", "date"), name="analytics_vitalsdaily_unique_day"
            ),
        )

    def __str__(self) -> str:
        return f"{self.site_id} vitals {self.date}"
