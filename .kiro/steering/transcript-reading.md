---
inclusion: fileMatch
fileMatchPattern: 'transcripts/**'
---

# Working with stored transcripts

## Default: never read a transcript whole

`transcripts/<id>/plain.txt` can be very large. Measured against a 2-hour video
it is 23,772 tokens; the `raw.json3` beside it is 908,561 and must never be
opened. Use `scripts/ytscript.py`, which returns only the answer:

```bash
python scripts/ytscript.py info    <id>                  # title, duration, counts
python scripts/ytscript.py outline <id> --every 300      # locate a topic first
python scripts/ytscript.py search  <id> "<regex>"        # matching lines
python scripts/ytscript.py slice   <id> 12:00 14:30      # just that window
```

| approach | tokens |
|---|---|
| `raw.json3` whole | 908,561 |
| `plain.txt` whole | 23,772 |
| `outline --every 300` | 716 |
| `slice` 5-minute window | 1,063 |
| `search`, 5 hits | 72 |
| `info` | 48 |

Normal workflow: `info`, then `outline` or `search` to find the region, then
`slice` it.

## Exception: producing a 정리본 (cleanup)

Writing a 정리본 is the one task that legitimately needs the whole transcript,
because the prompt asks for the full logical flow to be restructured. When the
user asks for one, reading `plain.txt` in full is correct and expected.

Procedure:

1. Resolve which video they mean. `python scripts/ytclean.py list --todo` shows
   what still lacks a 정리본; `ytscript.py info <id>` confirms a specific one.
2. Read the instructions in `prompts/cleanup.md`. Follow them exactly. If the
   user names a different prompt, read `prompts/<name>.md` instead.
3. Read `transcripts/<id>/plain.txt` in full.
4. Write the result to `transcripts/<id>/clean.md`. Front matter is required so
   the site can display provenance:

   ```markdown
   ---
   source: <video id>
   prompt: cleanup
   generated: YYYY-MM-DD
   ---

   ...the 정리본...
   ```

5. Tell the user it is ready and that the reader shows it in the right pane.

Do one video per request unless asked otherwise. Each one costs roughly 17,000
tokens in and out, so batching many is expensive and was explicitly ruled out.

Never invent content that is not in the transcript, and never soften or sharpen
the speaker's claims. Numbers, place names and proper nouns must survive intact.
