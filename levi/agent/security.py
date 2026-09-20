"""Provider egress policy and external-agent capability principals."""

import ipaddress
import os
import secrets
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


def endpoint_addresses(url: str, allow_localhost=False):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Provider endpoint must be a plain HTTP(S) URL without credentials"
        )
    addresses = {
        row[4][0]
        for row in socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    }
    if not addresses:
        raise ValueError("Provider endpoint has no addresses")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if address.is_global:
            if parsed.scheme != "https":
                raise ValueError("Remote model endpoints require HTTPS")
        elif not (allow_localhost and address.is_loopback):
            raise ValueError(
                "Private, metadata and link-local model endpoints are disabled"
            )
    return addresses


@dataclass(frozen=True)
class Principal:
    id: str
    human: bool = False
    datasets: tuple[str, ...] = ()
    operations: tuple[str, ...] = ("read", "draft")

    def require(self, operation, repo_id=None):
        if not self.human and operation not in self.operations:
            raise PermissionError("Operation is outside this principal's scope")
        if not self.human and repo_id is not None and repo_id not in self.datasets:
            raise PermissionError("Dataset is outside this principal's scope")


def external_principal(bearer):
    from levi import service

    from .grants import authenticate
    from .store import Store

    if who := authenticate(Store(service.STATE), bearer):
        return who
    expected = os.getenv("LEVI_AGENT_TOKEN")
    if not expected or not secrets.compare_digest(bearer or "", expected):
        raise PermissionError("A configured LEVI_AGENT_TOKEN is required")
    from levi import service

    from .store import Store

    try:
        if not Store(service.STATE).get("settings", "external-access")["enabled"]:
            raise PermissionError("External Agent access is disconnected")
    except KeyError:
        pass
    return Principal(
        "external",
        datasets=tuple(filter(None, os.getenv("LEVI_AGENT_DATASETS", "").split(","))),
    )
