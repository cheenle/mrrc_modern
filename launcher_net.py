"""Ask the running server which URL the browser should get.

The launcher picks ``https://`` when *it* can produce a certificate, and the server picks
TLS from the files *it* could load when it starts. Two decisions, made separately, about
one socket — and when they disagree the browser is sent to a scheme nothing is listening
for. Chrome answers that with an error page on a blank tab, which is how a working install
gets reported as "black screen after installing" (field log 2026-10-02: the server logged
``falling back to plain HTTP`` while the launcher opened ``https://127.0.0.1:8888``).

So instead of guessing, probe. Shared by ``windows/launcher.py`` and ``macos/launcher.py``,
which had the same blind ``webbrowser.open(url)``.

The other half of that job is not being fatal: a probe that raises is worse than a probe
that answers "no" (field log 2026-10-10 — see ``answers()``).
"""
from __future__ import annotations

import time
import urllib.request
from urllib.error import HTTPError, URLError

HEALTH_PATH = "/api/health"

# A "is anything there?" check should be quick but not trigger-happy: a server that is
# still importing PyAudio answers a fraction of a second later.
_POLL_S = 0.3


def tls_context():
    """Context that accepts the self-signed bootstrap certificate.

    Verification is off on purpose: this probe answers "is somebody listening for TLS on
    this port", not "is that certificate trusted", and the bootstrap certificate is trusted
    by nobody until the user installs it.
    """
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


def answers(url: str, proc=None, timeout_s: float = 2.0,
            secure: bool | None = None) -> bool:
    """True as soon as ``url`` returns any HTTP response.

    Any status counts, including the 401 from the auth middleware: a refused login page
    still proves the server is listening on that scheme. Returns False on timeout, or the
    moment ``proc`` (the server we spawned) exits — waiting on a dead child only burns the
    whole timeout.

    ``secure`` overrides the TLS decision; by default it is read off the URL, so callers
    cannot disagree with the string they passed.
    """
    if secure is None:
        secure = url.startswith("https://")
    try:
        ctx = tls_context() if secure else None
    except Exception:  # noqa: BLE001 — a probe answers a question, it never ends the program
        # Field log 2026-10-10 (Windows frozen bundle): the import inside `tls_context()`
        # failed with `ImportError: DLL load failed while importing _ssl` because the
        # previous instance still held the file. It escaped this probe and the launcher
        # quit with “启动失败” — while the server it was about to spawn would have been
        # fine. Treat it as "this scheme did not answer", which is what sends the caller
        # on to the other scheme instead of aborting.
        return False
    probe = url.rstrip("/") + HEALTH_PATH
    deadline = time.monotonic() + timeout_s
    while True:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(probe, timeout=2, context=ctx):
                return True
        except HTTPError:
            return True
        except (URLError, OSError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)


def other_scheme(url: str) -> str:
    """The same host and port under the other scheme.

    The launcher builds its URL from the config, so the only thing that can disagree with
    the server is ``http`` vs ``https`` — this is the whole probe in one string, and it
    needs no config to be re-read.
    """
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def first_answering(urls, proc=None, timeout_s: float = 2.0) -> str | None:
    """The first URL in ``urls`` that answers, or None if none of them do.

    Order is the caller's policy: pass the preferred scheme first when waiting for a
    server that should already be up, and the *other* scheme first when the preferred one
    has just failed.
    """
    for url in urls:
        if answers(url, proc=proc, timeout_s=timeout_s):
            return url
    return None


def served_url(preferred: str, alternate: str, proc=None,
               timeout_s: float = 3.0) -> str:
    """The URL to open: ``preferred`` if it answers, else ``alternate`` if it does.

    ``preferred`` is asked first even when the caller has already waited on it, so the
    result never depends on a promise about what the caller tested. Neither answering
    returns ``preferred`` unchanged: there is nothing better to open, and the caller's own
    error path (server exited, start-up timeout) explains that far better than this probe
    can.
    """
    if alternate == preferred:
        return preferred
    if answers(preferred, proc=proc, timeout_s=timeout_s):
        return preferred
    if answers(alternate, proc=proc, timeout_s=timeout_s):
        return alternate
    return preferred
