"""Disk-pressure and retention purge.

When usage of DISK_CHECK_PATH exceeds PURGE_THRESHOLD_PCT, the oldest daily
telemetry partitions are dropped one at a time until usage falls to
PURGE_TARGET_PCT. Today's partition is never dropped.
"""
import logging
import shutil
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def disk_usage(path):
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free, "pct": round(u.used * 100.0 / u.total, 2)}


def run_purge(db, cfg, usage_fn=None):
    usage_fn = usage_fn or (lambda: disk_usage(cfg.DISK_CHECK_PATH)["pct"])
    today = datetime.now(timezone.utc).date()
    dropped = []

    # 1. retention by age
    if cfg.RETENTION_DAYS > 0:
        cutoff = today - timedelta(days=cfg.RETENTION_DAYS)
        for name, d, size in db.list_partitions():
            if d < cutoff:
                db.drop_partition(name)
                dropped.append({"partition": name, "bytes": size, "reason": "retention"})

    # 2. disk pressure
    pct = usage_fn()
    if pct > cfg.PURGE_THRESHOLD_PCT:
        log.warning("Disk usage %.1f%% > %.1f%% - purging oldest telemetry", pct, cfg.PURGE_THRESHOLD_PCT)
        for name, d, size in db.list_partitions():
            if d >= today:
                break
            db.drop_partition(name)
            dropped.append({"partition": name, "bytes": size, "reason": f"disk {pct:.1f}%"})
            pct = usage_fn()
            if pct <= cfg.PURGE_TARGET_PCT:
                break
        if pct > cfg.PURGE_TARGET_PCT:
            log.error("Disk still at %.1f%% after purging all past telemetry - "
                      "something other than this app is using the disk", pct)

    if dropped:
        parts = db.list_partitions()
        if parts:
            oldest = datetime.combine(parts[0][1], datetime.min.time(), tzinfo=timezone.utc)
            db.purge_events_before(oldest)
        db.log_event("system", "purge", {"dropped": dropped, "disk_pct_after": pct})
        log.info("Purged %d partition(s); disk now %.1f%%", len(dropped), pct)
    return {"dropped": dropped, "disk_pct": pct}
