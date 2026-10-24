"""What a page threw that no script caught, as every check that drives a browser hears it.

The browser gate and the demo's check both listen through ``Uncaught``, so an
engine's misreport is answered once for both. It imports nothing, since the
demo's check runs without Django.
"""

# WebKit's line for a fetch that failed, which it logs as a script error
# whether or not the script handles the rejection, and Playwright passes on as
# an uncaught error. A rejection no script handles WebKit reports on a line of
# its own ("Unhandled Promise Rejection"), which is held like any other, so the
# fetch's own line is dropped only for a request that was cancelled, as the
# gate's next navigation cancels a poll in flight.
_FAILED_FETCH = "Fetch API cannot load "
_FAILED_FETCH_ENDS = (" due to access control checks.", ".")

# How Playwright tells a cancelled request in WebKit (its _onLoadingFailed),
# and so the one wording it promises for one.
_CANCELLED = "cancelled"


def as_said(text):
    """``text`` as Playwright hands on an engine's error line: split at its
    first colon into a name and a message, the character after the colon
    lost (its splitErrorMessage), and joined again as the gate prints it."""

    at = text.find(":")
    name = text[:at] if at != -1 else ""
    message = text[at + 2 :] if at != -1 and at + 2 <= len(text) else text
    return f"{name}: {message}" if name else message


def first_frame(stack):
    """Where an uncaught error was thrown, from its stack as Playwright gives
    it in every engine (V8's form: a line per frame, each starting ``at``)."""

    frames = (line.strip() for line in (stack or "").splitlines())
    return next((f" {frame}" for frame in frames if frame.startswith("at ")), "")


class Uncaught:
    """Every error ``page`` throws that no script caught, each a line that
    starts with what ``where`` says of the page as it was thrown."""

    def __init__(self, page, where):
        self.where = where
        self.heard = []
        self.cancelled = set()
        page.on("pageerror", self.threw)
        page.on("requestfailed", self.failed)

    def threw(self, error):
        # An error the engine raises with no Error object (a ResizeObserver
        # loop) has a message and no name or stack.
        said = f"{error.name}: {error.message}" if error.name else error.message
        self.heard.append((said, f"{self.where()}: uncaught {said}{first_frame(error.stack)}"))

    def failed(self, request):
        if _CANCELLED in (request.failure or ""):
            self.cancelled.add(request.url)

    def so_far(self):
        """Each line heard since this was last asked, but a cancelled fetch's.

        Judged as it is asked, so a cancellation heard after its fetch's line
        still answers it."""

        cancelled = {as_said(f"{_FAILED_FETCH}{url}{end}") for url in self.cancelled for end in _FAILED_FETCH_ENDS}
        heard, self.heard = self.heard, []
        return [line for said, line in heard if said not in cancelled]
