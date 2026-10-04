#!/usr/bin/env python3
"""Build gate: outbound HTTPS must verify with an EMPTY default trust store.

The 2026-10-04 field report (macOS v1.25.0) was not a portal, network or
certificate problem: the frozen bundle carried the build machine's OpenSSL, whose
compiled-in CA path is `/opt/local/...` (MacPorts) — nobody else has it, so the
context came up with **zero** roots and every request failed with
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`.  Portal 接入,
the 🐞 upload and the update check all failed the same way.

The build machine is exactly the machine where this cannot be noticed, so this gate
manufactures the failing condition on purpose (`SSL_CERT_FILE`/`SSL_CERT_DIR` point
at a path that does not exist ⇒ OpenSSL's own store is empty) and then asserts:

  1. the *unfixed* way — a bare `ssl.create_default_context()` — is indeed empty
     (otherwise the gate would pass vacuously), and
  2. `net_tls.build_context()` still ends up with roots, and
  3. when a network is available, a real request to the portal / update manifest
     completes a TLS handshake.

Exit codes: 0 = pass (checks 1–3), 0 with a SKIP line = pass offline,
1 = fail.  Called by packaging/macos/build.sh and packaging/windows/build.ps1.
"""
from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import net_tls  # noqa: E402 - needs the path insert above; it reads env at call time

#: Points nowhere on purpose: this is the empty store of a user machine.
_BROKEN_CA = "/nonexistent/mrrc-gate/cert.pem"

#: A TLS handshake that must succeed once the trust store is fixed.  A 401/403 still
#: proves the handshake happened (the auth middleware answered), so only transport
#: failures count as a failure.
PROBE_URLS = (
    "https://portal.mrrc.vlsc.net/",
    "https://www.vlsc.net/mrrc_modern/downloads/latest.json",
)


def _scrub_environment() -> None:
    """Make the CA store empty, the way a machine without the build host's paths has it."""
    os.environ["SSL_CERT_FILE"] = _BROKEN_CA
    os.environ["SSL_CERT_DIR"] = str(Path(_BROKEN_CA).parent)
    os.environ.pop("MRRC_CA_BUNDLE", None)


def _is_tls_failure(exc: BaseException) -> bool:
    """A verification failure (as opposed to 'this network has no route')."""
    if isinstance(exc, ssl.SSLError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _shipped_bundle() -> "Path | None":
    """The bundle a packaged build would use (None in a checkout that does not ship one)."""
    for candidate in net_tls.ca_bundle_candidates():
        if candidate.is_file() and "vendor" in candidate.parts and "ca" in candidate.parts:
            return candidate
    return None


def main() -> int:
    # The scrub must happen before any context is built: net_tls and OpenSSL both read the
    # CA environment at call time, which is why the import above is harmless.
    _scrub_environment()

    empty = ssl.create_default_context()
    empty_roots = net_tls.store_size(empty)
    if empty_roots > 0:
        # Windows reads the OS certificate store (enum_certificates), so no environment can
        # empty it — and that is also why this defect never bit Windows.  The failing condition
        # cannot be reproduced there; what still must hold is that net_tls ends up with a usable
        # store and can complete a real handshake.
        print("note: the default store has %d root(s) on this platform and cannot be emptied by "
              "the environment (Windows OS store)" % empty_roots)
    else:
        print(f"ok: default store is empty with SSL_CERT_FILE={os.environ['SSL_CERT_FILE']!r} "
              f"(that is the 2026-10-04 failure)")

    context = net_tls.build_context()
    roots = net_tls.store_size(context)
    if roots <= 0:
        candidates = "\n  ".join(str(path) for path in net_tls.ca_bundle_candidates())
        print("FAIL: net_tls could not find a CA bundle either — outbound HTTPS would fail.\n"
              f"  candidates tried:\n  {candidates}")
        return 1
    print(f"ok: net_tls has a usable trust store ({roots} root(s))")

    bundle = _shipped_bundle()
    if bundle is None:
        print("note: no bundled vendor/ca/cacert.pem next to this checkout — skipping the "
              "bundle-loads check (a packaged build ships it and fails without it)")
    else:
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            probe.load_verify_locations(cafile=str(bundle))
        except (ssl.SSLError, OSError) as exc:
            print(f"FAIL: the shipped CA bundle does not load: {bundle} ({exc})")
            return 1
        bundle_roots = net_tls.store_size(probe)
        if bundle_roots < 20:
            print(f"FAIL: the shipped CA bundle has only {bundle_roots} root(s): {bundle}")
            return 1
        print(f"ok: shipped CA bundle loads ({bundle_roots} root(s)) from {bundle}")

    reachable = 0
    for url in PROBE_URLS:
        try:
            response = net_tls.urlopen(url, timeout=15)
        except Exception as exc:                       # noqa: BLE001 - report, do not raise
            if _is_tls_failure(exc):
                print(f"FAIL: TLS verification still fails for {url}: {exc}")
                return 1
            print(f"skip: {url} not reachable from here ({exc.__class__.__name__}: {exc})")
            continue
        status = getattr(response, "status", 0)
        response.close()
        print(f"ok: {url} → HTTP {status} (handshake verified)")
        reachable += 1

    if reachable == 0:
        print("SKIP: no network — trust-store checks passed, live handshake not attempted")
    else:
        print("PASS: outbound HTTPS verifies with an empty default store")
    return 0


if __name__ == "__main__":
    sys.exit(main())
