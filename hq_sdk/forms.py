"""The form fields that take files, each declaring the largest it takes, which
the page holds before sending and HQ holds the proxy in front of it to."""

from hq.platform.application.forms import FileField, MultipleFileField, MultipleFileInput

__all__ = ["FileField", "MultipleFileField", "MultipleFileInput"]
