"""A signed-in session for an existing account on a development server.

    python manage.py dev_session <username>

Prints ``<cookie name>=<session key>``, for a browser or ``curl --cookie`` to
send to a server that shares this database and ``DJANGO_SECRET_KEY``.
"""

from importlib import import_module
from typing import override

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.http import HttpRequest


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
        request = HttpRequest()
        request.session = import_module(settings.SESSION_ENGINE).SessionStore()
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        # With single sign-on on, an ID token with no expiry counts as expired
        # and the next page asks the provider again; this session has none.
        request.session["oidc_id_token_expiration"] = request.session.get_expiry_date().timestamp()
        # Signed with this process's key and the account's stored password hash.
        request.session.save()
        self.stdout.write(f"{settings.SESSION_COOKIE_NAME}={request.session.session_key}")
