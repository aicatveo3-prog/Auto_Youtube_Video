#!/usr/bin/env python3
"""
webui - local browser UI for picking channel videos and pulling their captions.

Start it with:  python scripts/webui.py        then open http://127.0.0.1:8765

Binds to 127.0.0.1 only, so nothing outside this machine can reach it. That is
the reason it ships without authentication; do not move it to 0.0.0.0 without
adding one.

No AI, no API keys. Channel listing and caption download are both yt-dlp.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WEB = ROOT / "web"
CHANNELS = ROOT / "channels"
TRANSCRIPTS = ROOT / "transcripts"

HOST, PORT = "127.0.0.1", 8765
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False  # keep Korean titles readable on the wire


# --------------------------------------------------------------------------- #
# background job
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    kind: str = "idle"
    status: str = "idle"          # idle | running | done | error | cancelled
    total: int = 0
    done: int = 0
    current: str = ""
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    results: dict[str, dict] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started if self.started else 0
        return {
            "kind": self.kind, "status": self.status,
            "total": self.total, "done": self.done,
            "current": self.current, "message": self.message,
            "elapsed": round(elapsed, 1),
            "results": self.results,
            "log": self.log[-40:],
        }


_lock = threading.Lock()
_job = Job()
_cancel = threading.Event()


def job_running() -> bool:
    with _lock:
        return _job.status == "running"


def start_job(kind: str, total: int, target):
    global _job
    with _lock:
        if _job.status == "running":
            return False
        _cancel.clear()
        _job = Job(kind=kind, status="running", total=total, started=time.time())
    threading.Thread(target=target, daemon=True).start()
    return True


def finish_job(status: str, message: str = ""):
    with _lock:
        _job.status = status
        _job.message = message
        _job.finished = time.time()
        _job.current = ""


def log(line: str):
    with _lock:
        _job.log.append(line)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def run_py(script: str, *args, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run one of our CLIs. Args stay a list so nothing reaches a shell."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), timeout=timeout, shell=False,
    )


def local_library() -> dict[str, dict]:
    """Video ids that already have a transcript on disk, with their word counts."""
    out: dict[str, dict] = {}
    if not TRANSCRIPTS.is_dir():
        return out
    for d in TRANSCRIPTS.iterdir():
        if not (d / "plain.txt").exists():
            continue
        info = {"words": None, "lang": None}
        meta = d / "meta.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                info["words"] = m.get("words")
                info["lang"] = (m.get("caption_kind") or "").split(":")[-1].split("/")[0]
            except (json.JSONDecodeError, OSError):
                pass
        out[d.name] = info
    return out


def load_channels() -> list[dict]:
    items = []
    if not CHANNELS.is_dir():
        return items
    for p in sorted(CHANNELS.glob("*.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        items.append({
            "key": p.stem,
            "channel": b.get("channel") or p.stem,
            "handle": b.get("handle", ""),
            "followers": b.get("followers"),
            "count": len(b.get("videos", [])),
            "tabs": b.get("tabs", []),
            "listed_at": b.get("listed_at", ""),
        })
    return items


def channel_file(key: str) -> Path:
    """Resolve a cache key to a path, refusing anything that escapes the folder."""
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", key or ""):
        raise ValueError("bad channel key")
    p = (CHANNELS / f"{key}.json").resolve()
    if not str(p).startswith(str(CHANNELS.resolve())):
        raise ValueError("bad channel key")
    return p


def read_channel_blobs() -> list[tuple[str, dict]]:
    out = []
    if not CHANNELS.is_dir():
        return out
    for p in sorted(CHANNELS.glob("*.json")):
        try:
            out.append((p.stem, json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def read_meta(vid: str) -> dict:
    p = TRANSCRIPTS / vid / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def extracted_ids() -> list[str]:
    if not TRANSCRIPTS.is_dir():
        return []
    return sorted(d.name for d in TRANSCRIPTS.iterdir()
                  if (d / "plain.txt").exists())


def owner_index(blobs: list[tuple[str, dict]]) -> dict[str, str]:
    """video id -> channel cache key, taken from the channel listings."""
    idx: dict[str, str] = {}
    for key, blob in blobs:
        for v in blob.get("videos", []):
            idx.setdefault(v["id"], key)
    return idx


def plain_text(vid: str) -> str:
    p = TRANSCRIPTS / vid / "plain.txt"
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


PROMPTS = ROOT / "prompts"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def split_front_matter(text: str) -> tuple[dict, str]:
    """Pull a simple key: value header off the top of a markdown file."""
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


def clean_path(vid: str) -> Path:
    return TRANSCRIPTS / vid / "clean.md"


def clean_ids() -> set[str]:
    """Videos that already have a 정리본."""
    if not TRANSCRIPTS.is_dir():
        return set()
    return {d.name for d in TRANSCRIPTS.iterdir() if (d / "clean.md").exists()}


FAILURES = TRANSCRIPTS / "_failures.json"
_fail_lock = threading.Lock()


def read_failures() -> dict[str, dict]:
    """
    Why a video has no transcript.

    Empty folders used to serve as this record by accident. Now that a folder is
    only created on success, the reason has to be stored explicitly, otherwise
    every run would retry the same hopeless videos.
    """
    if not FAILURES.exists():
        return {}
    try:
        data = json.loads(FAILURES.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def note_failure(vid: str, reason: str, detail: str = ""):
    with _fail_lock:
        data = read_failures()
        data[vid] = {"reason": reason, "detail": detail[:200],
                     "at": time.strftime("%Y-%m-%d %H:%M")}
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        FAILURES.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def clear_failure(vid: str):
    with _fail_lock:
        data = read_failures()
        if data.pop(vid, None) is not None:
            FAILURES.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def prompt_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError("bad prompt name")
    p = (PROMPTS / f"{name}.md").resolve()
    if not str(p).startswith(str(PROMPTS.resolve())):
        raise ValueError("bad prompt name")
    return p


# --------------------------------------------------------------------------- #
# routes: static
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/app.js")
def appjs():
    return send_from_directory(WEB, "app.js")


@app.get("/style.css")
def style():
    return send_from_directory(WEB, "style.css")


@app.get("/fonts/<path:name>")
def fonts(name: str):
    """Self-hosted webfont. Cached hard because the filename pins the version."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return jsonify({"error": "bad font name"}), 400
    resp = send_from_directory(WEB / "fonts", name)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# --------------------------------------------------------------------------- #
# routes: api
# --------------------------------------------------------------------------- #
@app.get("/api/job")
def api_job():
    with _lock:
        return jsonify(_job.snapshot())


@app.get("/api/channels")
def api_channels():
    return jsonify({"channels": load_channels()})


@app.get("/api/channel/<key>")
def api_channel(key: str):
    try:
        p = channel_file(key)
    except ValueError:
        return jsonify({"error": "invalid channel key"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    blob = json.loads(p.read_text(encoding="utf-8"))
    have = local_library()
    cleaned = clean_ids()
    fails = read_failures()
    for v in blob.get("videos", []):
        vid = v["id"]
        v["local"] = vid in have
        v["local_words"] = have.get(vid, {}).get("words")
        v["local_lang"] = have.get(vid, {}).get("lang")
        v["clean"] = vid in cleaned
        v.setdefault("access", "public")
        v["fail"] = "" if v["local"] else fails.get(vid, {}).get("reason", "")
    return jsonify(blob)


@app.post("/api/list")
def api_list():
    if job_running():
        return jsonify({"error": "a job is already running"}), 409
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"error": "channel is required"}), 400
    # An absent "tabs" key means "use the default"; an explicitly empty list is
    # a caller mistake and must not be silently widened to everything.
    raw_tabs = data["tabs"] if "tabs" in data else ["videos", "shorts"]
    if not isinstance(raw_tabs, list):
        return jsonify({"error": "tabs must be a list"}), 400
    tabs = [t for t in raw_tabs if t in ("videos", "shorts", "streams")]
    if not tabs:
        return jsonify({"error": "pick at least one tab"}), 400
    limit = data.get("limit")
    refresh = bool(data.get("refresh"))

    meta_lang = (data.get("meta_lang") or "ko").strip()
    if not re.fullmatch(r"[A-Za-z-]{0,10}", meta_lang):
        return jsonify({"error": "bad metadata language"}), 400

    args = ["list", target, "--tabs", ",".join(tabs), "--meta-lang", meta_lang]
    if limit:
        try:
            args += ["--limit", str(int(limit))]
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be a number"}), 400
    if refresh:
        args.append("--refresh")

    def work():
        with _lock:
            _job.current = target
        try:
            cp = run_py("ytchannel.py", *args, timeout=900)
        except subprocess.TimeoutExpired:
            finish_job("error", "listing timed out")
            return
        for line in (cp.stdout + cp.stderr).splitlines():
            if line.strip():
                log(line.strip())
        if cp.returncode != 0:
            tail = [l for l in cp.stderr.splitlines() if l.strip()]
            finish_job("error", tail[-1] if tail else "listing failed")
            return
        with _lock:
            _job.done = 1
        finish_job("done", (cp.stdout or "").strip().splitlines()[-1:][0]
                   if cp.stdout.strip() else "done")

    start_job("list", 1, work)
    return jsonify({"started": True})


@app.post("/api/extract")
def api_extract():
    if job_running():
        return jsonify({"error": "a job is already running"}), 409
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    ids = [i for i in ids if isinstance(i, str) and VIDEO_ID_RE.match(i)]
    if not ids:
        return jsonify({"error": "no valid video ids selected"}), 400
    lang = (data.get("lang") or "auto").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.*-]{1,12}", lang):
        return jsonify({"error": "bad language code"}), 400

    # Members-only videos cannot yield captions: 66 of 66 failed when tried.
    # Refuse them here so a stale page cannot queue guaranteed failures.
    members = {v["id"] for _, b in read_channel_blobs()
               for v in b.get("videos", []) if v.get("access") == "members"}
    blocked = [i for i in ids if i in members]
    ids = [i for i in ids if i not in members]
    if not ids:
        return jsonify({"error": "선택한 영상이 모두 멤버십 전용입니다. "
                                 "자막을 가져올 수 없습니다."}), 400

    def work():
        for n, vid in enumerate(ids, 1):
            if _cancel.is_set():
                finish_job("cancelled", f"stopped after {n - 1} of {len(ids)}")
                return
            with _lock:
                _job.current = vid
            try:
                cp = run_py("ytscript.py", "fetch", vid, "--lang", lang, timeout=900)
                out, err = cp.stdout.strip(), cp.stderr.strip()
                if cp.returncode == 0 and out.startswith("cached"):
                    state, detail = "cached", out
                elif cp.returncode == 0:
                    state, detail = "ok", out
                elif "no captions" in err:
                    state, detail = "no_captions", "자막 없음"
                else:
                    state = "error"
                    detail = (err.splitlines() or ["failed"])[-1]
            except subprocess.TimeoutExpired:
                state, detail = "error", "timed out"

            # Remember the outcome so the list can show it after a reload and
            # stop offering the same dead ends over and over.
            if state in ("ok", "cached"):
                clear_failure(vid)
            else:
                note_failure(vid, state, detail)

            with _lock:
                _job.results[vid] = {"state": state, "detail": detail}
                _job.done = n
            log(f"[{n}/{len(ids)}] {state} {vid} :: {detail}")

        with _lock:
            counts: dict[str, int] = {}
            for r in _job.results.values():
                counts[r["state"]] = counts.get(r["state"], 0) + 1
        finish_job("done", " · ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    start_job("extract", len(ids), work)
    return jsonify({"started": True, "count": len(ids),
                    "skipped_members": len(blocked)})


@app.post("/api/cancel")
def api_cancel():
    _cancel.set()
    return jsonify({"cancelling": True})


@app.post("/api/retry/<vid>")
def api_retry(vid: str):
    """
    Forget a recorded failure so the video becomes selectable again.

    Unlike members-only access, "no captions" is not permanent: YouTube may add
    auto-captions later, so this stays a retryable state.
    """
    if not VIDEO_ID_RE.match(vid):
        return jsonify({"error": "bad id"}), 400
    clear_failure(vid)
    return jsonify({"cleared": True})


@app.post("/api/select")
def api_select():
    """Persist checkbox state so a later --refresh keeps the user's choices."""
    data = request.get_json(silent=True) or {}
    try:
        p = channel_file(data.get("key", ""))
    except ValueError:
        return jsonify({"error": "invalid channel key"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    ids = {i for i in (data.get("ids") or [])
           if isinstance(i, str) and VIDEO_ID_RE.match(i)}
    blob = json.loads(p.read_text(encoding="utf-8"))
    for v in blob.get("videos", []):
        v["checked"] = v["id"] in ids
    p.write_text(json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8")
    return jsonify({"saved": len(ids)})


@app.get("/api/transcript/<vid>")
def api_transcript(vid: str):
    """Preview a fetched transcript. Truncated: the browser does not need it all."""
    if not VIDEO_ID_RE.match(vid):
        return jsonify({"error": "bad id"}), 400
    p = TRANSCRIPTS / vid / "plain.txt"
    if not p.exists():
        return jsonify({"error": "not fetched"}), 404
    text = p.read_text(encoding="utf-8")
    limit = 4000
    meta = {}
    mp = TRANSCRIPTS / vid / "meta.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return jsonify({
        "id": vid,
        "title": meta.get("title", ""),
        "words": meta.get("words"),
        "caption_kind": meta.get("caption_kind", ""),
        "truncated": len(text) > limit,
        "text": text[:limit],
        "path": str(p.relative_to(ROOT)),
    })


@app.get("/api/library")
def api_library():
    """Everything the home screen needs: channels, progress, and loose videos."""
    blobs = read_channel_blobs()
    owner = owner_index(blobs)
    have = set(extracted_ids())
    metas = {v: read_meta(v) for v in have}
    cleaned = clean_ids()

    channels = []
    claimed: set[str] = set()
    for key, blob in blobs:
        vids = blob.get("videos", [])
        mine = [v["id"] for v in vids if v["id"] in have]
        claimed.update(mine)
        words = sum((metas[v].get("words") or 0) for v in mine)
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
            "words": words,
            "avatar": blob.get("avatar") or "",
        })
    channels.sort(key=lambda c: (-c["extracted"], -c["total"]))

    # Transcripts whose channel was never listed, or was listed and then had the
    # video fall out of the cache. meta.channel_id lets us still name them.
    loose = []
    for vid in have:
        if vid in claimed or vid in owner:
            continue
        m = metas[vid]
        loose.append({
            "id": vid,
            "title": m.get("title") or vid,
            "channel": m.get("channel") or "",
            "channel_id": m.get("channel_id") or "",
            "words": m.get("words"),
            "duration": m.get("duration_sec"),
            "clean": vid in cleaned,
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    loose.sort(key=lambda v: -(v["words"] or 0))

    return jsonify({
        "totals": {
            "channels": len(channels),
            "listed": sum(c["total"] for c in channels),
            "extracted": len(have),
            "cleaned": len(cleaned & have),
            "words": sum((metas[v].get("words") or 0) for v in have),
        },
        "channels": channels,
        "loose": loose,
    })


@app.get("/api/video/<vid>")
def api_video(vid: str):
    """Full transcript for the reader. No timestamps: plain prose only."""
    if not VIDEO_ID_RE.match(vid):
        return jsonify({"error": "bad id"}), 400
    text = plain_text(vid)
    if not text:
        return jsonify({"error": "not fetched"}), 404
    m = read_meta(vid)
    key = owner_index(read_channel_blobs()).get(vid)
    return jsonify({
        "id": vid,
        "title": m.get("title") or vid,
        "channel": m.get("channel") or "",
        "channel_key": key,
        "has_clean": clean_path(vid).exists(),
        "duration": m.get("duration_sec"),
        "upload_date": m.get("upload_date", ""),
        "words": m.get("words"),
        "chars": m.get("chars"),
        "caption_kind": m.get("caption_kind", ""),
        "language": m.get("language", ""),
        "path": f"transcripts/{vid}/plain.txt",
        "text": text,
    })


@app.get("/api/prompts")
def api_prompts():
    items = []
    if PROMPTS.is_dir():
        for p in sorted(PROMPTS.glob("*.md")):
            meta, body = split_front_matter(p.read_text(encoding="utf-8"))
            items.append({
                "key": p.stem,
                "name": meta.get("name") or p.stem,
                "description": meta.get("description", ""),
                "chars": len(body),
            })
    return jsonify({"prompts": items})


@app.get("/api/prompt/<name>")
def api_prompt(name: str):
    """The instruction text itself, so the reader can offer a copy button."""
    try:
        p = prompt_path(name)
    except ValueError:
        return jsonify({"error": "invalid prompt name"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    meta, body = split_front_matter(p.read_text(encoding="utf-8"))
    return jsonify({
        "key": name,
        "name": meta.get("name") or name,
        "description": meta.get("description", ""),
        "text": body.strip(),
    })


@app.get("/api/clean/<vid>")
def api_clean_get(vid: str):
    if not VIDEO_ID_RE.match(vid):
        return jsonify({"error": "bad id"}), 400
    p = clean_path(vid)
    if not p.exists():
        return jsonify({"exists": False}), 200
    meta, body = split_front_matter(p.read_text(encoding="utf-8"))
    return jsonify({
        "exists": True,
        "id": vid,
        "prompt": meta.get("prompt", ""),
        "generated": meta.get("generated", ""),
        "chars": len(body),
        "text": body,
        "path": f"transcripts/{vid}/clean.md",
    })


@app.post("/api/clean/<vid>")
def api_clean_put(vid: str):
    """
    Save a 정리본 pasted in from the browser.

    Kiro writes the file directly when asked in chat; this endpoint exists so a
    result produced anywhere else can be dropped in without touching the disk by
    hand.
    """
    if not VIDEO_ID_RE.match(vid):
        return jsonify({"error": "bad id"}), 400
    d = TRANSCRIPTS / vid
    if not (d / "plain.txt").exists():
        return jsonify({"error": "먼저 자막을 추출하세요"}), 404

    data = request.get_json(silent=True) or {}
    body = (data.get("text") or "").strip()
    if len(body) < 50:
        return jsonify({"error": "내용이 너무 짧습니다"}), 400
    if len(body) > 400_000:
        return jsonify({"error": "내용이 너무 깁니다"}), 400
    name = (data.get("prompt") or "cleanup").strip()
    if not NAME_RE.match(name):
        return jsonify({"error": "invalid prompt name"}), 400

    _, body = split_front_matter(body)          # our header wins
    header = (f"---\nsource: {vid}\nprompt: {name}\n"
              f"generated: {time.strftime('%Y-%m-%d')}\n---\n\n")
    clean_path(vid).write_text(header + body.strip() + "\n", encoding="utf-8")
    return jsonify({"saved": True, "chars": len(body)})


@app.delete("/api/clean/<vid>")
def api_clean_delete(vid: str):
    if not VIDEO_ID_RE.match(vid):
        return jsonify({"error": "bad id"}), 400
    p = clean_path(vid)
    if p.exists():
        p.unlink()
    return jsonify({"deleted": True})


SNIPPET_PAD = 70


@app.get("/api/search")
def api_search():
    """
    Case-insensitive substring search over every stored transcript.

    Substring rather than regex on purpose: it is what works for Korean, where
    there are no word boundaries to anchor on, and it cannot be fed a pattern
    that takes exponential time.
    """
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"error": "검색어를 두 글자 이상 입력하세요."}), 400
    try:
        per = min(max(int(request.args.get("per", 4)), 1), 20)
    except ValueError:
        per = 4

    needle = q.lower()
    blobs = read_channel_blobs()
    owner = owner_index(blobs)
    names = {k: b.get("channel") or k for k, b in blobs}

    results = []
    total_hits = 0
    for vid in extracted_ids():
        raw = plain_text(vid)
        if not raw:
            continue
        # Paragraph wrapping inserts newlines mid-sentence, so flatten first or
        # a phrase spanning a line break would never match.
        flat = re.sub(r"\s+", " ", raw)
        low = flat.lower()
        if needle not in low:
            continue

        spots = []
        start = 0
        while True:
            i = low.find(needle, start)
            if i < 0:
                break
            spots.append(i)
            start = i + len(needle)
        total_hits += len(spots)

        snippets = []
        for i in spots[:per]:
            a = max(0, i - SNIPPET_PAD)
            b = min(len(flat), i + len(q) + SNIPPET_PAD)
            snippets.append({
                "before": ("..." if a > 0 else "") + flat[a:i],
                "match": flat[i:i + len(q)],
                "after": flat[i + len(q):b] + ("..." if b < len(flat) else ""),
            })

        m = read_meta(vid)
        key = owner.get(vid)
        results.append({
            "id": vid,
            "title": m.get("title") or vid,
            "channel": names.get(key) or m.get("channel") or "",
            "channel_key": key,
            "hits": len(spots),
            "more": max(0, len(spots) - per),
            "snippets": snippets,
        })

    results.sort(key=lambda r: -r["hits"])
    return jsonify({
        "query": q,
        "videos": len(results),
        "hits": total_hits,
        "searched": len(extracted_ids()),
        "results": results,
    })


def main():
    import argparse
    import socket
    import threading as _th
    import webbrowser

    ap = argparse.ArgumentParser(prog="webui")
    ap.add_argument("--open", action="store_true",
                    help="open the page in the default browser once bound")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    for d in (CHANNELS, TRANSCRIPTS):
        d.mkdir(parents=True, exist_ok=True)

    url = f"http://{HOST}:{args.port}"

    # Fail loudly instead of letting Flask raise a bare OSError, since the most
    # common cause is simply having the server open in another window already.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.6)
    already_up = probe.connect_ex((HOST, args.port)) == 0
    probe.close()
    if already_up:
        print(f"이미 실행 중입니다: {url}")
        print("브라우저에서 위 주소를 열면 됩니다. 새로 띄우려면 기존 창을 먼저 종료하세요.")
        if args.open:
            webbrowser.open(url)
        return 0

    print("=" * 58)
    print("  유튜브 스크립트 추출기")
    print(f"  주소  {url}")
    print("  종료  이 창에서 Ctrl+C")
    print("  주의  이 창을 닫으면 사이트도 함께 종료됩니다")
    print("=" * 58)

    if args.open:
        # Wait for the socket to accept before handing the URL to the browser.
        def opener():
            for _ in range(40):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                ok = s.connect_ex((HOST, args.port)) == 0
                s.close()
                if ok:
                    webbrowser.open(url)
                    return
                time.sleep(0.25)
        _th.Thread(target=opener, daemon=True).start()

    try:
        app.run(host=HOST, port=args.port, debug=False, threaded=True)
    except OSError as e:
        print(f"서버를 시작할 수 없습니다: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
