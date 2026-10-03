"""Outbound TLS trust for the packaged app.

Why this module exists (2026-10-04 field report, macOS v1.25.0)
--------------------------------------------------------------
A frozen macOS bundle carries the OpenSSL the *build* machine used, and a freshly
built ``SSLContext`` with no explicit CA file takes its trust store from that
library's compiled-in path — on the build machine (MacPorts) that is
``/opt/local/libexec/openssl3/etc/openssl/cert.pem``.  Users do not have
``/opt/local``, so up there the store came up **empty** and every outbound HTTPS
request failed with ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate``:

* the Cloud Hub portal (呼号接入) — the report that surfaced it,
* the 🐞 diagnostics upload — which is why that report had to be handed over by
  path instead of uploading itself,
* the update check (``upgrade_core``).

Nothing about the radio, the portal or the network was wrong; the app had no roots
to verify with.  So this module builds the context explicitly rather than trusting
whatever the frozen OpenSSL was compiled with: when the process's default store is
empty it loads our own bundle (``vendor/ca/cacert.pem``, shipped) or the platform's
system bundle.  Every outbound HTTPS request in the shipped code goes through
:func:`urlopen`; ``tests/test_tls_trust_store.py`` keeps it that way.

Stdlib only, no application imports beyond the env helper — it is imported by the
server, the launcher and the support/upgrade paths alike.
"""
from __future__ import annotations

import logging
import ssl
import sys
import urllib.request
from pathlib import Path

from config import _env

logger = logging.getLogger("mrrc.tls")

#: Explicit override: a PEM bundle chosen by whoever deployed this instance.
CA_BUNDLE_ENV = "MRRC_CA_BUNDLE"

#: Default timeout for :func:`urlopen` when the caller does not pick one.
DEFAULT_TIMEOUT = 30

#: Our own bundle, relative to whichever resource root holds it (see _resource_roots).
BUNDLED_CA_PARTS = ("vendor", "ca", "cacert.pem")

#: Where the platform keeps its roots.  Tried after the bundled bundle, so a system
#: with no bundle at all still verifies (macOS always has the first one).
SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",                    # macOS, *BSD
    "/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL, Fedora
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)


def _resource_roots() -> list[Path]:
    """Directories that may hold packaged resources (same order as scope_libraries).

    ``_MEIPASS`` first: a PyInstaller bundle resolves its data tree there (on macOS
    that is ``Contents/Resources`` through the ``Contents/MacOS/_internal`` symlink),
    then next to the executable (Windows onedir), then the source checkout.
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)

    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def ca_bundle_candidates() -> list[Path]:
    """CA bundles to try, in order, when the default store is empty.

    Our bundle comes before the system files on purpose: the same roots on every
    machine make the trust behaviour reproducible, and a release refreshes it.
    """
    candidates: list[Path] = []
    override = (_env(CA_BUNDLE_ENV) or "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    for root in _resource_roots():
        candidates.append(root.joinpath(*BUNDLED_CA_PARTS))
    candidates.extend(Path(path) for path in SYSTEM_CA_FILES)

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def store_size(context: ssl.SSLContext) -> int:
    """How many roots a context trusts; 0 means it cannot verify anything."""
    try:
        return int(context.cert_store_stats().get("x509", 0))
    except Exception:                      # pragma: no cover - no such failure seen
        return -1                          # unknown: let the caller keep going


def build_context(*, base: ssl.SSLContext | None = None,
                  candidates: list[Path] | None = None) -> ssl.SSLContext:
    """A verifying client context whose trust store is actually populated.

    ``base``/``candidates`` exist for tests: the shipped path is the no-argument call.
    """
    context = base if base is not None else ssl.create_default_context()
    if store_size(context) > 0:
        return context                     # the system store works (Windows store, normal hosts)

    tried = ca_bundle_candidates() if candidates is None else candidates
    for path in tried:
        if not path.is_file():
            continue
        try:
            context.load_verify_locations(cafile=str(path))
        except (ssl.SSLError, OSError) as exc:
            logger.debug("TLS: ignoring unusable CA bundle %s (%s)", path, exc)
            continue
        loaded = store_size(context)
        if loaded > 0:
            logger.info("TLS: default trust store was empty — loaded %d root(s) from %s",
                        loaded, path)
            return context

    logger.warning("TLS: no usable CA bundle (default store empty, %d candidate(s) unusable) — "
                   "outbound HTTPS will fail with CERTIFICATE_VERIFY_FAILED; "
                   "set %s to a PEM bundle", len(tried), CA_BUNDLE_ENV)
    return context


_context: ssl.SSLContext | None = None


def default_context() -> ssl.SSLContext:
    """The process-wide verifying context, built once."""
    global _context
    if _context is None:
        _context = build_context()
    return _context


def urlopen(url, data=None, timeout: int = DEFAULT_TIMEOUT, *, context=None):
    """``urllib.request.urlopen`` with this module's trust store.

    Drop-in for the stdlib call — same arguments, same exceptions — so a trust-store
    problem is fixed in one place instead of once per call site.
    """
    if context is None:
        context = default_context()
    return urllib.request.urlopen(url, data=data, timeout=timeout, context=context)
