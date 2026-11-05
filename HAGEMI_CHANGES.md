# hagemi 分支：在 MemoryOS 基础上的改动

## 原版出处

- **项目**：[BAI-LAB/MemoryOS](https://github.com/BAI-LAB/MemoryOS)（论文：[MemoryOS: Memory OS of AI Agent](https://arxiv.org/abs/2506.06326)）
- **基于的版本**：上游 `main` 的提交 [`587ed77`](https://github.com/BAI-LAB/MemoryOS/commit/587ed7755c7aed179965792830ff1b5ad9a6fa92)（2026-07-07）
- **许可证**：Apache License 2.0，沿用上游，见 [`LICENSE`](LICENSE)。本分支修改过的文件开头都加了修改声明。

短期、中期、长期三层记忆，自动摘要，用户画像，按热度升级到长期记忆，这些机制都是 MemoryOS 原有的，本分支没有改它的算法。下面列的是为 QQ 机器人「哈Gemi」做的改动。

## 1. MCP 服务端（`memoryos-mcp/server_new.py`）

- **按用户隔离**：`add_memory` / `retrieve_memory` / `get_user_profile` 都新增必填参数 `user_id`。每个 `user_id` 有独立的 MemoryOS 实例，短/中/长期记忆、用户画像、助手侧知识都分开存，互相读不到。原版一个服务进程只服务一个用户。
- 同时加载的用户实例上限 `MEMORYOS_MAX_USERS`（默认 16），超过就淘汰最久没用的。数据已经落盘，下次用到再加载。
- `user_id` 按白名单正则校验（`^[A-Za-z0-9][A-Za-z0-9:_-]{0,63}$`），落盘目录名里的 `:` 换成 `_`，防止路径穿越。
- **stdout 保护**：MCP stdio 协议要求 stdout 只能有协议消息。库里的 `print` 会被转到 stderr，不再破坏客户端握手。
- 启动时在后台预加载嵌入模型，第一次调用工具不用等十几秒。

## 2. 摘要解析（`memoryos-mcp/memoryos/utils.py`）

- 新增 `_parse_summary_list`。DeepSeek 等模型会把 JSON 包在 Markdown 代码围栏里，原来的 `json.loads` 解析失败后，每次都退回一句没有内容的兜底摘要（「General conversation segment…」）。现在先去掉围栏，再取第一个 JSON 数组或对象，也兼容 `{"summaries": [...]}`。

## 3. 启动与配置

- `run_memoryos_mcp.sh`（新增）：从 `.env.memoryos` 读 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL`，生成 `runtime_config.json`（权限 600）后启动 stdio MCP。提示信息写到 stderr。
- `.env.memoryos.example`（新增）：配置模板，支持任意 OpenAI 兼容接口（OpenAI、DeepSeek、Gemini 的 OpenAI 兼容端点）。
- `memoryos-mcp/requirements-cpu.txt`（新增）：纯 CPU 环境的依赖。
- `memoryos-mcp/config.json`：默认 `short_term_capacity` 10、`mid_term_heat_threshold` 5.0，嵌入模型 `BAAI/bge-m3`。密钥字段留空，真实密钥只放在 `.env.memoryos` 里（已加入 `.gitignore`）。

```bash
cp .env.memoryos.example .env.memoryos   # 填入 OPENAI_API_KEY
./run_memoryos_mcp.sh
```

## 4. 召回策略（`hagemi/client/memory-client.ts`）

这是机器人这边调用 MemoryOS 的客户端（TypeScript），不改 MemoryOS 本身，只改「检索结果怎么拼成提示词」。评测分数的提升主要来自这一层。

- **为什么由程序调用**：记忆按 QQ 号隔离，QQ 号只取自消息事件里的发送者。如果让模型自己调用记忆工具、自己传 `user_id`，用户说一句「用别人的 ID」就能读到别人的记忆。
- **原始做法**（v1）：`retrieve_memory(max_results=3)`，只用画像、3 条知识和 3 个中期检索页。刚说过、还没整理进中期记忆的事情检索不到，结果是越近的事越答不出来。
- **改进后**（v2）：
  - 最近 10 轮原话（短期记忆）；
  - 7 个中期检索页，按时间排序，去掉和近期原话重复的；
  - 用户侧、助手侧知识各 4 条，分开标注是谁说的，避免把助手的建议记成用户说的；
  - 用户画像；
  - 每条都带时间和说话人，最后提示「同一件事以最近一次为准，记忆里没有的就说不记得」。

## 5. 评测（`hagemi/bench/`）

- **考卷**：[Ornn8/memory-system-benchmark](https://github.com/Ornn8/memory-system-benchmark) 的公开示例卷（MIT 许可），108 题，27 个考点 × 0/3/15/45 天四个时间档，360 轮虚构对话历史。**考卷不随本仓库分发**，请从原仓库获取。
- **脚本**：`hagemi/bench/run_bench.py`（我们写的）。逐轮把历史喂给真实的 MCP 服务端；每题只用召回结果作答，不提供全文历史，也不给答案；再按考卷的评分标准判分。作答和判分都用 `deepseek-chat`。

```bash
git clone https://github.com/Ornn8/memory-system-benchmark hagemi/bench/memory-system-benchmark
BENCH_RECALL=v1 python hagemi/bench/run_bench.py   # 原始召回
BENCH_RECALL=v2 python hagemi/bench/run_bench.py   # 改进后的召回（约 45 分钟，消耗 API 额度）
```

**结果**（2026-11-04，`deepseek-chat` + `BAAI/bge-m3`，同一套配置）：

| 召回方式 | 总分 | 当天 | 3 天 | 15 天 | 45 天 | 召回为空 |
|---|---|---|---|---|---|---|
| v1 原始 | 48/108 = 44.4% | 3/27 | 2/27 | 22/27 | 21/27 | 1 |
| v2 改进 | **95/108 = 88.0%** | 25/27 | 25/27 | 22/27 | 23/27 | 0 |

- v1 的问题集中在当天和 3 天这两档：刚说过的事还没进中期记忆，原始召回又不带短期原话。
- v2 剩余的失分：`偏好` 0/4、`转述` 0/4、`分散的线索` 2/4、`没解释的矛盾` 2/4。`偏好` 这 4 题人工复核后认为是评委误判（回答忠实转述了用户原话），按此计算约 92%。但这是单个模型评委加人工判断的结果，没有交叉验证。
- 这个分数衡量的是「MemoryOS + 这套召回方式」整体，不能单独代表 MemoryOS。

## 文件一览

| 文件 | 状态 |
|---|---|
| `memoryos-mcp/server_new.py` | 修改 |
| `memoryos-mcp/memoryos/utils.py` | 修改 |
| `memoryos-mcp/config.json` | 修改（默认参数，不含密钥） |
| `run_memoryos_mcp.sh`、`.env.memoryos.example`、`memoryos-mcp/requirements-cpu.txt`、`.gitignore` | 新增 |
| `hagemi/client/memory-client.ts`、`hagemi/bench/run_bench.py`、本文件 | 新增 |
| 其它文件 | 与上游 `587ed77` 相同 |
