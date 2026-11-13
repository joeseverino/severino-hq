"""For a form field Django lacks and HQ's renderer draws: one that takes several files."""

from hq.platform.application.forms import MultipleFileField, MultipleFileInput

__all__ = ["MultipleFileField", "MultipleFileInput"]
