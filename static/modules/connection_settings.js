(function () {
  "use strict";

  function descriptor(dlg) {
    var model = dlg.querySelector("#cs-model").value;
    return (dlg._connectionCatalog && dlg._connectionCatalog[model]) || {
      transport: "serial",
      host_required: false,
    };
  }

  function update(dlg) {
    var network = descriptor(dlg).transport === "network";
    dlg.querySelector("#cs-serial-fields").hidden = network;
    dlg.querySelector("#cs-audio-fields").hidden = network;
    dlg.querySelector("#cs-network-fields").hidden = !network;
  }

  function sync(dlg, current) {
    dlg._connectionCatalog = current.connections || {};
    dlg.querySelector("#cs-sunsdr-host").value = current.sunsdr_host || "";
    dlg.querySelector("#cs-sunsdr-local-host").value = current.sunsdr_local_host || "";
    update(dlg);
  }

  function fields(dlg) {
    if (descriptor(dlg).transport !== "network") return {};
    return {
      sunsdr_host: dlg.querySelector("#cs-sunsdr-host").value.trim(),
      sunsdr_local_host: dlg.querySelector("#cs-sunsdr-local-host").value.trim(),
    };
  }

  function fillSelect(sel, items, key, current) {
    sel.replaceChildren();
    var auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "自动（推荐）";
    sel.appendChild(auto);
    var seen = {};
    items.forEach(function (it) {
      var v = it[key];
      if (!v || seen[v]) return;
      seen[v] = 1;
      var opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      if (current && v.indexOf(current) !== -1) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function post(path, body, done) {
    fetch(window.apiUrl(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : "{}",
    })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, j: j }; });
      })
      .then(done)
      .catch(function () { done({ ok: false, j: { error: "network" } }); });
  }

  function waitAndReload(dlg) {
    var msg = dlg.querySelector("#cs-msg");
    msg.textContent = "已保存，正在重启…";
    var t = setInterval(function () {
      fetch(window.apiUrl("/api/health")).catch(function () {
        clearInterval(t);
        var t2 = setInterval(function () {
          fetch(window.apiUrl("/api/health"))
            .then(function () {
              clearInterval(t2);
              window.location.reload();
            })
            .catch(function () {});
        }, 1200);
      });
    }, 800);
  }

  function openDlg(dlg) {
    dlg.style.display = "flex";
    dlg.querySelector("#cs-msg").textContent = "";
    Promise.all([
      fetch(window.apiUrl("/api/devices")).then(function (r) {
        return r.ok ? r.json() : null;
      }),
      fetch(window.apiUrl("/api/setup")).then(function (r) {
        return r.ok ? r.json() : null;
      }),
    ]).then(function (rs) {
      var dev = rs[0];
      var cur = rs[1];
      if (!dev || !cur) return;
      dlg.querySelector("#cs-model").value = cur.radio_model || "ft710";
      fillSelect(dlg.querySelector("#cs-port"), dev.serial_ports,
                 "device", cur.serial_port);
      fillSelect(dlg.querySelector("#cs-audio-rx"), dev.audio_rx,
                 "name", cur.audio_rx_device);
      fillSelect(dlg.querySelector("#cs-audio-tx"), dev.audio_tx,
                 "name", cur.audio_tx_device);
      sync(dlg, cur);
    });
  }

  function init() {
    var dlg = document.getElementById("conn-dialog");
    if (!dlg) return;
    dlg.querySelector("#cs-model").addEventListener("change", function () {
      update(dlg);
    });
    document.getElementById("cs-save").addEventListener("click", function () {
      var pwd = dlg.querySelector("#cs-password").value;
      if (pwd && pwd.length < 8) {
        dlg.querySelector("#cs-msg").textContent = "密码至少 8 位";
        return;
      }
      var body = {
        radio_model: dlg.querySelector("#cs-model").value,
        serial_port: dlg.querySelector("#cs-port").value,
        audio_rx_device: dlg.querySelector("#cs-audio-rx").value,
        audio_tx_device: dlg.querySelector("#cs-audio-tx").value,
      };
      Object.assign(body, fields(dlg));
      if (pwd) body.password = pwd;
      post("/api/setup", body, function (res) {
        if (res.ok) {
          waitAndReload(dlg);
        } else {
          dlg.querySelector("#cs-msg").textContent =
            "保存失败：" + ((res.j && res.j.error) || "未知错误");
        }
      });
    });
    document.getElementById("cs-restart").addEventListener("click", function () {
      post("/api/restart", null, function (res) {
        if (res.ok) {
          waitAndReload(dlg);
        } else {
          dlg.querySelector("#cs-msg").textContent =
            "重启失败：" + ((res.j && res.j.error) || "未知错误");
        }
      });
    });
    document.getElementById("cs-close").addEventListener("click", function () {
      dlg.style.display = "none";
    });
    document.querySelectorAll("[data-action='conn-settings']").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
        openDlg(dlg);
      });
    });
  }

  window.MRRCConnectionSettings = { sync: sync, fields: fields, update: update };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
