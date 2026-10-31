"""SSRF guard: competition links and email links are untrusted, so never let them reach the NAS, LAN or loopback."""
from __future__ import annotations

import ipaddress
import os
import socket
from functools import lru_cache
from urllib.parse import urlparse

import httpx

LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".home.arpa", ".localdomain")


def _allow_private() -> bool:
    return os.environ.get("ALLOW_PRIVATE_URLS", "").strip().lower() in {"1", "true", "yes"}


@lru_cache(maxsize=4096)
def _host_is_public(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return True  # unresolvable: the request will simply fail
    for info in infos:
        try:
            if not ipaddress.ip_address(str(info[4][0]).split("%")[0]).is_global:
                return False
        except ValueError:
            return False
    return True


def is_public_url(url: str, allow_inline: bool = False) -> bool:
    if _allow_private():
        return True
    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        return allow_inline and p.scheme in ("data", "blob", "about")
    host = (p.hostname or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(LOCAL_SUFFIXES) or "." not in host and ":" not in host:
        return False
    return _host_is_public(host)


class BlockedURL(Exception):
    pass


def _check(request: httpx.Request) -> None:
    if not is_public_url(str(request.url)):
        raise BlockedURL(f"Refused non-public address: {request.url.host}")


def guarded_client(**kwargs) -> httpx.Client:
    """httpx client that re-checks every request, including each redirect hop."""
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("max_redirects", 8)
    return httpx.Client(event_hooks={"request": [_check]}, **kwargs)
