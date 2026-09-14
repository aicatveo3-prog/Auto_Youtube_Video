#!/usr/bin/env python3
"""
ytchannel - list every video on a YouTube channel, cheaply.

Uses yt-dlp's flat playlist mode, which reads channel pages instead of making
one request per video. Measured: 448 videos in 8.4 seconds.

Commands
--------
  list <channel> [--tabs videos,shorts] [--refresh] [--limit N]
  cached                       Show which channels are already cached.

Cache lives in channels/<channel_id>.json and is reused until --refresh.

Notes on what YouTube gives us in flat mode:
  available : id, title, duration, view_count
  missing   : upload date (timestamp is null), caption availability
Entries come back newest-first, so recency is positional rather than by date.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "channels"

BASE_FLAGS = ["--no-warnings", "--flat-playlist", "--ignore-no-formats-error"]
VALID_TABS = ("videos", "shorts", "streams")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def need_ytdlp() -> str:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not exe:
        die("yt-dlp not found on PATH")
    return exe


def channel_url(target: str) -> str:
    """
    Normalise whatever the user typed into a channel base URL.

    Bare handles and bare channel ids are rejected by yt-dlp, so they have to
    be expanded here: '@veritasium' and 'UCHny...' both fail on their own.
    """
    t = target.strip().rstrip("/")
    if not t:
        die("empty channel", 2)

    if t.startswith("@"):
        return f"https://www.youtube.com/{t}"
    if CHANNEL_ID_RE.match(t):
        return f"https://www.youtube.com/channel/{t}"

    if t.startswith(("http://", "https://")):
        if "youtube.com" not in t and "youtu.be" not in t:
            die(f"not a YouTube URL: {t}", 2)
        # Drop a trailing tab segment so we can append our own.
        for tab in VALID_TABS + ("featured", "playlists", "community"):
            if t.endswith("/" + tab):
                t = t[: -(len(tab) + 1)]
                break
        return t

    # A bare word is treated as a handle: 'veritasium' -> '@veritasium'
    if re.fullmatch(r"[A-Za-z0-9._-]+", t):
        return f"https://www.youtube.com/@{t}"
    die(f"could not interpret {target!r} as a channel", 2)


AVATAR_SIZE = 176          # 2x the 88px the card can ever need
_SIZE_RE = re.compile(r"=s\d+(-|$)")


def pick_images(thumbs: list[dict]) -> tuple[str, str]:
    """
    Split a channel's thumbnail list into (avatar, banner).

    YouTube returns both in one array. The avatar is the square one; banners are
    wide. Entries with no dimensions are duplicates of the ones that have them,
    so they are ignored.
    """
    square, wide = [], []
    for t in thumbs:
        w, h, url = t.get("width"), t.get("height"), t.get("url") or ""
        if not url or not w or not h:
            continue
        (square if w == h else wide).append((w, url))

    avatar = max(square, default=(0, ""))[1]
    banner = max(wide, default=(0, ""))[1]
    # Google's image host honours the =sNNN size token, so ask for a sane size
    # instead of pulling a 900px portrait to draw a 46px circle.
    if avatar and _SIZE_RE.search(avatar):
        avatar = _SIZE_RE.sub(f"=s{AVATAR_SIZE}\\1", avatar)
    return avatar, banner


def fetch_tab(exe: str, base: str, tab: str, limit: int | None,
              meta_lang: str = "") -> tuple[dict, list[dict]]:
    """Return (channel_info, entries) for one tab. Missing tabs yield ({}, [])."""
    cmd = [exe, *BASE_FLAGS, "-J"]
    if meta_lang:
        cmd += ["--extractor-args", f"youtube:lang={meta_lang}"]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    cmd += [f"{base}/{tab}"]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=600, shell=False)
    if not cp.stdout.strip():
        return {}, []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {}, []
    # A missing tab or a channel that does not exist yields a bare "null".
    if not isinstance(data, dict):
        return {}, []

    avatar, banner = pick_images(data.get("thumbnails") or [])
    info = {
        "channel": data.get("channel") or data.get("uploader") or "",
        "channel_id": data.get("channel_id") or "",
        "channel_url": data.get("channel_url") or base,
        "handle": data.get("uploader_id") or "",
        "followers": data.get("channel_follower_count"),
        "description": (data.get("description") or "")[:500],
        "avatar": avatar,
        "banner": banner,
    }

    out = []
    for e in data.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        dur = e.get("duration")
        out.append({
            "id": vid,
            "title": e.get("title") or "",
            "duration": int(dur) if isinstance(dur, (int, float)) else None,
            "views": e.get("view_count"),
            "tab": tab,
            # Members-only videos never yield captions. YouTube flags them right
            # here in the cheap listing, so there is no reason to find out the
            # hard way one video at a time. Measured: 66/66 detected, whereas
            # matching on "[멤버십전용]" in the title caught only 31 of them.
            "access": ("members" if e.get("availability") == "subscriber_only"
                       else "public"),
            # Built by convention rather than stored: yt-dlp's thumbnail URLs
            # carry signed query params, this form is stable and shorter.
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    return info, out


def fetch_tab_merged(exe: str, base: str, tab: str, limit: int | None,
                     meta_lang: str) -> tuple[dict, list[dict]]:
    """
    Fetch one tab, optionally correcting the titles.

    Two passes are needed because the locale argument is all-or-nothing:
      - default locale  -> view counts parse, but Korean titles come back
                           machine-translated into English
      - youtube:lang=ko -> original Korean titles, but view counts arrive as
                           "조회수 4.5만회" which yt-dlp cannot parse, so they
                           land as null
    So take numbers from the first pass and titles from the second. Skipped
    entirely when meta_lang is empty, which keeps it to a single request.
    """
    info, entries = fetch_tab(exe, base, tab, limit, "")
    if not meta_lang or not entries:
        return info, entries

    loc_info, loc_entries = fetch_tab(exe, base, tab, limit, meta_lang)
    titles = {e["id"]: e["title"] for e in loc_entries if e.get("title")}
    for e in entries:
        if e["id"] in titles:
            e["title"] = titles[e["id"]]
    # Channel name is also localised; prefer it when we got one.
    if loc_info.get("channel"):
        info["channel"] = loc_info["channel"]
        info["description"] = loc_info.get("description") or info.get("description", "")
    return info, entries


def local_ids() -> set[str]:
    d = ROOT / "transcripts"
    if not d.is_dir():
        return set()
    return {p.name for p in d.iterdir() if (p / "plain.txt").exists()}


def cache_path(channel_id: str, base: str) -> Path:
    key = channel_id or re.sub(r"[^A-Za-z0-9_.@-]", "_", base.split("/")[-1])
    return CACHE / f"{key}.json"


def cmd_list(args) -> int:
    exe = need_ytdlp()
    base = channel_url(args.target)
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    for t in tabs:
        if t not in VALID_TABS:
            die(f"unknown tab {t!r}; choose from {', '.join(VALID_TABS)}", 2)

    # Reuse cache unless asked not to. Look it up by handle/base first.
    existing = None
    for p in CACHE.glob("*.json") if CACHE.is_dir() else []:
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if blob.get("channel_url", "").rstrip("/") == base.rstrip("/") or \
           blob.get("handle", "") == base.rsplit("/", 1)[-1]:
            existing = blob
            break
    if existing and not args.refresh:
        print(f"cached {existing['channel']} videos={len(existing['videos'])} "
              f"tabs={','.join(existing['tabs'])}")
        return 0

    info: dict = {}
    videos: list[dict] = []
    seen: set[str] = set()
    started = time.time()
    for tab in tabs:
        tab_info, entries = fetch_tab_merged(exe, base, tab, args.limit,
                                             args.meta_lang)
        if tab_info and not info:
            info = tab_info
        kept = 0
        for e in entries:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            videos.append(e)
            kept += 1
        print(f"  {tab:<8} {kept} videos", file=sys.stderr)

    if not videos:
        die(f"no videos found for {base}. check the channel exists and is public")

    # Merge rather than replace. A refresh with --limit used to truncate the
    # cache: listing 729 videos and then re-listing with --limit 4 left only 4
    # behind. Union keeps everything and still puts newly seen videos on top.
    old_videos = (existing or {}).get("videos", [])
    prior = {v["id"]: v for v in old_videos}
    for v in videos:
        was = prior.get(v["id"])
        v["checked"] = bool(was.get("checked")) if was else False
        # Older caches may hold data this pass could not see (e.g. a view count
        # that came back null); keep the previous value instead of losing it.
        if was:
            for field in ("views", "duration"):
                if v.get(field) is None and was.get(field) is not None:
                    v[field] = was[field]
            # Listing counts vary between runs (459 one pass, 399 the next), so
            # treat a missing members flag as missing data rather than as proof
            # the video became public. Wrongly clearing it would put an
            # unextractable video back in the selectable pool.
            if was.get("access") == "members" and v.get("access") != "members":
                v["access"] = "members"
    fresh_ids = {v["id"] for v in videos}
    kept = [v for v in old_videos if v["id"] not in fresh_ids]
    merged = videos + kept

    blob = {
        **info,
        "tabs": sorted(set(tabs) | set((existing or {}).get("tabs", []))),
        "listed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 1),
        "fetched_now": len(videos),
        "carried_over": len(kept),
        "videos": merged,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    path = cache_path(info.get("channel_id", ""), base)
    path.write_text(json.dumps(blob, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    extra = f" (+{len(kept)} kept from cache)" if kept else ""
    print(f"ok {info.get('channel') or base} videos={len(merged)}{extra} "
          f"in {blob['elapsed_sec']}s -> {path.relative_to(ROOT)}")
    return 0


def cmd_avatars(args) -> int:
    """
    Fill in avatar/banner for channels cached before those fields existed.

    Deliberately fetches with --playlist-end 1: only channel-level data is
    wanted, so a 459-video listing is never re-scraped and cannot be lost.
    """
    exe = need_ytdlp()
    if not CACHE.is_dir():
        die("channels/ 캐시가 없습니다")

    done = skipped = failed = 0
    for p in sorted(CACHE.glob("*.json")):
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  skip {p.name} (읽기 실패)")
            continue
        name = blob.get("channel") or p.stem
        if blob.get("avatar") and not args.refresh:
            skipped += 1
            print(f"  have {name}")
            continue

        base = blob.get("channel_url") or f"https://www.youtube.com/channel/{p.stem}"
        tab = (blob.get("tabs") or ["videos"])[0]
        info, _ = fetch_tab(exe, base, tab, 1, args.meta_lang)
        if not info.get("avatar"):
            failed += 1
            print(f"  FAIL {name} (아바타를 찾지 못함)")
            continue

        n_before = len(blob.get("videos", []))
        blob["avatar"] = info["avatar"]
        blob["banner"] = info.get("banner", "")
        if info.get("followers") is not None:
            blob["followers"] = info["followers"]
        p.write_text(json.dumps(blob, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        assert len(blob.get("videos", [])) == n_before, "video list must not change"
        done += 1
        print(f"  ok   {name}  ({n_before}개 영상 그대로)")

    print(f"avatars: 채움 {done} · 이미있음 {skipped} · 실패 {failed}")
    return 0


def cmd_cached(args) -> int:
    if not CACHE.is_dir():
        print("no cache yet")
        return 0
    have = local_ids()
    for p in sorted(CACHE.glob("*.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        vids = b.get("videos", [])
        done = sum(1 for v in vids if v["id"] in have)
        print(f"{b.get('channel', p.stem):<28} {len(vids):>5} videos  "
              f"{done:>4} extracted  listed {b.get('listed_at', '?')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="ytchannel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list")
    l.add_argument("target", help="@handle, channel id, or full URL")
    l.add_argument("--tabs", default="videos,shorts")
    l.add_argument("--limit", type=int, default=None,
                   help="stop after N entries per tab")
    l.add_argument("--meta-lang", default="ko",
                   help="preferred language for titles; empty string keeps "
                        "whatever YouTube defaults to")
    l.add_argument("--refresh", action="store_true")
    l.set_defaults(fn=cmd_list)

    c = sub.add_parser("cached"); c.set_defaults(fn=cmd_cached)

    av = sub.add_parser("avatars")
    av.add_argument("--refresh", action="store_true",
                    help="이미 있는 것도 다시 가져오기")
    av.add_argument("--meta-lang", default="ko")
    av.set_defaults(fn=cmd_avatars)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except subprocess.TimeoutExpired:
        die("yt-dlp timed out")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
