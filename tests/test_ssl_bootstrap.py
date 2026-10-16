"""Tests for ssl_bootstrap.py — self-signed TLS cert bootstrap (SDD V2.10)."""
import datetime
import ipaddress
import socket
import tempfile
import unittest
import unittest.mock  # explicit: this file patches builtins.__import__, and relying on another
                       # test module to have imported the submodule made it fail when run alone
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
            assert pair is not None, "no certificate was generated"
            cert_path, key_path = pair
            self.assertTrue(cert_path.exists())
            self.assertTrue(key_path.exists())
            self.assertEqual(cert_path.read_bytes()[:27], b"-----BEGIN CERTIFICATE-----")
            self.assertIn(b"PRIVATE KEY-----", key_path.read_bytes())

    def test_second_run_reuses_existing_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ssl_bootstrap.ensure_self_signed(Path(tmp))
            assert first is not None, "no certificate was generated"
            cert_bytes = first[0].read_bytes()
            second = ssl_bootstrap.ensure_self_signed(Path(tmp))
            assert second is not None, "no certificate was generated"
            self.assertEqual(first, second)
            self.assertEqual(second[0].read_bytes(), cert_bytes)  # not regenerated

    def test_cert_san_covers_localhost_and_ips(self):
        from cryptography import x509  # local import: bound in this scope, unlike the module-level try

        with tempfile.TemporaryDirectory() as tmp:
            made = ssl_bootstrap.ensure_self_signed(Path(tmp))
            assert made is not None, "no certificate was generated"
            cert_path = made[0]
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
        from cryptography import x509  # local import: bound in this scope, unlike the module-level try

        with tempfile.TemporaryDirectory() as tmp:
            made = ssl_bootstrap.ensure_self_signed(Path(tmp))
            assert made is not None, "no certificate was generated"
            cert_path = made[0]
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.assertEqual(cert.subject, cert.issuer)
            delta = cert.not_valid_after_utc - cert.not_valid_before_utc
            self.assertGreaterEqual(delta.days, 3600)  # ~10-year throwaway cert
            # ca=False — it is a server cert, not a CA
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
            self.assertFalse(bc.value.ca)


@unittest.skipUnless(HAS_CRYPTO, "cryptography not installed")
class SignForReuseTests(unittest.TestCase):
    """``sign_for`` is idempotent: a usable pair for the same name is reused, not re-signed.

    Regression (2026-10-15, live instance `bg7kxr`): the Cloud Hub **pins** the certificate an
    instance enrolled, so a signature that is never enrolled — a second connect, a retry after a
    failed enroll — leaves the pinned and the served certificate as two different documents, and
    the entry answers 502 for good. Restarting does not help: the process then serves the newer
    file again while nginx still checks the pinned one. Measured on that machine: two signatures
    three seconds apart (both paths backdate ``notBefore`` by five minutes), exactly one
    ``/enroll`` POST, permanent 502.

    Idempotence also keeps ``cert_reload_required`` quiet: a connect that re-enrolls the same
    certificate must not touch the file, or every reconnect asks the operator to restart.
    """

    NAME = "bg9zzz.mrrc.vlsc.net"

    def _paths(self, cert_dir: Path) -> tuple[Path, Path]:
        return cert_dir / "fullchain.pem", cert_dir / "bg9zzz.key"

    def _mint(self, cert_path: Path, key_path: Path, name: str, *,
              before_days: int = -1, after_days: int = 3650):
        """Write a pair of our own, so each check can be aimed at one property at a time."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + datetime.timedelta(days=before_days))
            .not_valid_after(now + datetime.timedelta(days=after_days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        return cert

    @staticmethod
    def _public_key_der(pair) -> bytes:
        from cryptography.hazmat.primitives import serialization
        from cryptography import x509

        der = serialization.Encoding.DER
        form = serialization.PublicFormat.SubjectPublicKeyInfo
        cert = x509.load_pem_x509_certificate(Path(pair[0]).read_bytes())
        key = serialization.load_pem_private_key(Path(pair[1]).read_bytes(), password=None)
        return (cert.public_key().public_bytes(der, form),
                key.public_key().public_bytes(der, form))

    def test_the_same_name_reuses_the_pair_it_already_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            first = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            assert first is not None
            before = first[0].read_bytes(), first[1].read_bytes()
            stamp = first[0].stat().st_mtime_ns

            again = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            self.assertEqual(again, first)
            self.assertEqual((again[0].read_bytes(), again[1].read_bytes()), before)
            self.assertEqual(again[0].stat().st_mtime_ns, stamp,
                             "the certificate was rewritten — a fresh mtime is what asks for a "
                             "restart on every reconnect")

    def test_a_pair_that_already_serves_another_name_is_replaced(self):
        """The hub verifies the upstream *by name*, so reusing the wrong one is a silent 502.

        The collision is real, not theoretical: the bootstrap path signs a ``localhost`` pair into
        the same ``fullchain.pem`` whenever the enrolled certificate is missing, and the instance's
        ``<label>.key`` stays next to it — so a bare existence check reuses a certificate that no
        hub will accept, and the enroll that follows is refused for the wrong name.
        """
        from cryptography import x509

        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            cert_path, key_path = self._paths(cert_dir)
            self._mint(cert_path, key_path, "localhost")     # matching key, foreign name

            second = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            assert second is not None
            cert = x509.load_pem_x509_certificate(second[0].read_bytes())
            cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
            self.assertEqual(cn, self.NAME)
            cert_der, key_der = self._public_key_der(second)
            self.assertEqual(cert_der, key_der)

    def test_a_key_that_does_not_match_the_certificate_is_replaced(self):
        """A lost private half is a pair TLS cannot complete — re-signed, never served."""
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            first = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            assert first is not None
            first_cert = first[0].read_bytes()
            other_dir = cert_dir / "other"
            other = ssl_bootstrap.sign_for(self.NAME, other_dir)
            assert other is not None
            first[1].write_bytes(other[1].read_bytes())          # certificate, foreign key

            again = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            assert again is not None
            self.assertNotEqual(again[0].read_bytes(), first_cert)
            cert_der, key_der = self._public_key_der(again)
            self.assertEqual(cert_der, key_der, "the returned pair does not fit together")

    def test_an_expired_certificate_is_replaced(self):
        from cryptography import x509

        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            cert_path, key_path = self._paths(cert_dir)
            self._mint(cert_path, key_path, self.NAME, before_days=-30, after_days=-1)

            again = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            assert again is not None
            cert = x509.load_pem_x509_certificate(again[0].read_bytes())
            self.assertGreater(cert.not_valid_after_utc, datetime.datetime.now(datetime.timezone.utc))

    def test_a_file_that_is_not_a_certificate_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            cert_path, key_path = self._paths(cert_dir)
            cert_path.parent.mkdir(parents=True, exist_ok=True)
            cert_path.write_bytes(b"not a certificate at all\n")
            key_path.write_bytes(b"nor a key\n")

            again = ssl_bootstrap.sign_for(self.NAME, cert_dir)
            assert again is not None
            self.assertEqual(again[0].read_bytes()[:27], b"-----BEGIN CERTIFICATE-----")
            self.assertIn(b"PRIVATE KEY-----", again[1].read_bytes())


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


@unittest.skipUnless(HAS_CRYPTO, "cryptography not installed")
class ServerSslBootstrapTests(unittest.TestCase):
    """server.main() must sign its own certificate when none is on disk.

    Regression (v1.24.5): server.py called ``ssl_bootstrap.sign_for`` without ever importing the
    module, so the NameError was swallowed by the broad ``except Exception`` around it and the
    server served plain HTTP while the launcher opened https:// — precisely the "installed, then
    a black screen" report this release set out to fix. The suite was green (1460 tests) and the
    frozen build still failed; only running the packaged Server exe against a clean per-user
    state showed it. The launcher has its own working copy of this logic, which is why an
    installed app can look healthy while the server-side path is dead.
    """

    class _Args:
        def __init__(self, cert, key, host="0.0.0.0", no_ssl=False):
            self.ssl_cert = cert
            self.ssl_key = key
            self.host = host
            self.no_ssl = no_ssl

    def test_a_missing_certificate_is_signed_not_degraded_to_plain_http(self):
        import server

        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp) / "certs"
            args = self._Args(str(cert_dir / "fullchain.pem"), str(cert_dir / "localhost.key"))
            kwargs = server._resolve_ssl_kwargs(args)
            # An empty dict is the bug: it means uvicorn serves HTTP and the launcher's https://
            # URL becomes a browser protocol error.
            self.assertTrue(kwargs.get("ssl_certfile"), "fell back to plain HTTP")
            self.assertTrue(kwargs.get("ssl_keyfile"), "fell back to plain HTTP")
            self.assertTrue(Path(kwargs["ssl_certfile"]).exists())
            self.assertTrue(Path(kwargs["ssl_keyfile"]).exists())

    def test_a_wildcard_bind_address_signs_for_localhost(self):
        """A certificate for 0.0.0.0 would not match the https://127.0.0.1 the launcher opens."""
        import server
        from cryptography import x509  # local import: bound in this scope, unlike the module-level try

        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp) / "certs"
            args = self._Args(str(cert_dir / "fullchain.pem"), str(cert_dir / "localhost.key"),
                              host="0.0.0.0")
            kwargs = server._resolve_ssl_kwargs(args)
            cert = x509.load_pem_x509_certificate(Path(kwargs["ssl_certfile"]).read_bytes())
            names = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            self.assertIn("localhost", names)

    def test_an_existing_pair_is_used_untouched(self):
        """A certificate the operator installed (or the hub enrolled) must not be replaced."""
        import server

        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp) / "certs"
            made = ssl_bootstrap.ensure_self_signed(cert_dir)
            assert made is not None, "the suite is skipped without cryptography"
            cert_path, key_path = made
            before = cert_path.read_bytes()
            args = self._Args(str(cert_path), str(key_path))
            kwargs = server._resolve_ssl_kwargs(args)
            self.assertEqual(kwargs["ssl_certfile"], str(cert_path))
            self.assertEqual(kwargs["ssl_keyfile"], str(key_path))
            self.assertEqual(cert_path.read_bytes(), before)

    def test_no_ssl_is_the_only_way_to_plain_http(self):
        import server

        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp) / "certs"
            args = self._Args(str(cert_dir / "fullchain.pem"), str(cert_dir / "localhost.key"),
                              no_ssl=True)
            self.assertEqual(server._resolve_ssl_kwargs(args), {})


if __name__ == "__main__":
    unittest.main()
