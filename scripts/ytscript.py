#!/usr/bin/env python3
"""
ytscript - YouTube transcript extractor tuned for minimal LLM context cost.

Design goal: the transcript never has to enter an AI agent's context window.
Everything is written to local files; stdout stays tiny on purpose.

Commands
--------
  fetch  <url|id> [--lang en] [--force]   Download + clean transcript to files.
  info   <url|id>                         Print stored metadata only.
  search <url|id> <query> [--max 20]      Grep the transcript, show [mm:ss] hits.
  slice  <url|id> <start> <end>           Print text between two mm:ss marks.
  outline <url|id> [--every 60]           Coarse time-bucketed digest.
  cost   <url|id>                         Token cost of each representation.

Files written under ./transcripts/<video_id>/
  raw.json3      original caption payload from YouTube
  plain.txt      deduplicated prose, no timestamps  (cheapest to read)
  timed.tsv      "<ms>\\t<seconds>\\t<text>" one caption line per row
  meta.json      id, title, channel, duration, counts

Exit codes: 0 ok, 1 failure, 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "transcripts"

# yt-dlp keeps getting throttled by YouTube's SABR rollout; rotating the
# player client is what actually gets us past the transient failures.
PLAYER_CLIENTS = ["default", "web_safari", "tv", "android_vr", "mweb"]

# Even with --skip-download, yt-dlp runs video format selection first and
# aborts with "Requested format is not available" on SABR-restricted videos,
# which kills subtitle-only runs that would otherwise succeed. This flag skips
# that step. Verified: without it 3 of 5 test videos failed, with it 0 failed.
BASE_FLAGS = ["--no-warnings", "--skip-download", "--no-playlist",
              "--ignore-no-formats-error"]

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def need_ytdlp() -> str:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not exe:
        die("yt-dlp not found on PATH. Install it first.")
    return exe


def video_id(target: str) -> str:
    """Accept a bare id or any common YouTube URL shape."""
    target = target.strip()
    if ID_RE.match(target):
        return target
    patterns = [
        r"[?&]v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"/shorts/([A-Za-z0-9_-]{11})",
        r"/embed/([A-Za-z0-9_-]{11})",
        r"/live/([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, target)
        if m:
            return m.group(1)
    die(f"could not parse a video id out of {target!r}")


def run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,  # never build a shell string; args stay as a list
    )


def ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def parse_ts(text: str) -> float:
    """'90' | '1:30' | '1:02:03' -> seconds."""
    parts = text.strip().split(":")
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        die(f"bad timestamp {text!r}; use SS, MM:SS or HH:MM:SS", 2)
    nums = [int(p) for p in parts]
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def count_tokens(text: str) -> int | None:
    """Best-effort token count. Returns None when tiktoken is unavailable."""
    try:
        import tiktoken
    except ImportError:
        return None
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text, disallowed_special=()))


def approx_tokens(text: str) -> int:
    """Tokenizer-free fallback: English prose lands near 4 chars/token."""
    return max(1, round(len(text) / 4))


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def dl_metadata(exe: str, vid: str) -> dict:
    url = f"https://www.youtube.com/watch?v={vid}"
    fields = ("%(id)s\t%(title)s\t%(channel)s\t%(duration)s"
              "\t%(upload_date)s\t%(language)s\t%(channel_id)s")
    for client in PLAYER_CLIENTS:
        cp = run([
            exe, *BASE_FLAGS,
            "--extractor-args", f"youtube:player_client={client}",
            "--print", fields, url,
        ])
        line = next(
            (l for l in cp.stdout.splitlines() if l.count("\t") >= 6), None
        )
        if line:
            f = line.split("\t")
            dur = f[3] if f[3] not in ("NA", "") else "0"
            return {
                "id": f[0],
                "title": f[1],
                "channel": f[2],
                "duration_sec": int(float(dur)),
                "upload_date": "" if f[4] == "NA" else f[4],
                # YouTube reports the video's own language here, which is what
                # lets --lang auto pull Korean captions for a Korean video
                # without the caller having to know in advance.
                "language": "" if f[5] in ("NA", "") else f[5],
                # Needed to group a transcript under its channel in the
                # library. Channel names get renamed and collide; ids do not.
                "channel_id": "" if f[6] in ("NA", "") else f[6],
                "player_client": client,
            }
    return {"id": vid, "title": "", "channel": "", "duration_sec": 0,
            "upload_date": "", "language": "", "channel_id": "",
            "player_client": "none"}


def resolve_langs(requested: str, detected: str) -> list[str]:
    """
    Build the language preference order.

    'auto' means: the video's own language first, then English as a safety net.
    Anything else is taken literally, still with an English fallback so a
    missing Korean track does not leave us empty-handed.
    """
    if requested != "auto":
        order = [requested]
    elif detected:
        order = [detected.split("-")[0], detected]
    else:
        order = []
    order.append("en")
    seen, out = set(), []
    for lang in order:
        if lang and lang not in seen:
            seen.add(lang)
            out.append(lang)
    return out


def dl_captions(exe: str, vid: str, langs: list[str],
                dest: Path) -> tuple[Path, str]:
    """Try each language in order, manual subs before auto-generated."""
    url = f"https://www.youtube.com/watch?v={vid}"
    tmpl = str(dest / "sub.%(ext)s")
    # Order matters: creator-written subs beat machine transcription, and an
    # exact language code beats the wildcard. The wildcard is last because on
    # multi-dub videos "en.*" expands to a dozen translated tracks.
    sources = [
        ("manual", ["--write-subs"]),
        ("auto", ["--write-auto-subs"]),
    ]
    last = ""
    for client in PLAYER_CLIENTS:
        for lang in langs:
            for kind, flags in sources:
                for spec in (lang, f"{lang}-orig", f"{lang}.*"):
                    for f in dest.glob("sub.*"):
                        f.unlink()
                    cp = run([
                        exe, *BASE_FLAGS,
                        "--extractor-args", f"youtube:player_client={client}",
                        *flags,
                        "--sub-langs", spec,
                        "--sub-format", "json3",
                        "-o", tmpl, url,
                    ], timeout=240)
                    got = sorted(dest.glob("sub.*.json3"))
                    if got:
                        return got[0], f"{kind}:{spec}/{client}"
                    tail = (cp.stderr or cp.stdout).strip().splitlines()
                    if tail:
                        last = tail[-1]
    die(f"no captions in {'/'.join(langs)} for {vid}. last yt-dlp msg: {last}")


def clean_json3(path: Path) -> list[tuple[int, str]]:
    """
    YouTube auto-captions use rolling windows: events flagged aAppend=1 carry
    only a newline and re-send text that the previous event already contained.
    Dropping them is what removes the ~3x duplication in the raw payload.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[int, str]] = []
    for ev in data.get("events", []):
        if ev.get("aAppend"):
            continue
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = text.replace("\u200b", "").strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        rows.append((int(ev.get("tStartMs", 0)), text))

    # Collapse any consecutive exact repeats that survived.
    out: list[tuple[int, str]] = []
    for ms, text in rows:
        if out and out[-1][1] == text:
            continue
        out.append((ms, text))
    return out


SENTENCE_END = (".", "!", "?", "\u3002", "\uff01", "\uff1f")
SOFT_LIMIT = 320
HARD_LIMIT = 900


def to_prose(rows: list[tuple[int, str]]) -> str:
    """
    Join caption lines into readable paragraphs.

    Auto-generated captions often carry no punctuation at all, which is normal
    for Korean tracks. Splitting only on sentence enders would then produce one
    unreadable block per video, so a hard character cap forces a break.
    """
    blocks: list[str] = []
    buf: list[str] = []
    size = 0
    for _, text in rows:
        buf.append(text)
        size += len(text) + 1
        ends_sentence = text.endswith(SENTENCE_END)
        if (ends_sentence and size > SOFT_LIMIT) or size > HARD_LIMIT:
            blocks.append(" ".join(buf))
            buf, size = [], 0
    if buf:
        blocks.append(" ".join(buf))

    wrapped = []
    for b in blocks:
        # break_long_words stays off so CJK runs and URLs are not chopped mid-token
        wrapped.append(textwrap.fill(b, width=100, break_long_words=False,
                                     break_on_hyphens=False))
    return "\n\n".join(wrapped) + "\n"


def cmd_fetch(args) -> int:
    exe = need_ytdlp()
    vid = video_id(args.target)
    dest = OUTDIR / vid
    plain, timed = dest / "plain.txt", dest / "timed.tsv"

    if plain.exists() and not args.force:
        meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
        print(f"cached {vid} words={meta['words']} lines={meta['lines']} "
              f"-> {plain.relative_to(ROOT)}")
        return 0

    # Download into scratch space and only create the real folder once there is
    # something to put in it. Creating it up front left an empty directory behind
    # for every failure: 85 of them accumulated over one 458-video run.
    tmp = Path(tempfile.mkdtemp(prefix=f"ytscript-{vid}-"))
    try:
        meta = dl_metadata(exe, vid)
        langs = resolve_langs(args.lang, meta.get("language", ""))
        src, kind = dl_captions(exe, vid, langs, tmp)
        rows = clean_json3(src)
        if not rows:
            die(f"captions for {vid} parsed to zero lines")

        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest / "raw.json3"))
        prose = to_prose(rows)
        plain.write_text(prose, encoding="utf-8")
        timed.write_text(
            "".join(f"{ms}\t{ms / 1000:.2f}\t{t}\n" for ms, t in rows),
            encoding="utf-8",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    meta.update({
        "lang_requested": args.lang,
        "lang_tried": langs,
        "caption_kind": kind,
        "lines": len(rows),
        "words": len(prose.split()),
        "chars": len(prose),
    })
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Deliberately terse: this is all an agent needs to decide what to read next.
    print(f"ok {vid} [{kind}] dur={ts(meta['duration_sec'])} "
          f"words={meta['words']} lines={meta['lines']} "
          f"-> {plain.relative_to(ROOT)}")
    return 0


# --------------------------------------------------------------------------- #
# read-side commands
# --------------------------------------------------------------------------- #
def load_rows(vid: str) -> list[tuple[int, str]]:
    p = OUTDIR / vid / "timed.tsv"
    if not p.exists():
        die(f"no local transcript for {vid}; run: ytscript fetch {vid}")
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append((int(parts[0]), parts[2]))
    return rows


def cmd_info(args) -> int:
    p = OUTDIR / video_id(args.target) / "meta.json"
    if not p.exists():
        die("no metadata; run fetch first")
    m = json.loads(p.read_text(encoding="utf-8"))
    print(f"{m['id']} | {m['title']} | {m['channel']} | "
          f"{ts(m['duration_sec'])} | words={m['words']} lines={m['lines']}")
    return 0


def cmd_search(args) -> int:
    rows = load_rows(video_id(args.target))
    try:
        pat = re.compile(args.query, re.IGNORECASE)
    except re.error as e:
        die(f"bad regex: {e}", 2)
    hits = [(ms, t) for ms, t in rows if pat.search(t)]
    if not hits:
        print(f"0 hits for {args.query!r}")
        return 0
    for ms, t in hits[: args.max]:
        print(f"[{ts(ms / 1000)}] {t}")
    if len(hits) > args.max:
        print(f"... {len(hits) - args.max} more hits suppressed")
    return 0


def cmd_slice(args) -> int:
    rows = load_rows(video_id(args.target))
    a, b = parse_ts(args.start), parse_ts(args.end)
    if b <= a:
        die("end must be after start", 2)
    sel = [(ms, t) for ms, t in rows if a * 1000 <= ms <= b * 1000]
    if not sel:
        print(f"no lines between {ts(a)} and {ts(b)}")
        return 0
    print(f"# {ts(a)}-{ts(b)} ({len(sel)} lines)")
    print(textwrap.fill(" ".join(t for _, t in sel), width=100))
    return 0


def cmd_outline(args) -> int:
    """Time-bucketed digest: cheap way to locate a topic before slicing."""
    rows = load_rows(video_id(args.target))
    if not rows:
        return 0
    step = args.every * 1000
    buckets: dict[int, list[str]] = {}
    for ms, t in rows:
        buckets.setdefault(ms // step, []).append(t)
    for k in sorted(buckets):
        joined = " ".join(buckets[k])
        head = joined[: args.width]
        if len(joined) > args.width:
            head = head.rsplit(" ", 1)[0] + "..."
        print(f"[{ts(k * args.every)}] {head}")
    return 0


def cmd_backfill(args) -> int:
    """
    Add channel_id to transcripts fetched before that field was recorded.

    The channel caches already list which video ids belong to which channel, so
    the mapping is recoverable offline with no extra network calls.
    """
    chan_dir = ROOT / "channels"
    owner: dict[str, tuple[str, str]] = {}
    if chan_dir.is_dir():
        for p in chan_dir.glob("*.json"):
            try:
                b = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            cid, cname = b.get("channel_id", ""), b.get("channel", "")
            for v in b.get("videos", []):
                owner.setdefault(v["id"], (cid, cname))

    changed = skipped = unknown = 0
    for d in sorted(OUTDIR.iterdir()) if OUTDIR.is_dir() else []:
        mp = d / "meta.json"
        if not mp.exists():
            continue
        meta = json.loads(mp.read_text(encoding="utf-8"))
        if meta.get("channel_id"):
            skipped += 1
            continue
        cid, cname = owner.get(d.name, ("", ""))
        if not cid:
            unknown += 1
            continue
        meta["channel_id"] = cid
        if not meta.get("channel"):
            meta["channel"] = cname
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        changed += 1
        print(f"  {d.name} -> {cid} ({cname})")
    print(f"backfill: filled={changed} already_had={skipped} "
          f"no_channel_cache={unknown}")
    return 0


def cmd_prune(args) -> int:
    """
    Remove transcript folders that hold nothing.

    Only genuinely empty directories go. A folder keeping any file at all is left
    alone, so no transcript can be lost by running this.
    """
    if not OUTDIR.is_dir():
        print("transcripts/ 폴더가 없습니다")
        return 0

    empty, kept, partial = [], 0, []
    for d in sorted(OUTDIR.iterdir()):
        if not d.is_dir():
            continue
        names = [p.name for p in d.iterdir()]
        if not names:
            empty.append(d)
        elif "plain.txt" in names:
            kept += 1
        else:
            partial.append((d, names))

    for d, names in partial:
        print(f"  keep {d.name}: plain.txt 는 없지만 파일이 있어 남깁니다 {names}")

    if not empty:
        print(f"prune: 빈 폴더 없음 (정상 {kept}개)")
        return 0

    if args.dry_run:
        for d in empty[:20]:
            print(f"  would delete {d.name}")
        if len(empty) > 20:
            print(f"  ... {len(empty) - 20}개 더")
        print(f"prune(dry-run): 삭제 대상 {len(empty)}개 · 유지 {kept}개")
        return 0

    for d in empty:
        try:
            d.rmdir()                      # fails if anything appeared meanwhile
        except OSError as e:
            print(f"  skip {d.name}: {e}")
    left = sum(1 for d in OUTDIR.iterdir() if d.is_dir() and not any(d.iterdir()))
    print(f"prune: {len(empty)}개 삭제 · 정상 {kept}개 유지 · 남은 빈 폴더 {left}개")
    return 0


def cmd_cost(args) -> int:
    vid = video_id(args.target)
    dest = OUTDIR / vid
    if not (dest / "plain.txt").exists():
        die("run fetch first")

    reps: list[tuple[str, str]] = []
    raw = dest / "raw.json3"
    if raw.exists():
        reps.append(("raw.json3", raw.read_text(encoding="utf-8")))
    reps.append(("timed.tsv", (dest / "timed.tsv").read_text(encoding="utf-8")))
    reps.append(("plain.txt", (dest / "plain.txt").read_text(encoding="utf-8")))

    exact = count_tokens("probe") is not None
    base = None
    print(f"{'representation':<16}{'chars':>10}{'tokens':>10}{'vs plain':>10}")
    for name, text in reps:
        tok = count_tokens(text) if exact else approx_tokens(text)
        if name == "plain.txt":
            base = tok
    for name, text in reps:
        tok = count_tokens(text) if exact else approx_tokens(text)
        ratio = f"{tok / base:.2f}x" if base else "-"
        print(f"{name:<16}{len(text):>10}{tok:>10}{ratio:>10}")
    print(f"({'tiktoken cl100k' if exact else 'approx chars/4'})")
    return 0


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(prog="ytscript", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch"); f.add_argument("target")
    f.add_argument("--lang", default="auto",
                   help="'auto' follows the video's own language, or pass a "
                        "code like ko/en. English is always the last fallback.")
    f.add_argument("--force", action="store_true")
    f.set_defaults(fn=cmd_fetch)

    i = sub.add_parser("info"); i.add_argument("target"); i.set_defaults(fn=cmd_info)

    s = sub.add_parser("search"); s.add_argument("target"); s.add_argument("query")
    s.add_argument("--max", type=int, default=20); s.set_defaults(fn=cmd_search)

    sl = sub.add_parser("slice"); sl.add_argument("target")
    sl.add_argument("start"); sl.add_argument("end"); sl.set_defaults(fn=cmd_slice)

    o = sub.add_parser("outline"); o.add_argument("target")
    o.add_argument("--every", type=int, default=60)
    o.add_argument("--width", type=int, default=140); o.set_defaults(fn=cmd_outline)

    c = sub.add_parser("cost"); c.add_argument("target"); c.set_defaults(fn=cmd_cost)

    b = sub.add_parser("backfill"); b.set_defaults(fn=cmd_backfill)

    pr = sub.add_parser("prune")
    pr.add_argument("--dry-run", action="store_true", help="지우지 않고 목록만")
    pr.set_defaults(fn=cmd_prune)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except subprocess.TimeoutExpired:
        die("yt-dlp timed out")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    os.makedirs(OUTDIR, exist_ok=True)
    raise SystemExit(main())
