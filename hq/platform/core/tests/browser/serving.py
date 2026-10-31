"""What the gates answer the browser with: the origin, the static files and the face they are drawn in."""

import mimetypes
import os
import re
from functools import cache
from pathlib import Path

from django.contrib.staticfiles import finders

# Secure, as HQ is only ever served: a page in a secure context prefetches a
# link on press, and one over plain http is refused it.
ORIGIN = "https://hq.example.test"

# The nonce a page a test puts together carries on its script tags, and the
# policy it is answered with names (gate.policy): HQ's script-src admits only
# a script whose tag carries the response's nonce.
NONCE = "hq-browser-gate"

# A face the stylesheet lets the browser skip when it is late (`font-display:
# optional`). A late face is drawn in the fallback for the page's whole life,
# so on a loaded machine a wrapping check would read one page in either face
# from run to run. The gate's copy waits for its face instead, and every
# measure waits for the fonts (`FONTS_READY`): each page is read in the face
# a reader whose font arrived in time sees.
_LATE_FACE = re.compile(rb"font-display:\s*optional")

# A reader whose Inter came late reads the page in the next face of the
# stylesheet's own stack (`--font`, tokens.css): the platform's system face.
# `HQ_BROWSER_FACE=fallback` (the browser tasks' `--fallback-face`) withholds
# Inter's face, so every page is drawn that way and a layout that holds only
# at Inter's widths fails.
_INTER = re.compile(rb"@font-face\s*\{[^}]*font-family:\s*Inter;[^}]*\}")


def in_the_face(css):
    """``css`` as the gate draws it: in Inter, waited for, or with Inter's
    face withheld, in the next face of the stack."""

    face = os.environ.get("HQ_BROWSER_FACE") or "inter"
    if face not in {"inter", "fallback"}:
        raise ValueError("HQ_BROWSER_FACE must be inter or fallback.")
    if face == "fallback":
        css = _INTER.sub(b"", css)
    return _LATE_FACE.sub(b"font-display: block", css)


@cache
def static_file(name):
    """A static file's type and body, read once a process: routing a request
    turns the browser's own cache off, so every load asks for each again."""

    asset = finders.find(name)
    if not asset:
        return None
    content_type = mimetypes.guess_type(asset)[0] or "application/octet-stream"
    body = Path(asset).read_bytes()
    if content_type == "text/css":
        body = in_the_face(body)
    return content_type, body
