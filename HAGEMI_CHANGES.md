# hagemi 分支的改动

基于上游 [BAI-LAB/MemoryOS](https://github.com/BAI-LAB/MemoryOS) `587ed77`，给 QQ 机器人「哈Gemi」用的 MCP 服务端改动。许可证沿用上游 Apache-2.0。

## memoryos-mcp/server_new.py

- **按用户隔离**：`add_memory` / `retrieve_memory` / `get_user_profile` 都新增必填参数 `user_id`。每个 `user_id` 有独立的 MemoryOS 实例（短/中/长期记忆、用户画像、助手侧知识都分开），互相读不到。
- 同时加载的用户实例上限 `MEMORYOS_MAX_USERS`（默认 16），超过就淘汰最久没用的（数据已落盘，下次用到再加载）。
- `user_id` 用白名单正则校验 `^[A-Za-z0-9][A-Za-z0-9:_-]{0,63}$`，落盘目录名里的 `:` 换成 `_`，防止路径穿越。
- **stdout 保护**：MCP stdio 协议要求 stdout 只能有协议消息。库里的 `print` 会被转到 stderr，不再破坏客户端握手。
- 启动时后台预加载嵌入模型，第一次调用工具不用等十几秒。

## memoryos-mcp/memoryos/utils.py

- `_parse_summary_list`：DeepSeek 等模型会把 JSON 包在 Markdown 代码围栏里，原来的 `json.loads` 解析失败后，每次都退回「General conversation segment…」兜底摘要。现在会先去掉围栏，再取第一个 JSON 数组或对象，也兼容 `{"summaries": [...]}`。

## 启动与配置

- `run_memoryos_mcp.sh`：从 `.env.memoryos` 读取 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL`，生成 `runtime_config.json`（权限 600）后启动 stdio MCP。提示信息写到 stderr。
- `.env.memoryos.example`：配置模板，支持任意 OpenAI 兼容接口（OpenAI、DeepSeek、Gemini OpenAI 兼容）。
- `memoryos-mcp/requirements-cpu.txt`：纯 CPU 环境的依赖。
- `memoryos-mcp/config.json`：默认 `short_term_capacity` 10、`mid_term_heat_threshold` 5.0，嵌入模型 `BAAI/bge-m3`。**密钥字段留空**，真实密钥只放在 `.env.memoryos` 里（已加入 `.gitignore`）。

```bash
cp .env.memoryos.example .env.memoryos   # 填入 OPENAI_API_KEY
./run_memoryos_mcp.sh
```
