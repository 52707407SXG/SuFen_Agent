# SuFen-Agent Development Guide

Work only inside this repository unless the task explicitly says otherwise.

Hard boundaries:

- Do not connect to My Stand production SQLite.
- Do not read or copy private `.env`, `auth.json`, logs, sessions, transcripts, memories, caches, `.venv`, or `node_modules`.
- Do not push to GitHub until the user explicitly says to upload.
- Keep SuFen public identity separate from Miner, Lucan, and other agents.
- All My Stand write operations must remain draft-only in SuFen.

Expected checks before handoff:

```bash
python scripts/sufen_rebrand_check.py
pytest tests/sufen
sufen --version
```
