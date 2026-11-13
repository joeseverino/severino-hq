"""Empty lines, toned values, lists of rows and figures, for the rows wave: the host pages that still draw these by hand."""

BASELINE: dict[tuple[str, str], int] = {
    ("calendars/calendar.html", "an empty line"): 1,
    ("content/content_detail.html", "an empty line"): 1,
    ("control_plane/_service_rows.html", "an empty line"): 1,
    ("control_plane/_tool_dns.html", "an empty line"): 1,
    ("control_plane/container_list.html", "an empty line"): 1,
    ("control_plane/machine_detail.html", "a toned value"): 1,
    ("control_plane/resource_detail.html", "an empty line"): 1,
    ("control_plane/tools.html", "an empty line"): 1,
    ("core/agent_policy.html", "a toned value"): 4,
    ("core/agent_policy.html", "an empty line"): 2,
    ("core/hq.html", "an empty line"): 2,
    ("dashboard.html", "a list of rows"): 1,
    ("partials/_posture.html", "a toned value"): 1,
    ("partials/_related.html", "a list of rows"): 1,
    ("projects/_updates.html", "an empty line"): 2,
    ("projects/project_list.html", "a toned value"): 1,
    ("reports/reports.html", "a list of rows"): 4,
}
