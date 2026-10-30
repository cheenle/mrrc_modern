/* AI 助手面板（规格 docs/superpowers/specs/2026-10-30-ai-assistant-design.md §10）。
 *
 * 这一层只做四件事：把问答画成气泡、把出处与「待确认」徽标摆出来、把降级翻成人话、
 * 提供「清空会话」。**没有任何知识**——知识库在中央，断网就是断网。
 *
 * 会话 id 只放内存：刷新页面就是新会话，不在浏览器里留痕。
 */
(() => {
    var state = { conversationId: null, busy: false };

    function byId(id) {
        return document.getElementById(id);
    }

    function bubble(kind, text) {
        var el = document.createElement('div');
        el.className = 'bubble ' + kind;
        el.textContent = text;
        byId('chat').appendChild(el);
        el.scrollIntoView({ block: 'end' });
        return el;
    }

    function renderAnswer(payload) {
        var badge;
        var a;
        var tip;
        var el = bubble('ai' + (payload.degraded ? ' warn' : ''), payload.answer || '（没有内容）');
        if (payload.needs_confirm) {
            badge = document.createElement('div');
            badge.className = 'badge';
            badge.textContent =
                payload.degraded === 'offline'
                    ? '离线：需要联网才能回答'
                    : 'AI 生成 · 待确认，请核对出处';
            el.appendChild(badge);
        }
        (payload.citations || []).forEach((cite) => {
            var line = document.createElement('div');
            line.className = 'cite';
            if (cite.url) {
                a = document.createElement('a');
                a.href = cite.url;
                a.target = '_blank';
                a.rel = 'noopener';
                a.textContent = '出处：' + cite.label;
                line.appendChild(a);
            } else {
                line.textContent = '出处：' + cite.label;
            }
            el.appendChild(line);
        });
        if (payload.degraded === 'offline') {
            tip = document.createElement('div');
            tip.className = 'cite';
            tip.textContent = '助手需要联网；本机不留知识库。';
            el.appendChild(tip);
        }
    }

    function setStatus(text) {
        byId('status').textContent = text || '';
    }

    function send() {
        if (state.busy) {
            return;
        }
        var text = byId('ask').value.trim();
        if (!text) {
            return;
        }
        state.busy = true;
        byId('send').disabled = true;
        byId('ask').value = '';
        bubble('me', text);
        setStatus('思考中…');
        fetch('/api/assistant/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ conversation_id: state.conversationId, message: text }),
        })
            .then((r) => r.json())
            .then((payload) => {
                if (payload && payload.conversation_id) {
                    state.conversationId = payload.conversation_id;
                }
                renderAnswer(payload || { answer: '助手没有回应。', degraded: 'offline' });
            })
            .catch(() => {
                renderAnswer({ answer: '助手暂时不可用（网络或服务端）。', degraded: 'offline' });
            })
            .then(() => {
                state.busy = false;
                byId('send').disabled = false;
                setStatus('');
            });
    }

    function clear() {
        fetch('/api/assistant/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ conversation_id: state.conversationId }),
        })
            .catch(() => {})
            .then(() => {
                state.conversationId = null;
                byId('chat').textContent = '';
                setStatus('已清空');
            });
    }

    function loadMeta() {
        fetch('/api/assistant/status', { credentials: 'same-origin' })
            .then((r) => {
                if (r.status === 404) {
                    byId('meta-body').textContent = '助手未开通。';
                    byId('send').disabled = true;
                    return null;
                }
                return r.json();
            })
            .then((info) => {
                if (!info) {
                    return;
                }
                if (info.endpoint === false) {
                    byId('send').disabled = true;
                    byId('meta-body').textContent =
                        '尚未开通：维护者还没有配置知识服务地址。';
                    setStatus('未开通');
                    return;
                }
                byId('meta-body').textContent =
                    '型号、版本、修复包编号、串口/声卡是否打开、最近 3 条错误码（已去掉路径与呼号）。' +
                    (info.endpoint ? '' : ' 网关未配置：回答将只有离线提示。');
            })
            .catch(() => {
                byId('meta-body').textContent = '读不到状态（可能是登录过期）。';
            });
    }

    byId('send').addEventListener('click', send);
    byId('clear').addEventListener('click', clear);
    byId('ask').addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
            send();
        }
    });
    loadMeta();
})();
