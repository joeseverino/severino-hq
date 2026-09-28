"""Issuing a certificate through ACME.

Also reading, validating and resuming the lineage an issue leaves behind.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import tarfile
import tempfile
from typing import Any

from control_plane.provider_adapters.contracts import ProviderError
from . import cloudflare_api, commands, provider_http


def certificate_bundle(fullchain: bytes, private_key: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, value in (("fullchain.pem", fullchain), ("privkey.pem", private_key)):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


def read_bundle(payload: bytes) -> tuple[bytes, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            names = set(archive.getnames())
            if names != {"fullchain.pem", "privkey.pem"}:
                raise ProviderError("Certificate snapshot contained unexpected files.")
            fullchain_file = archive.extractfile("fullchain.pem")
            private_key_file = archive.extractfile("privkey.pem")
            if fullchain_file is None or private_key_file is None:
                raise ProviderError("Certificate snapshot was incomplete.")
            return fullchain_file.read(), private_key_file.read()
    except (tarfile.TarError, OSError) as exc:
        raise ProviderError("Certificate snapshot was invalid.") from exc


def validate_certificate(
    fullchain: bytes, private_key: bytes, domains: list[str]
) -> str:
    with tempfile.TemporaryDirectory() as directory:
        cert_path = Path(directory) / "fullchain.pem"
        key_path = Path(directory) / "privkey.pem"
        cert_path.write_bytes(fullchain)
        key_path.write_bytes(private_key)
        cert_pub = commands.run_command(
            ["openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout"],
            step="reading the certificate",
        )
        key_pub = commands.run_command(
            ["openssl", "pkey", "-in", str(key_path), "-pubout"],
            step="reading the private key",
        )
        if cert_pub != key_pub:
            raise ProviderError("Certificate and private key do not match.")
        fingerprint = (
            commands.run_command(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(cert_path),
                    "-noout",
                    "-fingerprint",
                    "-sha256",
                ],
                step="reading the certificate fingerprint",
            )
            .decode()
            .strip()
            .split("=", 1)[-1]
            .replace(":", "")
            .lower()
        )
        san_output = commands.run_command(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-ext",
                "subjectAltName",
            ],
            step="reading the certificate names",
        ).decode()
        sans = {
            chunk.split(",", 1)[0].strip()
            for chunk in san_output.replace("\n", " ").split("DNS:")[1:]
        }
        missing = sorted(set(domains) - sans)
        if missing:
            raise ProviderError(
                "Issued certificate is missing names: " + ", ".join(missing) + "."
            )
        return fingerprint


# How long to wait for a DNS-01 challenge record to propagate. A tuning value,
# not a deployment identity, so it has a default and an override rather than a
# place in the vault.
ACME_PROPAGATION_SECONDS = os.environ.get("ACME_PROPAGATION_SECONDS", "30")


def _foreign_acme_entry(acme_dir: Path) -> str:
    """The first entry in the ACME state this process could not take ownership of.

    Certbot saves a renewal by copying the previous key's owner and group onto
    the new one. A process that is not root can only chown to itself, so one
    file carrying another group is enough to fail the save: after the CA has
    already issued, which spends a certificate against its rate limit and leaves
    an orphaned key behind. Checked before asking the CA for anything.
    """

    uid, gid = os.getuid(), os.getgid()
    for root, directories, files in os.walk(acme_dir):
        for name in (*directories, *files):
            path = Path(root, name)
            try:
                stat = path.lstat()
            except OSError:
                continue
            if stat.st_uid != uid or stat.st_gid != gid:
                return (
                    f"{path.relative_to(acme_dir)} is owned "
                    f"{stat.st_uid}:{stat.st_gid}, not {uid}:{gid}"
                )
    return ""


def issue_certificate(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    acme_dir = Path(provider_http.required("HQ", "ACME_DIR"))
    if not acme_dir.is_dir() or not os.access(acme_dir, os.W_OK):
        raise ProviderError("ACME state directory is not writable.")
    foreign = _foreign_acme_entry(acme_dir)
    if foreign:
        raise ProviderError(
            f"ACME state is not wholly the controller's: {foreign}. Certbot "
            "would be issued a certificate it cannot save, so nothing was requested."
        )
    commands.run_command(["certbot", "--version"], step="certbot preflight")
    credentials = acme_dir / "cloudflare.ini"
    credentials.write_text("dns_cloudflare_api_token = " + cloudflare_api.cloudflare_token() + "\n")
    credentials.chmod(0o600)
    command = [
        "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--email",
        provider_http.required("ACME", "EMAIL"),
        "--server",
        provider_http.required("ACME", "DIRECTORY_URL"),
        "--dns-cloudflare",
        "--dns-cloudflare-credentials",
        str(credentials),
        "--dns-cloudflare-propagation-seconds",
        ACME_PROPAGATION_SECONDS,
        "--config-dir",
        str(acme_dir / "config"),
        "--work-dir",
        str(acme_dir / "work"),
        "--logs-dir",
        str(acme_dir / "logs"),
        "--cert-name",
        spec["certificate_name"],
        "--force-renewal",
    ]
    for domain in spec["domains"]:
        command.extend(("-d", domain))
    try:
        commands.run_command(command, step="certbot certonly")
    finally:
        credentials.unlink(missing_ok=True)
    lineage = acme_dir / "config" / "live" / spec["certificate_name"]
    try:
        return (
            lineage.joinpath("fullchain.pem").read_bytes(),
            lineage.joinpath("privkey.pem").read_bytes(),
        )
    except OSError as exc:
        raise ProviderError("Certbot did not produce a complete lineage.") from exc


def resumable_lineage(
    spec: dict[str, Any], deployed_fingerprint: str
) -> tuple[bytes, bytes] | None:
    """Reuse a newer failed-transaction artifact instead of issuing again."""
    lineage = (
        Path(provider_http.required("HQ", "ACME_DIR")) / "config" / "live" / spec["certificate_name"]
    )
    try:
        fullchain = lineage.joinpath("fullchain.pem").read_bytes()
        private_key = lineage.joinpath("privkey.pem").read_bytes()
    except OSError:
        return None
    fingerprint = validate_certificate(fullchain, private_key, spec["domains"])
    if fingerprint == deployed_fingerprint:
        return None
    with tempfile.TemporaryDirectory() as directory:
        cert_path = Path(directory) / "fullchain.pem"
        cert_path.write_bytes(fullchain)
        raw_expiry = (
            commands.run_command(
                ["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"],
                step="openssl read lineage expiry",
            )
            .decode()
            .strip()
        )
    try:
        expiry = datetime.strptime(
            raw_expiry.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProviderError("Certbot lineage expiry is invalid.") from exc
    minimum_expiry = datetime.now(timezone.utc) + timedelta(
        days=spec["renewal_window_days"]
    )
    if expiry <= minimum_expiry:
        return None
    return fullchain, private_key


def lineage(spec: dict[str, Any]) -> tuple[bytes, bytes]:
    lineage = (
        Path(provider_http.required("HQ", "ACME_DIR")) / "config" / "live" / spec["certificate_name"]
    )
    try:
        return lineage.joinpath("fullchain.pem").read_bytes(), lineage.joinpath(
            "privkey.pem"
        ).read_bytes()
    except OSError as exc:
        raise ProviderError(
            "Certbot lineage is unavailable for reconciliation."
        ) from exc


def lineage_material(spec: dict[str, Any]) -> Callable[[], tuple[bytes, bytes]]:
    """Read this certificate's own material, when and only when it is wanted.

    Returned rather than read, so the ordinary pass (everything already where
    it should be) never opens a private key at all. The publisher calls it only
    once it has established that what is filed is a different certificate.

    The lineage on disk is the same one the deploy path installs from, so what is
    filed is what is served rather than a second rendering of it.
    """

    def read() -> tuple[bytes, bytes]:
        lineage = (
            Path(provider_http.required("HQ", "ACME_DIR"))
            / "config"
            / "live"
            / spec["certificate_name"]
        )
        return (
            lineage.joinpath("fullchain.pem").read_bytes(),
            lineage.joinpath("privkey.pem").read_bytes(),
        )

    return read
