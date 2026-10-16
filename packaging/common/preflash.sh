#!/bin/bash
# 出厂预置（发货版镜像）：固定口令 + 系统层（时区/区域）。
#
# 谁调它：mrrc-service-layer.sh 在 MRRC_PREFLASH=1 时（= `build-image.sh --preflash`）。
# 它在共享服务层里，所以盒子线与树莓派线走同一份实现 —— 但开关默认关，
# 不传 --preflash 的镜像里这个脚本一次都不会被执行。
#
#   bash preflash.sh /opt/mrrc_modern/env/mrrc.env      # 全量（要 root）
#   bash preflash.sh --env-only /tmp/mrrc.env           # 只写 env（单测真跑用，不需要 root）
#
# 幂等：重跑只覆盖同一批键，不动别的任何一行。
set -euo pipefail

# 开关闸门：默认关是"预置"这件事的安全属性，不是可选项 —— 一次误调用就会把
# 公开口令装进一台本来每台口令不同的镜像里。
if [ "${MRRC_PREFLASH:-0}" != "1" ]; then
 echo "preflash: MRRC_PREFLASH is not 1 — refusing to write anything." >&2
 echo "preflash: 这是出厂预置通道；要手动跑请显式 MRRC_PREFLASH=1 …" >&2
 exit 1
fi

ROOT_PASSWORD="${MRRC_PREFLASH_ROOT_PASSWORD:-abcd1234}"
USER_PASSWORD="${MRRC_PREFLASH_USER_PASSWORD:-abcd!1234}"
WEB_PASSWORD="${MRRC_PREFLASH_WEB_PASSWORD:-abcd!1234}"
TIMEZONE="${MRRC_PREFLASH_TIMEZONE:-Asia/Shanghai}"
LOCALE_NAME="${MRRC_PREFLASH_LOCALE:-zh_CN.UTF-8}"
MRRC_USER="${MRRC_USER:-mrrc}"

ENV_ONLY=0
POSITIONAL=()
for arg in "$@"; do
 case "$arg" in
  --env-only) ENV_ONLY=1 ;;
  *) POSITIONAL+=("$arg") ;;
 esac
done
ENV_FILE="${POSITIONAL[0]:-/opt/mrrc_modern/env/mrrc.env}"

log() { printf '\n=== preflash: %s ===\n' "$*"; }

# ── env 文件：Web 口令（唯一的写者）────────────────────────────────────
# 服务层的 sed 已经把 install.sh 在**构建主机**上猜出来的口令剥掉了，所以正常
# 流程里这两个键此刻还不存在。用 python 做"替换或追加"而不是 sed：口令里有
# `!`，而 sed 的替换串对 `&`、`\`、分隔符都敏感 —— 这里没有可被口令破坏的语法。
log "env file: $ENV_FILE"
python3 - "$ENV_FILE" "$WEB_PASSWORD" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
password = sys.argv[2]
wanted = {"MRRC_WEB_PASSWORD": password, "MRRC_AUTO_PASSWORD": ""}
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out, seen = [], set()
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in wanted.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

if [ "$ENV_ONLY" = "1" ]; then
 echo "preflash: env-only — 系统层（账号口令/时区/locale）跳过"
 exit 0
fi

# ── 账号口令 ─────────────────────────────────────────────────────────
# 账号由平台壳先建好（盒子的 adduser --system mrrc / Pi 的 FIRST_USER_NAME），
# 服务层的前置断言已经保证它在。不在这里建账号：那是平台的事。
log "accounts: fixed passwords"
id -u "$MRRC_USER" >/dev/null 2>&1 || {
 echo "preflash: no '$MRRC_USER' account — the platform shell must create it" >&2
 exit 1
}
printf 'root:%s\n%s:%s\n' "$ROOT_PASSWORD" "$MRRC_USER" "$USER_PASSWORD" | chpasswd

# ── 时区 ─────────────────────────────────────────────────────────────
# 写文件才是权威：chroot 里没有 D-Bus，timedatectl 是客户端、必然失败
# （与 hostname 同样的道理，见 mrrc-service-layer.sh）。
log "time zone: $TIMEZONE"
[ -f "/usr/share/zoneinfo/$TIMEZONE" ] || {
 echo "preflash: /usr/share/zoneinfo/$TIMEZONE is missing" >&2
 exit 1
}
ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
printf '%s\n' "$TIMEZONE" >/etc/timezone
timedatectl set-timezone "$TIMEZONE" 2>/dev/null || true

# ── 区域/语言 ────────────────────────────────────────────────────────
log "locale: $LOCALE_NAME"
[ -f /etc/locale.gen ] || {
 echo "preflash: /etc/locale.gen is missing (the locales package?)" >&2
 exit 1
}
# 基底可能带一份被裁过的 locale.gen（pi-gen 会裁）：缺就补一行，别失败 ——
# 这条脚本存在的理由是"装出能用的镜像"，不是"验基底的清单"。
if ! grep -q "^${LOCALE_NAME} " /etc/locale.gen; then
 if grep -q "^# *${LOCALE_NAME} " /etc/locale.gen; then
  sed -i "s|^# *${LOCALE_NAME} |${LOCALE_NAME} |" /etc/locale.gen
 else
  echo "preflash: appending ${LOCALE_NAME} to /etc/locale.gen (not present)"
  printf '%s UTF-8\n' "$LOCALE_NAME" >>/etc/locale.gen
 fi
fi
locale-gen
printf 'LANG=%s\n' "$LOCALE_NAME" >/etc/default/locale

# NTP：Armbian 默认已启用 systemd-timesyncd，这里只做显式声明；真判据在机器上
# （`timedatectl` 的 System clock synchronized）而不是构建期。
systemctl enable systemd-timesyncd.service 2>/dev/null || \
 echo "preflash: systemd-timesyncd was not enabled (check the base image)"

echo "preflash: done"
