# 从 Windows 电脑远控升级盒子（2026-10-25）

> 交付物：`dev_tools/mrrc-update.cmd`。设计同族：`dev_tools/mrrc-diag.cmd`（2026-10-24 取包）。
> 本文只记**为什么这么设计**；脚本本身是 ASCII 的，理由不能写在它里面（见 §5）。

## 1. 触发

一台盒子只在局域网里够得着：客户没有 SSH 能力、盒子上也没有桌面，页面能打开但
"点一下升级"这件事出了问题就没人能复现。取包脚本已经证明「Windows 电脑 + curl +
盒子自己的 API」这条路可行 —— 升级是同一个形状的另一半：**让盒子自己升**，电脑只替
用户点那一下。

这也解释了它**不做什么**：不传安装包、不替盒子下载、不在电脑上解压。包从更新通道
到盒子上，和用户在 `/settings` 里点是一样的路径。

## 2. 契约（四个端点，全部已存在）

| 步骤 | 调用 | 判据 |
| --- | --- | --- |
| 登录 | `POST /api/auth/login` `{password}` | 应答 `.ok == true`；cookie 落 `%TEMP%` |
| 查版本 | `GET /api/update/check` | `.current` / `.latest` / `.size` / `.notes` / `.available` |
| 动手 | `POST /api/update/apply` | `.ok == true`；否则 `.reason` 是**盒子给出的拒绝理由**（发射中/磁盘不足/已是最新/正在升级） |
| 跟随 | `GET /api/update/state` | `.state` ∈ `box_update.STATE_*`，`.detail` 给人看 |

状态机（`box_update.py`）：`idle → downloading → verifying → applying → restarting →
done | failed`。`done` 是**重启前**写的，所以脚本不能把 `done` 当成功 —— 真正的成功判据是
盒子回来之后再读一次 `check.current`，它必须等于目标版本。

## 3. 三条安全边界

1. **发射中不许升级**：这是盒子的 `_update_refusal()` 判的（PTT/TUNE/CQ/FT8 + 磁盘），
   脚本只转述。不要在客户端复制这个判断 —— 复制一份就多一处会漂的东西。
2. **失败要能自己退回去**：`box_update.apply()` 覆盖前先备份，重启后由看门狗探活，
   探不到就把备份翻回来。脚本对"回滚了"这件事**明说**（`After != Target` 时打印原因
   与日志位置），而不是报一句"升级失败"。
3. **口令不进 argv**：`--data-binary @file` + `%TEMP%` 下的一份 JSON，收尾时删除。
   这条路最可能在客户机器上跑，任务管理器与 `ps` 都看得见命令行。

## 4. 写这个工具时撞出来的缺陷（已修）

`box_update.watchdog_command()` 原来用 `curl -skf …/api/health` 判活。但 `/api/health`
**不是公开路径**（`PUBLIC_PATHS` 里只有 login / 静态件），盒子从回环问也要令牌 —— 它答
**401**，而 `-f` 把 401 当失败（实测退出码 22）。于是：新树已经起来、页面能用，看门狗 90 秒
后照样把备份翻回来 —— **每次自升级都静默回滚**。

判据改成"有没有 HTTP 应答"（`%{http_code}` != `000`）。这条规则仓库里已经写对过一次：
`launcher_net.answers()` 把 401 也算"服务在"，因为**探活问的是"这台机器还在不在"，
不是"我有没有权限"**。两个守卫：`test_box_update.py::…accepts_a_server_that_answers_401`
与 `test_box_update_cmd.py::test_a_dead_service_is_not_a_dead_network`。

## 5. 为什么脚本是纯 ASCII

cmd.exe 按**当前代码页**读批处理文件，不是按 UTF-8。中文注释在 936（中文 Windows 默认）
上有过把下一行吃掉的历史，而这类文件一坏就是"双击没反应"，最难远程排查。所以：

- 文件本身只有 ASCII（有测试盯着）；控制台上的中文（盒子的 `reason` / `detail`）靠
  `chcp 65001` + PowerShell 的 `[Console]::OutputEncoding` 显示；
- 走 PowerShell 5.1 就够（`ConvertFrom-Json`），不引入新依赖 —— 客户机器上能指望的
  只有 curl.exe（Win10 1809+ 自带）与 PowerShell。这与 `mrrc-diag.cmd` 一致。

## 6. 不做的

- **不自动升级**：没有任何定时器、也不会在检测到新版本时自己动手。升级会重启服务，
  那必须是有人按下的一次决定。
- **不上传任何东西**：结果只落在脚本旁边的 `mrrc-update-<电脑名>\`（版本、状态、
  原始应答），发不发由操作者决定。
- **不替代取包脚本**：升级失败要证据时，仍然用 `mrrc-diag.cmd` 取完整包（含日志）。
