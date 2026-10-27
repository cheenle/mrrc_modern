/* Shared client for /setup and /settings.
 *
 * One backend, two pages: this file is where the two agree about how a request
 * is made, how an error is rendered, and — the part that is expensive to get
 * wrong twice — how a page behaves when the box's own network is about to move
 * under it.
 *
 * A plain script, no modules and no build step, exactly like listen.js: the
 * pages are served straight off disk by the same FileResponse route.
 */
(function (global) {
  'use strict';

  // Deliberately not httpOnly — the WebSocket client reads it, so the page can
  // read it too and send it explicitly on fetch().
  var AUTH_COOKIE = 'mrrc_auth';

  function readCookie(name) {
    var match = document.cookie.match(
      new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  function authHeaders(extra) {
    var out = extra || {};
    var token = readCookie(AUTH_COOKIE);
    if (token) out['Authorization'] = 'Bearer ' + token;
    return out;
  }

  /* Resolves with the parsed body on 2xx; rejects with an Error whose `message`
   * is the server's own wording. A page never invents an error message and
   * never parses one — `error.status` is there for the two cases that need
   * different behaviour (401 → go to /login, 409 → something is already
   * running).
   *
   * `cache: 'no-store'` on every call: these pages report live network state,
   * and a cached /api/box/status would show an operator the network the box was
   * on before they changed it. */
  function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      cache: 'no-store',
      headers: authHeaders(opts.body ? { 'Content-Type': 'application/json' } : null)
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    return fetch(path, init).then(function (response) {
      return response.json().catch(function () { return {}; })
        .then(function (payload) {
          if (!response.ok) {
            var error = new Error(payload.error || ('HTTP ' + response.status));
            error.status = response.status;
            error.payload = payload;
            throw error;
          }
          return payload;
        });
    });
  }

  /* Upload raw bytes (the CQ recording). Same auth and the same error shape as
   * `api`, but the body is the Blob itself — base64 round-tripping a 30-second
   * take would inflate it by a third for no reason, and the server wants the
   * WAV bytes anyway because it has no decoder for anything else. */
  function upload(path, blob) {
    return fetch(path, {
      method: 'POST',
      cache: 'no-store',
      headers: authHeaders({ 'Content-Type': 'application/octet-stream' }),
      body: blob
    }).then(function (response) {
      return response.json().catch(function () { return {}; })
        .then(function (payload) {
          if (!response.ok) {
            var error = new Error(payload.error || ('HTTP ' + response.status));
            error.status = response.status;
            error.payload = payload;
            throw error;
          }
          return payload;
        });
    });
  }

  function on(selector, event, handler) {
    var node = document.querySelector(selector);
    if (node) node.addEventListener(event, handler);
    return node;
  }

  function setText(selector, text) {
    var node = document.querySelector(selector);
    if (node) node.textContent = text;
  }

  function show(selector, visible) {
    var node = document.querySelector(selector);
    if (node) node.classList.toggle('hidden', !visible);
  }

  function message(selector, text, kind) {
    var node = document.querySelector(selector);
    if (!node) return;
    node.textContent = text || '';
    node.className = 'msg' + (kind ? ' ' + kind : '');
  }

  function disabled(selector, yes) {
    var node = document.querySelector(selector);
    if (node) node.disabled = !!yes;
  }

  /* Five bars out of five, as text: the same scale the retired wizard used, and
   * it needs no images. */
  function signalText(signal) {
    var filled = Math.max(0, Math.min(5, Math.round((signal || 0) / 20)));
    return '▮▮▮▮▮'.slice(0, filled) + '▯▯▯▯▯'.slice(0, 5 - filled);
  }

  /* Which route this page is on decides what a dropped connection means: on
   * /setup the operator is about to lose the box on purpose, and the words that
   * say so have to be on screen *before* they press the button, not in the
   * error box after. */
  function secondsLabel(seconds) {
    var n = Math.max(0, Math.round(seconds || 0));
    return Math.floor(n / 60) + ' 分 ' + ('0' + (n % 60)).slice(-2) + ' 秒';
  }

  global.Box = {
    AUTH_COOKIE: AUTH_COOKIE,
    api: api,
    upload: upload,
    on: on,
    setText: setText,
    show: show,
    message: message,
    disabled: disabled,
    signalText: signalText,
    secondsLabel: secondsLabel
  };
})(window);
