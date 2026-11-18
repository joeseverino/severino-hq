"""The image's server: Uvicorn's own command, serving HQ where ``LISTENS_AT``
says, with the options the Dockerfile's CMD gives after it.

The command replaces this process, so the one serving process is Uvicorn's,
named by the application it serves as the release report finds it."""

import os
import sys

# Where HQ's image serves, and what HQ knows of its own listener.
LISTENS_AT = ("127.0.0.1", 8000)


def main() -> None:
    host, port = LISTENS_AT
    command = ["uvicorn", "hq.config.asgi:application", "--host", host, "--port", str(port), *sys.argv[1:]]
    os.execvp(command[0], command)  # noqa: S606 - the image's own server, from its fixed argv


if __name__ == "__main__":
    main()
