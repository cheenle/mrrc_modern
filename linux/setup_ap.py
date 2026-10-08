#!/usr/bin/env python3
"""mrrc-setup-ap.service: open a hotspot while the box cannot be reached.

Design: ``docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md`` (§6, D-6).

The rule is about the **network**, not about "first boot"::

    raise the hotspot  ⟺  no cable link AND no saved WiFi that connects

which is why a configured box never shows it (it has an uplink), why an
unconfigured one shows it again after every power cycle (it still has none), and
why there is no "is it configured yet" flag anywhere to drift out of sync.

Three fences around the open window (design D-6):

1. it exists only while there is no other uplink — the moment a cable is plugged
   in or a saved WiFi associates, this takes the hotspot down;
2. it closes by itself after ``MRRC_SETUP_AP_TIMEOUT_MIN`` minutes (default 30)
   and does **not** reopen until the box is power-cycled. That latch is
   per-process on purpose: persisting it would turn "you missed the window" into
   "the box is unreachable until you reflash it";
3. every raise, every close and every wizard entry is logged, and the raise goes
   to the HDMI console at WARNING, so an operator with a monitor never needs the
   hotspot at all (the mitigation for design risk R-2).

A resident service rather than a oneshot because the wizard page — and later
``/manage`` — need to ask whether the hotspot is up and how long is left. That
answer is ``state.json``, rewritten every tick, and its heartbeat is half of the
server's passwordless gate: a supervisor that stops heartbeating closes the gate
by itself, which is why "stale file" and "gate open" can never be true together.

The radio is single (design D-3), so while the server performs an AP→STA switch
this loop **stands down**; the server's claim in ``wizard.json`` is what says so.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

# In the image this file is /opt/mrrc_modern/linux/setup_ap.py and net_wifi.py
# sits beside its parent — the same layout as the repository, and the same trick
# linux/mrrc_radio.py already uses.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import net_wifi  # noqa: E402

DEFAULT_TIMEOUT_MIN = 30
DEFAULT_POLL_S = 5
DEFAULT_SETTLE_S = 30
# Once NM has settled on "nothing", one more pause for a DHCP handshake that is
# already in flight but not yet visible in the state column.
GRACE_AFTER_IDLE_S = 5.0
# A hotspot that cannot start is retried every tick; the log is not.
ERROR_LOG_GAP_S = 60.0

logger = logging.getLogger("mrrc.setup_ap")


def _int_or(value, default: int) -> int:
    """A positive int from the environment, or the default.

    Zero and negatives fall back too: ``MRRC_SETUP_AP_TIMEOUT_MIN=0`` would close
    the window the instant it opened, which is what a typo produces and not what
    anybody meant.
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return parsed if parsed > 0 else default


def settings(env: Optional[dict] = None) -> dict:
    """Everything this service reads, resolved in one place."""
    source = os.environ if env is None else env
    cfg = net_wifi.ap_settings(source)
    cfg["timeout_s"] = _int_or(source.get("MRRC_SETUP_AP_TIMEOUT_MIN"),
                               DEFAULT_TIMEOUT_MIN) * 60
    cfg["poll_s"] = _int_or(source.get("MRRC_SETUP_AP_POLL_S"), DEFAULT_POLL_S)
    cfg["settle_s"] = _int_or(source.get("MRRC_SETUP_AP_SETTLE_S"), DEFAULT_SETTLE_S)
    return cfg


class Supervisor:
    """One decision per tick. Every nmcli call goes through ``runner``.

    ``clock`` and ``sleep`` are injected for the same reason ``runner`` is: the
    interesting behaviour here is *when* things happen, and a test that waits
    thirty real minutes for a timeout is a test nobody runs.
    """

    def __init__(self, cfg: Optional[dict] = None,
                 runner: Optional[net_wifi.Runner] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 log=None):
        self.cfg = dict(cfg if cfg is not None else settings())
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.log = log or logger
        self._mode = net_wifi.MODE_OFF
        self._since = 0.0
        self._gateway = ""
        self._url = ""
        self._deadline = 0.0
        self._timed_out = False        # per-process by design (see the docstring)
        self._granted: set = set()     # nonces that already bought a fresh window
        self._last_tick = 0.0
        self._elapsed = 0.0
        self._last_error_log = 0.0
        self._stopped = False

    def stop(self) -> None:
        """Ask the loop to finish. Called by the SIGTERM handler."""
        self._stopped = True

    # ── one tick ────────────────────────────────────────────────
    def tick(self) -> str:
        """Decide once, publish once. Returns the mode now in force."""
        now = self.clock()
        self._elapsed = max(0.0, now - self._last_tick) if self._last_tick else 0.0
        self._last_tick = now

        claim = net_wifi.read_wizard(self.cfg["state_dir"])
        if net_wifi.claim_is_fresh(claim, now):
            return self._wizard_tick(claim, now)

        if not net_wifi.nm_running(self.runner):
            # Raising an AP before NM is up just fails, and the failure would be
            # blamed on the radio instead of on the boot order.
            return self._publish(net_wifi.MODE_OFF, now,
                                 reason="waiting for NetworkManager")

        if net_wifi.has_uplink(self.runner):
            if net_wifi.hotspot_active(self.runner):
                self.log.info("an uplink came up — taking the setup hotspot down")
                net_wifi.stop_hotspot(self.runner)
            self._deadline = 0.0
            self._timed_out = False       # a box that had a network and lost it
            return self._publish(net_wifi.MODE_OFF, now, reason="uplink")

        if self._timed_out:
            return self._publish(net_wifi.MODE_OFF, now, reason="timeout")

        if net_wifi.hotspot_active(self.runner):
            if not self._deadline:
                # Found an AP this process did not raise (a service restart while
                # the window was open). Adopt a deadline: without one the timeout
                # can never fire and the open network stays up forever.
                self._deadline = now + self.cfg["timeout_s"]
                self.log.info("adopted an already-running setup hotspot — the window "
                              "closes in %d minutes", self.cfg["timeout_s"] // 60)
            if now >= self._deadline:
                self.log.warning(
                    "the %d-minute setup window closed with nobody onboard — "
                    "hotspot off (power-cycle the box to open it again)",
                    self.cfg["timeout_s"] // 60)
                net_wifi.stop_hotspot(self.runner)
                self._deadline = 0.0
                self._timed_out = True
                return self._publish(net_wifi.MODE_OFF, now, reason="timeout")
            return self._publish(net_wifi.MODE_HOTSPOT, now)

        return self._raise(now)

    def _raise(self, now: float) -> str:
        """Open the window, arm the timer, and tell the HDMI console about it."""
        result = net_wifi.start_hotspot(self.cfg["ssid"], self.cfg["ifname"],
                                        self.runner)
        if not result.ok:
            self._complain("could not start the setup hotspot: %s", result.detail)
            return self._publish(net_wifi.MODE_OFF, now,
                                 reason=f"start failed: {result.detail}"[:200])
        self._deadline = now + self.cfg["timeout_s"]
        self._gateway = net_wifi.ipv4_address(self.cfg["ifname"], self.runner)
        self._url = net_wifi.setup_url(self._gateway, self.cfg["web_port"])
        self.log.warning(
            "SETUP HOTSPOT OPEN — this box has no network. Join Wi-Fi %r with a "
            "phone or laptop and open %s — that page needs no password (accept "
            "the self-signed certificate warning). It closes in %d minutes, or "
            "the moment the box gets a network.",
            self.cfg["ssid"], self._url, self.cfg["timeout_s"] // 60)
        return self._publish(net_wifi.MODE_HOTSPOT, now)

    def _wizard_tick(self, claim, now: float) -> str:
        """Stand down while the server owns the radio (design D-3: one at a time).

        The server performs the AP→STA switch itself. Three consequences:

        * the window is *compensated* for the time spent standing down, so a slow
          switch cannot eat the operator's minutes;
        * a FAILED switch buys exactly one fresh window per nonce — that is what
          makes "try again" work without a power cycle, while a nonce that
          repeats cannot keep an open network alive forever;
        * the published mode is ``off`` while switching, because the hotspot
          genuinely is down then and the gate must not claim otherwise.
        """
        if claim.state == net_wifi.WIZARD_OK:
            self.log.info("the wizard joined %r (address %s) — hotspot stays down",
                          claim.ssid, claim.address or "not reported")
            self._deadline = 0.0
            self._timed_out = False
            self._gateway = claim.address
            self._url = (net_wifi.setup_url(claim.address, self.cfg["web_port"])
                         if claim.address else "")
            return self._publish(net_wifi.MODE_STA, now, reason="joined")

        if claim.state == net_wifi.WIZARD_FAILED:
            fresh = bool(claim.nonce) and claim.nonce not in self._granted
            if fresh:
                self._granted.add(claim.nonce)
            self._timed_out = False
            if not net_wifi.hotspot_active(self.runner):
                # The server tried to put the hotspot back and could not. Without
                # this the box sits unreachable with nobody able to say why.
                return self._raise(now)
            if fresh:
                self._deadline = now + self.cfg["timeout_s"]
                self.log.warning("joining %r failed (%s) — the hotspot is back and "
                                 "the window restarted", claim.ssid,
                                 claim.error or "no reason given")
            return self._publish(net_wifi.MODE_HOTSPOT, now, reason="retry")

        # WIZARD_SWITCHING: the server is mid-switch and heartbeating.
        if self._deadline:
            self._deadline += self._elapsed
        return self._publish(net_wifi.MODE_OFF, now, reason="wizard switching")

    # ── publishing and logging ──────────────────────────────────
    def _publish(self, mode: str, now: float, *, reason: str = "") -> str:
        """Write one heartbeat of ``state.json`` — the evidence the gate runs on.

        ``since`` survives across ticks while the mode is unchanged so the page
        can say how long the window has been open; the gateway and URL are kept
        for ``sta`` (that is the address to hand the operator next) and cleared
        for ``off`` (there is no address to advertise).
        """
        if mode != self._mode:
            self._since = now
            self._mode = mode
            if mode == net_wifi.MODE_OFF:
                self._gateway = ""
                self._url = ""
        net_wifi.write_state(net_wifi.ApState(
            mode=mode,
            ssid=self.cfg["ssid"] if mode == net_wifi.MODE_HOTSPOT else "",
            gateway=self._gateway,
            network=net_wifi.ap_network(self._gateway) if self._gateway else "",
            url=self._url,
            reason=reason,
            since=self._since,
            heartbeat=now,
            deadline=self._deadline if mode == net_wifi.MODE_HOTSPOT else 0.0,
        ), self.cfg["state_dir"])
        return mode

    def _complain(self, fmt: str, *args) -> None:
        """Log a repeating failure at most once a minute.

        A hotspot that cannot start is retried every tick, and the unit forwards
        to the HDMI console: five identical lines a minute would bury the one
        message the operator actually needs.
        """
        now = self.clock()
        if now - self._last_error_log < ERROR_LOG_GAP_S:
            return
        self._last_error_log = now
        self.log.error(fmt, *args)

    # ── start-up and shut-down ──────────────────────────────────
    def _wait_for_nm(self) -> None:
        """Give NetworkManager time to autoconnect before deciding anything.

        Deciding at t=0 is how a box that *would* have had WiFi five seconds
        later ends up broadcasting an open hotspot for half an hour: D-6's
        criterion is "no saved WiFi that connects", and "connects" needs time.
        """
        deadline = self.clock() + self.cfg["settle_s"]
        while not self._stopped and self.clock() < deadline:
            if not net_wifi.nm_running(self.runner):
                self.sleep(1.0)
                continue
            if net_wifi.has_uplink(self.runner):
                self.log.info("NetworkManager already has an uplink — no setup hotspot")
                return
            if not net_wifi.any_device_connecting(self.runner):
                # NM has settled on "nothing". One grace period, because a DHCP
                # handshake in flight does not show in the state column yet.
                self.sleep(GRACE_AFTER_IDLE_S)
                if net_wifi.has_uplink(self.runner):
                    self.log.info("an uplink came up during the settle grace")
                    return
                break
            self.sleep(1.0)

    def _shutdown(self) -> None:
        """Leave the radio the way we found it.

        A `systemctl stop` that left the hotspot up would keep an open network
        alive with no supervisor behind it. The heartbeat goes stale on its own
        (which closes the gate within 45 s); taking the AP down is the part that
        needs doing.
        """
        try:
            if net_wifi.hotspot_active(self.runner):
                net_wifi.stop_hotspot(self.runner)
            self._publish(net_wifi.MODE_OFF, self.clock(), reason="service stopped")
        except Exception as exc:                                  # noqa: BLE001
            self.log.warning("could not clean up on stop: %s", exc)

    def run(self) -> int:
        """The service body: settle, then decide once per poll interval."""
        net_wifi.ensure_state_dir(self.cfg["state_dir"])
        try:
            self._wait_for_nm()
        except Exception as exc:                                  # noqa: BLE001
            self.log.error("could not wait for NetworkManager: %s", exc)
        while not self._stopped:
            started = self.clock()
            try:
                self.tick()
            except Exception as exc:                              # noqa: BLE001
                # A resident service that dies on one bad nmcli answer stops
                # being able to open the window at all, and the only symptom the
                # operator sees is "no hotspot ever appeared".
                self.log.exception("setup-ap tick failed: %s", exc)
            nap = self.cfg["poll_s"] - (self.clock() - started)
            self.sleep(max(1.0, nap))
        self._shutdown()
        return 0


def _install_signals(supervisor: Supervisor) -> None:
    """SIGTERM must lead to a clean shutdown, not a default kill.

    systemd stops a service with SIGTERM; the default action would skip
    ``_shutdown()`` and leave the hotspot up. Installed in ``main()`` rather than
    in ``run()`` so that tests driving ``run()`` do not replace the interpreter's
    own SIGINT handling.
    """
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, lambda *_args: supervisor.stop())
        except (ValueError, OSError, AttributeError, RuntimeError):
            pass        # not the main thread, or this platform lacks the signal


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] mrrc-setup-ap: %(message)s",
        stream=sys.stdout,
    )
    cfg = settings()
    supervisor = Supervisor(cfg)
    _install_signals(supervisor)
    logger.info("starting: ssid=%s ifname=%s window=%d min state_dir=%s",
                cfg["ssid"], cfg["ifname"], cfg["timeout_s"] // 60, cfg["state_dir"])
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
