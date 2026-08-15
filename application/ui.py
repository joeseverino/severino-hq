"""Stable view models consumed by HQ's shared server-rendered UI primitives."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Kpi:
    label: str
    value: str | int
    detail: str = ""
    url: str = ""
    is_zero: bool = False
