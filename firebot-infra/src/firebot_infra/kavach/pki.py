"""Tiny private PKI for provisioning TLS certificates (standard X.509 via the reviewed `cryptography` library).
Use for development and small fleets; a production fleet should keep the CA key offline."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_UTC = dt.timezone.utc


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def create_ca(common_name: str = "firebot-ca", valid_days: int = 3650) -> tuple[bytes, bytes]:
    """Returns (ca_cert_pem, ca_key_pem). Keep the key secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(_UTC)
    cert = (x509.CertificateBuilder().subject_name(_name(common_name)).issuer_name(_name(common_name))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=valid_days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), _pem_key(key)


def issue_cert(ca_cert_pem: bytes, ca_key_pem: bytes, common_name: str, valid_days: int = 365,
               not_before: dt.datetime | None = None, not_after: dt.datetime | None = None) -> tuple[bytes, bytes]:
    """Issue a client+server certificate whose Common Name is the node id. Returns (cert_pem, key_pem)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(_UTC)
    cert = (x509.CertificateBuilder().subject_name(_name(common_name)).issuer_name(ca_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(not_before or now - dt.timedelta(minutes=5))
            .not_valid_after(not_after or now + dt.timedelta(days=valid_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), _pem_key(key)


def write_pem(path: str | Path, pem: bytes, secret: bool = False) -> None:
    """Write a PEM file. Private keys (secret=True) are created 0600 and never overwritten."""
    if secret:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    else:
        Path(path).write_bytes(pem)
