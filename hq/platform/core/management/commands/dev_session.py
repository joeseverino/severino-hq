"""A signed-in session for an existing account on a development server.

    python manage.py dev_session <username>

Prints ``<cookie name>=<session key>``, for a browser or ``curl --cookie`` to
send to a server that shares this database and ``DJANGO_SECRET_KEY``.
"""

from typing import override

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hq.platform.core.dev_sign_in import new_session


class Command(BaseCommand):
    help = "Print a session cookie for an existing account; DEBUG only."

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username")

    @override
    def handle(self, *args, **options):
        # So it cannot mint a session for a server anyone else reaches.
        if not settings.DEBUG:
            raise CommandError("dev_session runs only with DEBUG on: it is for a development server.")
        try:
            user = get_user_model()._default_manager.get_by_natural_key(options["username"])
        except get_user_model().DoesNotExist:
            raise CommandError(f"No account is named {options['username']!r}; dev_session creates none.") from None
        # Signed with this process's key and the account's stored password hash.
        session = new_session(user)
        self.stdout.write(f"{settings.SESSION_COOKIE_NAME}={session.session_key}")
