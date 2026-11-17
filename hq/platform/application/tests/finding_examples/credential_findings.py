from datetime import timedelta

from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL

from ...tone import Tone
from . import AT, Example, Examples, Raised, connection, stored

PAGES = "cloudflare.pages_project"
READ = {PAGES: ({"name": "example", "account_id": "0123abcd"},)}
REFUSED = stored(reachable=False, refusal=CREDENTIAL_REFUSAL, error="Authentication error")
# Pages read, and every other account reading refused: a token that works,
# missing what the others need. An older controller marked each refusal as
# the credential's.
SOME_REFUSED = READ | {
    kind: REFUSED for kind, spec in OBSERVATIONS.items() if spec.provider == "cloudflare_api" and kind != PAGES
}
STORE = {"vault": "Example Vault", "item": "exampleitem01", "bootstrap": "op://Operator Vault/Cloudflare bootstrap"}


def connected(readings=READ, raises: tuple[Raised | str, ...] = (), expires_in: int | None = None) -> Example:
    """A Cloudflare connection whose token is kept in an item HQ can mint into."""

    expires = {} if expires_in is None else {"expires_at": AT + timedelta(days=expires_in)}
    return Example(
        readings=readings,
        given=lambda: connection("example-api", "cloudflare_api", store=STORE, **expires),
        raises=raises,
    )


EXAMPLES = (
    Examples(
        "credential-missing-permissions",
        fires=(connected(SOME_REFUSED, raises=("example-api's token is missing 9 permissions",)),),
        # Refused outright is a credential refused, said by connection-not-answering.
        quiet=(connected(), connected({PAGES: REFUSED})),
    ),
    Examples(
        "credential-expiring",
        fires=(
            connected(expires_in=10, raises=("example-api's token expires in 10 days",)),
            connected(
                expires_in=-1, raises=(Raised(title="example-api's token expired today", severity=Tone.SERIOUS),)
            ),
        ),
        quiet=(connected(), connected(expires_in=60)),
    ),
)
