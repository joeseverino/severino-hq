"""Taking a certificate an operator generated elsewhere, and holding it safely.

`cert-gen <service>.home.arpa` signs on the Mac against an air-gapped CA behind
a passphrase, so HQ cannot issue an internal certificate and does not pretend
to. It does what comes after: installs it where it belongs, keeps it for the
next install, and says when it is about to stop working. Only the leaf and its
key move, the pair otherwise pasted into a provider's form by hand.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.desired_state import desired_fingerprint
from hq.domains.control_plane.models import CertificateMaterial, ManagedResource
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core import secrets
from hq.platform.core.secrets import NO_STORE_KEY

from .command_fields import Describe
from .infrastructure import delivery_targets
from .moments import when_day
from .record_errors import CertificateError, PolicyError, RecordNotFound
from .security import Principal

UPLOAD = "certificate.upload"


@dataclass(frozen=True, slots=True)
class UploadCertificateCommand:
    fullchain: Annotated[str, Describe("The certificate and its chain, PEM-encoded.")]
    private_key: Annotated[str, Describe("The certificate's private key, PEM-encoded.")]


def inspect(fullchain: str, private_key: str) -> dict[str, Any]:
    """Read the certificate, and prove the key belongs to it.

    The pairing check is the point. A mismatched pair is accepted by every
    editor and every clipboard and fails only when a browser refuses the
    handshake: after deployment, on a service that was working before.
    """

    try:
        certificate = x509.load_pem_x509_certificate(fullchain.encode())
    except (ValueError, TypeError) as exc:
        raise CertificateError("Not a PEM certificate. Paste the contents of fullchain.pem.") from exc
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except TypeError as exc:
        raise CertificateError("The private key has a passphrase. Export it without one.") from exc
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise CertificateError("Not a PEM private key. Paste the contents of the .key file.") from exc

    if key.public_key().public_numbers() != certificate.public_key().public_numbers():
        raise CertificateError("The private key does not match the certificate.")

    expires = certificate.not_valid_after_utc
    if expires <= timezone.now():
        raise CertificateError(f"The certificate expired on {when_day(expires)}. Upload a current one.")
    return {
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        "not_after": expires,
        "subject": certificate.subject.rfc4514_string()[:500],
        "domains": _names(certificate),
    }


def _names(certificate: x509.Certificate) -> list[str]:
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    return [name.lower() for name in extension.value.get_values_for_type(x509.DNSName)]


@transaction.atomic
def store_certificate(
    command: UploadCertificateCommand,
    *,
    principal: Principal,
    current_key: str,
) -> dict[str, Any]:
    """Seal an uploaded pair against an existing certificate declaration.

    The ``certificate.upload`` capability, which authorizes it. Refuses before
    touching anything if there is nowhere safe to put it: a secret store that
    quietly degrades to plaintext is worse than none, because the operator
    would believe otherwise.
    """

    del principal
    details = inspect(command.fullchain, command.private_key)
    try:
        resource = ManagedResource.objects.select_for_update().get(key=current_key)
    except ManagedResource.DoesNotExist as exc:
        raise RecordNotFound("That record was not found.") from exc
    if PROVIDERS[resource.kind].material_capability != UPLOAD:
        raise PolicyError("Only an uploaded certificate record holds a certificate and key.")
    try:
        sealed = (secrets.seal(command.fullchain), secrets.seal(command.private_key))
    except secrets.SecretsUnavailable as exc:
        # Sealing fails only for want of a key, which names the setting to fix.
        raise PolicyError(NO_STORE_KEY) from exc

    CertificateMaterial.objects.update_or_create(
        resource=resource,
        defaults={
            "sealed_fullchain": sealed[0],
            "sealed_private_key": sealed[1],
            "fingerprint_sha256": details["fingerprint_sha256"],
            "not_after": details["not_after"],
            "subject": details["subject"],
            "domains": details["domains"],
        },
    )
    # What it covers, written where a failed resolution cannot lose it: a
    # certificate whose install target is removed still covers its names.
    # Read from the artifact, so an upload is what keeps it true.
    resource.spec = {**resource.spec, "domains": details["domains"]}
    # New material is new work, even when it covers exactly what the last one
    # did: a renewal is a different certificate at the same names, and the whole
    # point of uploading it is to get it installed, so the generation moves.
    resource.generation += 1
    resource.desired_fingerprint = desired_fingerprint(
        resource.kind,
        resource.spec,
        enabled=resource.enabled,
        targets=delivery_targets(),
        resource_key=resource.key,
    )
    resource.save(update_fields=("spec", "generation", "desired_fingerprint", "updated_at"))
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
        raise CertificateError("No certificate is stored for that record.") from exc
    return {
        "fullchain": secrets.unseal(material.sealed_fullchain),
        "private_key": secrets.unseal(material.sealed_private_key),
        "domains": list(material.domains or ()),
    }
