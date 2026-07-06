# Providers

SuFen first release ships a reviewed provider registry with DeepSeek as the
default model provider.

The public runtime reads only `SUFEN_PROVIDER`, `SUFEN_MODEL`,
`SUFEN_API_KEY`, and `SUFEN_BASE_URL`. Provider discovery is closed in
`SUFEN_AGENT_MODE=1`, so local private provider plugins cannot alter a fresh
clone.

## Layout

```text
providers/
├── base.py
├── __init__.py
└── README.md
```

The bundled DeepSeek profile lives in
`plugins/model-providers/deepseek/`. Future reviewed providers should be added
there with explicit tests and SuFen-only environment handling.
