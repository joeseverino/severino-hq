"""What the site was actually asked for, and how it answered.

Cloudflare is asked once, per day, per breakdown, and every surface (pages,
writeups, referrers, the dashboard tile) is a projection of ``RumDaily``. HQ
stores it because a reading is evidence: a live fetch cannot say what was true
in June, the plan keeps 184 days, and its account-wide quota (300 queries per
five minutes) is no budget for re-asking settled questions on every visit.
"""

from typing import override

from django.db import models

from hq.platform.core.rules import Rule


def _sampled(name: str) -> Rule:
    """A sample interval counts events per sample, so it is at least one."""

    return Rule(
        condition=models.Q(sample_interval__gte=1),
        name=name,
        violation_error_message="A sample interval is at least 1.",
        field="sample_interval",
    )


class AnalyticsSite(models.Model):
    """One Web Analytics site, as the credential reports it.

    Derived, never declared. The account can hold sites that describe nothing
    Cloudflare keeps one after whatever it measured goes away, so the sync
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

    @override
    def __str__(self) -> str:
        return self.host or self.site_tag


class AnalyticsCoverage(models.Model):
    """One completed day a site's credential successfully read.

    Traffic rows cannot carry this fact: a healthy site can have a real zero,
    and therefore no breakdown rows, for a day.  Recording coverage separately
    lets HQ distinguish "nothing happened" from "nobody looked", derive exact
    backfill work, and say when a requested window is partial without asking
    Cloudflare again merely to find out.
    """

    site = models.ForeignKey(AnalyticsSite, on_delete=models.CASCADE, related_name="coverage")
    date = models.DateField()

    class Meta:
        ordering = ("-date",)
        constraints = (models.UniqueConstraint(fields=("site", "date"), name="analytics_coverage_unique_day"),)
        indexes = (models.Index(fields=("date",)),)

    @override
    def __str__(self) -> str:
        return f"{self.site_id} covered {self.date}"


class RumDaily(models.Model):
    """One day of one breakdown: the single grain everything else derives from.

    A row is (site, date, dimension, value), so a new breakdown is a new
    ``Dimension`` member and a sync loop entry rather than a migration, a model,
    a read model and a template. The cost: dimensions cannot be crossed ("which
    referrers sent traffic to which page"), as Cloudflare's free view cannot; a
    table that could is a different shape, added beside this one when needed.
    """

    class Dimension(models.TextChoices):
        PATH = "path", "Page"
        REFERRER = "referrer", "Referrer"
        COUNTRY = "country", "Country"
        DEVICE = "device", "Device"
        BROWSER = "browser", "Browser"
        OS = "os", "Operating system"

    site = models.ForeignKey(AnalyticsSite, on_delete=models.CASCADE, related_name="daily")
    date = models.DateField()
    dimension = models.CharField(max_length=16, choices=Dimension.choices)
    # A path keeps its leading slash and trailing slash exactly as the browser
    # reported it, because that is what joins to a published URL. Normalizing
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
            _sampled("analytics_rumdaily_sample_interval"),
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

    @override
    def __str__(self) -> str:
        return f"{self.date} {self.dimension}={self.value}"


class VitalsDaily(models.Model):
    """One day of Core Web Vitals for a site.

    Site-wide until there is traffic to carry a path dimension: the dataset is
    sampled, and a day's handful of beacons split across twenty paths gives
    twenty numbers that each rest on nothing.
    """

    site = models.ForeignKey(AnalyticsSite, on_delete=models.CASCADE, related_name="vitals")
    date = models.DateField()

    # Nullable: a percentile Cloudflare has no data for arrives as -1, stored as
    # NULL so that averaging a column cannot quietly produce a negative time.
    largest_contentful_paint_ms = models.PositiveIntegerField(null=True, blank=True)
    interaction_to_next_paint_ms = models.PositiveIntegerField(null=True, blank=True)
    first_contentful_paint_ms = models.PositiveIntegerField(null=True, blank=True)
    time_to_first_byte_ms = models.PositiveIntegerField(null=True, blank=True)
    # Unitless, and small. Four decimal places is more than the metric resolves.
    cumulative_layout_shift = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)

    # The pass-rate buckets, which are what "Core Web Vitals" actually means:
    # a metric passes when 75% of samples are good. Stored as counts rather than
    # as a computed rate so a window of days can be summed before dividing,
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
            models.UniqueConstraint(fields=("site", "date"), name="analytics_vitalsdaily_unique_day"),
            _sampled("analytics_vitalsdaily_sample_interval"),
        )

    @override
    def __str__(self) -> str:
        return f"{self.site_id} vitals {self.date}"
