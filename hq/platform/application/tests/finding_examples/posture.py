from . import Example, Examples

OIDC = "hq.platform.core.oidc.HQOIDCAuthenticationBackend"

EXAMPLES = (
    Examples(
        "posture-unmet",
        # Under Advanced HQ posture, each check an HQ with nothing set up does not meet.
        fires=(Example(settings={"SEVERINO_POSTURE": "advanced"}, raises=("Password sign-in is installed",)),),
        # Otherwise an unmet check is an open item on its page, and no finding.
        quiet=(
            Example(settings={"SEVERINO_POSTURE": ""}),
            # Single sign-on alone meets the sign-in check.
            Example(settings={"SEVERINO_POSTURE": "advanced", "AUTHENTICATION_BACKENDS": [OIDC]}),
        ),
    ),
)
