#!/usr/bin/env bash
# Added by go0le (2026-11) for the 哈Gemi QQ bot. Launches the MCP server of BAI-LAB/MemoryOS
# (https://github.com/BAI-LAB/MemoryOS, Apache-2.0) with the API key taken from .env.memoryos.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
MCP_DIR="$ROOT/memoryos-mcp"
VENV="$ROOT/.venv"
ENV_FILE="$ROOT/.env.memoryos"
BASE_CONFIG="$MCP_DIR/config.json"
RUNTIME_CONFIG="$ROOT/runtime_config.json"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "MemoryOS venv missing at $VENV" >&2
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${OPENAI_API_KEY:-}" || "$OPENAI_API_KEY" == "REPLACE_ME" ]]; then
  echo "MemoryOS: OPENAI_API_KEY not set. Copy $ROOT/.env.memoryos.example to .env.memoryos and fill a real OpenAI-compatible key." >&2
  exit 1
fi

"$VENV/bin/python" - <<PY
import json, os, sys
base = json.load(open("$BASE_CONFIG"))
base["openai_api_key"] = os.environ["OPENAI_API_KEY"]
if os.environ.get("OPENAI_BASE_URL"):
    base["openai_base_url"] = os.environ["OPENAI_BASE_URL"]
if os.environ.get("LLM_MODEL"):
    base["llm_model"] = os.environ["LLM_MODEL"]
json.dump(base, open("$RUNTIME_CONFIG", "w"), indent=2, ensure_ascii=False)
# 含明文 API key：只允许属主读写
os.chmod("$RUNTIME_CONFIG", 0o600)
# MCP stdio 协议要求 stdout 只能有协议消息：这行提示写到 stderr，否则客户端握手会读到非 JSON 内容
print("MemoryOS runtime config written", file=sys.stderr, flush=True)
PY

cd "$MCP_DIR"
exec "$VENV/bin/python" server_new.py --config "$RUNTIME_CONFIG"
