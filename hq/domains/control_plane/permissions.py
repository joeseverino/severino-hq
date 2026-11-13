"""The permissions HQ asks an operator to grant, one table per provider.

Each member reads the way the provider's own screen names the permission,
where the operator grants it, and carries the name its API takes (``api``),
which the minting tasks resolve. Where the two differ, the text HQ shows names
both. Every reading's ``requires``, a connection's ``writes``, the setup card
and a gap's text take their names from these tables, and ``test_permissions``
fails a permission name written anywhere else.

A member is a ``str``, so a reader joins, sorts and compares it as one.
"""

from enum import StrEnum
from typing import Self


class Permission(StrEnum):
    """One permission, said as its provider's screen says it."""

    # How the provider's API names it: what a minting task asks for.
    api: str

    @classmethod
    def _member(cls, shown: str, api: str) -> Self:
        member = str.__new__(cls, shown)
        member._value_ = shown
        member.api = api
        return member


class Cloudflare(Permission):
    """Cloudflare API token permissions, as the token editor at My Profile,
    API Tokens names them, each with the scope it is granted at.

    https://developers.cloudflare.com/fundamentals/api/reference/permissions/
    lists each group twice, under Dashboard and under API. ``api`` is the API
    tab's name, which the permission group listing answers and
    ``mint:cloudflare`` resolves.
    """

    def __new__(cls, name: str, level: str, api: str = "") -> Self:
        shown = f"{name} ({level}; API: {api})" if api else f"{name} ({level})"
        return cls._member(shown, f"{api or name} ({level})")

    ACCESS_APPS_READ = "Access: Apps Read", "account"
    ACCESS_POLICIES_READ = "Access: Policies Read", "account"
    ACCESS_SERVICE_TOKENS_READ = "Access: Service Tokens Read", "account"
    ACCOUNT_ANALYTICS_READ = "Account Analytics Read", "account"
    ACCOUNT_SETTINGS_READ = "Account Settings Read", "account"
    PAGES_READ = "Cloudflare Pages Read", "account", "Pages Read"
    TUNNEL_READ = "Cloudflare Tunnel Read", "account"
    D1_READ = "D1 Read", "account"
    D1_EDIT = "D1 Edit", "account", "D1 Write"
    # What List Registrations needs
    # (https://developers.cloudflare.com/api/resources/registrar/subresources/registrations/methods/list/).
    # Neither the permissions reference nor that operation in Cloudflare's
    # OpenAPI spec (x-api-token-group) names a Registrar group, so this is the
    # name the permission group listing has answered;
    # ``mint:cloudflare --list-groups`` prints the current one.
    REGISTRAR_DOMAINS_READ = "Registrar: Domains Read", "account"
    DNS_WRITE = "DNS Write", "zone"
    PAGE_RULES_READ = "Page Rules Read", "zone"
    SINGLE_REDIRECT_READ = "Single Redirect Read", "zone", "Dynamic URL Redirects Read"
    SSL_AND_CERTIFICATES_READ = "SSL and Certificates Read", "zone"
    ZONE_READ = "Zone Read", "zone"
    ZONE_SETTINGS_READ = "Zone Settings Read", "zone"


# The access levels a GitHub App's permission dropdown offers, by the level
# its API and manifest name.
GITHUB_ACCESS = {"read": "Read-only", "write": "Read & write"}


class GitHub(Permission):
    """GitHub App permissions, as the App's settings page names each and its
    access level.

    Names: https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
    Levels: https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app
    ``api`` is the key ``deploy/github-apps.json`` and an installation token
    name, beside the screen's name where it is not that name in snake case.
    """

    def __new__(cls, name: str, level: str, key: str = "") -> Self:
        key = key or name.lower().replace(" ", "_")
        shown = f"{name}: {GITHUB_ACCESS[level]}"
        if key != name.lower().replace(" ", "_"):
            shown += f" (API: {key})"
        return cls._member(shown, f"{key}: {level}")

    ACTIONS_READ = "Actions", "read"
    ADMINISTRATION_READ = "Administration", "read"
    CHECKS_READ = "Checks", "read"
    CODE_SCANNING_ALERTS_READ = "Code scanning alerts", "read", "security_events"
    COMMIT_STATUSES_WRITE = "Commit statuses", "write", "statuses"
    CONTENTS_READ = "Contents", "read"
    CONTENTS_WRITE = "Contents", "write"
    DEPENDABOT_ALERTS_READ = "Dependabot alerts", "read", "vulnerability_alerts"
    DEPLOYMENTS_READ = "Deployments", "read"
    ISSUES_WRITE = "Issues", "write"
    METADATA_READ = "Metadata", "read"
    PACKAGES_READ = "Packages", "read"
    PULL_REQUESTS_READ = "Pull requests", "read"
    PULL_REQUESTS_WRITE = "Pull requests", "write"
    SECRET_SCANNING_ALERTS_READ = "Secret scanning alerts", "read"
    VARIABLES_READ = "Variables", "read", "actions_variables"
    WORKFLOWS_WRITE = "Workflows", "write"


class Tailscale(Permission):
    """Tailscale trust credential scopes. The admin console's Trust
    credentials page offers each scope's Read option as its scope ID with a
    ``:read`` suffix, so the screen and the API share one name.

    https://tailscale.com/kb/1623/trust-credentials#scopes, and for
    ``services:read`` the API reference, https://tailscale.com/api.
    """

    def __new__(cls, scope: str) -> Self:
        return cls._member(scope, scope)

    DEVICES_CORE_READ = "devices:core:read"
    DEVICES_POSTURE_ATTRIBUTES_READ = "devices:posture_attributes:read"
    DEVICES_ROUTES_READ = "devices:routes:read"
    DNS_READ = "dns:read"
    FEATURE_SETTINGS_READ = "feature_settings:read"
    NETWORKING_SETTINGS_READ = "networking_settings:read"
    POLICY_FILE_READ = "policy_file:read"
    SERVICES_READ = "services:read"
    USERS_READ = "users:read"


class NginxProxyManager(Permission):
    """Nginx Proxy Manager user permissions, as its user's Permissions dialog
    names each section and the View Only level. ``api`` is the user's
    permissions field and value.

    https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/frontend/src/locale/src/en.json
    """

    def __new__(cls, name: str, key: str = "") -> Self:
        key = key or name.lower().replace(" ", "_")
        shown = f"{name}: View Only"
        if key != name.lower().replace(" ", "_"):
            shown += f" (API: {key})"
        return cls._member(shown, f"{key}: view")

    ACCESS_LISTS_VIEW = "Access Lists"
    CERTIFICATES_VIEW = "Certificates"
    DEAD_HOSTS_VIEW = "404 Hosts", "dead_hosts"
    PROXY_HOSTS_VIEW = "Proxy Hosts"
    REDIRECTION_HOSTS_VIEW = "Redirection Hosts"
    STREAMS_VIEW = "Streams"


class Portainer(Permission):
    """What a Portainer API key's user is granted. The key acts as its user,
    who is given each environment under Environments, Manage access.

    https://docs.portainer.io/admin/environments/environments#manage-access
    """

    def __new__(cls, shown: str) -> Self:
        return cls._member(shown, shown)

    ENVIRONMENT_ACCESS = "environment access"
