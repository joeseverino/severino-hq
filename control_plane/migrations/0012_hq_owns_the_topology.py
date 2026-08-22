"""HQ owns what it used to read out of an authored document.

Everything the snapshot was still load-bearing for becomes a declaration HQ
holds and an operator can edit: the machines it cannot reach and the addresses
that name them, how each place takes a certificate, and which ports a container
answers on when it shares its machine's network. The last snapshot is read once,
here, to write those -- and then the table goes, because a second copy of an
answer is how the two of them start to disagree.

A certificate stops being a reference and states its own names and targets. It
keeps its key, its lineage and its generation: nothing about the certificate
changed, only where the description of it lives.
"""

from django.db import migrations


def _install_domains(attributes):
    return [
        domain
        for domain in attributes.get("install_domains") or []
        if "*" not in domain
    ]


def own_the_topology(apps, schema_editor):
    Snapshot = apps.get_model("control_plane", "TopologySnapshot")
    Resource = apps.get_model("control_plane", "ManagedResource")
    snapshot = Snapshot.objects.filter(pk="topology").first()
    payload = snapshot.payload if snapshot else {}

    # A machine for every host the document named, carrying what it is for and
    # every address that reaches it. Declared for all of them, not only the
    # unreachable ones: an address has to resolve to a name whether or not
    # anything sweeps the machine behind it.
    for host in payload.get("hosts", []):
        name = host.get("id")
        if not name:
            continue
        addresses = [
            address
            for address in (
                host.get("lan_ip"),
                host.get("ts_ip"),
                host.get("public_ip"),
            )
            if address
        ]
        Resource.objects.update_or_create(
            key=name,
            defaults={
                "kind": "machine",
                "spec": {
                    "name": name,
                    "role": host.get("role", ""),
                    "addresses": addresses,
                },
                "enabled": True,
                "generation": 1,
                "observed_generation": 0,
            },
        )

    # Ports a container answers on, for the ones sharing the machine's network.
    # The document listed these as prose; only the containers HQ already
    # declares get them, because a port list is not a reason to start watching
    # something nobody asked it to watch.
    declared_ports = {}
    for host in payload.get("hosts", []):
        for container in host.get("containers", []):
            ports = [
                int(part)
                for part in str(container.get("ports", "")).replace(",", " ").split()
                if part.isdigit()
            ]
            if ports:
                declared_ports[(host.get("id"), container.get("id"))] = ports
    for resource in Resource.objects.filter(kind="portainer.container"):
        ports = declared_ports.get(
            (resource.spec.get("host"), resource.spec.get("name"))
        )
        if ports:
            resource.spec = {**resource.spec, "serves_ports": ports}
            resource.save(update_fields=["spec"])

    # Each place a certificate was being installed becomes a target of its own,
    # keyed by the connection that reaches it, so the certificate can name it.
    installed_on = {}
    for dependency in payload.get("dependencies", []):
        if dependency.get("relation") != "consumes":
            continue
        attributes = dependency.get("attributes") or {}
        connection_ref = attributes.get("connection_ref")
        kind = attributes.get("kind")
        if not connection_ref or not kind:
            continue
        installed_on.setdefault(dependency.get("to"), []).append(connection_ref)
        Resource.objects.update_or_create(
            key=f"{connection_ref}-certificate-target",
            defaults={
                "kind": "tls.delivery_target",
                "spec": {
                    "kind": kind,
                    "connection_ref": connection_ref,
                    "name": attributes.get("name", connection_ref),
                    "certificate_resource": "",
                    "verify_domains": attributes.get("verify_domains") or [],
                    **(
                        {"certificate_directory": attributes["certificate_directory"]}
                        if kind == "caddy"
                        else {}
                    ),
                    **(
                        {
                            "discover_covered_hosts": bool(
                                attributes.get("discover_covered_hosts")
                            )
                        }
                        if kind == "npm"
                        else {}
                    ),
                    **(
                        {"install_domains": _install_domains(attributes)}
                        if kind == "cpanel"
                        else {}
                    ),
                },
                "enabled": True,
                "generation": 1,
                "observed_generation": 0,
            },
        )

    certificates = {entry.get("id"): entry for entry in payload.get("pki", [])}
    for resource in Resource.objects.filter(kind="tls.certificate"):
        reference = resource.spec.get("topology_ref")
        if not reference:
            continue
        entry = certificates.get(reference.removeprefix("pki:"), {})
        targets = installed_on.get(reference, [])
        spec = {key: value for key, value in resource.spec.items() if key != "topology_ref"}
        spec["certificate_name"] = (
            spec.get("certificate_name")
            or entry.get("certificate_name")
            or reference.removeprefix("pki:")
        )
        spec["domains"] = spec.get("domains") or entry.get("domains") or []
        spec["install_on"] = spec.get("install_on") or targets
        resource.spec = spec
        resource.save(update_fields=["spec"])
        # The name each target uses belongs to the certificate that was already
        # installed there. Without this every one of them would be renamed on
        # the next reconcile, and a renamed certificate at a proxy is a second
        # certificate beside the one it meant to replace.
        for target in Resource.objects.filter(
            kind="tls.delivery_target",
            key__in=[f"{ref}-certificate-target" for ref in targets],
        ):
            target.spec = {**target.spec, "certificate_resource": resource.key}
            target.save(update_fields=["spec"])


class Migration(migrations.Migration):

    dependencies = [
        ("control_plane", "0011_alter_operationrequest_action"),
    ]

    operations = [
        migrations.RunPython(own_the_topology, migrations.RunPython.noop),
        migrations.DeleteModel(name="TopologySnapshot"),
    ]
