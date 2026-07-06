# SuFen-Agent

SuFen 是 My Stand 的档案军师：面向业主、客户、经纪人和售后档案，基于授权资料、知识图谱、经纪人特征卡和 scoped memory，输出策略建议、事件草稿、字段 diff 草稿和 memoryPatch。

第一版目标是本地可运行、可验证、可继续改造。它不会连接 My Stand 生产 SQLite，不会读取现有私有 `.env`，不会提交密钥，也不会直接写正式档案。

## Quick Start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e ".[all]"
cp .env.example .env
```

填入 `.env` 里的 SuFen 专属 key 后：

```bash
sufen --version
sufen chat -q "AUTH-P-1001 这个业主现在该怎么跟" --task-package /path/to/task-package.json
sufen serve
```

健康检查：

```bash
curl http://127.0.0.1:8791/health
```

`/v1/chat` 面向 My Stand 后端注入的强任务包。缺少 `taskPackage`
时会 fail closed；带入授权档案上下文、经纪人特征卡、知识图谱引用和
scoped memory key 后，第一版业主房源档案场景会返回策略建议、事件草稿、
字段 diff 草稿和 memoryPatch。

生产模式默认调用 `SUFEN_PROVIDER` / `SUFEN_MODEL` / `SUFEN_PROVIDER_API_KEY` /
`SUFEN_BASE_URL` 指向的 OpenAI-compatible provider。测试或本地 dry-run
可以显式使用 `sufen chat --fake` 或 `SUFEN_FAKE_PROVIDER=1`。
`/v1/chat` 必须带 `Authorization: Bearer <SUFEN_SERVICE_API_KEY>` 或
`X-SuFen-API-Key: <SUFEN_SERVICE_API_KEY>`。

## First-Release Boundaries

- 只读取 `SUFEN_*` 环境变量。
- `SUFEN_SERVICE_API_KEY` 只用于 My Stand -> SuFen 服务间鉴权。
- `SUFEN_PROVIDER_API_KEY` 只用于 SuFen -> DeepSeek/OpenAI-compatible provider。
- `SUFEN_API_KEY` 仅作为 deprecated fallback 保留，部署建议不要使用。
- `/v1/chat` 校验服务间 API key；服务 key 未配置时生产对话 fail closed。
- 生产 `/v1/chat` 必须有 delegationToken；fake/dry-run 模式可放宽。
- delegationToken 使用 `SUFEN_DELEGATION_HMAC_SECRET` 做 HMAC 校验，并检查过期时间、subject/operator、allowedActions 和 nonce 重放。
- 所有 My Stand 写入都只生成草稿，由前端展示并由后端在用户确认后写入。
- scoped memory 路径使用稳定 ID，不使用中文名做主路径。
- 默认工具集是 SuFen 白名单：授权解析、授权档案读取适配位、知识图谱读取适配位、解析、web search/extract、scoped memory search/patch draft、事件草稿、字段 diff 草稿。
- HTTP 对话入口要求 My Stand 后端注入 `taskPackage`，不接受裸请求装成已掌握档案事实。

## Verification

```bash
python scripts/sufen_rebrand_check.py
python scripts/sufen_secret_scan.py
pytest tests/sufen
```

后续上传 GitHub 前，还需要按 `RUNBOOK.md` 和 `TEST_REPORT.md` 反复核对。
