"""STARTTLS certificate: the configured one, or a self-signed one generated once."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from logfmt import kv

log = logging.getLogger("relay")


def _generate_self_signed(cert_path: Path, key_path: Path, hostname: str) -> None:
    # RSA rather than EC: older scanners and printers often only speak RSA.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def load_tls_context(
    cert_file: Path | None, key_file: Path | None, data_dir: Path, hostname: str
) -> ssl.SSLContext | None:
    """An SSLContext for STARTTLS, or None (with a warning) if no certificate can be had."""
    source = "configured"
    if cert_file is None or key_file is None:
        source = "self-signed"
        cert_file = data_dir / "tls" / "cert.pem"
        key_file = data_dir / "tls" / "key.pem"
        if not (cert_file.exists() and key_file.exists()):
            try:
                _generate_self_signed(cert_file, key_file, hostname)
                log.info(kv("tls generated", cert=cert_file))
            except OSError as error:
                log.warning(kv("tls disabled", reason="cannot-write-certificate", path=cert_file, error=error))
                return None

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert_file, key_file)
    except (OSError, ssl.SSLError) as error:
        log.warning(kv("tls disabled", reason="cannot-load-certificate", path=cert_file, error=error))
        return None

    der = ssl.PEM_cert_to_DER_cert(cert_file.read_text())
    fingerprint = hashlib.sha256(der).hexdigest()
    log.info(kv("tls ready", source=source, cert=cert_file, sha256=fingerprint))
    return context
