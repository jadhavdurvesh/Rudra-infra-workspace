"""Mutual-TLS helpers for SETU's TCP transport (standard library `ssl` only).

Both sides present a certificate issued by the same private CA (see kavach.pki to make one) and verify the other's.
TLS 1.3 minimum. The certificate's Common Name is the node id, and the transport checks it against who it meant to
reach, so a valid certificate for a different node is not accepted."""
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any


def make_contexts(ca_file: str | Path, cert_file: str | Path, key_file: str | Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Returns (server_context, client_context) for one node."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_3
    server.load_cert_chain(str(cert_file), str(key_file))
    server.load_verify_locations(str(ca_file))
    server.verify_mode = ssl.CERT_REQUIRED                  # clients must present a certificate too
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.minimum_version = ssl.TLSVersion.TLSv1_3
    client.load_cert_chain(str(cert_file), str(key_file))
    client.load_verify_locations(str(ca_file))
    client.verify_mode = ssl.CERT_REQUIRED
    client.check_hostname = False                           # identity is checked by node id (CN) instead of DNS name
    return server, client


def peer_cn(sock: Any) -> str | None:
    """Common Name of the verified peer certificate on an established TLS socket."""
    try:
        cert = sock.getpeercert()
    except (ValueError, ssl.SSLError, OSError):
        return None
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None
