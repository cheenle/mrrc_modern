# 树莓派镜像包（rpi64）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 产出并发布 `MRRC-Modern-v1.14.1-rpi64.img.xz`——Raspberry Pi OS Lite 64-bit (Bookworm) 定制镜像，烧卡即用、首启零配置（预置 mrrc.env 优先，自动生成兜底）。

**架构：** pi-gen（锁定 `2026-06-18-raspios-bookworm-arm64`）标准 stage0-2 + 自定义 stage4（`packaging/rpi/pi-gen-stage4/`）；本机 Docker Desktop（Apple Silicon VM = aarch64，原生构建无仿真）；代码部署到 `/opt/mrrc_modern` + 构建期 venv；`mrrc-modern.service`（systemd）+ `mrrc-firstboot.service`（oneshot）。规格：`docs/superpowers/specs/2026-09-09-raspberry-pi-image-design.md`。

**技术栈：** bash（pi-gen/build 脚本）、systemd units、Python 3（venv 内运行）、rsync、xz、e2fsprogs debugfs（镜像抽查）。

**环境约束：** 测试一律 `.venv/bin/python`（禁止 activate/裸 python3，见 macos-installer gotcha 10）；Docker Desktop 需运行（`open -a Docker`）；磁盘 ≥20GB 空闲。

---

## 文件结构

| 文件 | 职责 | 动作 |
| --- | --- | --- |
| `linux/first_run.py` | 首启自动配置（密码/串口探测/电台识别），自 `macos/first_run.py` 移植 + Linux 串口过滤 | 创建 |
| `tests/test_linux_first_run.py` | first_run 单测（纯逻辑，mock serial） | 创建 |
| `packaging/rpi/pi-gen-stage4/00-install-packages/00-packages` | apt 包列表 | 创建 |
| `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run.sh` | chroot 内：venv、用户、服务启用、构建闸门断言 | 创建 |
| `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/…` | service 单元 ×2、firstboot wrapper、mrrc-show-password、motd 脚本（代码树由 build-image.sh 拷入 `files/opt/mrrc_modern/`） | 创建 |
| `packaging/rpi/build-image.sh` | 驱动：Docker 检查 → 克隆 pi-gen → 拷 stage → 构建 → xz → SHA | 创建 |
| `packaging/rpi/verify-image.sh` | xz 完整性 + 体积界 + debugfs 抽查 | 创建 |
| `tests/test_rpi_packaging.py` | 打包文件结构断言（仿 test_windows_packaging_files.py） | 创建 |
| `pi_pack.md` / `docs/RASPBERRY_PI_GUIDE.md` | 构建方 / 用户向手册 | 创建 |
| `website/{index,zh/index}.html` 等 | Pi 下载卡片 | 修改 |
| `.agents/skills/dual-platform-release/SKILL.md` | 补 Pi 线引用 | 修改 |

不做（规格 §8）：在线自更新、armhf、桌面版、PyInstaller、服务端代码改动、FTDI 库自动下载。SDD/CHANGELOG：SDD V2.36 条目随本计划写入（对规格 §7"随下次发版"的小偏离，理由：doc-sync 即时性，发布物当次上线）；CHANGELOG 不动（无版本号变更）。

---

### 任务 1：`linux/first_run.py` 移植 + 单测（红→绿）

**文件：**

- 创建：`linux/first_run.py`（自 `macos/first_run.py` 拷贝后改 3 处，见步骤 3）
- 创建：`tests/test_linux_first_run.py`

- [ ] **步骤 1：编写失败测试**

```python
"""Tests for linux/first_run.py — Pi first-boot auto-configuration."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "linux"))

import first_run  # noqa: E402


class _FakePort:
    def __init__(self, device, description="", hwid=""):
        self.device = device
        self.description = description
        self.hwid = hwid


class SerialFilterTests(unittest.TestCase):
    def test_linux_globs_exclude_onboard_uarts(self):
        ports = [
            _FakePort("/dev/ttyS0", "ttyS0"),
            _FakePort("/dev/ttyS31", "ttyS31"),
            _FakePort("/dev/ttyprintk", "ttyprintk"),
            _FakePort("/dev/ttyUSB0", "CP2102 USB to UART", "usb"),
            _FakePort("/dev/ttyACM0", "IC-7300", "usb"),
            _FakePort("", "bogus"),
        ]
        got = first_run._candidates(ports)
        devices = [p.device for p in got]
        self.assertEqual(devices, ["/dev/ttyUSB0", "/dev/ttyACM0"])

    def test_usb_serial_description_sorts_first(self):
        ports = [_FakePort("/dev/ttyACM0", "Linux USB Serial"),
                 _FakePort("/dev/ttyUSB1", "CP210x UART Bridge")]
        self.assertEqual(first_run.detect_serial_ports(ports)[0], "/dev/ttyUSB1")


class FirstRunFlowTests(unittest.TestCase):
    def test_apply_generates_password_and_skips_board_uarts(self):
        # FT-710 answers on the second candidate (ttyUSB1); ttyS0 never opened.
        opened = []

        def open_func(port, baudrate, timeout):
            opened.append(port)
            ser = MagicMock()
            if port == "/dev/ttyUSB1":
                ser.read.side_effect = lambda n: b"ID" if baudrate == 38400 else b""
            else:
                ser.read.return_value = b""
            return ser

        env = {}
        cfg = Path("/tmp/test-mrrc.env")
        cfg.unlink(missing_ok=True)
        with unittest.mock.patch.object(
            first_run, "detect_serial_ports",
            return_value=["/dev/ttyS0", "/dev/ttyUSB1", "/dev/ttyACM0"],
        ):
            out = first_run.apply_first_run(env, cfg, open_func=open_func)
        self.assertTrue(out["MRRC_AUTO_PASSWORD"] == "1")
        self.assertGreaterEqual(len(out["MRRC_WEB_PASSWORD"]), 20)
        self.assertEqual(out["MRRC_SERIAL_PORT"], "/dev/ttyUSB1")
        self.assertEqual(out["MRRC_RADIO_MODEL"], "ft710")
        self.assertEqual(out["MRRC_PORT_CONFIRMED"], "1")
        self.assertEqual(opened, ["/dev/ttyUSB1"])  # ttyS0 skipped, order honoured
        self.assertIn("MRRC_WEB_PASSWORD=", cfg.read_text(encoding="utf-8"))

    def test_needs_first_run_false_when_settled(self):
        env = {"MRRC_WEB_PASSWORD": "s3cret-pass", "MRRC_RADIO_MODEL": "ic7300",
               "MRRC_SERIAL_PORT": "/dev/ttyUSB0", "MRRC_PORT_CONFIRMED": "1"}
        self.assertFalse(first_run.needs_first_run(env))
```

- [ ] **步骤 2：运行验证失败**

```bash
.venv/bin/python -m unittest tests.test_linux_first_run -v
```

预期：FAIL/ERROR（`No module named 'first_run'`）。

- [ ] **步骤 3：创建 `linux/first_run.py`**

`cp macos/first_run.py linux/first_run.py` 后做 3 处修改（其余逐字保留，docstring 首行改为 `First-launch auto-configuration for the Raspberry Pi image (systemd mrrc-firstboot).`）：

1. `_candidates` 的非 darwin 分支替换为 Linux 过滤：

```python
    # Linux: drop onboard UARTs and pseudo-devices; USB serial ports only.
    import re
    return [
        p for p in ports
        if p.device and re.match(r"^/dev/(ttyUSB|ttyACM)\d+$", p.device)
    ]
```

1. 文件尾部追加 `__main__` 入口（firstboot wrapper 直接调用）：

```python
def main() -> int:
    """Entry point for mrrc-firstboot.service (runs as root)."""
    env_path = Path(os.environ.get("MRRC_ENV_FILE", "/opt/mrrc_modern/env/mrrc.env"))
    if os.environ.get("MRRC_PRESEED") == "1":
        return 0  # preseed handled by the wrapper; nothing to generate
    env = dict(
        line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    )
    if not needs_first_run(env):
        print("[mrrc-firstboot] config settled; skipping auto-config")
        return 0
    env = apply_first_run(env, env_path)
    print("=" * 56)
    print("  MRRC Modern 首次配置完成")
    print(f"  网页密码: {env['MRRC_WEB_PASSWORD']}")
    print(f"  电台型号: {env['MRRC_RADIO_MODEL']}  串口: {env['MRRC_SERIAL_PORT']}")
    print(f"  浏览器打开: https://raspberrypi.local:8888")
    print("  (本提示也会出现在 SSH 登录 motd; sudo mrrc-show-password 可随时查看)")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

1. `DEFAULT_SERIAL_PORTS` 的 else 分支保持 `("/dev/ttyUSB0",)` 不动（原文件已正确）。

- [ ] **步骤 4：运行验证通过**

```bash
.venv/bin/python -m unittest tests.test_linux_first_run -v
```

预期：OK（5 tests）。注意 `test_apply…` 中 `unittest.mock.patch.object` 需 `import unittest.mock`——在测试文件顶部补 `import unittest.mock`（或改用 `from unittest import mock` 后 `mock.patch.object`）。

- [ ] **步骤 5：Commit**

```bash
git add linux/first_run.py tests/test_linux_first_run.py
git commit -m "feat(rpi): linux first-run auto-config ported from macOS (SDD V2.36)"
```

---

### 任务 2：pi-gen stage4 文件 + 打包结构测试（红→绿）

**文件：**

- 创建：`tests/test_rpi_packaging.py`
- 创建：`packaging/rpi/pi-gen-stage4/00-install-packages/00-packages`
- 创建：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run.sh`
- 创建：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/etc/systemd/system/mrrc-modern.service`
- 创建：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/etc/systemd/system/mrrc-firstboot.service`
- 创建：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/opt/mrrc_modern/linux/firstboot_wrapper.py`
- 创建：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/usr/local/bin/mrrc-show-password`
- 创建：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/etc/update-motd.d/10-mrrc`

- [ ] **步骤 1：编写失败测试 `tests/test_rpi_packaging.py`**

```python
"""Structural tests for the Raspberry Pi image packaging (mirrors
test_windows_packaging_files.py)."""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RPI = REPO_ROOT / "packaging" / "rpi"
STAGE = RPI / "pi-gen-stage4"


class RpiPackagingFilesTests(unittest.TestCase):
    def test_stage_layout_exists(self):
        self.assertTrue((STAGE / "00-install-packages" / "00-packages").is_file())
        self.assertTrue((STAGE / "01-deploy-mrrc" / "00-run.sh").is_file())

    def test_service_units_pin_user_and_env(self):
        svc = (STAGE / "01-deploy-mrrc" / "files" / "etc" / "systemd" / "system" / "mrrc-modern.service").read_text(encoding="utf-8")
        self.assertIn("User=mrrc", svc)
        self.assertIn("EnvironmentFile=/opt/mrrc_modern/env/mrrc.env", svc)
        self.assertIn("Restart=on-failure", svc)
        self.assertIn("WorkingDirectory=/opt/mrrc_modern", svc)
        fb = (STAGE / "01-deploy-mrrc" / "files" / "etc" / "systemd" / "system" / "mrrc-firstboot.service").read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=!/var/lib/mrrc/firstboot-done", fb)
        self.assertIn("Type=oneshot", fb)

    def test_build_and_verify_scripts_exist(self):
        for name in ("build-image.sh", "verify-image.sh"):
            p = RPI / name
            self.assertTrue(p.is_file(), name)
            self.assertIn("set -euo pipefail", p.read_text(encoding="utf-8"))

    def test_password_helper_requires_root_path(self):
        helper = (STAGE / "01-deploy-mrrc" / "files" / "usr" / "local" / "bin" / "mrrc-show-password").read_text(encoding="utf-8")
        self.assertIn("/opt/mrrc_modern/env/mrrc.env", helper)
        self.assertIn("MRRC_WEB_PASSWORD", helper)
```

- [ ] **步骤 2：运行验证失败**

```bash
.venv/bin/python -m unittest tests.test_rpi_packaging -v
```

预期：FAIL（文件不存在）。

- [ ] **步骤 3：创建全部 stage 文件**

`00-install-packages/00-packages`（纯文本）：

```
python3-venv python3-pip portaudio19-dev libopus0 avahi-daemon e2fsprogs
```

`files/etc/systemd/system/mrrc-modern.service`：

```ini
[Unit]
Description=MRRC Modern web control server
After=network-online.target mrrc-firstboot.service
Wants=network-online.target
Requires=mrrc-firstboot.service

[Service]
User=mrrc
Group=mrrc
WorkingDirectory=/opt/mrrc_modern
EnvironmentFile=/opt/mrrc_modern/env/mrrc.env
ExecStart=/opt/mrrc_modern/venv/bin/python server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`files/etc/systemd/system/mrrc-firstboot.service`：

```ini
[Unit]
Description=MRRC Modern first-boot auto-configuration
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/var/lib/mrrc/firstboot-done

[Service]
Type=oneshot
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/firstboot_wrapper.py
StandardOutput=console
StandardError=journal
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

`files/opt/mrrc_modern/linux/firstboot_wrapper.py`：

```python
#!/usr/bin/env python3
"""mrrc-firstboot.service entry point (root): preseed → auto-config → cert."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ENV_DIR = Path("/opt/mrrc_modern/env")
ENV_FILE = ENV_DIR / "mrrc.env"
PRESEED = Path("/boot/firmware/mrrc.env")
DONE = Path("/var/lib/mrrc/firstboot-done")
CERT_DIR = Path("/var/lib/mrrc/certs")


def adopt_preseed() -> bool:
    if not PRESEED.is_file():
        return False
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    text = PRESEED.read_text(encoding="utf-8")
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
    # defaults for a headless box: dual-stack bind, LAN-reachable
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    keys = {l.split("=", 1)[0] for l in lines if "=" in l and not l.strip().startswith("#")}
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
```

`files/usr/local/bin/mrrc-show-password`：

```bash
#!/bin/bash
# Print the MRRC Modern web password from the env file (root-only file).
exec grep -E '^MRRC_WEB_PASSWORD=' /opt/mrrc_modern/env/mrrc.env | cut -d= -f2-
```

`files/etc/update-motd.d/10-mrrc`：

```bash
#!/bin/sh
echo "  MRRC Modern: https://raspberrypi.local:8888   (web password: sudo mrrc-show-password)"
```

`01-deploy-mrrc/00-run.sh`（chroot 内执行；代码树由 build-image.sh 预先放入 `files/opt/mrrc_modern/`）：

```bash
#!/bin/bash -e
# Stage: deploy MRRC Modern runtime (chroot). Code tree already in place.

# ── venv (build-time; first boot needs no network) ──
python3 -m venv /opt/mrrc_modern/venv
/opt/mrrc_modern/venv/bin/pip install --upgrade pip
PIP_BREAK_SYSTEM_PACKAGES=1 /opt/mrrc_modern/venv/bin/pip install -r /opt/mrrc_modern/requirements.txt

# ── groups & ownership ──
usermod -aG dialout,audio mrrc
chown -R mrrc:mrrc /opt/mrrc_modern
mkdir -p /var/lib/mrrc/certs
chown -R mrrc:mrrc /var/lib/mrrc

# ── service & helper wiring ──
chmod 755 /opt/mrrc_modern/linux/firstboot_wrapper.py /usr/local/bin/mrrc-show-password /etc/update-motd.d/10-mrrc
systemctl enable mrrc-firstboot.service
systemctl enable mrrc-modern.service
systemctl enable ssh

# ── version stamp (written by build-image.sh as files/opt/mrrc_modern/VERSION) ──
echo "MRRC Modern $(cat /opt/mrrc_modern/VERSION) — rpi64 image"

# ── BUILD GATE: runtime imports + syntax inside the image ──
/opt/mrrc_modern/venv/bin/python -m py_compile /opt/mrrc_modern/server.py \
    /opt/mrrc_modern/linux/first_run.py /opt/mrrc_modern/linux/firstboot_wrapper.py
/opt/mrrc_modern/venv/bin/python -c "import fastapi, uvicorn, serial, pyaudio, numpy, cryptography; print('deps OK')"
/opt/mrrc_modern/venv/bin/python -c "import sys; sys.path.insert(0,'/opt/mrrc_modern'); import scope_libraries; print('scope libs OK')"
```

（`scope_libraries` 经根 shim 可导入——root 的 `scope_pipe.py`/`scope_libraries.py` 是 `backends.ft710` 的兼容 shim，`sys.path` 加 `/opt/mrrc_modern` 后即可。）

- [ ] **步骤 4：运行验证通过**

```bash
.venv/bin/python -m unittest tests.test_rpi_packaging -v
bash -n packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run.sh
```

预期：OK；bash -n 无输出。

- [ ] **步骤 5：Commit**

```bash
git add packaging/rpi tests/test_rpi_packaging.py
git commit -m "feat(rpi): pi-gen stage4 — deploy, venv, systemd units, firstboot (SDD V2.36)"
```

---

### 任务 3：构建驱动 + 验证脚本 + 首次构建

**文件：**

- 创建：`packaging/rpi/build-image.sh`
- 创建：`packaging/rpi/verify-image.sh`

- [ ] **步骤 1：创建 `packaging/rpi/build-image.sh`**

```bash
#!/usr/bin/env bash
# Build the MRRC Modern Raspberry Pi image (rpi64) via pi-gen in Docker.
# Prereqs: Docker Desktop running (Apple Silicon = native aarch64), >=20GB free.
# Usage: packaging/rpi/build-image.sh          (version from CHANGELOG top entry)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIGEN_REF="2026-06-18-raspios-bookworm-arm64"
WORK="$REPO_ROOT/build/pi-gen"
OUT="$REPO_ROOT/dist/rpi"

VERSION="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_ROOT/CHANGELOG.md" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' )"
: "${VERSION:?Could not read version from CHANGELOG.md}"
echo "==> Building MRRC Modern ${VERSION} rpi64 image"

# ── preflight ──
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not running (open -a Docker)"; exit 1; }
FREE_GB=$(df -g "$REPO_ROOT" | awk 'NR==2 {print $4}')
(( FREE_GB >= 20 )) || { echo "ERROR: need >=20GB free, have ${FREE_GB}GB"; exit 1; }

# ── pi-gen clone (pinned) ──
rm -rf "$WORK"
git clone --depth 1 --branch "$PIGEN_REF" https://github.com/RPi-Distro/pi-gen.git "$WORK"

# ── stage: copy ours in, inject code tree ──
rsync -a --delete "$REPO_ROOT/packaging/rpi/pi-gen-stage4/" "$WORK/rpi-stage4/"
STAGE_FILES="$WORK/rpi-stage4/01-deploy-mrrc/files"
mkdir -p "$STAGE_FILES/opt/mrrc_modern"
rsync -a "$REPO_ROOT/" "$STAGE_FILES/opt/mrrc_modern/" \
  --exclude ".git/" --exclude "venv/" --exclude ".venv/" --exclude "dist/" \
  --exclude "build/" --exclude "logs/" --exclude "certs/" --exclude "promo/" \
  --exclude "FT710Mobile/" --exclude "FT710Android/" --exclude "website/" \
  --exclude "tests/" --exclude ".agents/" --exclude "docs/" --exclude "SDD/" \
  --exclude "packaging/" --exclude "macos/" --exclude "windows/" \
  --exclude "__pycache__/" --exclude "*.pyc" --exclude ".DS_Store" \
  --exclude "atr1000_tuner.json"
printf '%s\n' "${VERSION#v}" > "$STAGE_FILES/opt/mrrc_modern/VERSION"
mkdir -p "$STAGE_FILES/opt/mrrc_modern/vendor/ftdi"

# ── pi-gen config ──
cat > "$WORK/config" <<EOF
IMG_NAME="MRRC-Modern-${VERSION}-rpi64"
RELEASE=bookworm
TARGET_ARCH=64
FIRST_USER_NAME=mrrc
FIRST_USER_PASS=mrrc
ENABLE_SSH=1
DEPLOY_COMPRESSION=none
STAGE_LIST="stage0 stage1 stage2 rpi-stage4"
EOF

cd "$WORK"
IGNORE_FILE_CHANGES=1 PRESERVE_CONTAINER=0 ./build-docker.sh

# ── artifact ──
mkdir -p "$OUT"
IMG=$(ls "$WORK/deploy/"*.img | head -1)
XZ_OUT="$OUT/MRRC-Modern-${VERSION}-rpi64.img.xz"
xz -T0 -9 -c "$IMG" > "$XZ_OUT"
rm -f "$IMG"
echo "==> Done: $XZ_OUT"
shasum -a 256 "$XZ_OUT"
```

- [ ] **步骤 2：创建 `packaging/rpi/verify-image.sh`**

```bash
#!/usr/bin/env bash
# Verify the built rpi64 image: xz integrity, size bounds, debugfs spot-checks.
# Requires: e2fsprogs (brew install e2fsprogs — debugfs at e2fsprogs/sbin).
set -euo pipefail
IMG_XZ="${1:?usage: verify-image.sh <img.xz>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEBUGFS="$(brew --prefix e2fsprogs 2>/dev/null)/sbin/debugfs"
[ -x "$DEBUGFS" ] || DEBUGFS=debugfs

echo "==> xz integrity"
xz -t "$IMG_XZ"
BYTES=$(stat -f %z "$IMG_XZ")
if (( BYTES < 400000000 || BYTES > 2000000000 )); then
    echo "ERROR: image size ${BYTES} outside 0.4-2.0GB bounds"; exit 1
fi
echo "==> size OK (${BYTES} bytes)"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "==> decompress (for debugfs)"
xz -dc "$IMG_XZ" > "$TMP/root.img"
for f in /opt/mrrc_modern/VERSION /etc/systemd/system/mrrc-modern.service \
         /etc/systemd/system/mrrc-firstboot.service /usr/local/bin/mrrc-show-password; do
    echo "--- $f ---"
    "$DEBUGFS" -R "cat $f" "$TMP/root.img" 2>/dev/null | head -5
done
echo "==> venv python present:"
"$DEBUGFS" -R "stat /opt/mrrc_modern/venv/bin/python3" "$TMP/root.img" 2>/dev/null | head -2
echo "==> VERIFY_OK"
```

- [ ] **步骤 3：结构测试 + 语法 + 首次构建**

```bash
.venv/bin/python -m unittest tests.test_rpi_packaging -v        # OK
bash -n packaging/rpi/build-image.sh packaging/rpi/verify-image.sh
open -a Docker; sleep 25                                        # 启动 Docker Desktop
packaging/rpi/build-image.sh                                    # 15-40 分钟
```

预期：构建完成，输出 `dist/rpi/MRRC-Modern-v1.14.1-rpi64.img.xz` + SHA-256。

**已知风险与预案：** 若 Docker Desktop 在 loop-mount 步骤失败（privileged 容器缺 /dev/loop），改在 ham.vlsc.net 构建：`apt install docker.io qemu-user-static binfmt-support`（Ubuntu 24.04 宿主机 x86_64 上交叉构建 arm64，pi-gen 官方支持路径），脚本不变、在宿主机直接 `./build-docker.sh`。构建日志留存到 pi_pack.md 故障表。

- [ ] **步骤 4：验证镜像**

```bash
brew list e2fsprogs >/dev/null 2>&1 || brew install e2fsprogs
packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v1.14.1-rpi64.img.xz
```

预期：`VERIFY_OK`，抽查文件内容正确（VERSION=1.14.1、User=mrrc、ConditionPathExists 行）。

- [ ] **步骤 5：Commit（脚本与产物记录；img.xz 不入库，dist/ 已 gitignore）**

```bash
git add packaging/rpi tests/test_rpi_packaging.py
git commit -m "feat(rpi): build + verify drivers; first rpi64 image build (SDD V2.36)"
```

---

### 任务 4：手册 + 网站卡片 + skill 增补 + SDD

**文件：**

- 创建：`pi_pack.md`、`docs/RASPBERRY_PI_GUIDE.md`
- 修改：`website/index.html`、`website/zh/index.html`、`.agents/skills/dual-platform-release/SKILL.md`、`SDD/14-version-history.md`、`SDD/README.md`

- [ ] **步骤 1：`pi_pack.md`（构建方手册）**——章节：环境拓扑（Mac Docker → pi-gen 容器原生 aarch64）、一次性准备（Docker Desktop、e2fsprogs、≥20GB）、每次构建流程（build-image.sh → verify-image.sh → sha 记录）、发布（scp 到 www downloads + 网站卡片更新 + deploy.sh）、锁定 pi-gen ref 变更流程、故障表（Docker 未启/磁盘不足/loop-mount 失败→ham.vlsc.net 方案/pip 慢→pi-gen 缓存 stage）

- [ ] **步骤 2：`docs/RASPBERRY_PI_GUIDE.md`（用户向，中文）**——章节：①烧卡（Imager → 使用自定义镜像 → 选 img.xz → 预置密码可选：先拷 mrrc.env 进 boot 分区）②上电首启（LED/约 90 秒）③拿密码（预置跳过；兜底=HDMI 控制台/SSH motd/`sudo mrrc-show-password`）④登录 `https://raspberrypi.local:8888`（自签证书一次警告）⑤接电台（FT-710：CP210x 内核自带、双 ttyUSB 自动探测、MOD SOURCE=USB 仍需设；IC-7300：单口 CI-V；音频 USB 声卡自动识别，冲突时 `MRRC_AUDIO_RX/TX_DEVICE` 锁定）⑥FT-710 真频谱点亮（FTDI 官方页下载 aarch64 `libft4222.so`/`libftd2xx.so` → `sudo cp /opt/mrrc_modern/vendor/ftdi/` → `sudo systemctl restart mrrc-modern`）⑦服务管理（`sudo systemctl restart mrrc-modern`、日志 `journalctl -u mrrc-modern -f`）⑧更新=重烧新镜像 ⑨故障表。

- [ ] **步骤 3：网站卡片**——EN/ZH index.html 的 macOS 卡片 `</div>` 之后插入同构 Pi 卡片（标题 "Raspberry Pi · rpi64"、`downloads/MRRC-Modern-v1.14.1-rpi64.img.xz` 链接、大小/SHA-256（构建后实测值）、烧录+预置密码一句话、链接到指南）；hero 区与 footer 的平台列表补 Pi。

- [ ] **步骤 4：skill 与 SDD**——`.agents/skills/dual-platform-release/SKILL.md` 的 Overview 末尾加一句：Pi 镜像（可选产物）构建/发布见 `pi_pack.md`（`packaging/rpi/build-image.sh`，产物 `MRRC-Modern-v<ver>-rpi64.img.xz`）。SDD `14-version-history.md` 加 V2.36 条目（rpi64 打包线：pi-gen stage4/firstboot/预置密码/FTDI 手动点亮/694 测试+结构测试，镜像随 v1.14.1 发布）；`SDD/README.md` Version→V2.36、Status 加 "rpi64 image packaging line (SDD V2.36)"。

- [ ] **步骤 5：验证 + Commit**

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"   # 696+ OK
git add pi_pack.md docs/RASPBERRY_PI_GUIDE.md website/ .agents/skills SDD/
git commit -m "docs(rpi): pi_pack + user guide + website Pi card + skill/SDD sync (SDD V2.36)"
```

---

### 任务 5：发布镜像 + 提交推送

- [ ] **步骤 1：上传**（约 1GB）

```bash
scp dist/rpi/MRRC-Modern-v1.14.1-rpi64.img.xz www.vlsc.net:/tmp/MRRC-Modern-v1.14.1-rpi64.img.xz.new
ssh www.vlsc.net 'sudo -n mv /tmp/MRRC-Modern-v1.14.1-rpi64.img.xz.new /var/www/vlsc.net/mrrc_modern/downloads/ && sudo -n chown www-data:www-data /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.14.1-rpi64.img.xz && sudo -n chmod 644 /var/www/vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.14.1-rpi64.img.xz'
```

- [ ] **步骤 2：部署网站 + 验证**

```bash
cd website && echo y | ./deploy.sh
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.14.1-rpi64.img.xz   # 200 + content-length == 本地
curl -s https://www.vlsc.net/mrrc_modern/ | grep -c "rpi64"                              # >0
```

- [ ] **步骤 3：Commit + push**

```bash
git add -A && python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged   # exit 0
git commit -m "release(rpi): publish v1.14.1 rpi64 image (SDD V2.36)" --quiet
git push origin main
git ls-remote origin main | head -c 7   # == 本地 HEAD
```

---

## 自检记录

1. **规格覆盖度**：§1 产物/版本规则→任务 3；§2 密码路径→任务 2（wrapper/first_run）+任务 4 文档；§3 stage4→任务 2；§4 FTDI→任务 2（vendor 目录）+任务 4 文档；§5 构建链→任务 3；§6 验证→任务 2 闸门+任务 3 verify；§7 发布/文档→任务 4/5；§8 不做清单未违反。SDD V2.36 时机与规格 §7 有小偏离（当次写入），已在计划头部声明。
2. **占位符扫描**：脚本/单元/测试均为完整内容；Pi 卡片 HTML 因依赖实测大小/SHA（任务 3 产物），任务 4 步骤 3 以"同构卡片 + 实测值替换"表述且给出锚点，属构建产物回填而非占位符。
3. **类型/命名一致性**：`/opt/mrrc_modern/env/mrrc.env`（wrapper/first_run/service 三处一致）；`/var/lib/mrrc/firstboot-done`（unit Condition 与 wrapper 一致）；`slider-rfpower-p` 无涉。`MRRC_ENV_FILE`、`MRRC_PRESEED` 为 wrapper↔first_run 契约，两端一致。
