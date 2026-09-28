"""The remote's self-signed certificate, and fingerprints to pin it by.

A remote on a home or lab network has no name a public authority would sign, so
it makes its own certificate once and keeps it. Trust is established the other
way: the main machine shows the fingerprint, the remote prints the same one in
its terminal, and the user compares them before saying yes. From then on the
fingerprint is pinned, and a different certificate is refused until the user
trusts it again.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os

from .. import paths

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"


def fingerprint(der: bytes) -> str:
    """SHA-256 of the certificate, as colon-separated hex pairs."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def ensure_certificate(directory: str | None = None, hostnames: tuple[str, ...] = ()) -> tuple[str, str]:
    """The remote's certificate and key paths, made on first use.

    An EC P-256 key, valid for ten years: it is identified by its fingerprint,
    not its expiry, and renewing it would mean every main machine re-trusting
    it. The key file is ``0600``.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory = directory or paths.remote_worker_dir()
    cert_path, key_path = os.path.join(directory, CERT_NAME), os.path.join(directory, KEY_NAME)
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    os.makedirs(directory, mode=0o700, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CoBirb remote worker")])
    alt: list = [x509.DNSName("localhost")]
    for host in hostnames:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alt.append(x509.DNSName(host))
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .sign(key, hashes.SHA256())
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def certificate_fingerprint(cert_path: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    return fingerprint(cert.public_bytes(serialization.Encoding.DER))
