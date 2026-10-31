# Support 看板 Web 决策设计

日期：2026-11-01

## 1. 目标与边界

在现有 MRRC Modern Support 后台增加带认证的看板决策页面，让操作员可以在浏览器中对「待决策」条目执行排期、不处理和退回待决策，并填写决策备注。

公开页面 `https://www.vlsc.net/mrrc_modern/board/` 保持无需密码、只读。运营页面使用 `https://www.vlsc.net/mrrc_modern/support/board/`，沿用 Support 的 Basic Auth 用户名和密码。

网页提交后立即显示「等待 Mac 应用」。Mac 每分钟同步一次决策，正式修改看板状态并重新发布。服务器不执行 shell、不通过 SSH 控制 Mac、不直接操作 Git。

本次不改动自动分类、答复发布和无人值守实施的规则，不为看板增加新的用户体系。

## 2. 架构与数据流

现有 Support receiver 是服务器上唯一需要增加的 Web 入口；现有 `support_board.py` 和 Mac 上的 `board_state.json` 继续是看板状态与状态迁移的唯一权威。

```text
操作员浏览器
  → Support receiver（Basic Auth、校验、持久化决策队列）
  → 立即返回 pending
  → Mac support_autopilot 每分钟拉取
  → board.decide() 修改本地 board_state.json
  → 重建并发布公开看板 + 同步看板状态镜像
  → 确认并删除服务器队列项
  → 运营页面显示最新状态
```

每次发布看板时，Mac 同时将 `board_state.json` 发布到服务器的 Support 私有存储 `/var/www/support-modern/board_state.json`。该文件只是运营页面使用的只读镜像，不是第二份可写状态。

## 3. 服务器组件

改动集中在 `tools/support_receiver/server.py`，继续使用标准库和现有 systemd 服务。

### 3.1 页面与接口

所有以下路径都要求 Basic Auth；经 nginx 映射后，括号内为公网路径：

- `GET /board/`（`/mrrc_modern/support/board/`）：运营决策页面。
- `GET /api/board/state`：返回看板镜像和当前待处理决策。
- `POST /api/board/decisions`：创建决策。
- `GET /api/board/decisions`：Mac 拉取待处理决策，按创建时间排序。
- `DELETE /api/board/decisions/<decision_id>`：Mac 确认一项已处理，删除队列项。

创建决策只接受 JSON：

```json
{
  "bundle_id": "20261024-120317-2e05",
  "action": "schedule",
  "note": "确认要做，安排处理"
}
```

动作只允许 `schedule`、`reject`、`backlog`。编号继续使用现有 `ID_RE`，备注按 UTF-8 字符数限制为 200。服务器按动作核对镜像中的当前状态：`schedule`/`reject` 只允许从 `backlog` 发起，`backlog` 只允许从 `scheduled`/`rejected` 发起；其他迁移一律返回 409，避免覆盖实施中或已完成的条目。

成功创建返回 HTTP 201 和：

```json
{"ok": true, "decision_id": "<uuid>", "status": "pending"}
```

### 3.2 队列存储

每项决策保存为 `/var/www/support-modern/board-decisions/<uuid>.json`：

```json
{
  "decision_id": "<uuid>",
  "bundle_id": "20261024-120317-2e05",
  "action": "schedule",
  "note": "确认要做，安排处理",
  "created_at": "2026-11-01T12:00:00Z"
}
```

写入使用同目录临时文件加 `os.replace()`。队列文件存在即表示待处理；Mac 确认后删除。服务重启不会丢失待处理决策。读取时忽略临时文件和不符合 UUID 白名单的文件名。

### 3.3 运营页面

运营页面是 receiver 返回的轻量 HTML/JavaScript 页面，通过 `/api/board/state` 获取数据，不直接读取磁盘路径或暴露文件内容。

- 展示现有各状态列和卡片；没有待处理决策的 `backlog` 卡片显示备注输入和「排期」「不处理」，`scheduled`/`rejected` 卡片显示备注输入和「退回待决策」。
- 其他状态只读；同一卡片已有待处理决策时禁用所有动作。
- 提交后禁用该卡片按钮并显示「已提交，等待 Mac 应用」。
- 所有服务端文本通过 `textContent` 写入 DOM，不拼接为 HTML。
- 页面定时刷新状态；提交失败保留备注并显示错误，不假装成功。

公开看板页头增加相对链接 `../support/board/`，文字为「运营决策」。

## 4. Mac 同步组件

在 `dev_tools/support_autopilot.py` 增加：

- `list_board_decisions()`：读取并再次校验服务器返回的队列项。
- `ack_board_decision(decision_id)`：确认队列项。
- `sync_board_decisions()`：应用一轮决策并返回处理结果。
- `--sync-decisions`：只同步决策，不下载诊断包、不调用模型、不进入无人值守实施。

处理每项决策时重新加载本地看板状态，并执行与服务器相同的迁移校验：`schedule`/`reject` 仅接受 `backlog`，`backlog` 仅接受 `scheduled`/`rejected`。

- 当前状态允许该动作：调用现有 `board.decide()`，保存状态。
- 当前状态不允许该动作：不覆盖既有状态，记录为过期决策并确认移除。
- 条目不存在或请求字段不合法：记录拒绝原因并确认移除，避免坏请求永久阻塞队列。

只有成功改变至少一项本地状态时才重建公开看板。看板发布成功后确认对应队列项；保存或发布失败则不确认，保留下轮重试。若一项应用失败，继续处理其他项，单项失败不终止整轮。

每次 `rebuild_board_page()` 都同时发布状态镜像。公开页发布和镜像发布分别记录结果，任一失败都使本轮相关决策保持未确认。

同步入口与现有 `--once` 共用单飞锁，防止两个进程同时读写状态。`--once` 也先执行一次决策同步。crontab 保留现有每 10 分钟分析任务，并增加每分钟 `--sync-decisions` 任务；安装和卸载命令同时管理两条任务。

现有 `python3 dev_tools/fde_board.py decide ...` 保留，行为不变。

## 5. 安全与一致性

- 运营页面、状态读取、决策创建、拉取和确认全部要求 Basic Auth；公开看板不需要认证。
- 创建决策必须带 `Content-Type: application/json` 和自定义头 `X-MRRC-Decision: 1`，其他形式拒绝，降低 Basic Auth 下的跨站请求风险。
- 服务器与 Mac 两端都校验编号、动作、备注和决策 UUID；路径只由白名单值组成。
- Mac 不信任服务器镜像或队列中的状态迁移结果，最终仍只调用 `board.decide()`。
- 不在 URL 中携带密码、决策内容或会话令牌。
- 决策不进入自动实施队列之外的任何执行通道；服务器没有 shell、SSH 或 Git 权限。
- 应用在 Mac 本地状态保存和公开页/镜像发布之后才确认，因此崩溃最多造成同一决策重放。重放时状态已变化，会按过期决策移除，不会重复触发实施。
- 决策同步仍受现有自动实施规则约束：只有 `scheduled` 条目会进入实施，且每轮单飞、全量测试通过才提交。

## 6. 错误处理

- 未认证：401，并返回现有 `WWW-Authenticate`。
- 非法编号、动作、备注、请求头或 JSON：400，不创建文件。
- 条目不存在或不是 `backlog`：409，网页刷新状态。
- 镜像尚不存在：运营页面明确显示「看板状态尚未同步」，决策接口拒绝创建。
- 队列读写失败：500，网页保留输入；Mac 记录错误并保留未确认项。
- 公开页或镜像发布失败：不确认相关队列项，下一轮重试。
- 服务器返回格式损坏：Mac 拒绝整轮并记录错误，不部分猜测执行。

## 7. 测试与验收

### 7.1 自动化测试

扩展现有测试文件，不新建平行测试体系：

- `tests/test_support_receiver.py`：真实本地 HTTP 往返，覆盖认证、运营页、非法请求、创建/读取/确认队列、重启后队列保留和跨站请求头守卫。
- `tests/test_support_board.py`：公开页仍只读，并包含运营入口。
- `tests/test_support_autopilot.py`：覆盖应用三种动作、过期与非法项、发布失败不确认、单项失败不阻塞、状态镜像发布、单飞锁和 cron 安装/卸载。

本地端到端验收使用临时目录和真实 receiver：写入一张测试卡片 → 网页 API 提交排期 → Mac 同步 → 本地状态与渲染页变为已排期 → 队列为空。不得用真实用户卡片做试验性排期。

### 7.2 部署验收

1. 运行完整 `python3 -m unittest discover -s tests -v`。
2. 用现有 `deploy_support_receiver.sh` 更新服务器，安装每分钟同步 cron。
3. 未登录访问运营页和决策接口均为 401；使用现有 Support 凭证可读取运营页和状态。
4. 线上只执行非破坏探针，不擅自改变真实卡片排期。
5. 浏览器最终由操作员对真实待决策卡片做一次自愿决策，确认 pending → 正式状态的完整链路。

## 8. 文档同步

实现后同步 `AGENTS.md`、`docs/FDE.md`、`README.md`、`tests/README.md` 和相关 SDD 章节，明确公开看板只读、Support 运营页可决策、Mac 为状态权威及队列确认规则。
