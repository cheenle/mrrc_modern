"""Self-signed TLS certificate bootstrap for the desktop launcher.

A fresh Windows install has no certificates, and `server.py` only enables
SSL when cert+key exist — so the launcher generates a throwaway
self-signed certificate on first run (10-year validity, SANs covering
localhost / hostname / LAN IPs) and reuses it afterwards.  This is a
convenience certificate for LAN use: browsers will show an "untrusted"
warning the user accepts once.  Real deployments can point
MRRC_SSL_CERT / MRRC_SSL_KEY at proper certificates instead (the
launcher and server both honour those env vars first).
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path

logger = logging.getLogger("mrrc.ssl")

CERT_FILENAME = "server.crt"
KEY_FILENAME = "server.key"
VALID_DAYS = 3650  # throwaway cert — no renewal machinery by design


def _lan_ips() -> list[str]:
    """Best-effort local IPv4 addresses for the certificate SAN list."""
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # TEST-NET-1 (RFC 5737) — selects the outbound interface
            # without sending any traffic.
            s.connect(("192.0.2.1", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)


def sign_for(name: str, cert_dir: Path, filename: str = "fullchain.pem"):
    """Generate a self-signed pair whose CN and SAN are ``name`` (an instance's entry name).

    The Cloud Hub verifies the instance by exactly this name (its /enroll endpoint refuses a
    certificate for any other), so the certificate the app serves when it is published through the
    hub must carry it. Same primitives as the localhost pair, different subject.
    """
    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / filename
    key_path = cert_dir / (name.split(".")[0] + ".key")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        logger.warning("cryptography not installed — cannot sign a certificate for %s", name)
        return None
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    logger.info("signed a self-signed certificate for %s", name)
    return cert_path, key_path


def ensure_self_signed(cert_dir: Path):
    """Return (cert_path, key_path), generating a self-signed pair if absent.

    Returns None when the cryptography package is unavailable or
    generation fails — the caller then falls back to plain HTTP.
    """
    cert_dir = Path(cert_dir)
    cert_path = cert_dir / CERT_FILENAME
    key_path = cert_dir / KEY_FILENAME
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        logger.warning("cryptography not installed — cannot generate self-signed cert")
        return None

    try:
        key = ec.generate_private_key(ec.SECP256R1())
        hostname = socket.gethostname()
        san_entries: list[x509.GeneralName] = [
            x509.DNSName("localhost"),
            x509.DNSName(hostname),
            x509.DNSName(f"{hostname}.local"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv6Address("::1")),
        ]
        for ip in _lan_ips():
            try:
                san_entries.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
            except ValueError:
                pass

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .sign(key, hashes.SHA256())
        )

        cert_dir.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        try:
            key_path.chmod(0o600)
        except OSError:
            pass  # Windows ACLs don't map POSIX modes — fine
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        logger.info("Generated self-signed TLS certificate: %s", cert_path)
        return cert_path, key_path
    except Exception as e:
        logger.warning("Self-signed certificate generation failed: %s", e)
        return None
