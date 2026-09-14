#!/usr/bin/env python3
"""
build_site - bake the library into a static site under docs/ for GitHub Pages.

GitHub Pages serves files only; it cannot run the Flask server. So this script
pre-computes exactly what the read-only API used to return, writes it as flat
JSON files, and copies the web assets with their absolute paths made relative
(the site lives under /<repo>/ on Pages, not /).

The generated docs/ is a self-contained read-only mirror. Collecting, extracting
and 정리본 authoring stay in the local server; this is just the viewer.

Run manually with:  python scripts/build_site.py
The web server also runs it automatically before each auto-upload.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANNELS = ROOT / "channels"
TRANSCRIPTS = ROOT / "transcripts"
PROMPTS = ROOT / "prompts"
WEB = ROOT / "web"
DOCS = ROOT / "docs"
DATA = DOCS / "data"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# --------------------------------------------------------------------------- #
# readers (mirror webui.py so the output shape matches the old API exactly)
# --------------------------------------------------------------------------- #
def read_channel_blobs() -> list[tuple[str, dict]]:
    out = []
    if CHANNELS.is_dir():
        for p in sorted(CHANNELS.glob("*.json")):
            try:
                out.append((p.stem, json.loads(p.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError):
                pass
    return out


def read_meta(vid: str) -> dict:
    p = TRANSCRIPTS / vid / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def plain_text(vid: str) -> str:
    p = TRANSCRIPTS / vid / "plain.txt"
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def extracted_ids() -> list[str]:
    if not TRANSCRIPTS.is_dir():
        return []
    return sorted(d.name for d in TRANSCRIPTS.iterdir()
                  if (d / "plain.txt").exists())


def clean_ids() -> set[str]:
    if not TRANSCRIPTS.is_dir():
        return set()
    return {d.name for d in TRANSCRIPTS.iterdir() if (d / "clean.md").exists()}


def owner_index(blobs) -> dict[str, str]:
    idx: dict[str, str] = {}
    for key, blob in blobs:
        for v in blob.get("videos", []):
            idx.setdefault(v["id"], key)
    return idx


def local_info(vid: str) -> dict:
    m = read_meta(vid)
    return {
        "words": m.get("words"),
        "lang": (m.get("caption_kind") or "").split(":")[-1].split("/")[0],
    }


def split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body.lstrip("\n")


def read_clean(vid: str) -> dict:
    p = TRANSCRIPTS / vid / "clean.md"
    if not p.exists():
        return {"exists": False}
    meta, body = split_front_matter(p.read_text(encoding="utf-8"))
    return {
        "exists": True, "id": vid,
        "prompt": meta.get("prompt", ""),
        "generated": meta.get("generated", ""),
        "chars": len(body), "text": body,
        "path": f"transcripts/{vid}/clean.md",
    }


def prompt_names() -> dict[str, str]:
    """Map each prompt key (file stem) to its display name from front matter."""
    names: dict[str, str] = {}
    if PROMPTS.is_dir():
        for p in sorted(PROMPTS.glob("*.md")):
            meta, _ = split_front_matter(p.read_text(encoding="utf-8"))
            names[p.stem] = meta.get("name") or p.stem
    return names


def clean_meta(vid: str) -> dict:
    """Lightweight front-matter read (prompt + date) for list rows."""
    p = TRANSCRIPTS / vid / "clean.md"
    if not p.exists():
        return {}
    meta, _ = split_front_matter(p.read_text(encoding="utf-8"))
    return {"prompt": meta.get("prompt", ""), "generated": meta.get("generated", "")}


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def write_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def build_library(blobs, have, cleaned, metas, cmeta, pnames) -> dict:
    owner = owner_index(blobs)
    channels, claimed = [], set()
    for key, blob in blobs:
        vids = blob.get("videos", [])
        mine = [v["id"] for v in vids if v["id"] in have]
        claimed.update(mine)
        channels.append({
            "key": key,
            "channel": blob.get("channel") or key,
            "handle": blob.get("handle", ""),
            "followers": blob.get("followers"),
            "listed_at": blob.get("listed_at", ""),
            "tabs": blob.get("tabs", []),
            "total": len(vids),
            "extracted": len(mine),
            "cleaned": len([v for v in mine if v in cleaned]),
            "words": sum((metas[v].get("words") or 0) for v in mine),
            "avatar": blob.get("avatar") or "",
        })
    channels.sort(key=lambda c: (-c["extracted"], -c["total"]))

    loose = []
    for vid in have:
        if vid in claimed or vid in owner:
            continue
        m = metas[vid]
        entry = {
            "id": vid, "title": m.get("title") or vid,
            "channel": m.get("channel") or "",
            "channel_id": m.get("channel_id") or "",
            "words": m.get("words"), "duration": m.get("duration_sec"),
            "clean": vid in cleaned,
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        }
        if vid in cleaned:
            pk = cmeta.get(vid, {}).get("prompt", "")
            entry["clean_prompt"] = pk
            entry["clean_prompt_name"] = pnames.get(pk, pk)
            entry["clean_at"] = cmeta.get(vid, {}).get("generated", "")
        loose.append(entry)
    loose.sort(key=lambda v: -(v["words"] or 0))

    return {
        "totals": {
            "channels": len(channels),
            "listed": sum(c["total"] for c in channels),
            "extracted": len(have),
            "cleaned": len(cleaned & have),
            "words": sum((metas[v].get("words") or 0) for v in have),
        },
        "channels": channels,
        "loose": loose,
    }


def build_channel(key, blob, have, cleaned, local, cmeta, pnames) -> dict:
    # Same enrichment the /api/channel endpoint did. No failure state on the
    # static site (retrying is a write action), so 'fail' is left blank.
    for v in blob.get("videos", []):
        vid = v["id"]
        v["local"] = vid in have
        v["local_words"] = local.get(vid, {}).get("words")
        v["local_lang"] = local.get(vid, {}).get("lang")
        v["clean"] = vid in cleaned
        if vid in cleaned:
            pk = cmeta.get(vid, {}).get("prompt", "")
            v["clean_prompt"] = pk
            v["clean_prompt_name"] = pnames.get(pk, pk)
            v["clean_at"] = cmeta.get(vid, {}).get("generated", "")
        v.setdefault("access", "public")
        v["fail"] = ""
    return blob


def build_video(vid, owner) -> dict:
    m = read_meta(vid)
    return {
        "id": vid,
        "title": m.get("title") or vid,
        "channel": m.get("channel") or "",
        "channel_key": owner.get(vid),
        "has_clean": (TRANSCRIPTS / vid / "clean.md").exists(),
        "duration": m.get("duration_sec"),
        "upload_date": m.get("upload_date", ""),
        "words": m.get("words"),
        "chars": m.get("chars"),
        "caption_kind": m.get("caption_kind", ""),
        "language": m.get("language", ""),
        "path": f"transcripts/{vid}/plain.txt",
        "text": plain_text(vid),
        # Folded in so the reader needs a single request per video.
        "clean": read_clean(vid),
    }


# --------------------------------------------------------------------------- #
# asset copy (make absolute paths relative, inject the static flag)
# --------------------------------------------------------------------------- #
def copy_assets():
    DOCS.mkdir(parents=True, exist_ok=True)

    html = (WEB / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/style.css"', 'href="style.css"')
    html = html.replace('src="/app.js"', 'src="app.js"')
    # Flag read by app.js to switch every read to the pre-built JSON files.
    html = html.replace("</head>", '<script>window.YT_STATIC=true;</script>\n</head>')
    (DOCS / "index.html").write_text(html, encoding="utf-8")

    css = (WEB / "style.css").read_text(encoding="utf-8")
    css = css.replace('url("/fonts/', 'url("fonts/')
    (DOCS / "style.css").write_text(css, encoding="utf-8")

    shutil.copyfile(WEB / "app.js", DOCS / "app.js")

    fonts_src = WEB / "fonts"
    if fonts_src.is_dir():
        (DOCS / "fonts").mkdir(exist_ok=True)
        for f in fonts_src.iterdir():
            dst = DOCS / "fonts" / f.name
            if not dst.exists() or dst.stat().st_size != f.stat().st_size:
                shutil.copyfile(f, dst)

    # Without this, GitHub Pages runs Jekyll, which drops files whose names
    # start with '_' — and plenty of YouTube ids do (e.g. _K25c-nL3Hc).
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    # Search-engine blocking. The real defence is the <meta name="robots"
    # noindex> tag in index.html, which Google honours per page. This robots.txt
    # is belt-and-suspenders: on a project page (user.github.io/repo/) crawlers
    # only read robots.txt at the domain root, so this subpath copy is advisory.
    (DOCS / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
def main() -> int:
    blobs = read_channel_blobs()
    have = set(extracted_ids())
    cleaned = clean_ids()
    metas = {v: read_meta(v) for v in have}
    local = {v: local_info(v) for v in have}
    owner = owner_index(blobs)
    pnames = prompt_names()
    cmeta = {v: clean_meta(v) for v in cleaned}

    # Rebuild data from scratch so deletions do not leave stale files behind.
    if DATA.exists():
        shutil.rmtree(DATA)
    (DATA / "ch").mkdir(parents=True, exist_ok=True)
    (DATA / "v").mkdir(parents=True, exist_ok=True)

    total = 0
    total += write_json(DATA / "library.json",
                        build_library(blobs, have, cleaned, metas, cmeta, pnames))

    for key, blob in blobs:
        total += write_json(DATA / "ch" / f"{key}.json",
                           build_channel(key, blob, have, cleaned, local, cmeta, pnames))

    for vid in sorted(have):
        total += write_json(DATA / "v" / f"{vid}.json", build_video(vid, owner))

    # One flat index for client-side full-text search.
    search = []
    for vid in sorted(have):
        m = metas[vid]
        key = owner.get(vid)
        name = ""
        if key:
            for k, b in blobs:
                if k == key:
                    name = b.get("channel") or ""
                    break
        search.append({
            "id": vid,
            "title": m.get("title") or vid,
            "channel": name or m.get("channel") or "",
            "key": key,
            "text": re.sub(r"\s+", " ", plain_text(vid)).strip(),
        })
    total += write_json(DATA / "search.json", search)

    copy_assets()

    print(f"built docs/ : 채널 {len(blobs)} · 영상 {len(have)} · 정리본 {len(cleaned)} "
          f"· data {total/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
