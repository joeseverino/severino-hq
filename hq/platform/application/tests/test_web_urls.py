"""An href built from data someone else wrote is a web address or nothing.

``web_url``'s promise is a property, over text Hypothesis (a development tool
the image lacks) writes, and every hostile link seen so far.
"""

import re
import string
from unittest import skipUnless

from django.test import SimpleTestCase

from ..entity_links import web_url

try:
    from hypothesis import example, given, strategies as st
except ImportError:  # pragma: no cover - the image does not carry it
    HYPOTHESIS = False
else:
    HYPOTHESIS = True

HOSTILE = (
    "javascript:fetch('/api/v2/')",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox",
    "https://",
    "//evil.example.com/path",
    "file:///etc/passwd",
)


def _as_a_browser_reads(href: str) -> str:
    """``href`` with what a browser drops before reading a scheme (the URL
    Standard's C0 controls and spaces at the ends, tabs and newlines anywhere)."""

    return re.sub(r"[\t\n\r]", "", href.strip("".join(map(chr, range(0x21)))))


if HYPOTHESIS:
    SCHEMES = st.sampled_from(("http", "https", "HTTPS", "javascript", "data", "file", "vbscript", ""))
    # Text that starts like an address, so the property reaches past the scheme.
    ADDRESSY = st.builds(
        lambda before, scheme, separator, rest: f"{before}{scheme}{separator}{rest}",
        st.text(" \t\n\x00\x1f", max_size=2),
        SCHEMES,
        st.sampled_from((":", "://", ":/", "//", ":\\\\", "\t://")),
        st.text(max_size=30),
    )
    HOST = st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,8}[a-z0-9])?(?:\.[a-z0-9]{1,8}){0,3}", fullmatch=True)
    PATH = st.text(string.ascii_letters + string.digits + "-._~/%?=&#", max_size=30).map(lambda path: "/" + path)
    PORT = st.none() | st.integers(1, 65535)


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class WebUrlProperties(SimpleTestCase):
    if HYPOTHESIS:

        @given(st.sampled_from(("http", "https")), HOST, PORT, PATH)
        @example("http", "app.example.com", 8080, "/x?y=1")
        def test_a_web_address_passes_as_written(self, scheme, host, port, path):
            url = f"{scheme}://{host}{f':{port}' if port else ''}{path}"

            self.assertEqual(web_url(url), url)

        @given(st.one_of(st.text(), ADDRESSY, st.none(), st.integers()))
        @example("")
        @example(42)
        @example("http://[")
        @example("HTTP://\x08")
        @example("HTTP://\\")
        @example("HTTP:\\\\0")
        def test_whatever_it_returns_a_browser_reads_as_a_web_address(self, value):
            """Never raises; an href is http(s) and names a host, as a browser reads it."""

            href = web_url(value)

            if value in HOSTILE:
                self.assertEqual(href, "")
            if href:
                self.assertRegex(_as_a_browser_reads(href), r"(?i)^https?://[^/?#\\]")

        # Each link seen hostile so far is tried on every run.
        for hostile in HOSTILE:
            test_whatever_it_returns_a_browser_reads_as_a_web_address = example(hostile)(
                test_whatever_it_returns_a_browser_reads_as_a_web_address
            )
        del hostile
