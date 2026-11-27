"""Flashing an MRRC-Passport (ESP32-C3) from the box, with its Wi-Fi pre-set.

Why this module exists: a new Passport out of the bag has no idea which network
or which server it belongs to, and typing an SSID, a WPA passphrase and a box
password on a four-position knob is not provisioning — it is a support call.  The
box already knows all three, and the board's USB is a plain CDC-ACM port, so the
box can write the firmware *and* a pre-filled `cfg` NVS partition and hand back a
device that boots already connected (design
`docs/superpowers/specs/2026-11-25-passport-flash-design.md`).

Three facts this module is built on, all measured off the device's own build:

* the partition table puts **nvs at 0x9000, size 0x6000** and the app at 0x10000
  (`~/HAM/mrrc-passport/partitions.csv`), so a full image can be written at 0 and
  the NVS partition can be replaced separately;
* the firmware reads its config from NVS namespace **`cfg`**, keys `ssid`,
  `pass`, `host`, `srv_pass` (`main/mrrc_cfg.c`) — the same four fields its own
  provisioning page writes;
* the board exposes **ESP32-C3 native USB Serial/JTAG**, which Linux enumerates as
  `/dev/ttyACM*` (`docs/hardware-design/AI_HARDWARE_DEVELOPMENT_GUIDE.md`).

Everything here is pure or takes an injectable runner: the NVS encoder is
byte-checked against ESP-IDF's own generator, the port filtering is a function of
the port list, and the esptool invocations are data until a runner executes them.
No test in this repository touches a serial port.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

# ── NVS on-flash format (ESP-IDF v2) ───────────────────────────────────────
# Layout of one 4 KiB page: 32 B header, 32 B entry-state bitmap, then 126 × 32 B
# entries.  Everything not written stays 0xFF (erased).
NVS_PAGE_SIZE = 4096
NVS_ENTRY_SIZE = 32
NVS_PAGE_HEADER = 32
NVS_BITMAP = 32
NVS_FIRST_ENTRY = NVS_PAGE_HEADER + NVS_BITMAP          # 64
NVS_MAX_ENTRIES = (NVS_PAGE_SIZE - NVS_FIRST_ENTRY) // NVS_ENTRY_SIZE   # 126

PAGE_STATE_ACTIVE = 0xFFFFFFFE
NVS_VERSION2 = 0xFE
CHUNK_ANY = 0xFF
TYPE_U8 = 0x01
TYPE_STR = 0x21


class NvsError(ValueError):
    """An NVS value the firmware could never read back (too long, wrong type)."""


def _crc(data: bytes) -> int:
    """The CRC ESP-IDF uses: raw zlib CRC-32, initial value all ones, no final xor."""
    return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


def _entry_crc(entry: "bytes | bytearray") -> int:
    """CRC over the first four bytes plus key+value (the crc field itself excluded)."""
    return _crc(bytes(entry[0:4]) + bytes(entry[8:32]))


def _string_entry(ns_index: int, key: str, value: str) -> bytes:
    """One variable-length string entry, exactly as `nvs_partition_gen` writes it.

    V2 keeps the length in the value area (offset 24) and the bytes themselves in
    the *following* entries; `span` counts the header plus those entries.  The
    string is not NUL-terminated — a terminator would be one more byte than the
    firmware's `nvs_get_str` needs, and the CSV generator omits it too.
    """
    raw = value.encode("utf-8") + b"\x00"            # the terminator IS stored
    if len(raw) > 4000:
        raise NvsError(f"NVS string value for {key!r} is {len(raw)} bytes (limit 4000)")
    data_entries = ((len(raw) + 31) & ~31) // 32
    entry = bytearray(b"\xff") * NVS_ENTRY_SIZE
    entry[0] = ns_index & 0xFF
    entry[1] = TYPE_STR
    entry[2] = data_entries + 1                      # span, header included
    entry[3] = CHUNK_ANY
    struct.pack_into("<H", entry, 24, len(raw))      # length field (NUL included)
    struct.pack_into("<I", entry, 28, _crc(raw))
    entry[8:24] = b"\x00" * 16
    key_bytes = key.encode("utf-8")
    if len(key_bytes) > 15:
        raise NvsError(f"NVS key {key!r} is longer than 15 bytes")
    entry[8:8 + len(key_bytes)] = key_bytes
    struct.pack_into("<I", entry, 4, _entry_crc(entry))
    body = raw + b"\xff" * (data_entries * NVS_ENTRY_SIZE - len(raw))
    return bytes(entry) + body


def _namespace_entry(ns_index: int, name: str) -> bytes:
    """A namespace entry is a U8 held at index 0 whose value is the namespace id."""
    entry = bytearray(b"\xff") * NVS_ENTRY_SIZE
    entry[0] = 0
    entry[1] = TYPE_U8
    entry[2] = 1
    entry[3] = CHUNK_ANY
    entry[24] = ns_index & 0xFF
    key = name.encode("utf-8")
    if len(key) > 15:
        raise NvsError(f"NVS namespace/key {name!r} is longer than 15 bytes")
    entry[8:24] = b"\x00" * 16                        # keys are NUL-padded
    entry[8:8 + len(key)] = key
    struct.pack_into("<I", entry, 4, _entry_crc(entry))
    return bytes(entry)


def _page(entries: list[bytes], seq: int) -> bytes:
    """One active page carrying `entries`, with the state bitmap ESP-IDF expects."""
    page = bytearray(b"\xff") * NVS_PAGE_SIZE
    header = bytearray(b"\xff") * NVS_PAGE_HEADER
    struct.pack_into("<I", header, 0, PAGE_STATE_ACTIVE)
    struct.pack_into("<I", header, 4, seq)
    header[8] = NVS_VERSION2
    struct.pack_into("<I", header, 28, _crc(bytes(header[4:28])))
    page[0:NVS_PAGE_HEADER] = header

    bitmap = bytearray(b"\xff") * NVS_BITMAP
    offset = NVS_FIRST_ENTRY
    used = 0
    for entry in entries:
        used += len(entry) // NVS_ENTRY_SIZE
    if used > NVS_MAX_ENTRIES:
        raise NvsError(f"{used} entries do not fit in one NVS page")
    for i in range(used):
        bitmap[(i * 2) // 8] &= ~(1 << ((i * 2) % 8)) & 0xFF
    page[NVS_PAGE_HEADER:NVS_FIRST_ENTRY] = bitmap
    for entry in entries:
        page[offset:offset + len(entry)] = entry
        offset += len(entry)
    return bytes(page)


#: The four keys the Passport reads (`main/mrrc_cfg.c`), in the order its
#: provisioning page writes them.  `token` is deliberately absent: it is a
#: credential the device earns on first login, and baking one would be a
#: 30-day key handed out with the hardware.
CFG_NAMESPACE = "cfg"
CFG_KEYS = ("ssid", "pass", "host", "srv_pass")
#: Partition table: `nvs, data, nvs, 0x9000, 0x6000`.
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000


def build_nvs_image(values: dict, namespace: str = CFG_NAMESPACE,
                    size: int = NVS_SIZE) -> bytes:
    """A complete NVS partition image carrying exactly `values` in `namespace`.

    Only the first page is written; the rest of the partition stays erased, which
    is what a freshly formatted NVS looks like — `nvs_flash_init()` activates the
    one page that has data instead of asking for a format.
    """
    unknown = sorted(set(values) - set(CFG_KEYS))
    if unknown:
        raise NvsError(f"unknown config key(s): {', '.join(unknown)}")
    missing = [k for k in CFG_KEYS if k not in values]
    if missing:
        raise NvsError(f"missing config key(s): {', '.join(missing)}")
    for key in CFG_KEYS:
        if not isinstance(values[key], str):
            raise NvsError(f"{key} must be a string")
    entries = [_namespace_entry(1, namespace)]
    entries += [_string_entry(1, key, values[key]) for key in CFG_KEYS]
    image = _page(entries, seq=0) + b"\xff" * (size - NVS_PAGE_SIZE)
    if len(image) != size:
        raise NvsError(f"NVS image is {len(image)} bytes, expected {size}")
    return image


def parse_nvs_image(image: bytes) -> dict:
    """Read back what we just wrote — the on-box proof the flash landed.

    Deliberately minimal (namespace → {key: string value}) because that is all the
    verification needs: if the four values come back, the device will find them
    too.  Anything unparseable raises instead of returning a partial dict, so a
    half-written partition cannot be reported as success.
    """
    if len(image) < NVS_PAGE_SIZE or len(image) % NVS_PAGE_SIZE:
        raise NvsError(f"NVS image size {len(image)} is not a whole number of pages")
    out: dict = {}
    for page_no in range(0, len(image), NVS_PAGE_SIZE):
        page = image[page_no:page_no + NVS_PAGE_SIZE]
        state, = struct.unpack_from("<I", page, 0)
        if state != PAGE_STATE_ACTIVE:
            continue
        namespaces = {}
        offset = NVS_FIRST_ENTRY
        while offset + NVS_ENTRY_SIZE <= NVS_PAGE_SIZE:
            entry = page[offset:offset + NVS_ENTRY_SIZE]
            if entry == b"\xff" * NVS_ENTRY_SIZE:
                break
            span = entry[2]
            if span < 1:
                raise NvsError(f"corrupt entry at 0x{offset:x} (span 0)")
            if struct.unpack_from("<I", entry, 4)[0] != _entry_crc(entry):
                raise NvsError(f"entry CRC mismatch at 0x{offset:x}")
            key = entry[8:24].split(b"\x00", 1)[0].decode("utf-8", "replace")
            if entry[0] == 0 and entry[1] == TYPE_U8:
                namespaces[key] = entry[24]
            elif entry[1] == TYPE_STR:
                n = struct.unpack_from("<H", entry, 24)[0]
                # The bytes live in the entries *after* this header, not in the
                # header's own value area (that holds the length and the CRC).
                start = offset + NVS_ENTRY_SIZE
                raw = page[start:start + n]
                if raw.endswith(b"\x00"):
                    raw = raw[:-1]
                out.setdefault(namespace_of(namespaces, entry[0]), {})[key] = \
                    raw.decode("utf-8", "surrogateescape")
            offset += span * NVS_ENTRY_SIZE
    return out


def namespace_of(namespaces: dict, index: int) -> str:
    """Reverse map an NVS namespace index back to its name."""
    for name, idx in namespaces.items():
        if idx == index:
            return name
    return f"#{index}"


# ── firmware catalog (the site publishes it; the box never builds one) ─────
class CatalogError(ValueError):
    """The site's firmware catalog is unusable — never guess a download."""


@dataclass(frozen=True)
class Build:
    """One flashable Passport build as the site describes it."""

    version: str
    url: str
    sha256: str
    size: int
    chip: str = "esp32c3"
    flash_mode: str = "dio"
    flash_freq: str = "80m"
    # `detect`, not a literal: esptool patches the bootloader's flash-size field as
    # it writes, and a number here *overrides* what the image was built for. The
    # catalogue does not carry a flashSize, so the old "4MB" default rewrote an
    # 8 MB board's bootloader to 4 MB on every flash — and that board's partition
    # table reserves 0x7f0000 for the app, so its bootloader then refused every
    # partition table and the terminal reboot-looped before `app_main`:
    #   E flash_parts: partition 2 invalid - offset 0x10000 size 0x7f0000
    #                  exceeds flash chip size 0x400000
    #   E boot: Failed to verify partition table
    # `detect` asks the chip (8 MB XMC on the 2026-11-27 unit) and leaves the
    # header alone when the sizes agree, which is right for both variants.
    flash_size: str = "detect"
    notes: str = ""

    @property
    def nvs_offset(self) -> int:
        return NVS_OFFSET


def parse_catalog(data) -> list[Build]:
    """Validate a catalog document; newest first, nothing inferred.

    A catalog entry that cannot be flashed must never reach the page as a button:
    a wrong offset or a missing SHA is a bricked device, not a retry.
    """
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8", "replace")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as e:
            raise CatalogError(f"catalog is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise CatalogError("catalog is not an object")
    rows = data.get("builds")
    if not isinstance(rows, list) or not rows:
        raise CatalogError("catalog has no builds")
    out: list[Build] = []
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogError("catalog build is not an object")
        for key in ("version", "url", "sha256", "size"):
            if key not in row:
                raise CatalogError(f"catalog build is missing {key}")
        if not isinstance(row["version"], str) or not row["version"].strip():
            raise CatalogError(f"catalog build has no version: {row['version']!r}")
        if not isinstance(row["url"], str):
            raise CatalogError(f"catalog url is not a string: {row['url']!r}")
        sha = str(row["sha256"]).lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise CatalogError(f"bad sha256 for {row.get('version')!r}")
        url = str(row["url"])
        if not url.startswith("https://"):
            raise CatalogError(f"catalog url is not https: {url!r}")
        try:
            size = int(row["size"])
        except (TypeError, ValueError) as e:
            raise CatalogError(f"bad size for {row.get('version')!r}") from e
        if size <= 0:
            raise CatalogError(f"bad size for {row.get('version')!r}")
        out.append(Build(
            version=str(row["version"]), url=url, sha256=sha, size=size,
            chip=str(row.get("chip") or "esp32c3"),
            flash_mode=str(row.get("flashMode") or "dio"),
            flash_freq=str(row.get("flashFreq") or "80m"),
            flash_size=str(row.get("flashSize") or "detect"),
            notes=str(row.get("notes") or ""),
        ))
    return sorted(out, key=lambda b: _version_key(b.version), reverse=True)


def _version_key(text: str) -> tuple:
    parts = []
    for piece in str(text).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


# ── serial ports ───────────────────────────────────────────────────────────
#: Where an ESP32-C3's native USB Serial/JTAG shows up on Linux, plus the USB-UART
#: bridges used by the older Passport revisions.
PORT_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")

#: Where a box carries its own copy of the flasher.  A hot-fix patch moves
#: files and cannot install a Python package, so a box that reached this
#: version that way has the route and this module but no `esptool` — and
#: `/passport` answers 501 on exactly the boxes the feature was built for.
#: `packaging/common/stage-tree.sh` does not exclude `vendor/`, so the tree
#: rides the same channel as the code.
VENDOR_BUNDLE = os.path.join("vendor", "esptool")


def esptool_bundle_dir(root: str = "") -> str:
    """The vendored esptool tree under `root`, or "" when there is none.

    Empty is the normal answer on a desktop build: the PyInstaller `datas`
    list is a whitelist and does not carry this directory (its `.so` files are
    aarch64 Linux), and a desktop host has no Passport on a USB port.
    """
    base = root or os.path.dirname(os.path.abspath(__file__))
    bundle = os.path.join(base, VENDOR_BUNDLE)
    return bundle if os.path.isfile(os.path.join(bundle, "esptool", "__init__.py")) else ""


def esptool_env(bundle: str, base_env: dict) -> dict:
    """`base_env` with `bundle` prepended to PYTHONPATH (a copy, never mutated).

    `python -m esptool` runs in a *new* interpreter: appending to this
    process's `sys.path` would make availability checks say "yes" while every
    actual flash still died with `No module named esptool`.
    """
    env = dict(base_env)
    if not bundle:
        return env
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = bundle + (os.pathsep + existing if existing else "")
    return env

SERIAL_BY_ID = "/dev/serial/by-id"


def usb_device_key(path: str, by_id_dir: str = SERIAL_BY_ID) -> str:
    """The USB *device* behind a serial path, from the by-id symlinks.

    A path is not a device.  The FT-710's CP2105 exposes two ttys from one chip
    (``if00-port0`` is CAT, ``if01-port0`` is the scope), so excluding the path
    `cat.port` names leaves the other interface looking like a free board: on
    2026-11-27 the box at 192.168.1.217 offered ``/dev/ttyUSB1`` as a Passport
    candidate, one click away from writing 1.7 MB into the radio's USB bridge.

    The by-id name is ``usb-<vendor>_<model>_<serial>-if<NN>-port<M>``; strip
    the interface suffix and every interface of that chip shares one key.
    Returns "" when the path has no by-id entry (no udev, a container, a test),
    in which case the caller falls back to path-only exclusion.
    """
    if not path:
        return ""
    target = os.path.basename(path)
    try:
        names = os.listdir(by_id_dir)
    except OSError:
        return ""
    for name in names:
        try:
            if os.path.basename(os.path.realpath(os.path.join(by_id_dir, name))) == target:
                return name.split("-if")[0]
        except OSError:
            continue
    return ""


def candidate_ports(available: Iterable[str], *, radio_port: str = "",
                    esptool_port: str = "",
                    by_id_dir: str = SERIAL_BY_ID) -> list[str]:
    """Serial ports that may be a Passport, with the radio's ports removed.

    The radio must never be a candidate: the box talks to it on the very same kind
    of path, and `esptool` would happily try to enter a bootloader on a CAT port
    (which, on a Yaesu, is a burst of binary the radio answers with `?;`).

    Two exclusions, and both are needed: the exact paths the caller knows about,
    and — through ``usb_device_key`` — every *other* interface of the same USB
    device, because one chip can expose several ttys.
    """
    blocked = {p for p in (radio_port, esptool_port) if p}
    blocked_keys = {k for k in (usb_device_key(p, by_id_dir) for p in blocked) if k}
    seen, out = set(), []
    for path in sorted(available):
        if path in seen or path in blocked:
            continue
        if blocked_keys and usb_device_key(path, by_id_dir) in blocked_keys:
            continue
        seen.add(path)
        out.append(path)
    return out


def port_kind(path: str) -> str:
    """`acm` for native USB (C3), `usb` for a bridge — the page shows which."""
    return "acm" if "/ttyACM" in path or "usbmodem" in path else "usb"


# ── esptool invocations (data, not side effects) ───────────────────────────
@dataclass(frozen=True)
class Step:
    """One esptool invocation, described rather than run."""

    label: str
    argv: list

    def as_line(self) -> str:
        return " ".join(self.argv)


def esptool_cmd(port: str) -> list:
    """`python -m esptool` — the interpreter that is running this server.

    Not the `esptool.py` script on `$PATH`: the box runs inside a venv, and a
    service started by systemd has a PATH that does not include it.
    """
    return [sys.executable, "-m", "esptool", "--port", port]


def readback_path(nvs: str) -> str:
    """Where the read-back step writes the partition.

    **Not `-`**: this is esptool 4.14, whose INFO log goes to *stdout*, and whose
    `read_flash … -` payload never arrives at all when stdout is a pipe. Measured
    on the 2026-11-27 board: 563 bytes of log, 0 bytes of flash, `rc=0` — so the
    verification blamed the flash for a transport quirk and reported a correct
    board as failed. A named file in the same workspace is read back in full.
    """
    return os.path.join(os.path.dirname(nvs) or ".", "readback.bin")


def _file_size(path: str) -> int:
    """`os.path.getsize` that answers 0 for a file that is not there."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _payload_from_file(path: str, size: int) -> bytes:
    """"The first `size` bytes of the read-back file, or b"" when unusable."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(size + 1)
    except OSError:
        return b""
    return blob[:size] if len(blob) >= size else b""


def plan_flash(port: str, merged: str, nvs: str, build: Build) -> list[Step]:
    """The whole flash, as steps that can be shown before anything is written.

    `write_flash` verifies every byte it writes (`--verify` is esptool's default,
    spelled out here because a silent default is not a contract), and the NVS
    partition is read back by `verify_nvs` afterwards.
    """
    return [
        Step("chip id", esptool_cmd(port) + ["--before", "default_reset",
                                             "--after", "no_reset", "chip_id"]),
        Step(f"firmware {build.version}", esptool_cmd(port) + [
            "--chip", build.chip, "--baud", "460800", "--before", "default_reset",
            "--after", "no_reset", "write_flash", "--verify",
            "--flash_mode", build.flash_mode, "--flash_freq", build.flash_freq,
            "--flash_size", build.flash_size,
            "0x0", merged,
        ]),
        # `--after no_reset` is not decoration: the firmware step above keeps the
        # board in the bootloader, and this one has to do the same. With esptool's
        # default (`hard_reset`) the ESP32-C3 rebooted into the app it had just been
        # given, the app took over the USB-Serial/JTAG peripheral, the device
        # re-enumerated, and the *next* step opened the port mid-handover —
        # 2026-11-27 field log: "read-back delivered 91 of 24576 bytes" and a flash
        # that had actually succeeded was reported as failed.
        Step("wifi config", esptool_cmd(port) + [
            "--chip", build.chip, "--baud", "460800", "--after", "no_reset",
            "write_flash", "--verify",
            f"0x{NVS_OFFSET:x}", nvs,
        ]),
        Step("read back", esptool_cmd(port) + [
            "--chip", build.chip, "--baud", "460800", "read_flash",
            f"0x{NVS_OFFSET:x}", f"0x{NVS_SIZE:x}", readback_path(nvs),
        ]),
        Step("reset", esptool_cmd(port) + ["--after", "hard_reset", "run"]),
    ]


# ── the flash job ──────────────────────────────────────────────────────────
Runner = Callable[[list], "tuple[int, bytes]"]


@dataclass
class FlashResult:
    ok: bool = False
    reason: str = ""
    lines: list = field(default_factory=list)
    chip: str = ""
    verified: bool = False


def run_flash(*, port: str, merged: str, values: dict, build: Build,
              runner: Runner, workspace: str, namespace: str = CFG_NAMESPACE) -> FlashResult:
    """Build the NVS image, flash both regions, read the config back.

    `runner(argv) -> (returncode, output_bytes)` is injected: the tests drive the
    whole sequence with a fake runner and a fake serial port, so nothing here can
    open a real device by accident.
    """
    result = FlashResult()
    image = build_nvs_image(values, namespace=namespace)
    nvs_path = os.path.join(workspace, "nvs.bin")
    try:
        with open(nvs_path, "wb") as fh:
            fh.write(image)
    except OSError as e:
        result.reason = f"cannot write the config image: {e}"
        return result

    steps = plan_flash(port, merged, nvs_path, build)
    readback = readback_path(nvs_path)
    for step in steps:
        # The read-back travels over the ESP32-C3's USB-Serial/JTAG link, where a
        # short read is a transport hiccup rather than evidence about the flash. One
        # retry is cheap; declaring a correct board broken is not (2026-11-27: 563 of
        # 24576 bytes, while both writes had been hash-verified). The
        # `--after no_reset` in plan_flash is the fix for the cause — this is the
        # belt to that pair of braces.
        attempts = 2 if step.label == "read back" else 1
        for attempt in range(1, attempts + 1):
            result.lines.append(f"$ {step.as_line()}"
                                + (" (retry)" if attempt > 1 else ""))
            code, out = runner(step.argv)
            text = (out.decode("utf-8", "replace")
                    if isinstance(out, (bytes, bytearray)) else str(out))
            if step.label == "read back":
                # Never echo the read-back payload: it is 24 KB of binary, and the
                # page's log pane ends up in screenshots and support bundles. The
                # count shown is what the *file* holds — stdout carries esptool's
                # log, not the partition (see `readback_path`).
                result.lines.append(
                    f"  ({_file_size(readback) or 'unknown'} bytes read back)")
            else:
                _append_lines(result.lines, text)
            if code != 0:
                result.reason = f"{step.label} failed (exit {code})"
                if "Failed to connect" in text or "No serial data received" in text:
                    result.reason += (" — the board is not in download mode; hold BOOT "
                                      "while plugging it in, then retry")
                return result

            if step.label == "chip id":
                for line in text.splitlines():
                    if "Chip is" in line:
                        result.chip = line.split("Chip is", 1)[1].strip()
                if result.chip and "ESP32-C3" not in result.chip:
                    result.reason = f"unexpected chip: {result.chip}"
                    return result
                break

            if step.label != "read back":
                break              # the writes are verified by esptool itself

            try:
                payload = _payload_from_file(readback, len(image))
                if payload:
                    got = parse_nvs_image(payload)
                else:
                    # A fake runner (the test suite) produces bytes, not a file.
                    got = parse_nvs_image(_read_back_bytes(out, text, len(image)))
            except NvsError as e:
                if attempt < attempts:
                    result.lines.append(f"  ! {e} — retrying")
                    continue
                result.reason = f"read-back is not a valid NVS image: {e}"
                return result
            want = {k: values[k] for k in CFG_KEYS}
            have = got.get(namespace, {})
            if have != want:
                diff = sorted(set(want.items()) ^ set(have.items()))
                result.reason = "read-back mismatch: " + ", ".join(
                    f"{k}={v!r}" for k, v in diff)
                return result
            result.verified = True
            break
    result.ok = result.verified
    if not result.ok and not result.reason:
        result.reason = "flashed, but the config could not be verified"
    return result


#: The page shows a log pane, so the recorded lines are bounded: esptool's
#: progress output alone is hundreds of lines per write.
LOG_LINES_MAX = 200


def _append_lines(lines: list, text: str, limit: int = LOG_LINES_MAX) -> None:
    for line in str(text or "").splitlines():
        lines.append(f"  {line}")
    if len(lines) > limit:
        del lines[: len(lines) - limit]


def _read_back_bytes(raw, text: str, size: int) -> bytes:
    """The NVS partition as esptool handed it back.

    `read_flash … -` writes the region to stdout unchanged, so the runner's bytes
    are the image itself; some builds/versions print it as text instead, which is
    the second shape handled here.  A size that is not a whole number of NVS pages
    is a failure, not something to trim into shape — a short read means the
    verification would be checking bytes nobody flashed.
    """
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= size:
        return bytes(raw[:size])
    blob = (raw.decode("latin-1", "replace") if isinstance(raw, (bytes, bytearray))
            else str(text or ""))
    hexdigits = "".join(c for c in blob if c in "0123456789abcdefABCDEF")
    if len(hexdigits) < size * 2:
        raise NvsError(f"read-back delivered {len(hexdigits) // 2} of {size} bytes")
    return bytes.fromhex(hexdigits[: size * 2])



def redact(line: str, secret: str) -> str:
    """Strip a secret out of anything that may reach a log or a support bundle.

    The Wi-Fi passphrase and the box password both pass through this page, and a
    support bundle is exactly the artefact that must not carry them.
    """
    if secret and secret in line:
        return line.replace(secret, "***")
    return line

