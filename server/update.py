"""Server panel update checker (teacher panel ONLY, no student counterpart).

Compares local version.py with latest server-v* GitHub release.
Cached 6h in data/update_check.json; offline-safe (short timeout, never 500).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

REPO = os.environ.get("BAQYLAU_REPO", "d1pmip/baqylau.ai")
CACHE_FILE = os.environ.get("BAQYLAU_UPDATE_CACHE", "data/update_check.json")
TTL = 6 * 3600


def _local_version() -> str:
    try:
        from version import VERSION
        return str(VERSION)
    except Exception:
        return "0.0.0"


def _num(v: str) -> tuple:
    out = []
    for p in str(v).lstrip("v").split("."):
        try:
            out.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except Exception:
            out.append(0)
    return tuple(out + [0] * 3)[:3]


def _fetch_latest() -> dict | None:
    url = f"https://api.github.com/repos/{REPO}/releases"
    req = urllib.request.Request(url, headers={"User-Agent": "BaqylauServer",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            releases = json.loads(r.read().decode())
    except Exception:
        return None
    best = None
    for rel in releases if isinstance(releases, list) else []:
        tag = str((rel or {}).get("tag_name", ""))
        if not tag.startswith("server-v"):
            continue
        ver = tag[len("server-v"):]
        if best is None or _num(ver) > _num(best):
            best = ver
    if best is None:
        return None
    return {"version": best, "url": f"https://github.com/{REPO}/releases/tag/server-v{best}"}


def check(force: bool = False) -> dict:
    """{"local":..., "latest":...|None, "update":bool, "url":...|None, "checked_at":...}"""
    local = _local_version()
    cached: dict | None = None
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, encoding="utf-8") as fh:
                cached = json.load(fh)
    except Exception:
        cached = None
    if not force and cached and (time.time() - float(cached.get("checked_at", 0))) < TTL:
        latest = cached.get("latest")
        return {"local": local, "latest": latest,
                "update": bool(latest and _num(latest) > _num(local)),
                "url": cached.get("url"), "checked_at": cached.get("checked_at"),
                "cached": True}
    latest_info = _fetch_latest()
    res = {"local": local, "latest": latest_info["version"] if latest_info else None,
           "update": bool(latest_info and _num(latest_info["version"]) > _num(local)),
           "url": latest_info["url"] if latest_info else None,
           "checked_at": time.time(), "cached": False}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(CACHE_FILE)), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(res, fh)
    except Exception:
        pass
    return res
