#!/usr/bin/env python3
"""中央网关入口：装配配置、起服务、定期清过期会话。

    MRRC_ASSISTANT_MODEL_URL=https://api.deepseek.com/chat/completions \
    MRRC_ASSISTANT_MODEL=deepseek-chat \
    MRRC_ASSISTANT_API_KEY=sk-… \
    MRRC_ASSISTANT_INSTANCES=tok1:inst-1,tok2:inst-2 \
    python3 -m tools.assistant_gateway

三件事这里做、别处不做：

1. **实例 token 表**是唯一鉴权（盒子只带匿名 id，永不带呼号）；
2. **保留期清理**每 6 小时跑一次（180 天硬删）；
3. 缺配置或知识库读不了时**拒绝启动**并说清缺什么 —— 一个半配好的网关只会让每台盒子
   都收到 500，那比不启动更难查。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from tools.assistant_gateway.engine import Engine
from tools.assistant_gateway.http_server import make_server
from tools.assistant_gateway.model import ModelClient
from tools.assistant_gateway.store import Store

logger = logging.getLogger("mrrc.assistant")

PURGE_INTERVAL_S = 6 * 3600
DEFAULT_QUOTA = 20


class ConfigError(RuntimeError):
    """配置不全/不合法 —— `main()` 把它变成一句人话后退出。"""


def parse_instances(raw: str) -> dict:
    """`tok1:inst-1,tok2:inst-2` → {token: instance_id}。空项跳过，重复 token 拒绝。"""
    table: dict = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        token, _, instance = chunk.partition(":")
        if not token or not instance:
            raise ConfigError(f"实例表格式不对：{chunk!r}（应为 token:实例id）")
        if token in table:
            raise ConfigError(f"实例 token 重复：{token}")
        table[token] = instance
    return table


def _load_kb(path: Path) -> list:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"知识库读不了：{path}（{exc}）") from exc
    entries = blob.get("entries") if isinstance(blob, dict) else None
    if not entries:
        raise ConfigError(f"知识库里没有条目：{path}")
    return entries


def _listen(raw: str) -> tuple:
    host, _, port = (raw or "127.0.0.1:8790").partition(":")
    try:
        return host or "127.0.0.1", port
    except Exception as exc:                     # pragma: no cover - 防御
        raise ConfigError(f"监听地址不合法：{raw!r}") from exc


def build(env=None):
    env = os.environ if env is None else env
    missing = [k for k in ("MRRC_ASSISTANT_MODEL_URL", "MRRC_ASSISTANT_MODEL",
                           "MRRC_ASSISTANT_API_KEY", "MRRC_ASSISTANT_INSTANCES")
               if not env.get(k)]
    if missing:
        raise ConfigError("缺少配置：" + ", ".join(missing))
    kb_path = Path(env.get("MRRC_ASSISTANT_KB") or Path(__file__).with_name("assistant_kb.json"))
    entries = _load_kb(kb_path)
    store = Store(Path(env.get("MRRC_ASSISTANT_DB") or "assistant.db"))
    quota = DEFAULT_QUOTA
    if env.get("MRRC_ASSISTANT_QUOTA"):
        try:
            quota = int(env["MRRC_ASSISTANT_QUOTA"])
        except ValueError as exc:
            raise ConfigError(f"MRRC_ASSISTANT_QUOTA 不是整数：{env['MRRC_ASSISTANT_QUOTA']!r}") from exc
    model = ModelClient(env["MRRC_ASSISTANT_MODEL_URL"], env["MRRC_ASSISTANT_MODEL"],
                               env["MRRC_ASSISTANT_API_KEY"])
    engine = Engine(entries=entries, model=model, quota_per_day=quota,
                            misses_so_far=lambda: 0)
    return engine, store, parse_instances(env["MRRC_ASSISTANT_INSTANCES"])


def _purge_loop(store: Store) -> None:
    while True:
        time.sleep(PURGE_INTERVAL_S)
        removed = store.purge_expired()
        if removed:
            logger.info("保留期清理：删除 %d 个会话", removed)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        engine, store, instances = build()
        host, port_text = _listen(os.environ.get("MRRC_ASSISTANT_LISTEN", ""))
        port = int(port_text)
    except (ConfigError, ValueError) as exc:
        print(f"assistant gateway 拒绝启动：{exc}", file=sys.stderr)
        return 2
    threading.Thread(target=_purge_loop, args=(store,), daemon=True).start()
    store.purge_expired()
    server = make_server(host=host, port=port, engine=engine,
                         store=store, instances=instances)
    logger.info("assistant gateway on %s:%s (%d 个实例)", host, port, len(instances))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
