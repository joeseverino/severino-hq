"""Certificates: planning a deployment, running it, and the handlers that start one.

Issuing lives in ``tls_issuance``, the proxy's certificate store in
``npm_certificates``, and checking what each consumer serves afterwards in
``tls_verification``.
"""

from __future__ import annotations

import json
from typing import Any

from control_plane.provider_adapters.tls import CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND
from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from control_plane.provider_adapters import onepassword
from . import (
    commands,
    npm_certificates,
    provider_http,
    provider_runtime,
    tls_issuance,
    tls_verification,
)
from .handlers import acts


def _cpanel_sites(consumer: dict[str, Any]) -> list[str]:
    """The cPanel sites this consumer installs on, decided before anything is issued.

    cPanel holds one certificate per *site*, and every alias of a site serves
    whatever that site holds. So the question is never "which names", it is
    "which sites serve the names HQ will check". The account is asked for its
    sites and their names, and the answer has to cover every verified name:

    - with `install_domains` declared, the sites serving those names;
    - without, every site that serves a verified name.

    A verified name no chosen site serves is refused here, by name, before any
    certificate is requested. So is a name the account does not serve at all.
    """

    try:
        answer = json.loads(commands.run_ssh(consumer["connection_ref"], "sites") or b"{}")
    except ValueError as exc:
        raise ProviderError(
            f"{consumer['name']} returned a site list HQ could not read."
        ) from exc
    sites = answer.get("sites") if isinstance(answer, dict) else None
    if not isinstance(sites, dict) or not sites:
        raise ProviderError(f"{consumer['name']} reported no sites.")
    site_of = {
        name.lower(): site
        for site, names in sites.items()
        for name in (site, *(names or ()))
    }
    verify = sorted({name.lower() for name in consumer.get("verify_domains", ())})
    declared = sorted({name.lower() for name in consumer.get("install_domains", ())})

    not_hosted = sorted(name for name in (*verify, *declared) if name not in site_of)
    if not_hosted:
        raise ProviderError(
            f"{consumer['name']} does not serve "
            + ", ".join(not_hosted)
            + ". Remove the name from the target, or add it to the hosting account."
        )
    chosen = sorted({site_of[name] for name in (declared or verify)})
    served = {name.lower() for site in chosen for name in (site, *sites[site])}
    unserved = [name for name in verify if name not in served]
    if unserved:
        raise ProviderError(
            f"{consumer['name']} would be checked at "
            + ", ".join(unserved)
            + " but installs only on "
            + ", ".join(chosen)
            + ". Add those names to the target's install list, or leave the list "
            "empty to install on every site that serves a checked name."
        )
    if not chosen:
        raise ProviderError(f"{consumer['name']} has no site to install on.")
    return chosen


def _plan_deployment(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Everything a deploy needs to know from the consumers, asked up front.

    Runs before a certificate is requested and before any consumer is touched, so
    a target that cannot be satisfied costs nothing: no issuance against the CA's
    rate limit, no half-deployed estate, no rollback.
    """

    return {
        consumer["name"]: _cpanel_sites(consumer)
        for consumer in spec["consumers"]
        if consumer["kind"] == "cpanel"
    }


def _deploy_certificate(
    spec: dict[str, Any],
    fullchain: bytes,
    private_key: bytes,
    plan: dict[str, list[str]],
    known: dict[str, int] | None = None,
) -> dict[str, Any]:
    deployment_status: dict[str, Any] = {}
    bundle = tls_issuance.certificate_bundle(fullchain, private_key)
    marker = b"-----END CERTIFICATE-----"
    leaf_body, separator, chain_body = fullchain.partition(marker)
    if not separator:
        raise ProviderError("Certificate chain does not contain a leaf certificate.")
    leaf = leaf_body + marker + b"\n"
    chain = chain_body.lstrip()
    for consumer in spec["consumers"]:
        try:
            if consumer["kind"] == "npm":
                certificate_id, identity = npm_certificates.npm_managed_certificate(
                    consumer,
                    spec["domains"],
                    fullchain,
                    private_key,
                    (known or {}).get(consumer["name"]),
                )
                deployment_status.update(
                    npm_certificate_id=certificate_id,
                    npm_certificate_identity=identity,
                )
                deployment_status.setdefault(npm_certificates.NPM_CERTIFICATE_IDS, {})[
                    consumer["name"]
                ] = certificate_id
            elif consumer["kind"] == "caddy":
                commands.run_ssh(consumer["connection_ref"], "deploy", bundle)
            elif consumer["kind"] == "cpanel":
                # One login for every site, and the account reports each one.
                sites = plan[consumer["name"]]
                payload = json.dumps(
                    {
                        "sites": sites,
                        "cert": leaf.decode(),
                        "key": private_key.decode(),
                        "cabundle": chain.decode(),
                    },
                    separators=(",", ":"),
                ).encode()
                commands.run_ssh(consumer["connection_ref"], "deploy", payload)
                deployment_status.setdefault("cpanel_sites", {})[
                    consumer["name"]
                ] = sites
        except ProviderError as exc:
            raise ProviderError(
                f"TLS deployment failed for {consumer['name']} "
                f"({consumer['kind']}): {exc}"
            ) from exc
    return deployment_status


def _deploy_tls_transaction(
    spec: dict[str, Any],
    fullchain: bytes,
    private_key: bytes,
    previous_fullchain: bytes,
    previous_key: bytes,
    *,
    plan: dict[str, list[str]],
    artifact_source: str,
    reason: str,
    message: str,
    known: dict[str, int] | None = None,
) -> ProviderResult:
    expected_fingerprint = tls_issuance.validate_certificate(
        fullchain, private_key, spec["domains"]
    )
    try:
        deployment_status = _deploy_certificate(
            spec, fullchain, private_key, plan, known
        )
        observed = tls_verification.verify_tls_deployment(spec, expected_fingerprint)
    except ProviderError as exc:
        try:
            _deploy_certificate(spec, previous_fullchain, previous_key, plan, known)
        except ProviderError as rollback_exc:
            raise ProviderError(
                f"Certificate deployment failed ({exc}); rollback also failed "
                f"({rollback_exc})."
            ) from rollback_exc
        raise ProviderError(
            f"Certificate deployment failed: {exc} Rollback succeeded.",
            status=exc.status,
        ) from exc
    status = {
        **tls_verification.tls_match_evidence(observed.status, expected_fingerprint),
        **deployment_status,
        "artifact_source": artifact_source,
        "renewed_fingerprint_sha256": expected_fingerprint,
    }
    return ProviderResult(
        changed=True,
        status=status,
        conditions=[
            provider_http.condition(
                "Ready", True, reason, "All TLS consumers serve the certificate."
            )
        ],
        message=message,
    )


def apply_tls_reconcile(
    spec: dict[str, Any], *, known: dict[str, int] | None = None
) -> ProviderResult:
    fullchain, private_key = tls_issuance.lineage(spec)
    expected = tls_issuance.validate_certificate(fullchain, private_key, spec["domains"])
    observed = tls_verification.reconcile_tls(spec)
    if tls_verification.tls_consumers_serve(spec, observed.status, expected):
        return ProviderResult(
            changed=False,
            status={
                **tls_verification.tls_match_evidence(observed.status, expected),
                "artifact_source": "existing_lineage",
            },
            conditions=[
                provider_http.condition("Ready", True, "Verified", "All TLS consumers match.")
            ],
            message="Certificate consumers already match the managed lineage.",
        )
    caddy = next((item for item in spec["consumers"] if item["kind"] == "caddy"), None)
    if caddy is None:
        raise ProviderError("Certificate reconciliation requires a rollback source.")
    plan = _plan_deployment(spec)
    previous_fullchain, previous_key = tls_issuance.read_bundle(
        commands.run_ssh(caddy["connection_ref"], "snapshot")
    )
    return _deploy_tls_transaction(
        spec,
        fullchain,
        private_key,
        previous_fullchain,
        previous_key,
        plan=plan,
        artifact_source="existing_lineage",
        reason="Reconciled",
        message="Certificate redistributed and verified without issuance.",
        known=known,
    )


def renew_tls(
    spec: dict[str, Any], *, known: dict[str, int] | None = None
) -> ProviderResult:
    caddy = next((item for item in spec["consumers"] if item["kind"] == "caddy"), None)
    if caddy is None:
        raise ProviderError("Certificate renewal requires a rollback source.")
    # Before the CA is asked for anything: a target that cannot be satisfied
    # should cost nothing.
    plan = _plan_deployment(spec)
    previous_fullchain, previous_key = tls_issuance.read_bundle(
        commands.run_ssh(caddy["connection_ref"], "snapshot")
    )
    previous_fingerprint = tls_issuance.validate_certificate(
        previous_fullchain, previous_key, spec["domains"]
    )
    resumed = tls_issuance.resumable_lineage(spec, previous_fingerprint)
    if resumed is None:
        fullchain, private_key = tls_issuance.issue_certificate(spec)
        artifact_source = "new_issuance"
    else:
        fullchain, private_key = resumed
        artifact_source = "existing_lineage"
    return _deploy_tls_transaction(
        spec,
        fullchain,
        private_key,
        previous_fullchain,
        previous_key,
        plan=plan,
        artifact_source=artifact_source,
        reason="Renewed",
        message="Certificate renewed, deployed, and verified.",
        known=known,
    )


def _publish_tls_facts(
    spec: dict[str, Any], result: ProviderResult, *, apply: bool
) -> ProviderResult:
    """Record what was just observed wherever the certificate says to record it.

    Runs after the certificate's own work and can only add to its report. A
    failure here is reported and then let go: publishing facts is a convenience
    for whoever opens the item next, and the certificate being installed and
    serving is the job. Raising would turn a password manager being unreachable
    into a certificate that failed to reconcile, and then into an automatic
    retry of a deployment that had nothing wrong with it.

    No condition is raised either, for the same reason: a `Degraded` on the
    certificate says the certificate is degraded, and this says a note about it
    was not filed. It goes in the status and in the message, where an operator
    reading the operation sees it.

    Nothing is written on a dry run. Being asked what a reconcile *would* do is
    not permission to change something outside HQ.
    """

    publications = spec.get("publish_to") or ()
    if not apply or not publications:
        return result
    desired = onepassword.facts(spec, result.status)
    published: list[dict[str, Any]] = []
    for publication in publications:
        if not desired:
            published.append(
                {
                    "target": publication["name"],
                    "written": False,
                    "detail": (
                        "HQ has no single fingerprint for this certificate yet."
                    ),
                }
            )
            continue
        try:
            published.append(
                onepassword.publish(
                    provider_runtime.RUNTIME, publication, desired, tls_issuance.lineage_material(spec)
                )
            )
        except (ProviderError, OSError, ValueError) as exc:
            # The message, not the exception type: `ProviderError` is written to
            # carry no credential material, and an item name is HQ's own.
            published.append(
                {"target": publication["name"], "written": False, "detail": str(exc)}
            )
    unfiled = [item["target"] for item in published if not item["written"]]
    return ProviderResult(
        changed=result.changed,
        status={**result.status, "published_facts": published},
        conditions=result.conditions,
        message=(
            f"{result.message} Facts were not recorded on: {', '.join(unfiled)}."
            if unfiled
            else result.message
        ),
    )


@acts(CERTIFICATE_KIND, "reconcile")
def _tls_reconcile(
    spec: dict[str, Any],
    *,
    apply: bool,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    known = npm_certificates.npm_certificate_ids(spec, observed)
    result = apply_tls_reconcile(spec, known=known) if apply else tls_verification.reconcile_tls(spec)
    return npm_certificates.with_npm_certificate_ids(
        _publish_tls_facts(spec, result, apply=apply), known
    )


@acts(CERTIFICATE_KIND, "renew")
def _tls_renew(
    spec: dict[str, Any],
    *,
    apply: bool,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    if apply:
        # A renewal is the moment the facts actually change (a new expiry and a
        # new fingerprint) so it is the one an item most needs to hear about.
        known = npm_certificates.npm_certificate_ids(spec, observed)
        return npm_certificates.with_npm_certificate_ids(
            _publish_tls_facts(spec, renew_tls(spec, known=known), apply=True), known
        )
    return ProviderResult(
        changed=True,
        status={},
        conditions=[],
        message="Certificate would be issued, deployed, verified, and rolled back on failure.",
    )


@acts(UPLOADED_CERTIFICATE_KIND, "reconcile")
def reconcile_uploaded_certificate(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Install a certificate HQ was given rather than one it issued.

    Deployment is identical (a proxy does not care which authority signed the
    thing it serves) so this reuses the same path as a renewal and differs
    only in where the material came from. It is never renewed here: the CA is
    air-gapped, and the certificate's expiry is reported so an operator knows
    when to generate the next one.
    """

    material = spec.get("material") or {}
    fullchain = material.get("fullchain") or ""
    private_key = material.get("private_key") or ""
    if not fullchain or not private_key:
        raise ProviderError(
            "HQ did not supply the stored certificate. Upload it again."
        )
    domains = list(material.get("domains") or ())
    if not apply:
        return ProviderResult(
            changed=True,
            status={"certificate_name": spec["certificate_name"], "domains": domains},
            conditions=[
                provider_http.condition("Ready", True, "Planned", "Would install the certificate.")
            ],
            message="Would install the stored certificate.",
        )
    target = {
        "certificate_name": spec["certificate_name"],
        "domains": domains,
        "consumers": spec["consumers"],
    }
    deployment = _deploy_certificate(
        target,
        fullchain.encode(),
        private_key.encode(),
        _plan_deployment(target),
        npm_certificates.npm_certificate_ids(spec, observed),
    )
    observed = {
        key: value
        for key, value in deployment.items()
        # The deployment report carries an npm certificate identity; nothing
        # secret-bearing may enter HQ, and the status guard rejects the whole
        # report if it does.
        if "private" not in key and "key" not in key
    }
    return ProviderResult(
        changed=True,
        status={
            "certificate_name": spec["certificate_name"],
            "domains": domains,
            **observed,
        },
        conditions=[
            provider_http.condition("Ready", True, "Installed", "Stored certificate installed.")
        ],
        message="Stored certificate installed.",
    )


@acts(UPLOADED_CERTIFICATE_KIND, "delete")
def delete_uploaded_certificate(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Remove an installed certificate, or refuse and say who has to do it.

    Only Nginx Proxy Manager can be undone from here. A Caddy target receives a
    certificate over an SSH forced command that implements ``deploy`` and
    nothing else, so removing one means editing the remote side, and a delete
    that reported success while leaving a file on a host would take HQ's
    declaration with it and lose the only record that the file is there.

    Refused whole rather than done partly, for the same reason.
    """

    elsewhere = sorted(
        consumer["name"] for consumer in spec["consumers"] if consumer["kind"] != "npm"
    )
    if elsewhere:
        raise ProviderError(
            "HQ can only remove this from Nginx Proxy Manager. Take it off "
            + ", ".join(elsewhere)
            + " by hand first, then remove those targets from this resource."
        )

    base_url = npm_certificates.npm_url()
    headers = {"Authorization": f"Bearer {npm_certificates.npm_token(base_url)}"}
    certificates = provider_http.request_json(f"{base_url}/nginx/certificates", headers=headers)
    installed = set(npm_certificates.npm_certificate_ids(spec, observed).values())
    matches = [item for item in certificates if item.get("id") in installed]
    if not matches:
        # A display name is not an identity: NPM lets anyone set one. A
        # certificate HQ holds no id for is left for an operator to judge.
        wanted = {npm_certificates.npm_certificate_name(consumer) for consumer in spec["consumers"]}
        named = sorted(
            str(item.get("nice_name"))
            for item in certificates
            if item.get("nice_name") in wanted
        )
        if named:
            raise ProviderError(
                "NPM holds " + ", ".join(named) + ", but HQ has no record of "
                "installing it, so it was not removed. Remove it in NPM if it "
                "is HQ's, then remove this again."
            )
        return ProviderResult(
            changed=False,
            status={"removed": True},
            conditions=[
                provider_http.condition("Ready", True, "Absent", "No such certificate in NPM.")
            ],
            message="Certificate was already absent from NPM.",
        )

    # A certificate still bound to a proxy host cannot be deleted without taking
    # TLS down on it. Naming the hosts is the actionable part: the operator has
    # to point them at something else first.
    hosts = provider_http.request_json(f"{base_url}/nginx/proxy-hosts", headers=headers)
    identifiers = {item["id"] for item in matches}
    still_bound = sorted(
        name
        for host in hosts
        if host.get("certificate_id") in identifiers
        for name in host.get("domain_names", [])
    )
    if still_bound:
        raise ProviderError(
            "Still serving " + ", ".join(still_bound) + ". Point those at "
            "another certificate before removing this one."
        )
    if apply:
        for item in matches:
            provider_http.request_json(
                f"{base_url}/nginx/certificates/{item['id']}",
                method="DELETE",
                headers=headers,
            )
    return ProviderResult(
        changed=True,
        status={"removed": True},
        conditions=[
            provider_http.condition("Ready", True, "Removed", "Certificate removed from NPM.")
        ],
        message="Certificate removed from NPM.",
    )
