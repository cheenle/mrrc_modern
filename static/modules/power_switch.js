// Radio power menu items — 「重开电台…」与「关机…」（设计 2026-10-16）。
//
// 服务端是唯一的执行点：守卫（TX / FT8 发射中拒绝、PS1 后 15 秒启动窗口拒绝 PS0、
// PS0 双发）与唤醒循环都在 server.py 的 _power_off() / _power_rescue() 里。这个模块
// 只做三件事：问一句、发一条、把状态画出来。
//
// 为什么是两项而不是一个开关：两种意图不可混淆 —— 「重开电台」关机后**会**持续尝试唤醒
// （有界、可取消），「关机」**绝不**自己上电。这正是 2026-07 那个一键开关被撤回之后
// 允许它回到界面上的前提。
(function () {
    'use strict';

    // 救援是否在进行中：决定顶栏那一格显示"唤醒中"还是"PWR 开/关"。
    let rescueActive = false;

    function send(field, value) {
        if (typeof sendCommand === 'function') {
            sendCommand(field, value);
        }
    }

    function askRestart() {
        const ok = window.confirm(
            '重开电台（关闭后自动尝试唤醒）：\n\n' +
            '1) 先关闭电台；\n' +
            '2) 之后每 30 秒再尝试唤醒一次，最多 15 分钟；\n' +
            '3) 期间远程会短暂不可用。\n\n' +
            '如果 15 分钟都没醒（实测开机命令本来就不可靠），需要有人到现场按电台面板。\n\n' +
            '继续吗？'
        );
        if (ok) {
            send('power', 'restart');
        }
    }

    function askOff() {
        const ok = window.confirm(
            '关闭电台（保持关闭）：\n\n' +
            '关掉之后不会自动唤醒 —— 远程将不可用，要再开机只能到现场按电台面板。\n\n' +
            '确定关机吗？'
        );
        if (ok) {
            send('power', false);
        }
    }

    function askCancel() {
        if (!rescueActive) {
            return;
        }
        if (window.confirm('停止正在进行的唤醒尝试？（电台保持关闭）')) {
            send('power', 'cancelRescue');
        }
    }

    // 顶栏那一格：平时显示 PWR 开/关（服务端 power_on 字段），
    // 救援期间显示第几次尝试，并且点一下就是"取消"。
    function renderPowerState(on) {
        const el = document.getElementById('status-power');
        if (!el || rescueActive) {
            return;
        }
        el.textContent = on ? 'PWR 开' : 'PWR 关';
        el.title = on
            ? '电台电源：开'
            : '电台电源：关（远程唤不醒时需要到现场按面板）';
    }

    function renderPowerRescue(msg) {
        const el = document.getElementById('status-power');
        const state = (msg && msg.state) || '';
        const attempt = (msg && msg.attempt) || 0;
        rescueActive = state === 'running';

        if (el) {
            if (rescueActive) {
                el.textContent = 'PWR 唤醒中 ' + attempt;
                el.title = '正在尝试唤醒电台（第 ' + attempt + ' 次）…点这里可以取消';
            } else if (state === 'woke') {
                el.textContent = 'PWR 开';
                el.title = '电台已开机';
            } else if (state === 'timeout' || state === 'cancelled') {
                el.textContent = 'PWR 关';
                el.title = '电台电源：关';
            }
        }

        if (typeof showToast !== 'function') {
            return;
        }
        if (state === 'running') {
            showToast('正在尝试唤醒电台（第 ' + attempt + ' 次）…点顶栏 PWR 可取消', 4000);
        } else if (state === 'woke') {
            showToast('电台已开机', 5000);
        } else if (state === 'timeout') {
            showToast('15 分钟内没唤醒：需要到现场按面板开机', 12000);
        } else if (state === 'cancelled') {
            showToast('已取消唤醒尝试（电台保持关闭）', 4000);
        }
    }

    function init() {
        document.querySelectorAll("[data-action='power-restart']").forEach(function (elm) {
            elm.addEventListener('click', function (e) {
                e.preventDefault();
                askRestart();
            });
        });
        document.querySelectorAll("[data-action='power-off']").forEach(function (elm) {
            elm.addEventListener('click', function (e) {
                e.preventDefault();
                askOff();
            });
        });
        const status = document.getElementById('status-power');
        if (status) {
            status.style.cursor = 'pointer';
            status.addEventListener('click', askCancel);
        }
    }

    // ft710_main.js 的 onmessage 直接调用（跨文件全局，与本仓 showToast 同一惯例）。
    window.renderPowerRescue = renderPowerRescue;
    window.renderPowerState = renderPowerState;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
