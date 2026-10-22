# 盒子线 Fix Pack（FP）编号设计

- **日期**：2026-10-22
- **状态**：设计定稿，随 v1.28.5 · FP1 实现
- **范围**：**只有盒子线**（`latest.json` 的 `boxLine` → `mrrc-modern-src-*.tar.gz`）。
  桌面整包线与静态补丁线本次不动。
- **相关代码**：`upgrade_core.py`（清单解析与比较）、`box_update.py`（设备端施加）、
  `server.py`（`/api/update/check`）、`static/settings.html`（更新卡）、
  `dev_tools/make_box_source.py`、`dev_tools/make_latest_json.py`
- **相关记录**：CHANGELOG 的 `[镜像线 …]` 条目族；SDD/14 本版条目

---

## 1. 背景：现场那个「沉默的 502」

2026-10-22，一台已发货盒子的公网入口连续四天 502，而它自己的设置页写着「已接通」。
根因是两处沉默叠加（详见 SDD V3.05）：`_cloud_start_tunnel()` 在缺 `fleet/frpc` 或
隧道配置时静默 `return`，而设置页只在 `tunnel_error` 非空时才提隧道。修复本身很小，
但它必须**送到现场那台盒子上**才有意义。

通道现状（2026-10-22 实测线上）：

| 通道 | 载体 | 能改 `server.py` | 线上 |
| --- | --- | --- | --- |
| 桌面整包 | `latest.json` → `installer` | ✅（要重建安装包） | `1.28.2` |
| **盒子源码树** | `latest.json` → `boxLine` → `mrrc-modern-src-*.tar.gz` | ✅ | `1.28.4` |
| 静态补丁 | `patches/patch.json` + zip | ❌ 只覆盖 `_internal/static/**` | 只有 `1.24.7-p1` |

⇒ 本次修复（`server.py` + `static/settings.html`）**只有盒子源码树这条通道能承载**。

## 2. 目标 / 非目标

**目标**

1. 盒子线的每一次「修复交付」有一个**人可以引用的编号**（`FP1`、`FP2`…），
   出现在页脚、更新卡、诊断包与清单里 —— 售后一句「你装的是哪个 FP」就能定位。
2. 已经装在现场的盒子（解析器只认三段版本号）**必须看得见**这次交付。
3. 不引入第二套版本真相：谁比谁新，仍然只由版本号决定。

**非目标**

- 不给桌面线加 FP（桌面要重建安装包，语义由正式版本号承担）。
- 不做「同一版本内的多次热修」（FP 与版本号一起前进，见 §3）。
- 不做自动升级：仍然由用户在「软件更新」卡上点一下。

## 3. 编号语义

- **FP 是盒子线的修复包序号，全局递增**（`FP1` → `FP2` → …），不随版本清零。
  选全局递增而非「每版从 FP1 起」：同一个数字在全生命周期里指认一次交付，
  售后对话里不会出现两个「FP1」。
- **FP 不参与版本比较**：`boxLine.version` 保持三段（`1.28.5`），`compare_versions()`
  不动。原因是一条已实测的硬约束：现场盒子跑的解析器是 `^v?(\d+)\.(\d+)\.(\d+)`，
  `1.28.2.1` 在它眼里等于 `1.28.2` ⇒ 判定「已是最新」⇒ **等于没发**。
- 每发一次盒子修复包：版本号 +1（三段），FP 号 +1。两者一起前进，职责不同：
  版本号回答「谁比谁新」，FP 回答「这是第几次修复」。

## 4. 清单契约（向后兼容）

```json
"boxLine": {
  "version": "1.28.5",
  "fixpack": "FP1",
  "url": "…/mrrc-modern-src-1.28.5.tar.gz",
  "sha256": "…", "size": 18640000,
  "notes": "FP1：…"
}
```

- `fixpack` 是**新键**。既有的 `parse_manifest()` 会忽略未知键 —— 这正是 `boxLine`
  当初被引入的方式（`upgrade_core.py` 里那条注释写明了这个策略），所以：
  **老盒子读新清单不会报错**，只是不显示 FP；它看到的 `version` 仍是三段，照常判断有没有新版。
- `parse_manifest()` 在 `box` 段额外规范化 `fixpack`：缺省 `""`，非法值（不是 `FP<数字>`）
  → `ManifestError`（宁可拒读，也不显示一个错编号）。
- `check_box()` 返回值新增 `current_fixpack` / `latest_fixpack`；**`available` 的判定与
  有无 `fixpack` 完全无关**（这条由测试钉死，防止将来有人「顺手」拿它比较）。

## 5. 设备端

- tar 包根多一个 `fixpack.txt`（一行 `FP1`）。`stage_over()` 照常复制（不在 `KEEP` 里）。
- `box_update.write_fixpack(home, "FP1")` 写完 `version.txt`/`VERSION` 后写；**空值删除文件**
  —— 否则换一个没有 FP 的包之后，页面还在报上一代的编号。
- `apply()` 的取值顺序：包内 `fixpack.txt` → 清单 `box.fixpack` → 空。
- `server.py` 的 `GET /api/update/check` 增加 `current_fixpack`（读本机 `fixpack.txt`）
  与 `latest_fixpack`（读清单）。
- 「软件更新」卡与 ☰ 页脚显示 `1.28.5 · FP1`；没有 FP 时**不留尾巴**（不显示 ` · `）。
- 诊断包：`support_bundle` 的版本行同时给出 fixpack（下一次报告自带答案）。

## 6. 记录（机器可读的标题）

`CHANGELOG.md` 新增（放在**最上面**，`make_box_source.version_of()` 取文件里第一个
`[镜像线 …]` 标题）：

```
## [镜像线 v1.28.5 · FP1] — 2026-10-22 — 隧道没起来时不再沉默 …
```

`dev_tools/make_box_source.py:version_of()` 的正则改为

```python
r"##\s*\[镜像线 v(\d+\.\d+\.\d+)(?:\s*·\s*(FP\d+))?\]"
```

FP 组可选 ⇒ 旧的 `## [镜像线 v1.28.4]` 仍然解析。桌面版本权威
（`release-artifacts.json` 的 `app_version_source`，`^## \[(v?[0-9]+\.[0-9]+\.[0-9]+)\]`）
不会被这个标题命中（`镜像线` 挡在版本号前面），所以 `MRRC.iss`、站点下载卡不受影响。

## 7. 构建链

- `dev_tools/make_box_source.py --fixpack FP1`：把 `fixpack.txt` 写进 staged 树
  （因此也在 tar 内），并在输出的 `box` 片段里带 `"fixpack": "FP1"`。
- `dev_tools/make_latest_json.py --box V V URL FILE --box-fixpack FP1`（可选参数）。
- `release_check.py` 新增一条守卫：`latest.json` 的 `boxLine.version` 必须等于
  CHANGELOG 里最新的 `[镜像线 vX.Y.Z]`（今天这条对齐全靠人；FP 组同样核对）。

## 8. 兼容矩阵

| 盒子解析器 | 清单带 `fixpack` | 结果 |
| --- | --- | --- |
| 旧（三段） | 是 | 正常判断有无新版；不显示 FP；不报错 |
| 旧（三段） | 否 | 与今天完全一致 |
| 新（本次起） | 是 | 判断有无新版 + 显示 `版本 · FPn` |
| 新 | 否 | 显示版本，无尾巴 |

## 9. 验收标准

1. `parse_manifest()` 接受合法 `fixpack`、拒绝 `fix1`/空 `version` 等非法组合；缺省为 `""`。
2. `check_box()` 在 `fixpack` 有无两种清单下 `available` 完全相同；`latest_fixpack` 透出。
3. `box_update.write_fixpack()` 写入 `FP1\n`；传空字符串时文件消失。
4. `make_box_source.py --fixpack FP1` 产出的 tar 内 `*/fixpack.txt == "FP1\n"`，且 stdout
   的 `box` 片段含 `"fixpack": "FP1"`；不带该参数时 tar 内没有这个文件、片段里也没有该键。
5. `make_latest_json.py --box … --box-fixpack FP1` 写进 `boxLine.fixpack`；`--box-fixpack`
   值非法时整体拒绝（不落盘）。
6. `GET /api/update/check` 返回 `current_fixpack` / `latest_fixpack`；更新卡渲染
   `当前 1.28.5 · FP1 → 可升级到 1.28.6 · FP2`，没有 FP 时为 `当前 1.28.5 → 可升级到 1.28.6`。
7. 全套件绿；`release_check.py` 离线 `32 ok / 0 failing`（新规则计入）。

## 10. 风险与不做

- **FP 容易被误当版本**：文档与页面统一写 `版本 · FPn`，清单里 FP 与 version 并列而不是拼成字符串。
- **FP 号可能断号**（同一个 FP 编号被撤回后重发）：允许断号，不允许复用 —— 复用一个编号等于
  让两份不同字节的包共用一个名字。
- **不做的**：桌面静态补丁带 FP；`patch.json` 与 FP 打通；自动升级。
