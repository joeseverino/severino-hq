"""Tailnet policy names, as the machines they stand for.

A policy names a device by an alias for one of its addresses (``hosts``), by a
tag it carries, or by its owner. An alias resolves through the address to the
machine the catalogue says answers there, and links to it; two aliases for
one machine (its IPv4 and IPv6 addresses) are one link with both names in its
title. A tag lists the devices carrying it. Users and unresolved names stay as
the policy writes them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from .entity_links import EntityLink, entity_link
from .locate import host_of


@dataclass(frozen=True)
class PolicyName:
    """One name as a policy writes it, and the entity it stands for if any."""

    text: str
    link: EntityLink | None = None


class PolicyNames:
    """Resolves policy names against one read of the catalogue and the policy."""

    def __init__(self, hosts: dict | None = None, machines: Iterable[Any] | None = None):
        from .connections import machines_once
        from .tailnet import policy

        catalog = tuple(machines_once() if machines is None else machines)
        self._owners = {
            host_of(str(address)): item.name for item in catalog for address in item.addresses
        }
        self._hosts = dict(policy().hosts if hosts is None else hosts)

    def machine_at(self, address: str) -> str:
        return self._owners.get(host_of(str(address).partition("/")[0]), "")

    def of(self, names: Iterable[str]) -> tuple[PolicyName, ...]:
        """Each name, with the aliases of one machine folded into one link."""

        found: list[PolicyName] = []
        by_machine: dict[str, int] = {}
        for name in names:
            text = str(name)
            owner = self.machine_at(self._hosts.get(text, "")) if text in self._hosts else ""
            if not owner:
                found.append(PolicyName(text))
                continue
            if owner in by_machine:
                index = by_machine[owner]
                link = found[index].link
                found[index] = replace(
                    found[index],
                    link=replace(link, title=f"{link.title}, {text}"),
                )
                continue
            by_machine[owner] = len(found)
            link = replace(entity_link("machine", owner, label=text), title=text)
            found.append(PolicyName(text, link))
        return tuple(found)

    def addresses(self, addresses: Iterable[str]) -> tuple[PolicyName, ...]:
        """Addresses, each as the machine answering there with the address."""

        found = []
        for address in addresses:
            text = str(address)
            owner = self.machine_at(text)
            found.append(
                PolicyName(
                    text,
                    entity_link("machine", owner, label=f"{owner} ({text})") if owner else None,
                )
            )
        return tuple(found)


def tagged(devices: dict, names: PolicyNames) -> dict[str, tuple[EntityLink, ...]]:
    """Each tag, and the machines of the devices carrying it."""

    found: dict[str, list[EntityLink]] = {}
    for device in devices.values():
        owner = next(
            (owner for address in device.addresses if (owner := names.machine_at(address))),
            "",
        )
        link = entity_link("machine", owner) if owner else None
        for tag in device.tags:
            if link is not None and link not in found.setdefault(tag, []):
                found[tag].append(link)
    return {tag: tuple(links) for tag, links in found.items()}
