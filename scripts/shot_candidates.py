#!/usr/bin/env python3
"""
shot_candidates - timed.tsv에서 캡쳐 후보 시점을 뽑아 사람이 라벨을 달기 쉽게 출력.

존코바 채널 영상은 대부분 "첫 번째 ~ N번째" 식으로 항목을 나열하고,
중간중간 "지금 보시면", "이거 보면" 처럼 화면 자료를 가리킨다.
그 두 종류의 신호가 있는 줄을 시간순으로 모아, 캡쳐할 만한 시점을 제안한다.

실제 라벨(캡션)은 사람이 정리본을 보고 붙인다. 이 스크립트는 후보만 만든다.

사용법:
    python scripts/shot_candidates.py <VIDEO_ID> [간격초]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = ROOT / "transcripts"

# 항목을 새로 시작하는 신호 — 이 지점이 정리본의 ## 섹션과 거의 일치한다.
ORDINAL = re.compile(
    r"(첫\s*번째|두\s*번째|세\s*번째|네\s*번째|다섯\s*번째|여섯\s*번째|"
    r"일곱\s*번째|여덟\s*번째|아홉\s*번째|열\s*번째|마지막|"
    r"\d\s*단계|\d\s*년\s*차|다음으로|그다음)"
)

# 화면의 자료를 가리키는 신호 — 캡쳐하면 의미가 있는 순간.
SCREEN = re.compile(
    r"(보시면|보실래요|보면은|보면\s|한번\s*볼|볼까요|보세요|보이시나요|보이죠|"
    r"이거\s*봐|지금\s*이|여기\s*보|왼쪽|오른쪽|이런\s*식으로|요런\s*식으로|"
    r"이렇게\s*하면|화면|비교|어때요)"
)


def read_timed(vid: str) -> list[tuple[float, str]]:
    """timed.tsv → [(초, 자막), ...]. 형식은 <밀리초>\t<초>\t<텍스트>."""
    p = TRANSCRIPTS / vid / "timed.tsv"
    out: list[tuple[float, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            sec = float(parts[1])
        except ValueError:
            continue
        text = parts[2].strip()
        if text:
            out.append((sec, text))
    return out


def fmt(sec: float) -> str:
    """초 → MM:SS (한 시간을 넘으면 H:MM:SS)."""
    s = int(round(sec))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def pick(rows: list[tuple[float, str]], gap: float) -> list[tuple[float, str, str]]:
    """후보 뽑기. 항목 시작(ordinal)을 우선하고, 화면 참조는 간격을 두고 채운다."""
    picked: list[tuple[float, str, str]] = []

    def far_enough(sec: float, min_gap: float) -> bool:
        return all(abs(sec - p[0]) >= min_gap for p in picked)

    # 1순위: 항목이 새로 시작하는 지점
    for sec, text in rows:
        if sec < 8:                      # 인트로/인사 구간은 건너뛴다
            continue
        if ORDINAL.search(text) and far_enough(sec, gap * 0.6):
            picked.append((sec, "항목", text))

    # 2순위: 화면 자료를 가리키는 지점
    for sec, text in rows:
        if sec < 8:
            continue
        if SCREEN.search(text) and far_enough(sec, gap):
            picked.append((sec, "화면", text))

    picked.sort(key=lambda r: r[0])
    return picked[:50]                   # 한 영상당 최대 50개


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    vid = sys.argv[1]
    gap = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    rows = read_timed(vid)
    if not rows:
        print(f"{vid}: timed.tsv를 읽을 수 없습니다.")
        return 1

    cands = pick(rows, gap)
    print(f"# {vid} — 후보 {len(cands)}개 (영상 길이 약 {fmt(rows[-1][0])})")
    for sec, kind, text in cands:
        print(f'{fmt(sec)}\t{kind}\t{text[:60]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
