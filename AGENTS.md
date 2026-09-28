# AGENTS.md

## 자동 커밋·푸시 (항상 적용)

정리본(`transcripts/<id>/clean.*.md`)이나 그 밖의 사이트 반영 파일을 새로 만들거나
수정한 뒤에는 **사용자가 따로 말하지 않아도** 아래를 자동으로 수행한다.

1. 변경한 파일만 `git add` 한다 (관련 없는 파일·비밀값 포함 금지).
2. 기존 커밋 스타일에 맞춰 한국어 메시지로 커밋한다. 예:
   `feat: 모두의부동산 DeepSeek 4.1 정리본 N편 추가`.
3. `git push origin main` 한다.
4. GitHub Actions(`Build static site`)가 끝날 때까지 기다린다
   (`gh run watch <id> --exit-status`).
5. Actions가 `docs/`를 리빌드해 되돌려 커밋하면 `git pull --rebase origin main`
   으로 로컬에 동기화한다.
6. 로컬과 `origin/main`이 일치하는지 확인한 뒤 결과를 보고한다.

## 정리본 작성 규칙

- 채널별 정리 지침은 `prompts/cleanup-<채널명>.md` 를 따른다.
  (예: 모두의부동산 → `prompts/cleanup-모두의부동산.md`)
- 모델별 결과는 `transcripts/<id>/clean.<모델>.md` 로 저장한다.
  (예: DeepSeek 4.1 → `clean.deepseek-4.1.md`)
- 맨 위 머리말(front matter)을 넣는다.
  ```markdown
  ---
  source: <VIDEO_ID>
  prompt: cleanup-<채널명>
  model: <모델명>
  generated: YYYY-MM-DD
  ---
  ```
- 머리말 바로 아래, 제목(`# ...`) 위에 **3줄 요약**을 인용 블록으로 넣는다.
  ```markdown
  > **3줄 요약**
  > 1. <핵심 결론>
  > 2. <근거·맥락>
  > 3. <행동 지침·전망>
  ```
- `plain.txt`(시간 없는 원본)를 바탕으로 하되, 캡처 시점이 필요하면
  `timed.tsv` 를 참고한다. 원본에 없는 내용은 지어내지 않는다.
