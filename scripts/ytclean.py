#!/usr/bin/env python3
"""
ytclean - manage the 정리본 (cleaned-up write-up) that sits beside each transcript.

The 정리본 itself is written by an AI following prompts/cleanup.md. This tool does
the bookkeeping around it: what still needs one, what a prompt says, and where
the output belongs.

Commands
--------
  list [--todo|--done] [--channel <key>]   What has a 정리본 and what does not.
  prompts                                  Available prompt files.
  show <id> [--prompt cleanup]             Print prompt + transcript together,
                                           ready to hand to any AI.
  save <id> <file> [--prompt cleanup]      Store a 정리본 from a file ('-' = stdin).
  drop <id>                                Delete a 정리본.

Layout
------
  prompts/<name>.md            instructions, editable
  transcripts/<id>/clean.md    the 정리본, with front matter naming its source
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = ROOT / "transcripts"
PROMPTS = ROOT / "prompts"
CHANNELS = ROOT / "channels"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def vid_dir(vid: str) -> Path:
    if not VIDEO_ID_RE.match(vid or ""):
        die(f"bad video id: {vid!r}", 2)
    d = TRANSCRIPTS / vid
    if not (d / "plain.txt").exists():
        die(f"no transcript for {vid}; extract it first")
    return d


def prompt_file(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        die(f"bad prompt name: {name!r}", 2)
    p = (PROMPTS / f"{name}.md").resolve()
    if not str(p).startswith(str(PROMPTS.resolve())):
        die("prompt path escapes prompts/", 2)
    if not p.exists():
        die(f"no such prompt: prompts/{name}.md")
    return p


def strip_front_matter(text: str) -> tuple[dict, str]:
    """Return (front matter as a flat dict, body)."""
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


def meta_of(vid: str) -> dict:
    p = TRANSCRIPTS / vid / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def all_videos() -> list[str]:
    if not TRANSCRIPTS.is_dir():
        return []
    return sorted(d.name for d in TRANSCRIPTS.iterdir()
                  if (d / "plain.txt").exists())


def channel_of(vid: str) -> str:
    """Prefer the channel cache, fall back to what the transcript recorded."""
    for p in CHANNELS.glob("*.json") if CHANNELS.is_dir() else []:
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if any(v["id"] == vid for v in b.get("videos", [])):
            return b.get("channel") or p.stem
    return meta_of(vid).get("channel", "")


# --------------------------------------------------------------------------- #
def cmd_list(args) -> int:
    rows = []
    for vid in all_videos():
        clean = TRANSCRIPTS / vid / "clean.md"
        has = clean.exists()
        if args.todo and has:
            continue
        if args.done and not has:
            continue
        m = meta_of(vid)
        ch = channel_of(vid)
        if args.channel and args.channel.lower() not in ch.lower():
            continue
        rows.append((vid, has, m.get("words") or 0, ch, m.get("title", ""),
                     clean.stat().st_size if has else 0))

    done = sum(1 for r in rows if r[1])
    print(f"{len(rows)}개 표시 · 정리본 있음 {done} · 없음 {len(rows) - done}")
    print()
    print(f"{'video id':<13}{'정리':<5}{'words':>7}  {'channel':<14}title")
    print("-" * 100)
    for vid, has, words, ch, title, size in rows[: args.max]:
        print(f"{vid:<13}{'O' if has else '-':<5}{words:>7}  {ch[:13]:<14}{title[:44]}")
    if len(rows) > args.max:
        print(f"... {len(rows) - args.max}개 더 (--max 로 늘리기)")
    return 0


def cmd_prompts(args) -> int:
    if not PROMPTS.is_dir():
        die("prompts/ 폴더가 없습니다")
    files = sorted(PROMPTS.glob("*.md"))
    if not files:
        die("prompts/ 안에 .md 파일이 없습니다")
    for p in files:
        meta, body = strip_front_matter(p.read_text(encoding="utf-8"))
        print(f"{p.stem:<16}{meta.get('name', ''):<18}{meta.get('description', '')[:52]}")
        print(f"{'':<16}{len(body):,}자")
    return 0


def cmd_show(args) -> int:
    d = vid_dir(args.id)
    _, instructions = strip_front_matter(
        prompt_file(args.prompt).read_text(encoding="utf-8"))
    text = (d / "plain.txt").read_text(encoding="utf-8")
    m = meta_of(args.id)

    print(instructions.strip())
    print()
    print("---")
    print()
    print(f"# {m.get('title', args.id)}")
    print(f"채널: {m.get('channel', '')}")
    print()
    print(text.strip())
    return 0


def cmd_save(args) -> int:
    d = vid_dir(args.id)
    body = (sys.stdin.read() if args.file == "-"
            else Path(args.file).read_text(encoding="utf-8"))
    body = body.strip()
    if len(body) < 50:
        die("내용이 너무 짧습니다 (50자 미만)")

    # Rewrite any front matter the AI may have emitted so provenance is ours.
    _, body = strip_front_matter(body)
    out = (f"---\nsource: {args.id}\nprompt: {args.prompt}\n"
           f"generated: {date.today().isoformat()}\n---\n\n{body.strip()}\n")
    (d / "clean.md").write_text(out, encoding="utf-8")
    print(f"saved transcripts/{args.id}/clean.md  ({len(body):,}자)")
    return 0


def cmd_drop(args) -> int:
    p = vid_dir(args.id) / "clean.md"
    if not p.exists():
        print(f"{args.id}: 정리본이 없습니다")
        return 0
    p.unlink()
    print(f"deleted transcripts/{args.id}/clean.md")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ytclean", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list")
    g = l.add_mutually_exclusive_group()
    g.add_argument("--todo", action="store_true", help="정리본 없는 것만")
    g.add_argument("--done", action="store_true", help="정리본 있는 것만")
    l.add_argument("--channel", default="", help="채널 이름 일부로 필터")
    l.add_argument("--max", type=int, default=40)
    l.set_defaults(fn=cmd_list)

    sub.add_parser("prompts").set_defaults(fn=cmd_prompts)

    s = sub.add_parser("show")
    s.add_argument("id")
    s.add_argument("--prompt", default="cleanup")
    s.set_defaults(fn=cmd_show)

    sv = sub.add_parser("save")
    sv.add_argument("id")
    sv.add_argument("file", help="파일 경로, 또는 - (표준입력)")
    sv.add_argument("--prompt", default="cleanup")
    sv.set_defaults(fn=cmd_save)

    dr = sub.add_parser("drop")
    dr.add_argument("id")
    dr.set_defaults(fn=cmd_drop)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
