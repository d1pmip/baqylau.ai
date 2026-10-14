"""Event logger + snapshot exporter (JSON/CSV + frame captures)."""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from dataclasses import asdict

import numpy as np

logger = logging.getLogger(__name__)


class AlertManager:
    """Append-only violation log with auto snapshot on CRITICAL.

    Хранение с группировкой по дням (YYYY-MM-DD):
      data/logs/YYYY-MM-DD/events.jsonl
      data/logs/YYYY-MM-DD/events.csv
      data/logs/YYYY-MM-DD/snapshots/CRITICAL_....jpg
    Для совместимости дублируется плоский legacy-лог:
      data/logs/events.jsonl(.csv) + data/logs/snapshots/
    """

    DAY_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")

    def __init__(self, log_dir: str = "data/logs", snapshot_dir: str = "data/logs/snapshots",
                 keep_days: int = 0) -> None:
        self.log_dir = log_dir
        self.snapshot_dir = snapshot_dir
        self.keep_days = int(keep_days or 0)
        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(snapshot_dir, exist_ok=True)
        self.jsonl_path = os.path.join(log_dir, "events.jsonl")
        self.csv_path = os.path.join(log_dir, "events.csv")
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["time", "level", "kind", "points", "score", "detail"])
        if self.keep_days > 0:
            try:
                self.purge_old(self.keep_days)
            except Exception:
                pass

    # -- daily grouping -------------------------------------------------
    @staticmethod
    def day_of(ts: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))

    def _dated_paths(self, ts: float | None = None) -> tuple[str, str, str, str, str]:
        """Return (day, day_dir, jsonl, csv, snap_dir) for timestamp."""
        day = self.day_of(ts)
        day_dir = os.path.join(self.log_dir, day)
        snap = os.path.join(day_dir, "snapshots")
        return day, day_dir, os.path.join(day_dir, "events.jsonl"), \
            os.path.join(day_dir, "events.csv"), snap

    def _ensure_day(self, day: str) -> tuple[str, str, str]:
        day_dir = os.path.join(self.log_dir, day)
        snap = os.path.join(day_dir, "snapshots")
        os.makedirs(snap, exist_ok=True)
        csv_p = os.path.join(day_dir, "events.csv")
        if not os.path.exists(csv_p):
            with open(csv_p, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["time", "level", "kind", "points", "score", "detail"])
        return day_dir, os.path.join(day_dir, "events.jsonl"), csv_p

    def dated_snapshot_dir(self, ts: float | None = None) -> str:
        day = self.day_of(ts)
        snap = os.path.join(self.log_dir, day, "snapshots")
        os.makedirs(snap, exist_ok=True)
        return snap

    def purge_old(self, keep_days: int = 60) -> int:
        """Удалить дневные папки старше keep_days. Возвращает число удалённых."""
        import datetime
        import shutil

        removed = 0
        try:
            names = os.listdir(self.log_dir)
        except FileNotFoundError:
            return 0
        cutoff = time.time() - int(keep_days) * 86400
        for n in names:
            if not self.DAY_RE.match(n):
                continue
            p = os.path.join(self.log_dir, n)
            if not os.path.isdir(p):
                continue
            try:
                dt = datetime.datetime.strptime(n, "%Y-%m-%d")
                if dt.timestamp() < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except Exception:
                continue
        return removed

    def _append_rec(self, rec: dict, ts: float) -> None:
        # dated copy
        try:
            day = self.day_of(ts)
            _, _, j_p, c_p, _ = self._dated_paths(ts)
            self._ensure_day(day)
            with open(j_p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(c_p, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([rec["time"], rec["level"], rec["kind"],
                                        rec["points"], rec["score"], rec["detail"]])
        except Exception as exc:
            logger.warning("dated alert log failed: %s", exc)
        # legacy mirror (совместимость со старыми отчётами/панелью)
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([rec["time"], rec["level"], rec["kind"],
                                        rec["points"], rec["score"], rec["detail"]])
        except Exception as exc:
            logger.warning("legacy alert log failed: %s", exc)

    def log(self, risk_state, frame: np.ndarray | None = None) -> None:
        """Persist each RiskEvent; snapshot on any CRITICAL event or state."""
        try:
            for ev in risk_state.events:
                rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev.t)),
                       "ts": ev.t, "level": ev.level, "kind": ev.kind,
                       "points": ev.points, "score": round(risk_state.score, 1),
                       "detail": ev.detail}
                self._append_rec(rec, ev.t)
            critical = risk_state.level == "CRITICAL" or any(
                e.level == "CRITICAL" for e in risk_state.events
            )
            if critical and frame is not None:
                self.snapshot(frame, risk_state)
        except Exception as exc:
            logger.warning("alert log failed: %s", exc)

    def snapshot(self, frame: np.ndarray, risk_state) -> str:
        """Save frame JPEG into dated + legacy snapshots, return dated path."""
        try:
            import cv2

            name = f"CRITICAL_{time.strftime('%Y%m%d_%H%M%S')}_{int(risk_state.score)}.jpg"
            dated_dir = self.dated_snapshot_dir()
            os.makedirs(self.snapshot_dir, exist_ok=True)
            dated_path = os.path.join(dated_dir, name)
            ok = bool(cv2.imwrite(dated_path, frame))
            if not ok:
                logger.warning("snapshot write failed: %s", dated_path)
                return ""
            # legacy mirror для старых отчётов (best-effort, тот же файл)
            try:
                legacy_path = os.path.join(self.snapshot_dir, name)
                if os.path.abspath(legacy_path) != os.path.abspath(dated_path):
                    cv2.imwrite(legacy_path, frame)
            except Exception:
                pass
            return dated_path
        except Exception as exc:
            logger.warning("snapshot failed: %s", exc)
            return ""

    def read_events(self) -> list[dict]:
        out = []
        seen_ts = set()

        def _load(path: str) -> None:
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        key = (rec.get("ts"), rec.get("kind"), rec.get("detail"))
                        if key in seen_ts:
                            continue
                        seen_ts.add(key)
                        out.append(rec)
            except FileNotFoundError:
                pass

        # сначала legacy, затем дневные папки YYYY-MM-DD
        _load(self.jsonl_path)
        try:
            for n in sorted(os.listdir(self.log_dir)):
                if self.DAY_RE.match(n):
                    _load(os.path.join(self.log_dir, n, "events.jsonl"))
        except FileNotFoundError:
            pass
        out.sort(key=lambda r: float(r.get("ts", 0) or 0))
        return out

    def timeline_figure(self, save_path: str = ""):
        """Matplotlib timeline of risk events. Returns fig or None."""
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            evs = self.read_events()
            if not evs:
                return None
            xs = [e["ts"] for e in evs]
            ys = [e["score"] for e in evs]
            colors = {"LOW": "green", "MEDIUM": "orange", "HIGH": "red", "CRITICAL": "darkred"}
            fig, ax = plt.subplots(figsize=(10, 3))
            ax.scatter(xs, [e["level"] for e in evs],
                       c=[colors.get(e["level"], "gray") for e in evs], s=[y + 10 for y in ys])
            ax.set_title("Violation timeline (level sized by score)")
            ax.set_xlabel("time")
            fig.tight_layout()
            if save_path:
                fig.savefig(save_path)
            return fig
        except Exception as exc:
            logger.debug("timeline failed: %s", exc)
            return None

    @staticmethod
    def _safe(o):
        try:
            return asdict(o)
        except Exception:
            return str(o)
