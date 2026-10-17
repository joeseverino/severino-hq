"""The host as it runs with no extension installed, for tests that count what the host draws."""

from contextlib import ExitStack, contextmanager
from unittest import mock


@contextmanager
def host_only():
    with ExitStack() as stack:
        stack.enter_context(mock.patch("hq.platform.application.domains.extension_domains", return_value=()))
        stack.enter_context(mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()))
        yield
