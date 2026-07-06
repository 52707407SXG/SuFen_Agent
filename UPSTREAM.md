# Upstream Provenance

SuFen-Agent starts from the mature upstream agent runtime at:

- Repository: `https://github.com/NousResearch/sufen-agent`
- Source commit imported locally: `590a19332e898fc9bda55a31999926572d8fbc26`
- License: MIT, retained in `LICENSE`

The upstream runtime is used as an internal chassis for conversation loop, provider/model calls, streaming, retry/fallback, context compression, memory interfaces, tool registry, sessions, HTTP server patterns, redaction, and tests.

SuFen's public identity, command, environment variables, My Stand task package, scoped memory rules, draft-only write policy, and business prompts are implemented in the `sufen/` package and SuFen-specific tests.
