# Why `vendor/ftdi/libftd2xx.so` is a symlink

`backends/ft710/scope_libraries.py` searches for a *pair* of libraries:
`libft4222` and `libftd2xx` must both be present in the same directory, or the
pair is rejected and the FT-710 falls back to the S-meter synthetic spectrum.
`scope_pipe.py` then `CDLL`s both paths and calls `FT_OpenEx`, `FT_Close`,
`FT_SetTimeouts` and `FT_SetLatencyTimer` on the second one.

On Linux there is only one file. FTDI's LibFT4222 package statically links
libftd2xx and re-exports it:

- its `ReadMe.txt` says so — *"The Linux version of libft4222 includes
  (statically links to) FTDI's libftd2xx"*;
- `install4222.sh` installs exactly one library (`build-arm-v8/libft4222.so.*`)
  and a single symlink to it, and never any `libftd2xx.so`;
- the package contains no `libftd2xx.so` for any architecture, only `ftd2xx.h`;
- the aarch64 build's `DT_NEEDED` lists no libftd2xx at all.

So one ELF fills both slots, and the second name is a symlink to it.

## The evidence

Against `libft4222-linux-1.4.4.232/build-arm-v8/libft4222.so.1.4.4.232`
(ELF 64-bit LSB shared object, ARM aarch64, `e_machine=183`):

```
导出符号总数: 1143
scope_pipe 实际需要的符号是否导出：
  FT_OpenEx                        ✅ 导出
  FT_Close                         ✅ 导出
  FT_SetTimeouts                   ✅ 导出
  FT_SetLatencyTimer               ✅ 导出
  FT4222_SPIMaster_SingleRead      ✅ 导出
  FT4222_SPIMaster_Init            ✅ 导出
  FT4222_SetClock                  ✅ 导出
  FT4222_UnInitialize              ✅ 导出
所有 FT_* 导出符号（81 个）
```

Reproduce it by reading the dynamic symbol table. There is no `readelf` on a
development Mac and no ELF reader in the base system, so this is the form that
works on both the build host and the box:

```bash
python3 - <<'PY'
import struct
d = open("vendor/ftdi/libft4222.so", "rb").read()
shoff = struct.unpack_from("<Q", d, 0x28)[0]
shentsize = struct.unpack_from("<H", d, 0x3A)[0]
shnum = struct.unpack_from("<H", d, 0x3C)[0]

def sh(i):
    o = shoff + i * shentsize
    return dict(type=struct.unpack_from("<I", d, o + 4)[0],
                off=struct.unpack_from("<Q", d, o + 0x18)[0],
                size=struct.unpack_from("<Q", d, o + 0x20)[0],
                link=struct.unpack_from("<I", d, o + 0x28)[0])

secs = [sh(i) for i in range(shnum)]
for s in secs:
    if s["type"] != 11:                      # SHT_DYNSYM
        continue
    strs = secs[s["link"]]
    names = set()
    for k in range(s["size"] // 24):
        o = s["off"] + k * 24
        nameoff = struct.unpack_from("<I", d, o)[0]
        shndx = struct.unpack_from("<H", d, o + 6)[0]
        if nameoff and shndx:
            e = d.index(b"\0", strs["off"] + nameoff)
            names.add(d[strs["off"] + nameoff:e].decode())
    print(f"FT_* exported: {sum(1 for n in names if n.startswith('FT_'))}")
    for w in ("FT_OpenEx", "FT_Close", "FT_SetTimeouts", "FT_SetLatencyTimer"):
        print(f"  {w:<30} {'yes' if w in names else 'NO'}")
PY
```

On a Linux host `readelf --dyn-syms --wide vendor/ftdi/libft4222.so | grep FT_OpenEx`
answers the same question.

## What it costs to lose

Nothing else. Without the pair the FT-710 drops to the S-meter synthetic
spectrum; CAT, audio and PTT are unaffected, and the other ten registry models
never look at these files (`DEPENDENCIES.md` records the same fallback).

## When it is a copy instead of a symlink (measured 2026-11-10)

A `cp` in place of the symlink looks identical on disk — same bytes, same size
— and is **silent until the next `scope_pipe` start**:

```
scope_pipe: spi_init_retry:attempt_1:error_1000
scope_pipe: spi_init_failed:all_attempts
scope_pipe: fatal:cannot_open_device   →  waterfall falls back to the S-meter
```

`FT_OpenEx` succeeds; it is `FT4222_SPIMaster_Init` that answers **1000 =
`FT4222_DEVICE_NOT_SUPPORTED`** ("this device is not a FT4222 chip"). Two
pathnames mean two `dlopen` objects with two D2XX device tables — the handle
created in one does not exist in the other. A symlink resolves to the same
inode, is loaded once, and both APIs share one table. That is the whole reason
this file is a symlink.

Because the failure needs a *new* pipe, a running one hides it: on the box the
pipe started 2026-11-09 01:39 kept streaming through fourteen hours and several
updates, and the breakage only surfaced the next time the server restarted
while a spectrum client was connected. Field log: `scope_pipe` dead since the
08:27 restart, `frames=0` on every attempt, while CAT answered `ID0800`.

`scope_libraries.load_ftdi_libraries()` now tolerates it: when the FT4222
library carries the D2XX API (directly, as the Linux build does, or through the
dependency it links, as macOS does), that **one** handle serves both slots, so
a copied second slot still works. It also logs
`libs_loaded:…:d2xx=shared|separate` with **full paths** plus a
`libs_warning:libftd2xx.so is a COPY of libft4222.so…` line, because the old
`libs_loaded:ft4222=libft4222.so:ftd2xx=libftd2xx.so` showed only basenames and
could not tell the two layouts apart. `tests/test_ftdi_library_pair.py` pins
both the classification and the shared-instance decision.

Restoring the symlink on a box is one command (the tree already ships it, and
`rsync -a` restores it on the next update, so this is never a permanent state):

```bash
cd /opt/mrrc_modern/vendor/ftdi && ln -sf libft4222.so libftd2xx.so
sudo systemctl restart mrrc-modern
```

`tests/test_box_profiles.py::VendoredFtdiTests` asserts both that the file is
an AArch64 ELF and that the second name is a symlink to the first. The
architecture assertion is the one that matters: fetching the wrong `build-*`
directory produces a file that is correctly named, passes every other check,
and fails only on the box.

## Moving them

The escape hatch is `MRRC_FTDI_LIB_DIR`, which `get_candidate_library_dirs()`
consults before its own defaults. If the libraries live somewhere else, point
it at that directory and both names must be there.
