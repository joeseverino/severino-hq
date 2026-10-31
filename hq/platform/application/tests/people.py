"""The person a request's session signed in, for tests of what a request says."""

from django.contrib.auth import get_user_model


def someone():
    """The person signed in, stored, as a session's user always is."""

    return get_user_model().objects.get_or_create(username="someone", defaults={"email": "someone@example.test"})[0]
