from django import template


register = template.Library()


@register.inclusion_tag("partials/_table_sort_header.html", takes_context=True)
def table_sort_header(context, label, ascending, descending=""):
    table = context["table"]
    current = table["selected_sort"]
    descending = descending or f"-{ascending}"
    next_sort = ascending if current == descending else descending
    params = context["request"].GET.copy()
    params["sort"] = next_sort
    params.pop("page", None)
    direction = (
        "ascending"
        if current == ascending
        else "descending"
        if current == descending
        else "none"
    )
    return {
        "label": label,
        "url": f"?{params.urlencode()}",
        "direction": direction,
    }
