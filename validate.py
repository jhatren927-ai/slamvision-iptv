#!/usr/bin/env python3
"""Validate generated SlamVision IPTV feeds and sample stream URLs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

UA = "SlamVision-IPTV/1.0 (+https://github.com/jhatren927-ai/slamvision-iptv)"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_m3u(path: Path) -> tuple[str, list[dict]]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("#EXTM3U"):
        raise SystemExit(f"{path} does not start with #EXTM3U")
    header = text.splitlines()[0]
    entries: list[dict] = []
    pending: dict | None = None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            pending = {"extinf": line}
            m = re.match(r"#EXTINF:(-?\d+(?:\.\d+)?)\s*(.*)", line)
            if not m:
                raise SystemExit(f"bad EXTINF: {line[:120]}")
            pending["duration"] = m.group(1)
            rest = m.group(2)
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', rest))
            pending["attrs"] = attrs
            name = rest.split(",", 1)[-1] if "," in rest else ""
            pending["name"] = name
        elif line.startswith("#") or not line.strip():
            continue
        else:
            if not pending:
                raise SystemExit(f"{path}: URL with no EXTINF: {line[:120]}")
            if not (line.startswith("http://") or line.startswith("https://")):
                raise SystemExit(f"{path}: non-http URL: {line[:120]}")
            pending["url"] = line.strip()
            entries.append(pending)
            pending = None
    if pending:
        raise SystemExit(f"{path}: trailing EXTINF with no URL")
    return header, entries


def validate_live(header: str, entries: list[dict], guide_channels: set[str]) -> list[str]:
    issues: list[str] = []
    if "url-tvg=" not in header:
        issues.append("live M3U header missing url-tvg")
    ids: list[str] = []
    chnos: list[int] = []
    prev_chno = -1
    for e in entries:
        a = e["attrs"]
        for req in ("tvg-id", "tvg-name", "group-title", "tvg-chno"):
            if not a.get(req):
                issues.append(f"missing {req} on {e.get('name')}")
        tid = a.get("tvg-id", "")
        ids.append(tid)
        try:
            n = int(a.get("tvg-chno", ""))
            chnos.append(n)
            if n < prev_chno:
                issues.append(f"channel order not by number: {n} after {prev_chno}")
            prev_chno = n
        except ValueError:
            issues.append(f"bad tvg-chno on {tid}")
        if tid and tid not in guide_channels:
            issues.append(f"tvg-id {tid} has no XMLTV channel")
    if len(ids) != len(set(ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        issues.append(f"duplicate live tvg-id: {dup[:8]}")
    return issues


def parse_xmltv(path: Path) -> tuple[set[str], int, list[str]]:
    issues: list[str] = []
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "tv":
        raise SystemExit("guide.xml root is not <tv>")
    channels = []
    for ch in root.findall("channel"):
        cid = ch.get("id")
        if not cid:
            issues.append("XMLTV channel missing id")
            continue
        if ch.find("display-name") is None or not (ch.find("display-name").text or "").strip():
            issues.append(f"XMLTV channel {cid} missing display-name")
        channels.append(cid)
    if len(channels) != len(set(channels)):
        issues.append("duplicate XMLTV channel ids")
    chanset = set(channels)
    programmes = 0
    ts_re = re.compile(r"^\d{14} [+-]\d{4}$")
    for p in root.findall("programme"):
        programmes += 1
        cid = p.get("channel")
        if cid not in chanset:
            issues.append(f"programme for unknown channel {cid}")
        for key in ("start", "stop"):
            val = p.get(key) or ""
            if not ts_re.match(val):
                issues.append(f"bad {key} time '{val}' on {cid}")
            elif not val.endswith(" +0000"):
                issues.append(f"{key} is not UTC +0000: {val}")
        if p.find("title") is None or not (p.find("title").text or "").strip():
            issues.append(f"programme missing title on {cid}")
    return chanset, programmes, issues


def head_check(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            ctype = r.headers.get("Content-Type", "")
            length = r.headers.get("Content-Length")
            final = r.geturl()
    except urllib.error.HTTPError as e:
        # Some CDNs reject HEAD; fall back to a 1-byte GET range.
        if e.code in (403, 405, 501):
            return range_probe(url)
        return {"ok": False, "code": e.code, "ctype": "", "length": None, "url": url, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": 0, "ctype": "", "length": None, "url": url, "error": str(e)}
    ok = code in (200, 206) and ("video" in ctype.lower() or "mpegurl" in ctype.lower() or "octet-stream" in ctype.lower())
    # archive.org may report video/mp4 on 200 after redirects — already handled.
    if code in (200, 206) and not ok:
        # empty content-type but mp4 path still counts
        if urlparse(final).path.lower().endswith((".mp4", ".m3u8", ".ts", ".mkv", ".webm")):
            ok = True
    return {"ok": ok, "code": code, "ctype": ctype, "length": length, "url": final, "error": None}


def range_probe(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Range": "bytes=0-1", "Accept": "*/*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            code = r.status
            ctype = r.headers.get("Content-Type", "")
            length = r.headers.get("Content-Length") or r.headers.get("Content-Range")
            final = r.geturl()
        ok = code in (200, 206) and (
            "video" in ctype.lower() or urlparse(final).path.lower().endswith((".mp4", ".m3u8", ".ts"))
        )
        return {"ok": ok, "code": code, "ctype": ctype, "length": length, "url": final, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": 0, "ctype": "", "length": None, "url": url, "error": str(e)}


def ffprobe(url: str) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height:format=duration,format_name",
        "-of",
        "json",
        "-user_agent",
        UA,
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "ffprobe failed")[:300]}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad ffprobe json"}
    streams = data.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    fmt = data.get("format") or {}
    return {
        "ok": has_video,
        "has_video": has_video,
        "duration": fmt.get("duration"),
        "format": fmt.get("format_name"),
        "streams": [
            {k: s.get(k) for k in ("codec_type", "codec_name", "width", "height")}
            for s in streams
        ],
        "error": None if has_video else "no video stream",
    }


def sample(entries: list[dict], n: int) -> list[dict]:
    if n <= 0 or not entries:
        return []
    if len(entries) <= n:
        return entries
    # spread across the list
    step = max(len(entries) // n, 1)
    picked = [entries[i * step] for i in range(n)]
    return picked


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="output")
    p.add_argument("--head-live", type=int, default=10)
    p.add_argument("--head-vod", type=int, default=10)
    p.add_argument("--probe", type=int, default=3, help="ffprobe this many live and VOD urls")
    args = p.parse_args()
    d = Path(args.dir)
    live_path = d / "slamvision.m3u"
    vod_path = d / "slamvision-ondemand.m3u"
    guide_path = d / "guide.xml"

    issues: list[str] = []
    guide_channels, programmes, gissues = parse_xmltv(guide_path)
    issues.extend(gissues)
    header, live = parse_m3u(live_path)
    issues.extend(validate_live(header, live, guide_channels))
    vod: list[dict] = []
    if vod_path.is_file():
        _h, vod = parse_m3u(vod_path)
        if not vod:
            issues.append("On Demand M3U parsed but has no entries")
    else:
        issues.append("slamvision-ondemand.m3u missing")

    index_path = d / "ondemand" / "index.json"
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            issues.append(f"ondemand/index.json is not valid JSON: {e}")
        else:
            cats = index.get("categories") or []
            print(f"VOD JSON shelves: {len(cats)}, total_entries: {index.get('total_entries')}")
            if not cats:
                issues.append("ondemand/index.json has no categories")
            for cat in cats:
                cid = cat.get("id") or ""
                cpath = d / "ondemand" / f"{cid}.json"
                if not cid:
                    issues.append("ondemand category missing id")
                    continue
                if not cpath.is_file():
                    issues.append(f"missing ondemand/{cid}.json")
                    continue
                try:
                    body = json.loads(cpath.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    issues.append(f"ondemand/{cid}.json is not valid JSON: {e}")
                    continue
                items = body.get("items") or []
                if int(cat.get("count") or 0) != len(items):
                    issues.append(f"ondemand/{cid}.json count {cat.get('count')} != {len(items)} items")
                missing_url = sum(1 for it in items if not (isinstance(it, dict) and str(it.get("url") or "").startswith("http")))
                if missing_url:
                    issues.append(f"ondemand/{cid}.json has {missing_url} items without an http url")
    elif vod:
        issues.append("ondemand/index.json missing")

    live_ids = {e["attrs"].get("tvg-id") for e in live}
    unused_guide = sorted(guide_channels - live_ids)
    # channels in the guide but not the playlist (no current stream) are worth noting, not failing
    print(f"LIVE entries: {len(live)}")
    print(f"VOD entries: {len(vod)}")
    print(f"XMLTV channels: {len(guide_channels)}, programmes: {programmes}")
    if unused_guide:
        print(f"Guide channels with no live URL: {len(unused_guide)} ({', '.join(unused_guide[:12])})")

    live_heads = []
    vod_heads = []
    for kind, entries, n, bucket in (
        ("live", live, args.head_live, live_heads),
        ("vod", vod, args.head_vod, vod_heads),
    ):
        for e in sample(entries, n):
            res = head_check(e["url"])
            res["name"] = e.get("name")
            bucket.append(res)
            flag = "OK" if res["ok"] else "FAIL"
            print(f"HEAD {kind} {flag} {res['code']} {res['ctype']} {e.get('name')} ({res.get('error') or res['url'][:80]})")
            if not res["ok"]:
                issues.append(f"HEAD {kind} failed: {e.get('name')} {res}")

    probes = []
    for kind, entries in (("live", live), ("vod", vod)):
        for e in sample(entries, args.probe):
            res = ffprobe(e["url"])
            res["name"] = e.get("name")
            res["kind"] = kind
            probes.append(res)
            flag = "OK" if res["ok"] else "FAIL"
            print(f"PROBE {kind} {flag} {e.get('name')} dur={res.get('duration')} fmt={res.get('format')} {res.get('error') or ''}")
            if not res["ok"]:
                issues.append(f"ffprobe {kind} failed: {e.get('name')} {res.get('error')}")

    report = {
        "live_entries": len(live),
        "vod_entries": len(vod),
        "xmltv_channels": len(guide_channels),
        "xmltv_programmes": programmes,
        "head_live": live_heads,
        "head_vod": vod_heads,
        "ffprobe": probes,
        "issues": issues,
        "ok": not issues,
    }
    (d / "validate.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if issues:
        print(f"\nFAILED with {len(issues)} issue(s):")
        for i in issues:
            print(" -", i)
        return 1
    print("\nVALIDATION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
