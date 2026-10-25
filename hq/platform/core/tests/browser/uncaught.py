"""What a page threw that no script caught, as every check that drives a browser hears it.

The browser gate and the demo's check both listen through ``Uncaught``, so an
engine's misreport is answered once for both. It imports nothing, since the
demo's check runs without Django.
"""

# WebKit's line for a fetch that failed, which it logs as a script error
# whether or not the script handles the rejection, and Playwright passes on as
# an uncaught error. A rejection no script handles WebKit reports on a line of
# its own ("Unhandled Promise Rejection"), which is held like any other, so the
# fetch's own line says nothing that one does not.
_FAILED_FETCH = "Fetch API cannot load"


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
        page.on("pageerror", self.threw)

    def threw(self, error):
        # An error the engine raises with no Error object (a ResizeObserver
        # loop) has a message and no name or stack.
        said = f"{error.name}: {error.message}" if error.name else error.message
        self.heard.append((said, f"{self.where()}: uncaught {said}{first_frame(error.stack)}"))

    def so_far(self):
        """Each line heard since this was last asked, but WebKit's line for a failed fetch."""

        heard, self.heard = self.heard, []
        return [line for said, line in heard if not said.startswith(_FAILED_FETCH)]
