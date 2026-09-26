"""Errors shared across layers."""


class UpstreamUnavailable(RuntimeError):
    """A service outside HQ could not be reached or is not configured.

    Every such error derives from this, so code that reads from any source can
    degrade on one class instead of knowing each upstream's own.
    """
