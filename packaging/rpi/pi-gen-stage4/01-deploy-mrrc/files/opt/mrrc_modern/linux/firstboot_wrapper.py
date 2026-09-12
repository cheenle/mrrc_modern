#!/usr/bin/env python3
"""mrrc-firstboot.service entry point (root): preseed → auto-config → cert.

Runs once on the first boot of a freshly flashed MRRC Modern Raspberry Pi
image. Order of operations:

1. Adopt a pre-seeded ``/boot/firmware/mrrc.env`` if the operator placed one
   on the SD card (copied into place, original renamed ``mrrc.env.applied``).
2. Otherwise run ``linux/first_run.py`` to generate a web password and probe
   for the radio (banner goes to the HDMI console).
3. Generate the self-signed HTTPS certificate (ssl_bootstrap).
4. Append headless defaults (dual-stack bind, port, cert paths) to the env
   file, hand it to the ``mrrc`` user, stamp ``firstboot-done`` and disable
   this unit — subsequent boots are no-ops (guard condition).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# linux/first_run.py is this script's sibling inside the image; it owns the
# tolerant env reader (BOM/UTF-8/cp936/latin-1) that keeps a preseed saved by
# an ANSI editor from killing the very first boot.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from first_run import read_env_text  # noqa: E402  # type: ignore[import-not-found]

ENV_DIR = Path("/opt/mrrc_modern/env")
ENV_FILE = ENV_DIR / "mrrc.env"
PRESEED = Path("/boot/firmware/mrrc.env")
DONE = Path("/var/lib/mrrc/firstboot-done")
CERT_DIR = Path("/var/lib/mrrc/certs")


def adopt_preseed() -> bool:
    if not PRESEED.is_file():
        return False
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    # The preseed is written on the operator's PC and travels on the SD card,
    # so it may be GBK/UTF-16 rather than UTF-8 (field class 2026-09-12).
    # read_env_text never raises; the copy is normalised to UTF-8 here.
    text, encoding = read_env_text(PRESEED)
    if encoding != "utf-8":
        print(f"[mrrc-firstboot] preseed is not valid UTF-8; read as {encoding}")
    ENV_FILE.write_text(text, encoding="utf-8")
    shutil.copy2(PRESEED, PRESEED.with_suffix(".env.applied"))
    PRESEED.unlink()
    print("[mrrc-firstboot] preseed mrrc.env adopted from boot partition")
    return True


def ensure_cert() -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    venv_py = "/opt/mrrc_modern/venv/bin/python"
    code = (
        "from pathlib import Path; import ssl_bootstrap; "
        f"ssl_bootstrap.ensure_self_signed(Path({str(CERT_DIR)!r}))"
    )
    subprocess.run([venv_py, "-c", code], check=True, cwd="/opt/mrrc_modern")


def main() -> int:
    if DONE.exists():
        return 0
    if not ENV_FILE.exists():
        adopt_preseed()
    if not ENV_FILE.exists():
        # no preseed → auto-generate (console banner printed by first_run.main)
        subprocess.run(
            ["/opt/mrrc_modern/venv/bin/python", "/opt/mrrc_modern/linux/first_run.py"],
            check=True, cwd="/opt/mrrc_modern",
        )
    else:
        subprocess.run(
            ["/opt/mrrc_modern/venv/bin/python", "/opt/mrrc_modern/linux/first_run.py"],
            env={**os.environ, "MRRC_PRESEED": "1"}, check=True, cwd="/opt/mrrc_modern",
        )
    ensure_cert()
    # defaults for a headless box: dual-stack bind, port, HTTPS cert paths
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    keys = {l.split("=", 1)[0] for l in lines
            if l.strip() and not l.strip().startswith("#") and "=" in l}
    for k, v in (("MRRC_WEB_HOST", "::"), ("MRRC_WEB_PORT", "8888"),
                 ("MRRC_SSL_CERT", str(CERT_DIR / "server.crt")),
                 ("MRRC_SSL_KEY", str(CERT_DIR / "server.key"))):
        if k not in keys:
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(["chown", "-R", "mrrc:mrrc", str(ENV_DIR)], check=True)
    subprocess.run(["chmod", "640", str(ENV_FILE)], check=True)
    DONE.parent.mkdir(parents=True, exist_ok=True)
    DONE.write_text("ok\n", encoding="utf-8")
    subprocess.run(["systemctl", "disable", "mrrc-firstboot.service"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
