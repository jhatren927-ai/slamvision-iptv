#!/usr/bin/env python3
"""Build SlamVision M3U + XMLTV feeds from the public site API.

Does not modify the website. Live channel URLs are the currently scheduled
archive.org tape (see README for the mid-show tradeoff).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API = os.environ.get("SLAMVISION_API", "https://ring-channel-tv.emergent.host").rstrip("/")
PUBLIC_BASE = os.environ.get(
    "FEED_BASE", "https://jhatren927-ai.github.io/slamvision-iptv"
).rstrip("/")
UA = "SlamVision-IPTV/1.0 (+https://github.com/jhatren927-ai/slamvision-iptv)"
CATEGORY_NAMES = {
    "eras": "Eras",
    "promotions": "Promotions",
    "shows": "Shows",
    "wrestlers": "Wrestlers",
}
# Match the Android app's on-screen name for channel id rndm.
DISPLAY_NAME_OVERRIDES = {"rndm": "Surprise TV"}
WORKERS = int(os.environ.get("SLAMVISION_WORKERS", "1"))
NOW_WORKERS = int(os.environ.get("SLAMVISION_NOW_WORKERS", "3"))
RETRIES = 5
# Cloudflare / origin errors we should back off on instead of aborting the channel.
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 523, 524}
MIN_INTERVAL = float(os.environ.get("SLAMVISION_MIN_INTERVAL", "0.6"))

_rate_lock = __import__("threading").Lock()
_last_request = 0.0
_inflight = __import__("threading").Semaphore(int(os.environ.get("SLAMVISION_INFLIGHT", "2")))
# now-fetch uses NOW_WORKERS but still shares this inflight cap


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _throttle() -> None:
    global _last_request
    with _rate_lock:
        wait = MIN_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()


# Consecutive hard failures; VOD stops if this exceeds CIRCUIT_LIMIT.
_consecutive_failures = 0
CIRCUIT_LIMIT = int(os.environ.get("SLAMVISION_CIRCUIT", "6"))


class CircuitOpen(RuntimeError):
    pass


def reset_circuit() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def http_get(url: str, timeout: int = 30) -> bytes:
    global _consecutive_failures
    last: Exception | None = None
    for attempt in range(RETRIES):
        if _consecutive_failures >= CIRCUIT_LIMIT:
            raise CircuitOpen(f"too many API failures ({_consecutive_failures}); backing off this run")
        _inflight.acquire()
        try:
            _throttle()
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read()
                _consecutive_failures = 0
                return body
            except urllib.error.HTTPError as e:
                last = e
                retryable = e.code in RETRY_STATUS
            except Exception as e:  # noqa: BLE001 — retries on transient network errors
                last = e
                retryable = True
        finally:
            _inflight.release()
        if retryable and attempt < RETRIES - 1:
            delay = min(8.0, 1.5 * (2**attempt))
            log(f"  retry {attempt + 1} after {delay:.1f}s: {last}")
            time.sleep(delay)
            continue
        break
    _consecutive_failures += 1
    raise last or RuntimeError(f"GET failed: {url}")


def http_json(url: str) -> Any:
    return json.loads(http_get(url))


def category_label(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    return CATEGORY_NAMES.get(key, (raw or "Other").strip().title() or "Other")


def display_name(channel: dict) -> str:
    cid = str(channel.get("id") or "")
    if cid in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[cid]
    return str(channel.get("name") or cid)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        s = re.sub(r"\.\d+", "", s)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def xmltv_ts(value: str | None) -> str | None:
    dt = parse_iso(value)
    if not dt:
        return None
    return dt.strftime("%Y%m%d%H%M%S +0000")


def xml_esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def m3u_attr(value: str) -> str:
    return (value or "").replace('"', "'").replace("\n", " ").replace("\r", " ")

def m3u_name(value: str) -> str:
    # Display names after EXTINF cannot contain commas; many players split on the first one.
    return m3u_attr(value).replace(",", " ·")



def attr_block(**attrs: str | int | None) -> str:
    parts: list[str] = []
    for key, val in attrs.items():
        if val is None or val == "":
            continue
        parts.append(f'{key}="{m3u_attr(str(val))}"')
    return (" " + " ".join(parts)) if parts else ""


LOGO_DIR = Path(__file__).resolve().parent / "static" / "logos"


def logo_url(channel_id: str | None = None) -> str:
    # Per-channel name tile when one exists (tools/make_logos.py), else the brand logo.
    if channel_id and (LOGO_DIR / f"{channel_id}.png").is_file():
        return f"{PUBLIC_BASE}/logos/{urllib.parse.quote(channel_id)}.png"
    return f"{PUBLIC_BASE}/logo.png"


def fetch_channels() -> list[dict]:
    data = http_json(f"{API}/api/channels")
    if not isinstance(data, list):
        raise RuntimeError("unexpected /api/channels payload")
    channels = [c for c in data if isinstance(c, dict) and c.get("id")]
    channels.sort(key=lambda c: (int(c.get("number") or 0), str(c.get("id"))))
    return channels


def fetch_guide() -> dict:
    # The site caps this at 12 hours; 24 returns 422.
    return http_json(f"{API}/api/guide?hours=12")


def fetch_now(channel_id: str) -> dict | None:
    url = f"{API}/api/channels/{urllib.parse.quote(channel_id, safe='')}/now"
    try:
        data = http_json(url)
    except Exception as e:  # noqa: BLE001
        log(f"  /now failed for {channel_id}: {e}")
        return None
    return data if isinstance(data, dict) else None


def current_stream(now: dict | None) -> tuple[str | None, dict | None, float | None]:
    if not now:
        return None, None, None
    cur = now.get("current") or {}
    prog = cur.get("program") if isinstance(cur, dict) else None
    if not isinstance(prog, dict):
        return None, None, None
    url = (prog.get("stream_url") or "").strip()
    if not url:
        ident = (prog.get("identifier") or "").strip()
        fname = (prog.get("file_name") or "").strip()
        if ident and fname:
            url = f"https://archive.org/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(fname)}"
    offset = now.get("offset_seconds")
    try:
        off = float(offset) if offset is not None else None
    except (TypeError, ValueError):
        off = None
    return (url or None), prog, off


def fetch_now_all(channels: list[dict]) -> dict[str, dict | None]:
    ids = [str(c["id"]) for c in channels]
    out: dict[str, dict | None] = {}
    log(f"Fetching /now for {len(ids)} channels…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=NOW_WORKERS) as pool:
        futs = {pool.submit(fetch_now, cid): cid for cid in ids}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            cid = futs[fut]
            try:
                out[cid] = fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"  /now error {cid}: {e}")
                out[cid] = None
            if i % 20 == 0 or i == len(ids):
                log(f"  {i}/{len(ids)}")
    return out


def write_live_m3u(path: Path, channels: list[dict], now_map: dict[str, dict | None]) -> dict:
    lines = [
        f'#EXTM3U url-tvg="{PUBLIC_BASE}/guide.xml" x-tvg-url="{PUBLIC_BASE}/guide.xml"',
        "#EXTENC:UTF-8",
        "#PLAYLIST:SlamVision",
        "# SlamVision IPTV beta. Live URLs are the tape currently scheduled on each channel.",
        "# Playback starts at the beginning of that tape, not mid-show. Playlist refreshes every 30 minutes.",
    ]
    playable = 0
    missing: list[str] = []
    for ch in channels:
        cid = str(ch["id"])
        name = display_name(ch)
        number = int(ch.get("number") or 0)
        group = category_label(ch.get("category"))
        url, prog, _off = current_stream(now_map.get(cid))
        if not url:
            missing.append(f"{number} {cid}")
            continue
        title = name
        if isinstance(prog, dict) and prog.get("title"):
            # Keep the channel as the listing name; players show EPG title separately.
            title = name
        ext = attr_block(
            **{
                "tvg-id": cid,
                "tvg-name": m3u_name(name),
                "tvg-logo": logo_url(cid),
                "tvg-chno": number,
                "group-title": group,
            }
        )
        lines.append(f"#EXTINF:-1{ext},{m3u_name(title)}")
        lines.append(url)
        playable += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"playable": playable, "missing": missing, "total_channels": len(channels)}


def write_guide(path: Path, channels: list[dict], guide: dict) -> dict:
    rows = guide.get("rows") or []
    row_by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ch = row.get("channel") or {}
        cid = ch.get("id")
        if cid:
            row_by_id[str(cid)] = row

    tv = ET.Element(
        "tv",
        {
            "generator-info-name": "SlamVision IPTV",
            "generator-info-url": "https://github.com/jhatren927-ai/slamvision-iptv",
        },
    )
    # Channels first, in number order, then programmes.
    for ch in channels:
        cid = str(ch["id"])
        el = ET.SubElement(tv, "channel", {"id": cid})
        name = display_name(ch)
        dn = ET.SubElement(el, "display-name")
        dn.text = name
        if ch.get("number"):
            dn2 = ET.SubElement(el, "display-name")
            dn2.text = str(int(ch["number"]))
        ET.SubElement(el, "icon", {"src": logo_url(cid)})

    programmes = 0
    skipped = 0
    for ch in channels:
        cid = str(ch["id"])
        row = row_by_id.get(cid) or {}
        for slot in row.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            start = xmltv_ts(slot.get("start"))
            stop = xmltv_ts(slot.get("end"))
            prog = slot.get("program") or {}
            if not start or not stop or not isinstance(prog, dict):
                skipped += 1
                continue
            pel = ET.SubElement(tv, "programme", {"start": start, "stop": stop, "channel": cid})
            title = ET.SubElement(pel, "title", {"lang": "en"})
            title.text = str(prog.get("title") or "Untitled")
            episode = (prog.get("episode") or "").strip()
            if episode:
                st = ET.SubElement(pel, "sub-title", {"lang": "en"})
                st.text = episode
            desc_parts = [p for p in (episode, display_name(ch)) if p]
            if desc_parts:
                de = ET.SubElement(pel, "desc", {"lang": "en"})
                de.text = " · ".join(desc_parts)
            try:
                dur = float(prog.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            if dur > 0:
                ln = ET.SubElement(pel, "length", {"units": "seconds"})
                ln.text = str(int(round(dur)))
            programmes += 1

    xml_bytes = ET.tostring(tv, encoding="utf-8", xml_declaration=True)
    # ElementTree doesn't add a DOCTYPE; the declaration plus utf-8 is enough for players.
    path.write_bytes(xml_bytes + b"\n")
    return {
        "programmes": programmes,
        "skipped": skipped,
        "window_start": guide.get("window_start"),
        "window_end": guide.get("window_end"),
        "server_time": guide.get("server_time"),
        "hours": 12,
        "hours_note": "The site API caps /api/guide at 12 hours (24 returns 422).",
    }


CACHE_DIR: Path | None = None
CACHE_TTL = 7 * 24 * 3600


def set_cache_dir(path: Path | None) -> None:
    global CACHE_DIR
    CACHE_DIR = path
    if CACHE_DIR:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(channel_id: str, page: int) -> Path | None:
    if not CACHE_DIR:
        return None
    safe = urllib.parse.quote(channel_id, safe="")
    return CACHE_DIR / safe / f"page-{page}.json"


def fetch_ondemand_page(channel_id: str, page: int) -> dict:
    cached = _cache_path(channel_id, page)
    if cached and cached.is_file() and time.time() - cached.stat().st_mtime < CACHE_TTL:
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("items") is not None:
                return data
        except (OSError, json.JSONDecodeError):
            pass
    q = urllib.parse.urlencode({"page": page, "channel_id": channel_id})
    data = http_json(f"{API}/api/ondemand?{q}")
    if not isinstance(data, dict):
        raise RuntimeError(f"bad ondemand page {channel_id} p{page}")
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(data), encoding="utf-8")
    return data


def iter_channel_tapes(channel_id: str) -> list[dict]:
    first = fetch_ondemand_page(channel_id, 1)
    items = list(first.get("items") or [])
    total = int(first.get("total") or len(items))
    # Page size is fixed at 24 by the site.
    page_size = max(len(first.get("items") or []) or 24, 1)
    last_page = max((total + page_size - 1) // page_size, 1)
    if last_page <= 1:
        return [it for it in items if isinstance(it, dict)]
    # Fetch remaining pages a few at a time so we do not burst the site.
    got: dict[int, list] = {}
    failed: list[int] = []
    pages = list(range(2, last_page + 1))
    batch = max(WORKERS, 1)
    for i in range(0, len(pages), batch):
        chunk = pages[i : i + batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch) as pool:
            futs = {pool.submit(fetch_ondemand_page, channel_id, p): p for p in chunk}
            for fut in concurrent.futures.as_completed(futs):
                p = futs[fut]
                try:
                    data = fut.result()
                    got[p] = list(data.get("items") or [])
                except Exception as e:  # noqa: BLE001
                    log(f"  ondemand {channel_id} page {p} failed: {e}")
                    failed.append(p)
        if failed:
            # one more gentle pass for this channel
            still = []
            for p in failed:
                time.sleep(1.0)
                try:
                    data = fetch_ondemand_page(channel_id, p)
                    got[p] = list(data.get("items") or [])
                except Exception as e:  # noqa: BLE001
                    log(f"  ondemand {channel_id} page {p} failed again: {e}")
                    still.append(p)
            failed = still
    for p in sorted(got):
        items.extend(got[p])
    return [it for it in items if isinstance(it, dict)]


def tape_entries(tape: dict, group: str, logo: str) -> list[tuple[str, str]]:
    """Return (extinf_line, url) pairs for a tape, one per playable part."""
    parts = [p for p in (tape.get("parts") or []) if isinstance(p, dict) and (p.get("stream_url") or "").strip()]
    if not parts:
        return []
    title = str(tape.get("title") or "Untitled tape").strip() or "Untitled tape"
    episode = str(tape.get("episode") or "").strip()
    year = str(tape.get("year") or "").strip()
    extra_parts = []
    if episode and episode not in title:
        extra_parts.append(episode)
    if year and year not in title and year not in episode:
        extra_parts.append(year)
    label = f"{title} ({' · '.join(extra_parts)})" if extra_parts else title
    key = str(tape.get("key") or tape.get("identifier") or title)
    out: list[tuple[str, str]] = []
    for i, part in enumerate(parts, 1):
        url = part["stream_url"].strip()
        dur = part.get("duration") or tape.get("duration") or -1
        try:
            dur_f = float(dur)
        except (TypeError, ValueError):
            dur_f = -1
        dur_s = -1 if dur_f <= 0 else int(round(dur_f))
        part_title = label if len(parts) == 1 else f"{label} (part {i}/{len(parts)})"
        vid = key if len(parts) == 1 else f"{key}#p{i}"
        ext = attr_block(
            **{
                "tvg-id": vid,
                "tvg-name": m3u_name(part_title),
                "tvg-logo": logo,
                "group-title": group,
            }
        )
        out.append((f"#EXTINF:{dur_s}{ext},{m3u_name(part_title)}", url))
    return out


def fetch_new_tapes(limit: int = 40) -> list[dict]:
    try:
        data = http_json(f"{API}/api/ondemand/new?limit={limit}")
    except Exception as e:  # noqa: BLE001
        log(f"  /ondemand/new failed: {e}")
        return []
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if isinstance(data, dict):
        return [t for t in (data.get("items") or []) if isinstance(t, dict)]
    return []



def channels_from_m3u(path: Path) -> list[dict]:
    channels: list[dict] = []
    pending: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXTINF:"):
            attrs = dict(__import__("re").findall(r'([\w-]+)="([^"]*)"', line))
            pending = attrs
        elif pending and line.startswith("http"):
            channels.append(
                {
                    "id": pending.get("tvg-id") or "",
                    "name": pending.get("tvg-name") or pending.get("tvg-id") or "",
                    "number": int(pending.get("tvg-chno") or 0),
                    "category": pending.get("group-title") or "",
                }
            )
            pending = None
    return [c for c in channels if c.get("id")]


def write_ondemand_m3u_from_cache(path: Path, channels: list[dict]) -> dict:
    """Assemble the VOD playlist from cached /api/ondemand pages only."""
    lines = [
        f'#EXTM3U url-tvg="{PUBLIC_BASE}/guide.xml"',
        "#EXTENC:UTF-8",
        "#PLAYLIST:SlamVision On Demand",
        "# Built from cached On Demand pages. The GitHub Action fills this in gradually.",
    ]
    stats = {"entries": 0, "tapes": 0, "skipped_unplayable": 0, "channels_with_vod": 0, "errors": []}
    logo = logo_url()
    by_id = {str(c["id"]): c for c in channels}
    cache = CACHE_DIR or (path.parent / ".cache" / "ondemand")
    if not cache.is_dir():
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return stats
    # Prefer channel order from the live list; then any extra cache folders.
    folder_ids = {urllib.parse.unquote(d.name) for d in cache.iterdir() if d.is_dir()}
    ordered = [c for c in channels if c["id"] in folder_ids]
    seen = {c["id"] for c in ordered}
    for fid in sorted(folder_ids - seen):
        ordered.append({"id": fid, "name": fid, "number": 0, "category": ""})
    for ch in ordered:
        cid = str(ch["id"])
        name = display_name(ch)
        folder = cache / urllib.parse.quote(cid, safe="")
        pages = sorted(folder.glob("page-*.json"), key=lambda p: int(p.stem.split("-")[-1]))
        tapes: list[dict] = []
        for pf in pages:
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tapes.extend(it for it in (data.get("items") or []) if isinstance(it, dict))
        wrote = 0
        for tape in tapes:
            entries = tape_entries(tape, name, logo)
            if not entries:
                stats["skipped_unplayable"] += 1
                continue
            stats["tapes"] += 1
            for ext, url in entries:
                lines.append(ext)
                lines.append(url)
                stats["entries"] += 1
                wrote += 1
        if wrote:
            stats["channels_with_vod"] += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stats["bytes"] = path.stat().st_size
    return stats


def write_ondemand_m3u(path: Path, channels: list[dict]) -> dict:
    lines = [
        f'#EXTM3U url-tvg="{PUBLIC_BASE}/guide.xml"',
        "#EXTENC:UTF-8",
        "#PLAYLIST:SlamVision On Demand",
        "# Full On Demand library, grouped by channel. Tapes that appear on more than one channel are listed under each.",
    ]
    stats = {
        "entries": 0,
        "tapes": 0,
        "skipped_unplayable": 0,
        "channels_with_vod": 0,
        "pages_fetched": 0,
        "errors": [],
    }
    logo = logo_url()

    new_tapes = fetch_new_tapes(40)
    if new_tapes:
        for tape in new_tapes:
            entries = tape_entries(tape, "New on SlamVision", logo)
            if not entries:
                stats["skipped_unplayable"] += 1
                continue
            stats["tapes"] += 1
            for ext, url in entries:
                lines.append(ext)
                lines.append(url)
                stats["entries"] += 1

    for i, ch in enumerate(channels, 1):
        cid = str(ch["id"])
        name = display_name(ch)
        group = name
        log(f"  On Demand {i}/{len(channels)} {name} ({cid})")
        try:
            tapes = iter_channel_tapes(cid)
        except CircuitOpen as e:
            log(f"    Circuit open: {e}. Writing what we have so far.")
            stats["errors"].append(str(e))
            break
        except Exception as e:  # noqa: BLE001
            log(f"    FAILED: {e}")
            stats["errors"].append(f"{cid}: {e}")
            continue
        wrote = 0
        for tape in tapes:
            entries = tape_entries(tape, group, logo)
            if not entries:
                stats["skipped_unplayable"] += 1
                continue
            stats["tapes"] += 1
            for ext, url in entries:
                lines.append(ext)
                lines.append(url)
                stats["entries"] += 1
                wrote += 1
        if wrote:
            stats["channels_with_vod"] += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stats["bytes"] = path.stat().st_size
    return stats


def vod_is_stale(path: Path, hours: float) -> bool:
    if not path.is_file() or path.stat().st_size < 100:
        return True
    age = time.time() - path.stat().st_mtime
    return age > hours * 3600


def write_index(path: Path, meta: dict) -> None:
    live_url = f"{PUBLIC_BASE}/slamvision.m3u"
    vod_url = f"{PUBLIC_BASE}/slamvision-ondemand.m3u"
    guide_url = f"{PUBLIC_BASE}/guide.xml"
    updated = meta.get("generated_at") or ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SlamVision IPTV (beta)</title>
  <style>
    body {{ margin: 0; font-family: Georgia, serif; background: #0a0e18; color: #e8edf7; }}
    main {{ max-width: 42rem; margin: 0 auto; padding: 3rem 1.25rem 4rem; }}
    h1 {{ font-family: Arial, sans-serif; letter-spacing: .06em; color: #f5d76e; font-size: 1.4rem; }}
    p, li {{ line-height: 1.5; }}
    a {{ color: #f5d76e; }}
    code, pre {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .9rem; }}
    pre {{ background: #151b2b; padding: 1rem; overflow-x: auto; border-radius: 6px; color: #fff; }}
    .mute {{ color: #9aa3b5; font-size: .95rem; }}
  </style>
</head>
<body>
  <main>
    <h1>SLAMVISION IPTV · BETA</h1>
    <p>Free playlist and guide for Roku IPTV apps. This is a beta. Live channels start at the beginning of the tape that is currently scheduled, not mid-show.</p>
    <p>Updated: {xml_esc(updated)}</p>
    <p>Live playlist ({meta.get("live_channels", "?")} channels):</p>
    <pre>{xml_esc(live_url)}</pre>
    <p>TV guide (next 12 hours):</p>
    <pre>{xml_esc(guide_url)}</pre>
    <p>On Demand library:</p>
    <pre>{xml_esc(vod_url)}</pre>
    <p>On a Roku, install <strong>IPTV Smart Player Lite</strong>, add a playlist, and paste the live playlist URL. Use the Roku mobile app keyboard so you can paste. If the guide is empty, add the guide URL in the app’s EPG settings. Add the On Demand URL as a second playlist.</p>
    <p class="mute">The full SlamVision app, with a real live guide that joins mid-show, is on Android TV and Fire TV. In Downloader, delete any old SlamVision .apk in Files, then enter code 3059600.</p>
    <p class="mute"><a href="https://github.com/jhatren927-ai/slamvision-iptv">GitHub</a> · <a href="https://ring-channel-tv.emergent.host/">SlamVision</a></p>
  </main>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SlamVision IPTV feeds")
    parser.add_argument("--out", default="output", help="output directory")
    parser.add_argument(
        "--vod",
        choices=("always", "auto", "never"),
        default="auto",
        help="rebuild On Demand list (auto = if missing or older than --vod-max-age-hours)",
    )
    parser.add_argument("--vod-max-age-hours", type=float, default=24.0)
    parser.add_argument("--vod-from-cache", action="store_true", help="build On Demand only from cached pages, no new API calls")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    set_cache_dir(out / ".cache" / "ondemand")
    logo_src = Path(__file__).resolve().parent / "static" / "logo.png"
    if logo_src.is_file():
        shutil.copy2(logo_src, out / "logo.png")
    if LOGO_DIR.is_dir():
        shutil.copytree(LOGO_DIR, out / "logos", dirs_exist_ok=True)

    if args.vod_from_cache:
        if not any((out / ".cache" / "ondemand").glob("*/*.json")):
            log("No On Demand cache present")
            return 1
        # Channel names come from the already-generated live playlist if present.
        log("Building On Demand playlist from cache only (no API calls)…")
        # A tiny stub channel list from cached folder names is not enough for titles.
        # Prefer the live M3U already in this output dir, else fail.
        live_existing = out / "slamvision.m3u"
        if not live_existing.is_file():
            log("Need an existing slamvision.m3u to name On Demand groups")
            return 1
        channels = channels_from_m3u(live_existing)
        set_cache_dir(out / ".cache" / "ondemand")
        vod_stats = write_ondemand_m3u_from_cache(out / "slamvision-ondemand.m3u", channels)
        log(f"On Demand from cache: {vod_stats['entries']} entries")
        return 0

    log("Health-checking the SlamVision API…")
    try:
        fetch_channels()
        reset_circuit()
    except Exception as e:  # noqa: BLE001
        log(f"API is not healthy ({e}). Not fetching anything this run.")
        return 2

    log("Fetching channel list and 12-hour guide…")
    reset_circuit()
    channels = fetch_channels()
    guide = fetch_guide()
    now_map = fetch_now_all(channels)

    live_path = out / "slamvision.m3u"
    guide_path = out / "guide.xml"
    vod_path = out / "slamvision-ondemand.m3u"

    live_stats = write_live_m3u(live_path, channels, now_map)
    guide_stats = write_guide(guide_path, channels, guide)
    log(f"Live: {live_stats['playable']}/{live_stats['total_channels']} playable")
    if live_stats["missing"]:
        log("  no stream for: " + ", ".join(live_stats["missing"][:20]))
    log(f"Guide: {guide_stats['programmes']} programmes, window {guide_stats['window_start']} → {guide_stats['window_end']}")

    vod_stats: dict | None = None
    do_vod = args.vod == "always" or (args.vod == "auto" and vod_is_stale(vod_path, args.vod_max_age_hours))
    if do_vod:
        log("Building On Demand playlist (this can take a few minutes)…")
        vod_stats = write_ondemand_m3u(vod_path, channels)
        log(f"On Demand: {vod_stats['entries']} entries from {vod_stats['tapes']} tapes, {vod_stats['bytes']} bytes")
        if vod_stats["errors"]:
            log("  errors: " + "; ".join(vod_stats["errors"][:10]))
    elif vod_path.is_file():
        log(f"On Demand playlist kept (age ok): {vod_path.stat().st_size} bytes")
    else:
        log("On Demand playlist skipped")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta = {
        "generated_at": generated_at,
        "api": API,
        "public_base": PUBLIC_BASE,
        "live_channels": live_stats["playable"],
        "listed_channels": live_stats["total_channels"],
        "missing_live": live_stats["missing"],
        "guide": guide_stats,
        "vod": vod_stats,
        "vod_rebuilt": bool(do_vod),
    }
    (out / "build.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    write_index(out / "index.html", meta)
    log("Wrote " + str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
