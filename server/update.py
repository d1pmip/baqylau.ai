"""Server panel update checker (teacher panel ONLY, no student counterpart).

- Compares local version.py with latest server-v* GitHub release.
- Banner shows ONLY when release is strictly newer (older/equal -> silence).
- Newer release zip auto-downloads to data/updates/ (background thread).
- Check every 12h (cache) + forced check at server startup.
- Offline-safe: short timeouts, never raises, never 500.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger("baqylau")

REPO = os.environ.get("BAQYLAU_REPO", "d1pmip/baqylau.ai")
CACHE_FILE = os.environ.get("BAQYLAU_UPDATE_CACHE", "data/update_check.json")
CACHE_SCHEMA = 2
TTL = 12 * 3600
UPDATES_DIR = os.environ.get("BAQYLAU_UPDATES", "data/updates")
ASSET_NAME = "BaqylauServer-win64.zip"

_downloading = False


def _local_version() -> str:
    try:
        from version import VERSION
        return str(VERSION).strip()
    except Exception:
        return "0.0.0"


def _num(v: str) -> tuple:
    """'1.0.2' -> (1, 0, 2). Garbage/empty -> (0, 0, 0). Never raises."""
    out = []
    for p in str(v or "").strip().lstrip("v").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        try:
            out.append(int(digits) if digits else 0)
        except Exception:
            out.append(0)
    return tuple((out + [0, 0, 0])[:3])


def is_newer(latest: str, local: str) -> bool:
    """True only when release version is STRICTLY newer than local."""
    try:
        return _num(latest) > _num(local)
    except Exception:
        return False


def _fetch_latest() -> dict | None:
    url = f"https://api.github.com/repos/{REPO}/releases"
    req = urllib.request.Request(url, headers={"User-Agent": "BaqylauServer",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            releases = json.loads(r.read().decode())
    except Exception as exc:
        logger.debug("update fetch failed: %s", exc)
        return None
    best: str | None = None
    asset_url: str | None = None
    if not isinstance(releases, list):
        return None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name", "") or "")
        if not tag.startswith("server-v"):
            continue
        ver = tag[len("server-v"):].strip()
        if best is None or _num(ver) > _num(best):
            best = ver
            asset_url = None
            try:
                for a in rel.get("assets", []) or []:
                    if str((a or {}).get("name", "")) == ASSET_NAME:
                        asset_url = (a or {}).get("browser_download_url")
                        break
            except Exception:
                asset_url = None
    if best is None:
        return None
    return {"version": best,
            "url": f"https://github.com/{REPO}/releases/tag/server-v{best}",
            "asset_url": asset_url}


def _read_cache() -> dict | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("v") != CACHE_SCHEMA:
            return None
        return data
    except Exception:
        return None


def _write_cache(res: dict) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(CACHE_FILE)), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(res, fh)
    except Exception:
        pass


def local_zip(version: str) -> str | None:
    """Already auto-downloaded zip path, or None."""
    if not version:
        return None
    p = os.path.join(UPDATES_DIR, f"BaqylauServer-{version}.zip")
    try:
        if os.path.exists(p) and os.path.getsize(p) > 100_000:
            return p
    except Exception:
        pass
    return None


def _download_asset(version: str, url: str | None) -> None:
    """Best-effort background download of the release zip. Never raises."""
    global _downloading
    if not url or local_zip(version):
        return
    if _downloading:
        return
    _downloading = True
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BaqylauServer"})
        with urllib.request.urlopen(req, timeout=120) as r:
            if int(r.headers.get("Content-Length", "0") or 0) > 500 * 1024 * 1024:
                return
            os.makedirs(UPDATES_DIR, exist_ok=True)
            tmp = os.path.join(UPDATES_DIR, f".dl-{version}.part")
            total = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 500 * 1024 * 1024:
                        break
                    fh.write(chunk)
            if total > 100_000:
                os.replace(tmp, os.path.join(UPDATES_DIR, f"BaqylauServer-{version}.zip"))
                logger.info("update downloaded: server-v%s", version)
    except Exception as exc:
        logger.debug("update download failed: %s", exc)
    finally:
        _downloading = False


def check(force: bool = False) -> dict:
    """{"local","latest","update","url","asset_ready","checked_at"}.

    update=True ONLY when release version is strictly newer than local.
    """
    local = _local_version()
    cached = _read_cache()
    if not force and cached:
        try:
            fresh = (time.time() - float(cached.get("checked_at", 0))) < TTL
        except Exception:
            fresh = False
        if fresh:
            latest = cached.get("latest")
            upd = bool(latest) and is_newer(str(latest), local)
            return {"local": local, "latest": latest, "update": upd,
                    "url": cached.get("url"),
                    "asset_ready": bool(latest and local_zip(str(latest))),
                    "checked_at": cached.get("checked_at"), "cached": True}
    latest_info = _fetch_latest()
    latest = latest_info["version"] if latest_info else None
    upd = bool(latest) and is_newer(str(latest), local)
    res = {"v": CACHE_SCHEMA, "local": local, "latest": latest, "update": upd,
           "url": latest_info["url"] if latest_info else None,
           "checked_at": time.time()}
    _write_cache(res)
    logger.info("update check: local=%s latest=%s update=%s", local, latest, upd)
    if upd and latest_info and latest_info.get("asset_url"):
        t = threading.Thread(target=_download_asset,
                             args=(str(latest), latest_info["asset_url"]),
                             daemon=True)
        t.start()
    return {"local": local, "latest": latest, "update": upd,
            "url": res["url"],
            "asset_ready": bool(latest and local_zip(str(latest))),
            "checked_at": res["checked_at"], "cached": False}


def check_at_startup() -> None:
    """Forced fresh check at server start (background, never blocks)."""
    if os.environ.get("BAQYLAU_NO_STARTUP_CHECK"):
        return

    def _run():
        try:
            check(force=True)
        except Exception as exc:
            logger.debug("startup update check failed: %s", exc)

    threading.Thread(target=_run, daemon=True).start()
