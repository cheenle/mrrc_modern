#!/usr/bin/env python3
"""FDE 看板运营决策页（需 Basic Auth，由 server.py 的 GET /board/ 提供）。

纯渲染器：不读文件、不联网，只把一份完整的 HTML/JS 文档作为字符串交回。
所有看板数据都走浏览器端的相对路径 fetch（../api/board/state 与
../api/board/decisions）：从公开入口 /mrrc_modern/support/board/ 打开时，
相对路径解析到 /mrrc_modern/support/api/...，nginx 再反代到本服务的
/api/...；写绝对 /api/... 会逃到站点根，是部署错误。

安全约定（与 server.py 的队列守卫对应，客户端再兜一层）：
  - 所有服务端字符串只经 textContent 进 DOM，绝不 innerHTML；
  - 排期/拒绝只对 backlog，退回待决策只对 scheduled/rejected；
  - 队列 pending 里的卡片禁用全部操作，避免重复提交；
  - 提交在途时禁用全部按钮，错误时保留备注输入；
  - 每次重建 DOM 前把 .note-input 草稿存入 noteDrafts（按卡片 id），重建后
    恢复——15 秒定时刷新和别卡的提交都不会 wipe 正在输入的备注；只有提交
    成功的那张卡清掉自己的草稿；
  - 页面必须以尾部斜杠访问（../api 的深度依赖当前路径），裸 /board 由
    同源 location.replace 补齐斜杠。
"""

# 七个看板状态与公开看板 support_board.COLUMNS 一一对应。
COLUMNS = (
    ("backlog", "待决策（新需求）"),
    ("scheduled", "已排期（bug 自动）"),
    ("in_progress", "实施中"),
    ("answered", "已答复（无需代码）"),
    ("done", "已提交（fde/ 分支）"),
    ("failed", "实施失败（已回滚）"),
    ("rejected", "不处理"),
)

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MRRC Modern · 看板运营决策</title>
<style>
body{background:#101018;color:#e2e2e8;font-family:-apple-system,tahoma,sans-serif;margin:0;padding:20px;max-width:1500px}
h1{font-size:21px;margin:0 0 4px}
.muted{color:#8a8a96;font-size:13px;margin:0 0 12px;line-height:1.7}
.muted a{color:#8fb7e0}
.status{min-height:18px;font-size:13px;color:#9a9aa8;margin:0 0 10px}
.status.error{color:#ff9db1}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
@media(max-width:640px){.board{grid-template-columns:1fr}}
.col{background:#17171f;border:1px solid #262633;border-radius:10px;padding:10px;min-height:120px}
.col h2{font-size:13px;margin:2px 4px 10px;color:#c9c9d4}
.col h2 .n{float:right;color:#6f6f80}
.card{background:#1e1e29;border:1px solid #2c2c3a;border-radius:8px;padding:9px;margin-bottom:10px;font-size:13px}
.card code{color:#9fb4c7;font-size:11px}
.tags{margin-bottom:6px}
.title{margin:2px 0 4px;line-height:1.5}
.hint,.result,.note,.branch{margin:4px 0 0;color:#9a9aa8;font-size:12px;line-height:1.5}
.result{color:#c7b98a}
.k,.s{display:inline-block;border-radius:4px;padding:0 6px;font-size:11px;margin-right:4px}
.k-bug{background:#4a2230;color:#ff9db1}.k-feature{background:#1f3a2c;color:#8fe3b0}
.k-noise{background:#2b2b36;color:#9a9aa8}.k-未分类{background:#2b2b36;color:#9a9aa8}
.s-high{background:#4a2230;color:#ff9db1}.s-med{background:#46361c;color:#f2ce7b}
.s-low{background:#1f3a2c;color:#8fe3b0}
.empty{color:#5c5c68;text-align:center}
.actions{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.actions input{flex:1 1 100%;background:#12121a;border:1px solid #2c2c3a;border-radius:6px;color:#e2e2e8;padding:5px 8px;font-size:12px}
.actions button{background:#26476b;color:#dcebff;border:0;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer}
.actions button:hover:not(:disabled){background:#315a86}
.actions button:disabled{opacity:.45;cursor:default}
.pending-note{margin:6px 0 0;color:#f2ce7b;font-size:12px}
</style>
</head>
<body>
<h1>🐞 FDE 看板 · 运营决策</h1>
<p class="muted">本页需口令；公开只读看板在 <a href="../../board/">/mrrc_modern/board/</a>，答复页在 <a href="../../answers/">/mrrc_modern/answers/</a>。
排期 / 不处理 / 退回待决策的决策先入服务器队列，由 Mac 每分钟拉取、应用到权威看板状态后再确认删除；已提交未应用的卡片会锁定其操作按钮。
状态自动刷新（15 秒）。</p><div id="status" class="status" role="status" aria-live="polite"></div>
<div id="error" class="status" role="alert" aria-live="assertive" hidden></div>
<div id="board" class="board"></div>
<script>
"use strict";

// 必须以尾部斜杠访问：页面里的相对 API 路径 ../api/... 依赖当前路径的深度。
// 裸 /board（少一层）会让 ../api 逃到站点根；这里用同源重定向补齐斜杠。
if (!location.pathname.endsWith("/")) {
  location.replace(location.pathname + "/" + location.search + location.hash);
}

var COLUMNS = [
  ["backlog", "待决策（新需求）"],
  ["scheduled", "已排期（bug 自动）"],
  ["in_progress", "实施中"],
  ["answered", "已答复（无需代码）"],
  ["done", "已提交（fde/ 分支）"],
  ["failed", "实施失败（已回滚）"],
  ["rejected", "不处理"]
];
var ACTION_LABELS = {
  schedule: "排期",
  reject: "不处理",
  backlog: "退回待决策"
};
var REFRESH_MS = 15000;

var submitting = false;
var pendingIds = new Set();
var noteDrafts = new Map();
var lastData = null;

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null && text !== "") {
    node.textContent = String(text);
  }
  return node;
}

function setStatus(text) {
  document.getElementById("status").textContent = text || "";
}

function setError(text) {
  var area = document.getElementById("error");
  area.textContent = text || "";
  area.hidden = !text;
}

async function api(path, options) {
  var init = {credentials: "same-origin"};
  if (options) {
    for (var key in options) {
      if (Object.prototype.hasOwnProperty.call(options, key)) {
        init[key] = options[key];
      }
    }
  }
  var response = await fetch(path, init);
  var body = await response.json().catch(function () { return {}; });
  if (!response.ok) {
    throw new Error(body && body.reason ? body.reason : "HTTP " + response.status);
  }
  return body;
}

function submitDecision(bundleId, action, note) {
  return api("../api/board/decisions", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-MRRC-Decision": "1"},
    body: JSON.stringify({bundle_id: bundleId, action: action, note: note})
  });
}

function actionsFor(status) {
  if (status === "backlog") {
    return ["schedule", "reject"];
  }
  if (status === "scheduled" || status === "rejected") {
    return ["backlog"];
  }
  return [];
}

function addField(card, className, label, value) {
  if (!value) {
    return;
  }
  var p = el("p", className);
  if (label) {
    p.appendChild(el("span", "label", label));
  }
  p.appendChild(document.createTextNode(String(value)));
  card.appendChild(p);
}

function snapshotDrafts() {
  var inputs = document.querySelectorAll(".note-input");
  for (var i = 0; i < inputs.length; i++) {
    var card = inputs[i].closest(".card");
    if (!card) {
      continue;
    }
    if (inputs[i].value) {
      noteDrafts.set(card.id, inputs[i].value);
    } else {
      noteDrafts.delete(card.id);
    }
  }
}

function buildCard(bundleId, entry) {
  var card = el("div", "card");
  card.id = bundleId;
  var tags = el("div", "tags");
  tags.appendChild(el("span", "k k-" + (entry.kind || ""), entry.kind || "未分类"));
  if (entry.severity) {
    tags.appendChild(el("span", "s s-" + entry.severity, entry.severity));
  }
  tags.appendChild(el("code", "", bundleId));
  tags.appendChild(el("span", "t", entry.updated || ""));
  card.appendChild(tags);
  addField(card, "title", "", entry.title);
  addField(card, "hint", "线索：", entry.code_hint);
  addField(card, "result", "结果：", entry.result);
  if (entry.branch) {
    var ref = entry.branch;
    if (entry.commit) {
      ref = entry.branch + "@" + String(entry.commit).slice(0, 8);
    }
    addField(card, "branch", "分支：", ref);
  }
  addField(card, "note", "决策备注：", entry.note);
  var actions = actionsFor(entry.status);
  var isPending = pendingIds.has(bundleId);
  if (actions.length) {
    var row = el("div", "actions");
    var input = el("input", "note-input");
    input.type = "text";
    input.maxLength = 200;
    input.placeholder = "决策备注（可选，≤200 字）";
    if (noteDrafts.has(bundleId)) {
      input.value = noteDrafts.get(bundleId);
    }
    row.appendChild(input);
    actions.forEach(function (action) {
      var btn = el("button", "", ACTION_LABELS[action]);
      btn.type = "button";
      btn.disabled = isPending || submitting;
      btn.addEventListener("click", function () {
        onDecide(bundleId, action, input);
      });
      row.appendChild(btn);
    });
    card.appendChild(row);
  }
  if (isPending) {
    card.appendChild(el("p", "pending-note", "已提交，等待 Mac 应用"));
  }
  return card;
}

function render(data) {
  lastData = data;
  snapshotDrafts();
  var board = document.getElementById("board");
  while (board.firstChild) {
    board.removeChild(board.firstChild);
  }
  var entries = data.board || {};
  COLUMNS.forEach(function (col) {
    var status = col[0];
    var column = el("section", "col");
    column.id = status;
    var head = el("h2", "", col[1] + " ");
    head.appendChild(el("span", "n", String(countStatus(entries, status))));
    column.appendChild(head);
    var ids = Object.keys(entries).filter(function (id) {
      var e = entries[id];
      return e && e.status === status;
    }).sort(function (a, b) {
      return String(entries[b].updated || "").localeCompare(
        String(entries[a].updated || ""));
    });
    if (!ids.length) {
      column.appendChild(el("p", "empty", "—"));
    }
    ids.forEach(function (id) {
      column.appendChild(buildCard(id, entries[id]));
    });
    board.appendChild(column);
  });
}

function countStatus(entries, status) {
  return Object.keys(entries).filter(function (id) {
    var e = entries[id];
    return e && e.status === status;
  }).length;
}

async function refresh() {
  try {
    var data = await api("../api/board/state");
    pendingIds = new Set((data.pending || []).map(function (d) {
      return d && d.bundle_id;
    }));
    render(data);
    setError("");
    setStatus("已更新 " + new Date().toLocaleString());
  } catch (err) {
    setError("刷新失败：" + (err && err.message ? err.message : String(err)));
  }
}

function setAllButtonsDisabled(disabled) {
  var buttons = document.querySelectorAll(".actions button");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].disabled = disabled || pendingIds.has(buttons[i].closest(".card").id);
  }
}

async function onDecide(bundleId, action, input) {
  if (submitting) {
    return;
  }
  // 按码点截断（而非 UTF-16 单元），避免在 200 边界劈开代理对
  var note = Array.from(input.value).slice(0, 200).join("");
  submitting = true;
  setAllButtonsDisabled(true);
  setError("");
  setStatus("正在提交 " + bundleId + "（" + ACTION_LABELS[action] + "）…");
  try {
    await submitDecision(bundleId, action, note);
    pendingIds.add(bundleId);
    noteDrafts.delete(bundleId);
    input.value = "";
    setStatus("已提交 " + bundleId + "（" + ACTION_LABELS[action] +
              "），等待 Mac 应用。");
    render(lastData);
    await refresh();
  } catch (err) {
    setStatus("");
    setError("提交失败：" + (err && err.message ? err.message : String(err)) +
             "——备注已保留，可重试。");
  } finally {
    submitting = false;
    setAllButtonsDisabled(false);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""


def render_board_admin() -> str:
    """返回完整的运营决策页 HTML（纯函数，无 I/O）。"""
    return PAGE
