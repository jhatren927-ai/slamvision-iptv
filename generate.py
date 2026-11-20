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
    """Viewer-facing name; matches the Android app's ChannelNames.display."""
    cid = str(channel.get("id") or "")
    name = str(channel.get("name") or cid).strip()
    if cid in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[cid]
    if cid.lower() == "rndm" or name.lower() in {"random wrestling", "random"}:
        return "Surprise TV"
    if name.lower() in {"wwe", "wwf", "wwwf"}:
        return name.upper()
    return name or cid


def category_slug(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    if key in CATEGORY_NAMES:
        return key
    reverse = {v.lower(): k for k, v in CATEGORY_NAMES.items()}
    return reverse.get(key, key or "other")


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


def catalog_id(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (raw or "").strip())
    return s.strip("-._") or "other"


def section_for(kind: str) -> str:
    key = (kind or "").strip().lower()
    if key in {"new", "just added"}:
        return "Just Added"
    if key == "promotions":
        return "Promotions"
    if key in {"shows", "eras", "shows & eras"}:
        return "Shows & Eras"
    if key in {"wrestlers", "star channels"}:
        return "Star Channels"
    return "More Channels"


def duration_sec(value: Any) -> int:
    try:
        dur = float(value)
    except (TypeError, ValueError):
        return -1
    return -1 if dur <= 0 else int(round(dur))


def playable_parts(tape: dict) -> list[dict]:
    parts: list[dict] = []
    for p in tape.get("parts") or []:
        if not isinstance(p, dict):
            continue
        url = (p.get("stream_url") or "").strip()
        if not url:
            continue
        parts.append(
            {
                "url": url,
                "duration": duration_sec(p.get("duration") or tape.get("duration")),
                "title": str(p.get("title") or "").strip(),
            }
        )
    return parts


def tape_label(tape: dict, part_index: int | None = None, part_count: int = 1) -> str:
    title = str(tape.get("title") or "Untitled tape").strip() or "Untitled tape"
    episode = str(tape.get("episode") or "").strip()
    year = str(tape.get("year") or "").strip()
    extra: list[str] = []
    if episode and episode not in title:
        extra.append(episode)
    if year and year not in title and year not in episode:
        extra.append(year)
    label = f"{title} ({' · '.join(extra)})" if extra else title
    if part_count > 1 and part_index is not None:
        return f"{label} (part {part_index}/{part_count})"
    return label


def catalog_item(tape: dict, logo: str, part: dict, part_index: int, part_count: int) -> dict:
    key = str(tape.get("key") or tape.get("identifier") or tape.get("title") or "tape")
    if part_count > 1:
        key = f"{key}#p{part_index}"
    item = {
        "id": key,
        "channel_id": str(tape.get("channel_id") or ""),
        "identifier": str(tape.get("identifier") or ""),
        "title": tape_label(tape, part_index if part_count > 1 else None, part_count),
        "show": str(tape.get("title") or "").strip(),
        "episode": str(tape.get("episode") or "").strip(),
        "year": str(tape.get("year") or "").strip(),
        "duration": part["duration"],
        "logo": logo,
        "url": part["url"],
    }
    if part_count > 1:
        item["part"] = part_index
        item["parts"] = part_count
    return item


def collect_items(tapes: list[dict], logo_for, stats: dict) -> list[dict]:
    """Playable parts for a shelf, one entry per unique archive.org URL."""
    items: list[dict] = []
    seen: set[str] = set()
    for tape in tapes:
        parts = playable_parts(tape)
        if not parts:
            stats["skipped_unplayable"] += 1
            continue
        stats["tapes"] += 1
        logo = logo_for(str(tape.get("channel_id") or ""))
        for i, part in enumerate(parts, 1):
            if part["url"] in seen:
                stats["deduped"] += 1
                continue
            seen.add(part["url"])
            items.append(catalog_item(tape, logo, part, i, len(parts)))
    return items


def add_group(
    groups: list[dict],
    stats: dict,
    gid: str,
    title: str,
    kind: str,
    items: list[dict],
    number: int = 0,
    logo: str | None = None,
) -> None:
    if not items:
        return
    groups.append(
        {
            "id": catalog_id(gid),
            "title": title,
            "kind": kind,
            "section": section_for(kind),
            "number": number,
            "logo": logo or logo_url(),
            "items": items,
        }
    )
    stats["entries"] += len(items)
    stats["categories"] += 1
    if kind != "new":
        stats["channels_with_vod"] += 1


def m3u_entry(item: dict, group: str) -> tuple[str, str]:
    dur_s = item["duration"] if item.get("duration", -1) and item["duration"] > 0 else -1
    ext = attr_block(
        **{
            "tvg-id": item.get("id") or item.get("url"),
            "tvg-name": m3u_name(item.get("title") or "Untitled tape"),
            "tvg-logo": item.get("logo") or logo_url(),
            "group-title": group,
        }
    )
    return f"#EXTINF:{dur_s}{ext},{m3u_name(item.get('title') or 'Untitled tape')}", item["url"]


def write_ondemand_m3u_file(path: Path, groups: list[dict]) -> int:
    lines = [
        f'#EXTM3U url-tvg="{PUBLIC_BASE}/guide.xml"',
        "#EXTENC:UTF-8",
        "#PLAYLIST:SlamVision On Demand",
        "# Full library. group-title matches the Android On Demand shelves: Just Added, years/eras, promotions, shows, and star channels.",
    ]
    for g in groups:
        for item in g["items"]:
            ext, url = m3u_entry(item, g["title"])
            lines.append(ext)
            lines.append(url)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.stat().st_size


def write_ondemand_json(out: Path, groups: list[dict], generated_at: str) -> dict:
    """Write ondemand/index.json plus one JSON file per category for incremental Roku loads."""
    dest = out / "ondemand"
    dest.mkdir(parents=True, exist_ok=True)
    cat_bytes = 0
    index_cats: list[dict] = []
    for g in groups:
        payload = {
            "id": g["id"],
            "title": g["title"],
            "kind": g["kind"],
            "section": g["section"],
            "count": len(g["items"]),
            "logo": g.get("logo") or logo_url(),
            "items": g["items"],
        }
        path = dest / f"{g['id']}.json"
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        path.write_text(text, encoding="utf-8")
        cat_bytes += path.stat().st_size
        index_cats.append(
            {
                "id": g["id"],
                "title": g["title"],
                "kind": g["kind"],
                "section": g["section"],
                "count": len(g["items"]),
                "logo": g.get("logo") or logo_url(),
                "url": f"{PUBLIC_BASE}/ondemand/{g['id']}.json",
            }
        )
    index = {
        "generated_at": generated_at,
        "logo": logo_url(),
        "m3u": f"{PUBLIC_BASE}/slamvision-ondemand.m3u",
        "total_entries": sum(c["count"] for c in index_cats),
        "categories": index_cats,
    }
    index_path = dest / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    keep = {f"{g['id']}.json" for g in groups} | {"index.json"}
    for path in dest.glob("*.json"):
        if path.name not in keep:
            path.unlink()
    # Also write under logos/ondemand so the currently installed refresh
    # workflow (which publishes logos/ but not yet ondemand/) keeps a copy.
    logo_dest = out / "logos" / "ondemand"
    logo_dest.mkdir(parents=True, exist_ok=True)
    for path in dest.glob("*.json"):
        shutil.copy2(path, logo_dest / path.name)
    for path in logo_dest.glob("*.json"):
        if path.name not in keep:
            path.unlink()
    return {
        "index_bytes": index_path.stat().st_size,
        "category_bytes": cat_bytes,
        "category_files": len(index_cats),
        "index_url": f"{PUBLIC_BASE}/ondemand/index.json",
        "groups": [{"id": c["id"], "title": c["title"], "section": c["section"], "count": c["count"]} for c in index_cats],
    }


def write_ondemand_outputs(out: Path, groups: list[dict], stats: dict, generated_at: str | None = None) -> dict:
    ts = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stats["bytes"] = write_ondemand_m3u_file(out / "slamvision-ondemand.m3u", groups)
    json_stats = write_ondemand_json(out, groups, ts)
    stats.update(json_stats)
    stats["json_bytes"] = json_stats["index_bytes"] + json_stats["category_bytes"]
    return stats


def fetch_new_tapes(limit: int = 40, use_network: bool = True) -> list[dict]:
    cached = (CACHE_DIR / "_new.json") if CACHE_DIR else None

    def from_payload(data: Any) -> list[dict] | None:
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict)]
        if isinstance(data, dict):
            return [t for t in (data.get("items") or []) if isinstance(t, dict)]
        return None

    if cached and cached.is_file():
        age_ok = time.time() - cached.stat().st_mtime < CACHE_TTL
        if age_ok or not use_network:
            try:
                parsed = from_payload(json.loads(cached.read_text(encoding="utf-8")))
                if parsed is not None:
                    return parsed
            except (OSError, json.JSONDecodeError):
                pass
        if not use_network:
            return []
    if not use_network:
        return []
    try:
        data = http_json(f"{API}/api/ondemand/new?limit={limit}")
    except Exception as e:  # noqa: BLE001
        log(f"  /ondemand/new failed: {e}")
        if cached and cached.is_file():
            try:
                parsed = from_payload(json.loads(cached.read_text(encoding="utf-8")))
                if parsed is not None:
                    return parsed
            except (OSError, json.JSONDecodeError):
                pass
        return []
    tapes = from_payload(data) or []
    if cached:
        cached.write_text(json.dumps(data), encoding="utf-8")
    return tapes


def cached_channel_tapes(channel_id: str) -> list[dict]:
    cache = CACHE_DIR
    if not cache:
        return []
    folder = cache / urllib.parse.quote(channel_id, safe="")
    if not folder.is_dir():
        return []
    pages = sorted(folder.glob("page-*.json"), key=lambda p: int(p.stem.split("-")[-1]))
    tapes: list[dict] = []
    for pf in pages:
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tapes.extend(it for it in (data.get("items") or []) if isinstance(it, dict))
    return tapes


def empty_vod_stats() -> dict:
    return {
        "entries": 0,
        "tapes": 0,
        "skipped_unplayable": 0,
        "deduped": 0,
        "channels_with_vod": 0,
        "categories": 0,
        "pages_fetched": 0,
        "errors": [],
    }


def build_ondemand_groups(channels: list[dict], fetch: bool) -> tuple[list[dict], dict]:
    """Build Android-style shelves: Just Added, then every live channel (years, promotions, shows, stars)."""
    stats = empty_vod_stats()
    groups: list[dict] = []

    new_tapes = fetch_new_tapes(40, use_network=fetch)
    if new_tapes:
        items = collect_items(new_tapes, logo_url, stats)
        add_group(groups, stats, "new", "New on SlamVision", "new", items, logo=logo_url())

    for i, ch in enumerate(channels, 1):
        cid = str(ch["id"])
        name = display_name(ch)
        kind = category_slug(ch.get("category"))
        log(f"  On Demand {i}/{len(channels)} {name} ({cid})")
        try:
            tapes = iter_channel_tapes(cid) if fetch else cached_channel_tapes(cid)
        except CircuitOpen as e:
            log(f"    Circuit open: {e}. Writing what we have so far.")
            stats["errors"].append(str(e))
            break
        except Exception as e:  # noqa: BLE001
            log(f"    FAILED: {e}")
            stats["errors"].append(f"{cid}: {e}")
            continue
        items = collect_items(tapes, lambda _tid, fallback=cid: logo_url(_tid or fallback), stats)
        if items:
            log(f"    {len(items)} playable (from {len(tapes)} listed)")
        add_group(
            groups,
            stats,
            cid,
            name,
            kind,
            items,
            number=int(ch.get("number") or 0),
            logo=logo_url(cid),
        )
    return groups, stats


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
    """Assemble the VOD playlist and JSON catalog from cached /api/ondemand pages only."""
    groups, stats = build_ondemand_groups(channels, fetch=False)
    return write_ondemand_outputs(path.parent, groups, stats)


def write_ondemand_m3u(path: Path, channels: list[dict]) -> dict:
    groups, stats = build_ondemand_groups(channels, fetch=True)
    return write_ondemand_outputs(path.parent, groups, stats)


def vod_is_stale(path: Path, hours: float) -> bool:
    if not path.is_file() or path.stat().st_size < 100:
        return True
    age = time.time() - path.stat().st_mtime
    return age > hours * 3600


def write_index(path: Path, meta: dict) -> None:
    live_url = f"{PUBLIC_BASE}/slamvision.m3u"
    vod_url = f"{PUBLIC_BASE}/slamvision-ondemand.m3u"
    vod_index_url = f"{PUBLIC_BASE}/ondemand/index.json"
    guide_url = f"{PUBLIC_BASE}/guide.xml"
    updated = meta.get("generated_at") or ""
    vod = meta.get("vod") or {}
    vod_entries = vod.get("entries", "?")
    vod_cats = vod.get("categories", "?")
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
    <p>On Demand library ({xml_esc(str(vod_entries))} titles in {xml_esc(str(vod_cats))} shelves):</p>
    <pre>{xml_esc(vod_url)}</pre>
    <p>On Demand JSON index (one file per shelf, for players that load a category at a time):</p>
    <pre>{xml_esc(vod_index_url)}</pre>
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
        log(
            f"On Demand from cache: {vod_stats['entries']} entries in "
            f"{vod_stats.get('categories', 0)} shelves, {vod_stats.get('bytes', 0)} bytes "
            f"(JSON {vod_stats.get('json_bytes', 0)} bytes)"
        )
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
    cache_ready = any((out / ".cache" / "ondemand").glob("*/*.json"))
    if do_vod:
        log("Building On Demand playlist (this can take a few minutes)…")
        vod_stats = write_ondemand_m3u(vod_path, channels)
        log(
            f"On Demand: {vod_stats['entries']} entries from {vod_stats['tapes']} tapes "
            f"in {vod_stats.get('categories', 0)} shelves, {vod_stats.get('bytes', 0)} bytes "
            f"(JSON {vod_stats.get('json_bytes', 0)} bytes)"
        )
        if vod_stats["errors"]:
            log("  errors: " + "; ".join(vod_stats["errors"][:10]))
    elif cache_ready:
        # Rebuild the M3U + JSON catalog from cache (no API). This keeps the
        # split catalog in the publish folder even when the live/guide-only path
        # is used.
        log("Rebuilding On Demand catalog from cache (no API calls)…")
        vod_stats = write_ondemand_m3u_from_cache(vod_path, channels)
        log(
            f"On Demand from cache: {vod_stats['entries']} entries in "
            f"{vod_stats.get('categories', 0)} shelves, {vod_stats.get('bytes', 0)} bytes"
        )
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
