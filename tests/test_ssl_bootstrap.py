"""Tests for ssl_bootstrap.py — self-signed TLS cert bootstrap (SDD V2.10)."""
import ipaddress
import socket
import tempfile
import unittest
from pathlib import Path

import ssl_bootstrap

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


@unittest.skipUnless(HAS_CRYPTO, "cryptography not installed")
class SelfSignedCertTests(unittest.TestCase):
    def test_generates_cert_and_key_on_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            pair = ssl_bootstrap.ensure_self_signed(Path(tmp))
            self.assertIsNotNone(pair)
            cert_path, key_path = pair
            self.assertTrue(cert_path.exists())
            self.assertTrue(key_path.exists())
            self.assertEqual(cert_path.read_bytes()[:27], b"-----BEGIN CERTIFICATE-----")
            self.assertIn(b"PRIVATE KEY-----", key_path.read_bytes())

    def test_second_run_reuses_existing_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ssl_bootstrap.ensure_self_signed(Path(tmp))
            cert_bytes = first[0].read_bytes()
            second = ssl_bootstrap.ensure_self_signed(Path(tmp))
            self.assertEqual(first, second)
            self.assertEqual(second[0].read_bytes(), cert_bytes)  # not regenerated

    def test_cert_san_covers_localhost_and_ips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _ = ssl_bootstrap.ensure_self_signed(Path(tmp))
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
            dns_names = san.get_values_for_type(x509.DNSName)
            ip_addrs = san.get_values_for_type(x509.IPAddress)
            self.assertIn("localhost", dns_names)
            self.assertIn(socket.gethostname(), dns_names)
            self.assertIn(ipaddress.IPv4Address("127.0.0.1"), ip_addrs)
            self.assertIn(ipaddress.IPv6Address("::1"), ip_addrs)

    def test_cert_is_self_issued_and_long_lived(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _ = ssl_bootstrap.ensure_self_signed(Path(tmp))
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.assertEqual(cert.subject, cert.issuer)
            delta = cert.not_valid_after_utc - cert.not_valid_before_utc
            self.assertGreaterEqual(delta.days, 3600)  # ~10-year throwaway cert
            # ca=False — it is a server cert, not a CA
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
            self.assertFalse(bc.value.ca)


class CryptoMissingTests(unittest.TestCase):
    def test_returns_none_without_cryptography(self):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch("builtins.__import__", side_effect=fake_import):
                self.assertIsNone(ssl_bootstrap.ensure_self_signed(Path(tmp)))


class LanIpTests(unittest.TestCase):
    def test_lan_ips_excludes_loopback(self):
        ips = ssl_bootstrap._lan_ips()
        self.assertNotIn("127.0.0.1", ips)
        for ip in ips:
            ipaddress.IPv4Address(ip)  # must all parse as IPv4


if __name__ == "__main__":
    unittest.main()
