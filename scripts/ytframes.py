#!/usr/bin/env python3
"""
ytframes - capture still frames from YouTube videos at chosen timestamps.

This is the local (execution) half of the frame-capture feature. The judgment
half — deciding *which* moments are worth a still — is done elsewhere: an
online AI reads the timed transcript from GitHub and writes a request file,
transcripts/<id>/shots.json. This script reads those requests and pulls the
actual frames with yt-dlp + ffmpeg, entirely on the local machine (the online
AI has no outbound access to YouTube).

Why local: yt-dlp fetches the video stream URL, and ffmpeg seeks to each
timestamp and grabs a single frame. Putting -ss *before* -i makes ffmpeg use
HTTP range requests, so a 2-hour video costs only a small read near each mark
instead of a full download.

Request file  transcripts/<id>/shots.json   (written by the online AI)
    { "source": "<id>",
      "shots": [ { "t": "3:12", "label": "강남 아파트 가격 그래프" }, ... ] }

Result files  transcripts/<id>/frames/<file>.jpg
              transcripts/<id>/frames/index.json   (what the site reads)
    { "source": "<id>", "generated": "YYYY-MM-DD",
      "frames": [ { "t": "3:12", "sec": 192, "label": "...",
                    "file": "f000192.jpg" }, ... ] }

Commands
--------
  sync            Capture every shots.json that is not yet captured.
  one <id>        Capture a single video's shots.json.
      --force     Re-capture even if frames already exist.
      --height N  Max frame height (default 720).
      --max N     Max frames per video (default 50).

Exit codes: 0 ok, 1 failure, 2 bad usage.
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
TRANSCRIPTS = ROOT / "transcripts"

# Cap per video. The user asked for a generous ceiling rather than a tight one;
# 50 stills is plenty for even a long lecture and keeps the repo from ballooning.
MAX_FRAMES = 50
DEFAULT_HEIGHT = 720

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# yt-dlp keeps getting throttled by YouTube's SABR rollout; rotating the player
# client is what actually gets us a usable stream URL. Mirrors ytscript.py.
PLAYER_CLIENTS = ["default", "web_safari", "tv", "mweb"]


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def need(exe_names: list[str], hint: str) -> str:
    for name in exe_names:
        found = shutil.which(name)
        if found:
            return found
    die(f"{exe_names[0]} not found on PATH. {hint}")


def parse_ts(text: str) -> int:
    """'90' | '3:12' | '1:02:03' -> whole seconds."""
    parts = str(text).strip().split(":")
    if not parts or not all(p.isdigit() for p in parts) or len(parts) > 3:
        die(f"bad timestamp {text!r}; use SS, MM:SS or HH:MM:SS", 2)
    total = 0
    for n in parts:
        total = total * 60 + int(n)
    return total


def fmt_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def hhmmss(sec: int) -> str:
    """ffmpeg-friendly HH:MM:SS."""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# yt-dlp / ffmpeg
# --------------------------------------------------------------------------- #
def stream_url(ytdlp: str, vid: str, height: int) -> str:
    """Ask yt-dlp for a single video stream URL no taller than `height`.

    The URL is signed and expires within hours, so it must be used right away.
    We fetch it once per video and reuse it for every frame of that video.
    """
    fmt = (f"bv*[height<={height}][ext=mp4]/bv*[height<={height}]/"
           f"best[height<={height}]/best")
    url = f"https://www.youtube.com/watch?v={vid}"
    last = ""
    for client in PLAYER_CLIENTS:
        cp = subprocess.run(
            [ytdlp, "--no-warnings", "--no-playlist", "--ignore-no-formats-error",
             "--extractor-args", f"youtube:player_client={client}",
             "-f", fmt, "-g", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, shell=False)
        lines = [l.strip() for l in cp.stdout.splitlines() if l.strip().startswith("http")]
        if lines:
            # First URL is the video stream (audio, if separate, comes second).
            return lines[0]
        last = (cp.stderr or cp.stdout or "").strip()
    die(f"could not get a stream URL for {vid}: {last[-160:]}")


def grab_frame(ffmpeg: str, url: str, sec: int, out: Path) -> bool:
    """Pull one frame at `sec`. -ss before -i = fast range-based seek."""
    cp = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-y",
         "-ss", hhmmss(sec), "-i", url,
         "-frames:v", "1", "-q:v", "2", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, shell=False)
    return cp.returncode == 0 and out.exists() and out.stat().st_size > 0


# --------------------------------------------------------------------------- #
# request / result files
# --------------------------------------------------------------------------- #
def load_shots(vid: str) -> list[dict]:
    p = TRANSCRIPTS / vid / "shots.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        die(f"{vid}: shots.json is not valid JSON ({e})")
    shots = data.get("shots") if isinstance(data, dict) else data
    if not isinstance(shots, list):
        die(f"{vid}: shots.json has no 'shots' list")
    out = []
    for s in shots:
        if isinstance(s, dict) and s.get("t"):
            out.append({"t": str(s["t"]), "label": str(s.get("label", "")).strip()})
    return out


def already_done(vid: str, shots: list[dict]) -> bool:
    """True when the existing capture already matches this request exactly, so a
    plain `sync` never redoes work. `--force` bypasses this."""
    idx = TRANSCRIPTS / vid / "frames" / "index.json"
    if not idx.exists():
        return False
    try:
        prev = json.loads(idx.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    prev_secs = sorted(f.get("sec") for f in prev.get("frames", []))
    want_secs = sorted(parse_ts(s["t"]) for s in shots)
    return prev_secs == want_secs and prev_secs != []


def capture_video(vid: str, *, force: bool, height: int, max_frames: int) -> int:
    """Capture one video's shots. Returns number of frames written."""
    shots = load_shots(vid)
    if not shots:
        print(f"skip {vid}: no shots.json")
        return 0

    if len(shots) > max_frames:
        print(f"note {vid}: {len(shots)} shots requested, capping at {max_frames}")
        shots = shots[:max_frames]

    if not force and already_done(vid, shots):
        print(f"cached {vid}: {len(shots)} frames already captured")
        return 0

    ytdlp = need(["yt-dlp", "yt-dlp.exe"], "Install with: python -m pip install yt-dlp")
    ffmpeg = need(["ffmpeg", "ffmpeg.exe"], "Install ffmpeg and add it to PATH.")

    frames_dir = TRANSCRIPTS / vid / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    url = stream_url(ytdlp, vid, height)

    written, seen = [], set()
    for s in shots:
        sec = parse_ts(s["t"])
        if sec in seen:                 # collapse duplicate timestamps
            continue
        seen.add(sec)
        fname = f"f{sec:06d}.jpg"
        out = frames_dir / fname
        if grab_frame(ffmpeg, url, sec, out):
            written.append({"t": fmt_ts(sec), "sec": sec,
                            "label": s["label"], "file": fname})
            print(f"  ok  {vid} @ {fmt_ts(sec)} -> frames/{fname}")
        else:
            print(f"  MISS {vid} @ {fmt_ts(sec)} (ffmpeg could not grab a frame)")

    if not written:
        die(f"{vid}: captured 0 frames (stream URL expired or seeks failed)")

    # Drop any stale jpgs from a previous, different request.
    keep = {w["file"] for w in written}
    for old in frames_dir.glob("f*.jpg"):
        if old.name not in keep:
            old.unlink(missing_ok=True)

    written.sort(key=lambda w: w["sec"])
    (frames_dir / "index.json").write_text(
        json.dumps({"source": vid,
                    "generated": time.strftime("%Y-%m-%d"),
                    "frames": written}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"done {vid}: {len(written)} frames")
    return len(written)


# --------------------------------------------------------------------------- #
def pending_ids() -> list[str]:
    if not TRANSCRIPTS.is_dir():
        return []
    return sorted(d.name for d in TRANSCRIPTS.iterdir()
                  if (d / "shots.json").exists())


def cmd_sync(args) -> int:
    ids = pending_ids()
    if not ids:
        print("올릴 shots.json이 없습니다. (온라인 AI가 아직 캡쳐 목록을 만들지 않음)")
        return 0
    total, done = 0, 0
    for vid in ids:
        n = capture_video(vid, force=args.force, height=args.height,
                          max_frames=args.max)
        total += n
        done += 1 if n else 0
    print(f"\n요약: 영상 {len(ids)}개 중 {done}개 캡쳐 · 프레임 {total}장")
    return 0


def cmd_one(args) -> int:
    vid = args.id.strip()
    if not ID_RE.match(vid):
        die(f"bad video id {vid!r}", 2)
    n = capture_video(vid, force=args.force, height=args.height, max_frames=args.max)
    return 0 if n else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="ytframes",
                                 description="Capture YouTube frames at chosen timestamps.")
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                    help=f"max frame height (default {DEFAULT_HEIGHT})")
    ap.add_argument("--max", type=int, default=MAX_FRAMES,
                    help=f"max frames per video (default {MAX_FRAMES})")
    ap.add_argument("--force", action="store_true",
                    help="re-capture even if frames already exist")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="capture every pending shots.json")
    p_one = sub.add_parser("one", help="capture a single video")
    p_one.add_argument("id")

    args = ap.parse_args()
    if args.cmd == "sync":
        return cmd_sync(args)
    if args.cmd == "one":
        return cmd_one(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
