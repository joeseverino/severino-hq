"""TLS fixtures for the differential parity harness.

Certificates are generated fresh for each run and never leave the scratch
directory. Every HTTP, TLS and command call is answered from the fixture;
openssl alone runs for real, so the Go validator is held to what openssl says.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import tarfile
import tempfile
from types import SimpleNamespace
from unittest import mock
import urllib.error

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from application import expiry
from control_plane.provider_adapters.contracts import ProviderError
from controller_runtime import (
    commands,
    provider_http,
    signing,
    tls,
    tls_issuance,
    tls_verification,
)
from controller_runtime import providers as runtime_providers

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
STAGED = re.compile(r"/[^\s=]*hq-tls-[^/\s]+")
REAL_RUN = commands.run_command
ERRORS = {
    "ConnectionRefusedError": ConnectionRefusedError,
    "ConnectionResetError": ConnectionResetError,
    "TimeoutError": TimeoutError,
    "SSLCertVerificationError": ssl.SSLCertVerificationError,
    "SSLEOFError": ssl.SSLEOFError,
    "gaierror": __import__("socket").gaierror,
    "OSError": OSError,
}


def _name(common, organization=None):
    attributes = [x509.NameAttribute(NameOID.COMMON_NAME, common)]
    if organization:
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    return x509.Name(attributes)


def _ca(subject):
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2040, 1, 1, tzinfo=timezone.utc))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _leaf(issuer, serial, names, days):
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(names[0])).issuer_name(issuer[1].subject)
        .public_key(key.public_key()).serial_number(serial)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(NOW + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        .sign(issuer[0], hashes.SHA256())
    )
    leaf = certificate.public_bytes(serialization.Encoding.PEM).decode()
    root = issuer[1].public_bytes(serialization.Encoding.PEM).decode()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {"leaf": leaf, "fullchain": leaf + root, "key": private}


def generate_pki():
    named = _ca(_name("Test Root", "Example CA"))
    bare = _ca(_name("Bare Root"))
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "old": _leaf(named, 2, ["a.example"], 5),
        "new": _leaf(named, 3, ["a.example"], 90),
        "next": _leaf(named, 4, ["a.example"], 120),
        "soon": _leaf(named, 5, ["a.example"], 10),
        "wild": _leaf(named, 6, ["a.example", "*.a.example"], 60),
        "bare": _leaf(bare, 7, ["a.example"], 70),
        "b": _leaf(named, 8, ["b.example"], 80),
        "rsa_key": rsa_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
    }


class _Response:
    def __init__(self, body):
        self._body = b"" if body is None else json.dumps(body).encode()
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _multipart(data, content_type):
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = []
    for chunk in data.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        head, _, body = chunk[2:].partition(b"\r\n\r\n")
        disposition = head.decode().split("\r\n")[0]
        parts.append({
            "field": re.search(r'name="([^"]*)"', disposition).group(1),
            "filename": re.search(r'filename="([^"]*)"', disposition).group(1),
            "content": body[:-2].decode(),
        })
    return parts


def _decode_input(data):
    if not data:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            if members:
                return {"tar": [
                    {"name": m.name, "mode": m.mode, "content": archive.extractfile(m).read().decode()}
                    for m in members
                ]}
    except tarfile.TarError:
        pass
    try:
        return json.loads(data)
    except ValueError:
        return data.decode()


def _command_key(argv):
    if argv[0] == "ssh":
        return "ssh " + argv[-1]
    if argv[0] == "op":
        return f"op {argv[1]} {argv[2]}"
    if argv[0] == "certbot":
        return f"certbot {argv[1]}"
    return argv[0]


class _Raw:
    def __init__(self, host):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Peer:
    def __init__(self, pem):
        self.pem = pem

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getpeercert(self, binary_form=False):
        if binary_form:
            return ssl.PEM_cert_to_DER_cert(self.pem)
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
            handle.write(self.pem)
        try:
            return ssl._ssl._test_decode_cert(handle.name)
        finally:
            os.unlink(handle.name)


class TLSFixture:
    def __init__(self, fixture, requests):
        self.fixture = fixture
        self.requests = requests

    def open_url(self, url, *, method="GET", headers=None, data=None, timeout=15):
        path = url.removeprefix("https://example.invalid")
        payload = None
        content_type = (headers or {}).get("Content-Type", "")
        if data is not None:
            payload = (
                {"multipart": _multipart(data, content_type)}
                if content_type.startswith("multipart/form-data")
                else json.loads(data)
            )
        self.requests.append({"path": path, "method": method, "payload": payload})
        statuses = dict(self.fixture.get("statuses", {}))
        routes = {"/api/tokens": {"token": "synthetic"}, **self.fixture.get("routes", {})}
        if method != "GET":
            statuses.update(self.fixture.get("write_statuses", {}))
            routes.update(self.fixture.get("answers", {}))
        if path in statuses:
            raise urllib.error.HTTPError(url, statuses[path], "refused", {}, None)
        if path not in routes and method == "GET":
            raise AssertionError(f"Unexpected read: {path}")
        return _Response(routes.get(path))

    def deployments(self):
        count = 0
        for request in self.requests:
            payload = request["payload"] or {}
            if request["method"] == "RUN" and payload["argv"][0] == "ssh" and payload["argv"][-1] == "deploy":
                count += 1
            if request["method"] == "POST" and request["path"].endswith("/upload"):
                count += 1
        return count

    def create_connection(self, address, timeout=None):
        return _Raw(address[0])

    def tls_context(self):
        return SimpleNamespace(wrap_socket=self.peer)

    def peer(self, raw, server_hostname):
        self.requests.append({"path": f"tls://{raw.host}:443", "method": "TLS", "payload": {"sni": server_hostname}})
        phases = self.fixture.get("tls") or []
        if not phases:
            raise ConnectionRefusedError()
        serve = phases[min(self.deployments(), len(phases) - 1)].get(f"{raw.host}|{server_hostname}")
        if serve is None:
            raise ConnectionRefusedError()
        if serve.get("error"):
            raise ERRORS[serve["error"]]()
        return _Peer(self.fixture["certs"][serve["cert"]])

    def run_command(self, command, *, input_bytes=None, step, env=None, subject=""):
        if command[0] == "openssl":
            return REAL_RUN(command, input_bytes=input_bytes, step=step, env=env, subject=subject)
        files = {}
        argv = []
        for arg in command:
            if "hq-tls-" in arg and "[file]=" in arg:
                label, path = arg.split("[file]=", 1)
                files[label] = Path(path).read_text()
            argv.append(STAGED.sub("<staged>", arg))
        record = {"argv": argv, "input": _decode_input(input_bytes)}
        if files:
            record["files"] = files
        key = _command_key(command)
        if key == "certbot certonly":
            record["credentials"] = Path(command[command.index("--dns-cloudflare-credentials") + 1]).read_text()
        self.requests.append({"path": command[0], "method": "RUN", "payload": record})
        outcome = self.fixture.get("commands", {}).get(key)
        if outcome is None and key != "certbot --version":
            raise ProviderError(f"{step} failed.")
        outcome = outcome or {}
        if outcome.get("fail") == "exit":
            raise ProviderError(f"{step} failed.")
        if outcome.get("fail") == "missing":
            raise ProviderError(f"{step} could not complete.")
        if key == "certbot certonly":
            config = command[command.index("--config-dir") + 1]
            name = command[command.index("--cert-name") + 1]
            live = Path(config, "live", name)
            live.mkdir(parents=True, exist_ok=True)
            for file, content in self.fixture.get("lineage", {}).items():
                Path(live, file).write_text(content)
        if outcome.get("stdout_b64"):
            return base64.b64decode(outcome["stdout_b64"])
        return outcome.get("stdout", "").encode()


def _surface(fixture):
    name, spec = fixture["surface"], fixture.get("spec")
    apply, observed = fixture.get("apply", False), fixture.get("observed")
    if name == "reconcile":
        return tls._tls_reconcile(spec, apply=apply, observed=observed)
    if name == "renew":
        return tls._tls_renew(spec, apply=apply, observed=observed)
    if name == "uploaded_reconcile":
        return tls.reconcile_uploaded_certificate(spec, apply=apply, observed=observed)
    if name == "uploaded_delete":
        return tls.delete_uploaded_certificate(spec, apply=apply, observed=observed)
    runtime = signing.SigningRuntime()
    if name == "sign":
        return {"signature": base64.b64encode(runtime.sign(fixture["ref"], fixture["data"].encode())).decode()}
    if name == "signing_public_key":
        return {"public_key": runtime.signing_public_key(fixture["ref"])}
    if name == "onepassword_probe":
        return runtime_providers._probe_onepassword(fixture["ref"])
    raise AssertionError(f"unknown surface {name}")


OWNED_PREFIXES = ("HQ_", "ACME_", "NPM_", "CADDY_", "CPANEL_", "VAULT_", "CLOUDFLARE_", "GITHUB_", "OP_")


def run(fixture, requests):
    """Answer one TLS fixture with the Python controller."""

    harness = TLSFixture(fixture, requests)
    env = {
        key: value for key, value in os.environ.items()
        if not key.endswith("_CONNECTION_REF") and not key.startswith(OWNED_PREFIXES)
    }
    env.update(fixture["env"])
    clock = SimpleNamespace(now=0.0)
    fake_time = SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    )
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(provider_http, "open_url", harness.open_url), \
            mock.patch.object(provider_http, "tls_context", harness.tls_context), \
            mock.patch.object(tls_verification.socket, "create_connection", harness.create_connection), \
            mock.patch.object(commands, "run_command", harness.run_command), \
            mock.patch.object(tls_verification, "time", fake_time), \
            mock.patch.object(tls_verification, "days_until", lambda when: expiry.days_until(when, NOW)), \
            mock.patch.object(tls_issuance, "datetime", wraps=datetime) as wall, \
            provider_http.provider_snapshot():
        wall.now.return_value = NOW
        return _surface(fixture)


def materialize(fixture, root):
    """A fresh scratch tree for one side of one fixture."""

    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(mode=0o700)
    for directory in fixture.get("dirs", ()):
        Path(root, directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, content in fixture.get("files", {}).items():
        path = Path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content)
        path.chmod(0o600)


def with_scratch(fixture, root):
    return json.loads(json.dumps(fixture).replace("{scratch}", str(root)))


# Fixtures

ENV = {
    "NPM_CONNECTION_REF": "npm", "NPM_URL": "https://example.invalid",
    "NPM_USERNAME": "user", "NPM_PASSWORD": "synthetic",
    "CADDY_CONNECTION_REF": "caddy", "CADDY_HOST": "edge.example", "CADDY_USER": "deploy",
    "CADDY_PORT": "2222", "CADDY_HOST_KEY": "ssh-ed25519 AAAA",
    "CPANEL_CONNECTION_REF": "cpanel", "CPANEL_HOST": "cp.example", "CPANEL_USER": "acct",
    "CPANEL_PORT": "22", "CPANEL_HOST_KEY": "ssh-ed25519 BBBB",
    "VAULT_CONNECTION_REF": "vault", "VAULT_PROVIDER": "onepassword", "VAULT_API_TOKEN": "synthetic-op",
    "GITHUB_CONNECTION_REF": "app",
    "HQ_CONTROLLER_SSH_DIR": "{scratch}/ssh", "HQ_ACME_DIR": "{scratch}/acme",
    "ACME_EMAIL": "ops@example.test", "ACME_DIRECTORY_URL": "https://acme.example/directory",
    "CLOUDFLARE_DNS_API_TOKEN": "synthetic-dns",
}
CADDY = {"kind": "caddy", "name": "edge", "connection_ref": "caddy",
         "certificate_directory": "/etc/caddy/certs", "verify_domains": ["a.example"]}
NPM = {"kind": "npm", "name": "proxy", "connection_ref": "npm", "verify_domains": ["a.example"],
       "discover_covered_hosts": False}
CPANEL = {"kind": "cpanel", "name": "host", "connection_ref": "cpanel", "verify_domains": ["a.example"],
          "install_domains": []}
VAULT = {"kind": "onepassword", "name": "notes", "connection_ref": "vault", "vault": "Certs", "item": "a"}
EDGE, PROXY, HOST = "edge.example|a.example", "example.invalid|a.example", "cp.example|a.example"


def _spec(*consumers, publish=(), domains=("a.example",)):
    return {"certificate_name": "a", "domains": list(domains), "consumers": list(consumers),
            "publish_to": list(publish), "renewal_window_days": 30}


def _lineage(pki, name):
    return {"acme/config/live/a/fullchain.pem": pki[name]["fullchain"],
            "acme/config/live/a/privkey.pem": pki[name]["key"]}


def _snapshot(pki, name):
    bundle = tls_issuance.certificate_bundle(pki[name]["fullchain"].encode(), pki[name]["key"].encode())
    return {"stdout_b64": base64.b64encode(bundle).decode()}


def _fixture(pki, surface, **values):
    fixture = {
        "provider": "tls", "surface": surface, "env": dict(ENV), "dirs": ["ssh", "acme"],
        "certs": {name: item["leaf"] for name, item in pki.items() if isinstance(item, dict)},
        "verification": {"timeout_seconds": 180, "interval_seconds": 5},
        "routes": {}, "commands": {}, "tls": [], "apply": False,
    }
    fixture.update(values)
    return fixture


def _item(fields=(), tags=(), files=()):
    return {"stdout": json.dumps({
        "fields": [{"label": label, "type": kind, "value": value} for label, kind, value in fields],
        "tags": list(tags), "files": [{"name": name} for name in files],
    })}


def _local_epoch(day):
    from application.timestamps import moment

    return str(int(moment(day, naive="keep").timestamp()))


def tls_fixtures(pki):
    f = lambda surface, **values: _fixture(pki, surface, **values)  # noqa: E731
    # Reading what consumers serve.
    yield f("reconcile", spec=_spec(CADDY), tls=[{EDGE: {"cert": "new"}}])
    yield f("reconcile", spec=_spec(CADDY), tls=[{EDGE: {"cert": "old"}}])
    yield f("reconcile", spec=_spec(CADDY, NPM), tls=[{EDGE: {"cert": "new"}, PROXY: {"cert": "wild"}}])
    yield f("reconcile", spec=_spec(CADDY, {**NPM, "verify_domains": []}), tls=[{EDGE: {"cert": "bare"}}])
    yield f("reconcile", spec=_spec(CADDY, NPM), tls=[{EDGE: {"error": "ConnectionRefusedError"}, PROXY: {"cert": "new"}}])
    for error in ("SSLCertVerificationError", "TimeoutError", "gaierror", "SSLEOFError"):
        yield f("reconcile", spec=_spec(CADDY), tls=[{EDGE: {"error": error}}])
    yield f("reconcile", spec=_spec({**CADDY, "verify_domains": []}))
    yield f("reconcile", spec=_spec({**CPANEL, "connection_ref": "missing"}, CADDY), tls=[{EDGE: {"cert": "new"}}])
    hosts = [
        {"id": 1, "domain_names": ["shop.a.example"], "enabled": True},
        {"id": 2, "domain_names": ["off.a.example"], "enabled": False},
        {"id": 3, "domain_names": ["zero.a.example"], "enabled": 0},
        {"id": 4, "domain_names": ["elsewhere.example"]},
    ]
    discover = {**NPM, "discover_covered_hosts": True}
    yield f("reconcile", spec=_spec(discover, domains=("a.example", "*.a.example")),
            routes={"/api/nginx/proxy-hosts": hosts},
            tls=[{PROXY: {"cert": "wild"}, "example.invalid|shop.a.example": {"cert": "wild"},
                  "example.invalid|zero.a.example": {"error": "TimeoutError"}}])
    yield f("reconcile", spec=_spec(discover), statuses={"/api/nginx/proxy-hosts": 403})
    yield f("reconcile", spec=_spec(CADDY, NPM), tls=[{EDGE: {"cert": "new"}, PROXY: {"cert": "new"}}],
            observed={"npm_certificate_ids": {"proxy": 12, "gone": 3, "edge": 1.5}})
    yield f("reconcile", spec=_spec(NPM), tls=[{PROXY: {"cert": "new"}}], observed={"npm_certificate_id": 9})

    # Applying a reconcile from the saved lineage.
    yield f("reconcile", apply=True, spec=_spec(CADDY), files=_lineage(pki, "new"), tls=[{EDGE: {"cert": "new"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY), files=_lineage(pki, "new"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}},
            tls=[{EDGE: {"cert": "old"}}, {EDGE: {"cert": "new"}}])
    yield f("reconcile", apply=True, spec=_spec(NPM), files=_lineage(pki, "new"), tls=[{PROXY: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY), tls=[{EDGE: {"cert": "new"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY), files=_lineage(pki, "new"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {"fail": "exit"}},
            tls=[{EDGE: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY), files=_lineage(pki, "new"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}},
            tls=[{EDGE: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY), files=_lineage(pki, "new"),
            commands={"ssh snapshot": {"stdout": "not a bundle"}}, tls=[{EDGE: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY, domains=("a.example", "b.example")), files=_lineage(pki, "new"),
            tls=[{EDGE: {"cert": "old"}}])

    # One matching reading never vouches for a consumer that was not read: deploy, then roll back.
    two = {**CADDY, "verify_domains": ["a.example", "www.a.example"]}
    yield f("reconcile", apply=True, spec=_spec(two), files=_lineage(pki, "new"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}}, tls=[{EDGE: {"cert": "new"}}])
    yield f("reconcile", apply=True, spec=_spec(two), files=_lineage(pki, "new"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}},
            tls=[{EDGE: {"cert": "old"}}, {EDGE: {"cert": "new"}, "edge.example|www.a.example": {"error": "ConnectionRefusedError"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY, {**CPANEL, "verify_domains": [], "install_domains": ["a.example"]}), files=_lineage(pki, "new"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {},
                      "ssh sites": {"stdout": json.dumps({"sites": {"a.example": []}})}},
            tls=[{EDGE: {"cert": "new"}}])

    # NPM as a consumer: create, reuse by id, refuse duplicates and foreign certificates.
    npm_hosts = {"/api/nginx/proxy-hosts": [
        {"id": 5, "domain_names": ["a.example"], "enabled": True},
        {"id": 6, "domain_names": ["b.example"], "enabled": True},
    ]}
    npm_deploy = {"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}}
    yield f("reconcile", apply=True, spec=_spec(CADDY, NPM), files=_lineage(pki, "new"), commands=npm_deploy,
            routes={"/api/nginx/certificates": [], **npm_hosts}, answers={"/api/nginx/certificates": {"id": 21}},
            tls=[{EDGE: {"cert": "old"}, PROXY: {"cert": "old"}}, {EDGE: {"cert": "new"}, PROXY: {"cert": "old"}},
                 {EDGE: {"cert": "new"}, PROXY: {"cert": "new"}}])
    yield f("reconcile", apply=True, spec=_spec(CADDY, NPM), files=_lineage(pki, "new"), commands=npm_deploy,
            observed={"npm_certificate_ids": {"proxy": 7}},
            routes={"/api/nginx/certificates": [{"id": 7, "nice_name": "renamed", "provider": "other"},
                                                {"id": 8, "nice_name": "Severino HQ - proxy", "provider": "other"}],
                    **npm_hosts},
            tls=[{EDGE: {"cert": "old"}, PROXY: {"cert": "old"}}, {EDGE: {"cert": "new"}, PROXY: {"cert": "old"}},
                 {EDGE: {"cert": "new"}, PROXY: {"cert": "new"}}])
    yield f("reconcile", apply=True, spec=_spec(NPM, CADDY), files=_lineage(pki, "new"), commands=npm_deploy,
            routes={"/api/nginx/certificates": [{"id": 1, "nice_name": "Severino HQ - proxy", "provider": "other"},
                                                {"id": 2, "nice_name": "Severino HQ - proxy", "provider": "other"}]},
            tls=[{EDGE: {"cert": "old"}, PROXY: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(NPM, CADDY), files=_lineage(pki, "new"), commands=npm_deploy,
            routes={"/api/nginx/certificates": [{"id": 1, "nice_name": "Severino HQ - proxy", "provider": "letsencrypt"}]},
            tls=[{EDGE: {"cert": "old"}, PROXY: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(NPM, CADDY), files=_lineage(pki, "new"), commands=npm_deploy,
            routes={"/api/nginx/certificates": [{"id": 1, "nice_name": "Severino HQ - proxy", "provider": "other"}],
                    "/api/nginx/proxy-hosts": [{"id": 6, "domain_names": ["b.example"]}]},
            tls=[{EDGE: {"cert": "old"}, PROXY: {"cert": "old"}}])
    yield f("reconcile", apply=True, spec=_spec(NPM, CADDY), files=_lineage(pki, "new"), commands=npm_deploy,
            routes={"/api/nginx/certificates": [{"id": 1, "nice_name": "Severino HQ - proxy", "provider": "other"}], **npm_hosts},
            write_statuses={"/api/nginx/certificates/1/upload": 500},
            tls=[{EDGE: {"cert": "old"}, PROXY: {"cert": "old"}}])

    # cPanel: the plan is decided before anything is issued.
    sites = {"stdout": json.dumps({"sites": {"a.example": ["www.a.example"], "b.example": []}})}
    yield f("reconcile", apply=True, spec=_spec(CADDY, CPANEL), files=_lineage(pki, "new"),
            commands={**npm_deploy, "ssh sites": sites},
            tls=[{EDGE: {"cert": "old"}, HOST: {"cert": "old"}}, {EDGE: {"cert": "new"}, HOST: {"cert": "old"}},
                 {EDGE: {"cert": "new"}, HOST: {"cert": "new"}}])
    for answer, consumer in (
        ({"stdout": json.dumps({"sites": {"b.example": []}})}, CPANEL),
        ({"stdout": json.dumps({"sites": {}})}, CPANEL),
        ({"stdout": "{not json"}, CPANEL),
        (sites, {**CPANEL, "verify_domains": ["a.example", "b.example"], "install_domains": ["www.a.example"]}),
    ):
        yield f("reconcile", apply=True, spec=_spec(CADDY, consumer), files=_lineage(pki, "new"),
                commands={**npm_deploy, "ssh sites": answer}, tls=[{EDGE: {"cert": "old"}, HOST: {"cert": "old"}}])

    # Recording facts in 1Password after a reading.
    expires = (NOW + timedelta(days=90)).date().isoformat()
    for item in (
        _item(tags=["personal"]),
        _item(fields=[("Covers", "STRING", "a.example"), ("Issued by", "STRING", "Example CA"),
                      ("Expires", "DATE", _local_epoch(expires)), ("Installed on", "STRING", "edge")],
              tags=["hq-managed"], files=["fullchain", "privkey"]),
        {"fail": "exit"},
        {"stdout": "[]"},
    ):
        yield f("reconcile", apply=True, spec=_spec(CADDY, publish=[VAULT]), files=_lineage(pki, "new"),
                tls=[{EDGE: {"cert": "new"}}], commands={"op item get": item, "op item edit": {}})
    yield f("reconcile", apply=True, spec=_spec(CADDY, publish=[VAULT, {**VAULT, "name": "second", "item": "b"}]),
            files=_lineage(pki, "new"), tls=[{EDGE: {"cert": "new"}}],
            commands={"op item get": _item(tags=["hq-managed"]), "op item edit": {"fail": "missing"}})

    # An item or vault that op would read as an option is refused before op runs.
    for target in ({**VAULT, "item": "--vault=Other"}, {**VAULT, "vault": "-x"}):
        yield f("reconcile", apply=True, spec=_spec(CADDY, publish=[target]), files=_lineage(pki, "new"),
                tls=[{EDGE: {"cert": "new"}}], commands={"op item get": _item(), "op item edit": {}})
    # A lineage name is checked before it becomes a path or a certbot argument.
    escape = {**_spec(CADDY), "certificate_name": "../escape"}
    yield f("reconcile", apply=True, spec=escape, files=_lineage(pki, "new"), tls=[{EDGE: {"cert": "old"}}])
    yield f("renew", apply=True, spec=escape, commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}},
            tls=[{EDGE: {"cert": "old"}}])

    # Renewal: resume a newer lineage, else issue; refuse what cannot be satisfied.
    yield f("renew", spec=_spec(CADDY))
    yield f("renew", apply=True, spec=_spec(CADDY), files=_lineage(pki, "next"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}},
            tls=[{EDGE: {"cert": "old"}}, {EDGE: {"cert": "next"}}])
    yield f("renew", apply=True, spec=_spec(two), files=_lineage(pki, "next"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}},
            tls=[{EDGE: {"cert": "old"}}, {EDGE: {"cert": "next"}}])
    issue = {"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}, "certbot certonly": {}}
    issued = {"fullchain.pem": pki["new"]["fullchain"], "privkey.pem": pki["new"]["key"]}
    for files in ({}, _lineage(pki, "old"), _lineage(pki, "soon")):
        yield f("renew", apply=True, spec=_spec(CADDY), files=files, commands=issue, lineage=issued,
                tls=[{EDGE: {"cert": "old"}}, {EDGE: {"cert": "new"}}])
    yield f("renew", apply=True, spec=_spec(CADDY), commands={**issue, "certbot certonly": {"fail": "exit"}},
            tls=[{EDGE: {"cert": "old"}}])
    yield f("renew", apply=True, spec=_spec(CADDY), commands={**issue, "certbot certonly": {}}, lineage={},
            tls=[{EDGE: {"cert": "old"}}])
    no_email = {key: value for key, value in ENV.items() if key != "ACME_EMAIL"}
    yield f("renew", apply=True, spec=_spec(CADDY), env=no_email, commands=issue, tls=[{EDGE: {"cert": "old"}}])
    yield f("renew", apply=True, spec=_spec(NPM))
    yield f("renew", apply=True, spec=_spec(CADDY, publish=[VAULT]), files=_lineage(pki, "next"),
            commands={"ssh snapshot": _snapshot(pki, "old"), "ssh deploy": {}, "op item get": _item(),
                      "op item edit": {}},
            tls=[{EDGE: {"cert": "old"}}, {EDGE: {"cert": "next"}}])

    # Uploaded certificates: install, and remove from NPM only.
    material = {"fullchain": pki["new"]["fullchain"], "private_key": pki["new"]["key"], "domains": ["a.example"]}
    uploaded = {"certificate_name": "lab", "install_on": ["caddy"], "consumers": [CADDY], "domains": ["a.example"],
                "material": material}
    yield f("uploaded_reconcile", spec=uploaded)
    yield f("uploaded_reconcile", apply=True, spec=uploaded, commands={"ssh deploy": {}})
    yield f("uploaded_reconcile", apply=True, spec={**uploaded, "material": {"domains": []}})
    yield f("uploaded_reconcile", apply=True, spec={**uploaded, "consumers": [NPM]},
            routes={"/api/nginx/certificates": [], **npm_hosts}, answers={"/api/nginx/certificates": {"id": 31}})
    npm_only = {**uploaded, "consumers": [NPM]}
    yield f("uploaded_delete", apply=True, spec=uploaded)
    certificates = {"/api/nginx/certificates": [{"id": 31, "nice_name": "Severino HQ - proxy", "provider": "other"}]}
    yield f("uploaded_delete", apply=True, spec=npm_only, routes=certificates)
    yield f("uploaded_delete", apply=True, spec=npm_only, routes={"/api/nginx/certificates": []})
    yield f("uploaded_delete", apply=True, spec=npm_only, observed={"npm_certificate_ids": {"proxy": 31}},
            routes={**certificates, "/api/nginx/proxy-hosts": [{"id": 5, "domain_names": ["a.example"], "certificate_id": 31}]})
    for apply in (False, True):
        yield f("uploaded_delete", apply=apply, spec=npm_only, observed={"npm_certificate_id": 31},
                routes={**certificates, "/api/nginx/proxy-hosts": [{"id": 5, "domain_names": ["a.example"], "certificate_id": 2}]})

    # Signing and the 1Password probe.
    key_files = {"ssh/app.key": pki["rsa_key"], "ssh/app.key.pub": "ssh-rsa AAAA app\n"}
    yield f("sign", ref="app", data="header.payload", files=key_files)
    for ref in ("", "a/b", ".hidden", "nope"):
        yield f("sign", ref=ref, data="x", files=key_files)
    yield f("sign", ref="app", data="x")
    yield f("signing_public_key", ref="app", files=key_files)
    yield f("signing_public_key", ref="app")
    for answer in ({"stdout": json.dumps([{"id": "a"}, {"id": "b"}])}, {"stdout": "{}"}, {"fail": "exit"}):
        yield f("onepassword_probe", ref="vault", commands={"op vault list": answer})
