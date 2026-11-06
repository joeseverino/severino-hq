"""Provider records a sweep finds, and the sweep that finds them."""

A_REWRITE = {"domain": "app.example.com", "answer": "10.0.0.10", "enabled": True}
ANOTHER = {"domain": "tool.example.com", "answer": "10.0.0.11", "enabled": True}
A_PROXY = {
    "domain_names": ["shop.example.com"],
    "forward_scheme": "http",
    "forward_host": "10.0.0.20",
    "forward_port": 3000,
    "ssl_forced": True,
    "http2_support": True,
    "allow_websocket_upgrade": True,
    "caching_enabled": False,
    "block_exploits": True,
    "access_list_id": 0,
    "advanced_config": "",
    "hsts_enabled": True,
    "hsts_subdomains": True,
    "trust_forwarded_proto": True,
    "enabled": True,
}


def a_sweep(**kinds) -> dict:
    return {kind: {"ok": True, "records": records} for kind, records in kinds.items()}


def kind_read(kind: str, when) -> None:
    """Record that a sweep read ``kind`` at ``when``, unless a later read stands."""

    from hq.domains.control_plane.models import ProviderInventory

    read = ProviderInventory.objects.filter(kind=kind).first()
    if read is None or read.observed_at < when:
        ProviderInventory.objects.update_or_create(kind=kind, defaults={"observed_at": when})
