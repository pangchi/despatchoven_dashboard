"""Background poller: reads the controller, keeps the live snapshot,
logs to PostgreSQL at an adaptive rate and runs the purge job."""
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import registers as R
from .controller import ControllerError
from .purge import run_purge

log = logging.getLogger(__name__)


class Poller(threading.Thread):
    def __init__(self, controller, db, cfg, runs=None):
        super().__init__(name="p3-poller", daemon=True)
        self.ctl, self.db, self.cfg = controller, db, cfg
        self.runs = runs
        from .events import ProcessEventDetector
        self.detector = ProcessEventDetector()
        self.stop_evt = threading.Event()
        self.state_lock = threading.Lock()
        self.latest = None          # last good snapshot (dict)
        self.comm_ok = False
        self.comm_error = None
        self.last_ok_ts = None
        self.db_ok = True          # connect() succeeded in create_app
        self.db_error = None
        self.state = "idle"
        self._last_active_mono = 0.0
        self._last_log_mono = 0.0
        self._last_purge_mono = 0.0
        self._last_part_mono = 0.0
        self._prev_sig = None
        self.last_purge = None
        self.samples_logged = 0
        self._wake = threading.Event()        # interrupts the sleep between polls
        self._pause_req = threading.Event()
        self._parked = threading.Event()
        self.paused_reason = None

    # ---------- public ----------
    def stop(self):
        self.stop_evt.set()
        self._wake.set()

    @contextmanager
    def paused(self, reason="settings", timeout=15.0):
        """Park the poll loop between polls and hold the controller lock, so the
        serial port is free for a connection test or a controller swap."""
        self.paused_reason = reason
        self._pause_req.set()
        self._wake.set()
        if self.is_alive() and not self._parked.wait(timeout):
            self._pause_req.clear()
            self.paused_reason = None
            raise TimeoutError("poller did not pause in time")
        try:
            with self.ctl.lock:          # also blocks control API calls on the old controller
                yield
        finally:
            self.paused_reason = None
            self._pause_req.clear()
            self._wake.set()

    def replace_controller(self, controller):
        """Swap in a new controller. Call only inside paused()."""
        old, self.ctl = self.ctl, controller
        try:
            old.close()
        except Exception:
            pass
        with self.state_lock:
            self.latest, self.comm_ok, self.comm_error = None, False, None
            self._prev_sig = None
        self.detector.reset()

    def status(self):
        with self.state_lock:
            return {
                "comm_ok": self.comm_ok, "comm_error": self.comm_error,
                "last_ok": self.last_ok_ts, "db_ok": self.db_ok, "db_error": self.db_error,
                "state": self.state, "samples_logged": self.samples_logged,
                "log_interval": self.log_interval(), "data": self.latest,
                "info": self.ctl.info, "last_purge": self.last_purge,
                "paused": self.paused_reason,
                "run": self._run_status(),
            }

    def _run_status(self):
        if not self.runs:
            return None
        cur = self.runs.current()
        out = {"next_label": self.runs.pending_label()}
        if cur:
            out.update(id=cur["id"], mode=cur["mode"], profile=cur["profile"], started_at=cur["started_at"],
                       label=cur.get("label"))
        return out

    def log_interval(self):
        return self.cfg.LOG_INTERVAL_ACTIVE if self.state == "active" else self.cfg.LOG_INTERVAL_IDLE

    def request_log(self):
        """Force the next poll to write a row (e.g. right after a control command)."""
        self._last_log_mono = 0.0

    # ---------- loop ----------
    def run(self):
        backoff = self.cfg.POLL_INTERVAL
        while not self.stop_evt.is_set():
            if self._pause_req.is_set():
                self._parked.set()
                while self._pause_req.is_set() and not self.stop_evt.is_set():
                    time.sleep(0.05)
                self._parked.clear()
                backoff = self.cfg.POLL_INTERVAL
                continue
            t0 = time.monotonic()
            try:
                if not self.ctl.info:
                    self.ctl.read_info()
                snap = self.ctl.snapshot()
                snap["ts"] = datetime.now(timezone.utc)
                self._on_snapshot(snap)
                backoff = self.cfg.POLL_INTERVAL
            except ControllerError as e:
                self._on_comm_error(e)
                backoff = min(backoff * 2, 30.0)
            except Exception as e:
                log.exception("poll failed")
                self._on_comm_error(e)
                backoff = min(backoff * 2, 30.0)
            self._housekeeping()
            self._wake.wait(max(0.05, backoff - (time.monotonic() - t0)))
            self._wake.clear()
        self.ctl.close()

    def _is_active(self, s):
        running = (s.get("manual_status") == 1 or s.get("timer_status") == 1
                   or s.get("profile_status") in (1, 2, 3) or (s.get("power") or 0) > 0)
        if running:
            return True
        thr = self.cfg.IDLE_PV_THRESHOLD
        return thr is not None and s.get("pv") is not None and s["pv"] > thr

    def _on_snapshot(self, s):
        now = time.monotonic()
        if self._is_active(s):
            self._last_active_mono = now
        state = "active" if now - self._last_active_mono < self.cfg.IDLE_HOLDOFF and self._last_active_mono else "idle"

        # derived / display fields
        mode = int(s.get("current_mode", 0))
        s["sp"] = s["working_sp"] if mode == 4 else s["manual_sp"]
        s["mode_label"] = R.CURRENT_MODE.get(mode, str(mode))
        s["profile_status_label"] = R.PROFILE_STATUS.get(int(s.get("profile_status", 0)), "?")
        s["segment_type_label"] = R.SEGMENT_TYPE.get(int(s.get("segment_type", 0)), "?")
        s["hl_tripped"] = bool(int(s.get("hl_status", 0)) & 0x4)
        s["sensor_break"] = bool(s.get("sensor_break"))
        s["state"] = state
        try:
            for action, detail, severity in self.detector.check(s):
                self.db_safe_event("controller", action, detail, ok=severity == "info", severity=severity)
                log.log(logging.WARNING if severity != "info" else logging.INFO, "event %s %s", action, detail)
        except Exception:
            log.exception("event detection failed")
        try:
            s["run_id"] = self.runs.update(s) if self.runs else None
        except Exception:
            log.exception("run tracking failed")
            s["run_id"] = None

        # log immediately on any discrete state change (including run start / end)
        sig = (mode, s.get("manual_status"), s.get("timer_status"), s.get("profile_status"),
               s.get("active_segment"), s.get("relay_bits"), s.get("alarm_bits"), s["hl_tripped"],
               s["sensor_break"], state, s["run_id"])
        changed = sig != self._prev_sig
        self._prev_sig = sig

        with self.state_lock:
            if not self.comm_ok:
                log.info("Controller communication OK")
                if self.comm_error:
                    self.db_safe_event("system", "comm_restored", {})
            self.latest, self.comm_ok, self.comm_error = s, True, None
            self.last_ok_ts = s["ts"]
            self.state = state

        if not self.db.connected:
            return                      # housekeeping retries the connection and keeps the real error
        if changed or now - self._last_log_mono >= self.log_interval():
            try:
                self.db.insert_telemetry(s)
                self._last_log_mono = now
                if self.runs:
                    self.runs.finish_stats()
                with self.state_lock:
                    self.samples_logged += 1
                    self.db_ok, self.db_error = True, None
            except Exception as e:
                log.warning("DB insert failed: %s", e)
                with self.state_lock:
                    self.db_ok, self.db_error = False, str(e)

    def _on_comm_error(self, e):
        with self.state_lock:
            first_failure = self.comm_ok or self.comm_error is None
            self.comm_ok, self.comm_error = False, str(e)
        if first_failure:
            log.warning("Controller communication lost: %s", e)
            self.db_safe_event("system", "comm_lost", {"error": str(e)}, ok=False)
        try:
            self.ctl.close()   # reopen port on next attempt (USB adapter replug etc.)
        except Exception:
            pass

    def db_safe_event(self, *a, **kw):
        try:
            self.db.log_event(*a, **kw)
        except Exception:
            pass

    def _housekeeping(self):
        now = time.monotonic()
        if not self.db.connected:
            if now - getattr(self, "_last_db_try", 0) < 30:
                return
            self._last_db_try = now
            try:
                self.db.connect()
                log.info("database connected")
                with self.state_lock:
                    self.db_ok, self.db_error = True, None
                self.db_safe_event("system", "db_connected", {})
            except Exception as e:
                with self.state_lock:
                    self.db_ok, self.db_error = False, str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
                return
        if now - self._last_part_mono > 3600:
            try:
                self.db.ensure_partitions()
                self._last_part_mono = now
            except Exception as e:
                log.warning("partition maintenance failed: %s", e)
        if now - self._last_purge_mono > self.cfg.PURGE_CHECK_INTERVAL:
            self._last_purge_mono = now
            try:
                res = run_purge(self.db, self.cfg)
                res["ts"] = datetime.now(timezone.utc)
                self.last_purge = res
            except Exception as e:
                log.warning("purge failed: %s", e)
