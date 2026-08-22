"""Taking a certificate an operator generated elsewhere, and holding it safely.

`cert-gen <service>.homelab` runs on the Mac against an air-gapped CA and prompts
for a passphrase, so HQ cannot issue an internally signed certificate and should
not pretend to. What it can do is everything after that: install the result
wherever it belongs, keep it so installing it somewhere else later is a click,
and say when it is about to stop working.

Only the leaf certificate and its key move -- the same pair that would otherwise
be pasted into a provider's web form by hand. The root CA key stays where it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from django.db import transaction
from django.utils import timezone

from control_plane.desired_state import desired_fingerprint
from control_plane.models import CertificateMaterial, ManagedResource
from core import secrets

from .infrastructure import delivery_targets
from .security import Capability, Principal


class CertificateError(ValueError):
    """The uploaded material is not a usable certificate and key pair."""


@dataclass(frozen=True)
class UploadCertificateCommand:
    key: str
    fullchain: str
    private_key: str
    install_on: tuple[str, ...] = ()


def inspect(fullchain: str, private_key: str) -> dict[str, Any]:
    """Read the certificate, and prove the key belongs to it.

    The pairing check is the point. A mismatched pair is accepted by every
    editor and every clipboard and fails only when a browser refuses the
    handshake -- after deployment, on a service that was working before.
    """

    try:
        certificate = x509.load_pem_x509_certificate(fullchain.encode())
    except (ValueError, TypeError) as exc:
        raise CertificateError(
            "That does not parse as a PEM certificate. Paste the contents of "
            "fullchain.pem."
        ) from exc
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except TypeError as exc:
        raise CertificateError(
            "That private key is passphrase-protected. HQ cannot hold a key it "
            "has to be prompted for; export it without one."
        ) from exc
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise CertificateError(
            "That does not parse as a PEM private key. Paste the contents of "
            "the .key file."
        ) from exc

    if key.public_key().public_numbers() != certificate.public_key().public_numbers():
        raise CertificateError(
            "That private key does not belong to that certificate. Installing "
            "the pair would break TLS on everything it is deployed to."
        )

    expires = certificate.not_valid_after_utc
    if expires <= timezone.now():
        raise CertificateError(
            f"That certificate expired on {expires:%-d %b %Y}. Generate a new "
            "one before installing it."
        )
    return {
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        "not_after": expires,
        "subject": certificate.subject.rfc4514_string()[:500],
        "domains": _names(certificate),
    }


def _names(certificate: x509.Certificate) -> list[str]:
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
    except x509.ExtensionNotFound:
        return []
    return [name.lower() for name in extension.value.get_values_for_type(x509.DNSName)]


@transaction.atomic
def store_certificate(
    command: UploadCertificateCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Seal an uploaded pair against an existing declaration.

    Refuses before touching anything if there is nowhere safe to put it: a
    secret store that quietly degrades to plaintext is worse than none, because
    the operator would believe otherwise.
    """

    del expected_updated_at
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    if not secrets.available():
        raise secrets.SecretsUnavailable(
            "HQ has no secret store key configured, so it will not hold a "
            "private key. Nothing was stored."
        )
    details = inspect(command.fullchain, command.private_key)
    try:
        resource = ManagedResource.objects.get(key=command.key)
    except ManagedResource.DoesNotExist as exc:
        raise CertificateError(f"No resource named {command.key!r}.") from exc

    CertificateMaterial.objects.update_or_create(
        resource=resource,
        defaults={
            "sealed_fullchain": secrets.seal(command.fullchain),
            "sealed_private_key": secrets.seal(command.private_key),
            "fingerprint_sha256": details["fingerprint_sha256"],
            "not_after": details["not_after"],
            "subject": details["subject"],
            "domains": details["domains"],
        },
    )
    # What it covers, written where a failed resolution cannot lose it. Held
    # only on the resolved spec, a certificate whose install target had been
    # removed would resolve to nothing and so cover nothing -- and the names it
    # answers for would read as uncovered because of a fault somewhere else
    # entirely. Read from the artifact, so an upload is what keeps it true.
    resource.spec = {**resource.spec, "domains": details["domains"]}
    # New material is new work, even when it covers exactly what the last one
    # did: a renewal is a different certificate at the same names, and the whole
    # point of uploading it is to get it installed. Without this the page said
    # "HQ installs it on the next controller pass" and no pass ever had a reason
    # to run -- the generation the controller compares against had not moved.
    resource.generation += 1
    resource.desired_fingerprint = desired_fingerprint(
        resource.kind,
        resource.spec,
        resource.enabled,
        targets=delivery_targets(),
        resource_key=resource.key,
    )
    resource.save(
        update_fields=("spec", "generation", "desired_fingerprint", "updated_at")
    )
    return {
        "ok": True,
        "resource": resource.key,
        # Everything except the material itself. The caller uploaded it, so it
        # learns nothing by being told it back, and a response is the easiest
        # place for a secret to end up somewhere it was not meant to go.
        "fingerprint_sha256": details["fingerprint_sha256"],
        "not_after": details["not_after"].isoformat(),
        "domains": details["domains"],
    }


def material_for(key: str) -> dict[str, str]:
    """The unsealed pair, for the controller that is about to install it."""

    try:
        material = CertificateMaterial.objects.get(resource__key=key)
    except CertificateMaterial.DoesNotExist as exc:
        raise CertificateError(f"No stored certificate for {key!r}.") from exc
    return {
        "fullchain": secrets.unseal(material.sealed_fullchain),
        "private_key": secrets.unseal(material.sealed_private_key),
        "domains": list(material.domains or ()),
    }


def store_uploaded_material(
    key: str, cleaned: dict[str, Any], *, principal: Principal
) -> dict[str, Any]:
    """Adapter for the create page, which collects the material with the rest."""

    return store_certificate(
        UploadCertificateCommand(
            key=key,
            fullchain=cleaned["fullchain"],
            private_key=cleaned["private_key"],
        ),
        principal=principal,
    )
