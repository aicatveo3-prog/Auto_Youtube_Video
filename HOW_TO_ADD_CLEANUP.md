# 정리본(clean.md) 추가 방법 — 외부 AI/사람용

이 저장소의 자막을 읽고 **정리본**을 만들어 넣는 방법입니다. 여기에 파일을
커밋하면 GitHub Actions가 자동으로 사이트를 다시 빌드해 반영합니다. 로컬 PC는
필요 없습니다.

## 1. 원본 자막 읽기

각 영상의 자막은 아래 경로에 있습니다.

```
transcripts/<VIDEO_ID>/plain.txt     ← 정리할 원본 (문어체 대상)
transcripts/<VIDEO_ID>/meta.json     ← 제목, 채널, 단어 수 등
```

`<VIDEO_ID>` 는 유튜브 영상 ID입니다 (예: `dekLqwB2les`).

## 2. 정리 지침

정리 규칙은 `prompts/cleanup.md` 에 있습니다. 그 지침을 그대로 따르세요.
(구어체→문어체, 반복 제거, 소제목 구조화, 원본 대비 60~70% 분량 등)

## 3. 정리본 저장 위치와 형식

결과를 **같은 폴더**에 `clean.md` 로 저장합니다.

```
transcripts/<VIDEO_ID>/clean.md
```

파일 맨 위에 아래 머리말(front matter)을 넣어주세요. 없어도 표시는 되지만,
넣으면 사이트가 출처/날짜를 함께 보여줍니다.

```markdown
---
source: <VIDEO_ID>
prompt: cleanup
generated: YYYY-MM-DD
---

(여기부터 정리본 본문. ##, ###, **볼드**, > 인용, 표, 🧠 요약 등 마크다운 사용)
```

## 4. 커밋하면 끝

`transcripts/<VIDEO_ID>/clean.md` 를 커밋/푸시하면:

1. GitHub Actions(`.github/workflows/build-pages.yml`)가 자동 실행되고
2. `scripts/build_site.py` 가 `docs/` 를 다시 빌드하고
3. 그 결과를 되돌려 커밋해서
4. 1~2분 뒤 Pages 사이트에 정리본이 나타납니다.

`docs/` 폴더는 자동 생성물이므로 **직접 수정하지 마세요.** 원본은 항상
`transcripts/<id>/clean.md` 입니다.

## 주의

- `raw.json3` 는 저장소에 없습니다(용량 문제로 제외). 정리는 `plain.txt` 로 하세요.
- 원본에 없는 내용을 지어내지 말고, 수치·고유명사·지역명은 정확히 보존하세요.
