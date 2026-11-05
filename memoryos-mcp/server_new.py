# Modified by go0le (2026-11) for the 哈Gemi QQ bot: see HAGEMI_CHANGES.md.
# Original: BAI-LAB/MemoryOS (https://github.com/BAI-LAB/MemoryOS), Apache License 2.0.

import sys
import os
import re
import json
import argparse
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, List


class _StdoutGuard:
    """
    MCP stdio 协议要求 stdout 只能输出协议消息，但 MemoryOS 库里到处是裸 print()（加载模型、写记忆、检索…）。
    把 sys.stdout 的写入全部转到 stderr；MCP 传输层取的是 sys.stdout.buffer，仍指向真实 stdout，不受影响。
    """
    def __init__(self, real):
        self._real = real
        self.buffer = real.buffer
        self.encoding = getattr(real, "encoding", "utf-8")
        self.errors = getattr(real, "errors", "strict")

    def write(self, s):
        return sys.stderr.write(s)

    def flush(self):
        sys.stderr.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self._real.fileno()


sys.stdout = _StdoutGuard(sys.stdout)
# 确保当前目录在sys.path中，以便导入memoryos模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'memoryos'))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    print(f"ERROR: Failed to import FastMCP. Exception: {e}", file=sys.stderr)
    print("请安装最新版本的MCP: pip install --upgrade mcp", file=sys.stderr)
    sys.exit(1)

try:
    from memoryos import Memoryos
    from utils import get_timestamp
except ImportError as e:
    print(f"无法导入MemoryOS模块: {e}", file=sys.stderr)
    print("请确保项目结构正确，memoryos目录应包含所有必要文件", file=sys.stderr)
    sys.exit(1)

# 配置（启动时读入）与按用户隔离的 MemoryOS 实例表。
# 每个 user_id 有独立的短/中/长期记忆、用户画像，助手侧知识也按用户分开，互相读不到。
_config: Dict[str, Any] = {}
_instances: "OrderedDict[str, Memoryos]" = OrderedDict()
_instances_lock = threading.Lock()
MAX_LOADED_USERS = int(os.environ.get("MEMORYOS_MAX_USERS", "16"))
_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,63}$")


def load_config(config_path: str) -> Dict[str, Any]:
    """读取并校验配置（不创建任何用户实例）"""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    required_fields = ['openai_api_key', 'data_storage_path']
    for field in required_fields:
        if field not in config:
            raise ValueError(f"配置文件缺少必需字段: {field}")
    return config


def _safe_dir_id(user_id: str) -> str:
    """user_id 会被拼进磁盘路径，只允许字母数字和 : _ -，':' 落盘时换成 '_'"""
    if not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
        raise ValueError("user_id 非法：必须是 1-64 位字母/数字/冒号/下划线/连字符，且以字母或数字开头")
    return user_id.replace(":", "_")


def get_memoryos(user_id: str) -> Memoryos:
    """取（必要时创建）某个用户专属的 MemoryOS 实例；超过上限时淘汰最久未用的（数据已落盘，下次再加载）"""
    dir_id = _safe_dir_id(user_id)
    with _instances_lock:
        inst = _instances.get(dir_id)
        if inst is not None:
            _instances.move_to_end(dir_id)
            return inst
        config = _config
        inst = Memoryos(
            user_id=dir_id,
            openai_api_key=config['openai_api_key'],
            data_storage_path=config['data_storage_path'],
            openai_base_url=config.get('openai_base_url'),
            assistant_id=f"{config.get('assistant_id', 'default_assistant_profile')}_{dir_id}",
            short_term_capacity=config.get('short_term_capacity', 10),
            mid_term_capacity=config.get('mid_term_capacity', 2000),
            long_term_knowledge_capacity=config.get('long_term_knowledge_capacity', 100),
            retrieval_queue_capacity=config.get('retrieval_queue_capacity', 7),
            mid_term_heat_threshold=config.get('mid_term_heat_threshold', 5.0),
            llm_model=config.get('llm_model', 'gpt-4o-mini'),
            embedding_model_name=config.get('embedding_model_name', 'all-MiniLM-L6-v2')
        )
        _instances[dir_id] = inst
        while len(_instances) > MAX_LOADED_USERS:
            _instances.popitem(last=False)
        return inst


def _warm_embedding_model() -> None:
    """后台预加载嵌入模型，避免第一次调用工具时等十几秒"""
    try:
        from utils import get_embedding
        name = _config.get('embedding_model_name', 'all-MiniLM-L6-v2')
        kwargs = {'use_fp16': True} if 'bge-m3' in name.lower() else {}
        get_embedding("warmup", model_name=name, use_cache=False, **kwargs)
    except Exception as e:
        print(f"嵌入模型预加载失败（首次调用时会再试）: {e}", file=sys.stderr)

# 创建FastMCP服务器实例
mcp = FastMCP("MemoryOS")

@mcp.tool()
def add_memory(user_id: str, user_input: str, agent_response: str, timestamp: Optional[str] = None, meta_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    向MemoryOS系统添加新的记忆（用户输入和助手回应的对话对）。记忆按 user_id 严格隔离。
    
    Args:
        user_id: 记忆所属用户的标识。必须原样使用消息开头 [用户标识：…] 里的值，不得自行编造或改写
        user_input: 用户的输入或问题
        agent_response: 助手的回应
        timestamp: 时间戳（可选，格式：YYYY-MM-DD HH:MM:SS）
        meta_data: 可选的元数据（JSON对象）
    
    Returns:
        包含操作结果的字典
    """
    try:
        memoryos_instance = get_memoryos(user_id)
        if not user_input or not agent_response:
            return {
                "status": "error",
                "message": "user_input and agent_response are required"
            }
        
        memoryos_instance.add_memory(
            user_input=user_input,
            agent_response=agent_response,
            timestamp=timestamp or get_timestamp(),
            meta_data=meta_data or {}
        )
        
        result = {
            "status": "success",
            "message": "Memory has been successfully added to MemoryOS",
            "timestamp": timestamp or get_timestamp(),
            "details": {
                "user_input_length": len(user_input),
                "agent_response_length": len(agent_response),
                "has_meta_data": meta_data is not None
            }
        }
        
        return result
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error adding memory: {str(e)}"
        }

@mcp.tool()
def retrieve_memory(user_id: str, query: str, relationship_with_user: str = "friend", style_hint: str = "", max_results: int = 10) -> Dict[str, Any]:
    """
    根据查询从MemoryOS检索相关的记忆和上下文信息，包括短期记忆、中期记忆和长期知识。只能检索 user_id 自己的记忆。
    
    Args:
        user_id: 记忆所属用户的标识。必须原样使用消息开头 [用户标识：…] 里的值，不得自行编造或改写
        query: 检索查询，描述要寻找的信息
        relationship_with_user: 与用户的关系类型（如：friend, assistant, colleague等）
        style_hint: 回应风格提示
        max_results: 返回的最大结果数量
    
    Returns:
        包含检索结果的字典，包括：
        - short_term_memory: 当前短期记忆中的所有QA对
        - retrieved_pages: 从中期记忆检索的相关页面
        - retrieved_user_knowledge: 从用户长期知识库检索的相关条目
        - retrieved_assistant_knowledge: 从助手知识库检索的相关条目
    """
    try:
        memoryos_instance = get_memoryos(user_id)
        if not query:
            return {
                "status": "error",
                "message": "query parameter is required"
            }
        
        # 使用retriever获取相关上下文
        retrieval_results = memoryos_instance.retriever.retrieve_context(
            user_query=query,
            user_id=memoryos_instance.user_id
        )
        
        # 获取短期记忆内容
        short_term_history = memoryos_instance.short_term_memory.get_all()
        
        # 获取用户画像
        user_profile = memoryos_instance.get_user_profile_summary()
        
        # 组织返回结果
        result = {
            "status": "success",
            "query": query,
            "timestamp": get_timestamp(),
            "user_profile": user_profile if user_profile and user_profile.lower() != "none" else "No detailed user profile",
            "short_term_memory": short_term_history,
            "short_term_count": len(short_term_history),
            "retrieved_pages": [{
                'user_input': page['user_input'],
                'agent_response': page['agent_response'],
                'timestamp': page['timestamp'],
                'meta_info': page['meta_info']
            } for page in retrieval_results["retrieved_pages"][:max_results]],

            "retrieved_user_knowledge": [{
                    'knowledge': k['knowledge'],
                    'timestamp': k['timestamp']
                } for k in retrieval_results["retrieved_user_knowledge"][:max_results]],

            "retrieved_assistant_knowledge": [{
                'knowledge': k['knowledge'],
                'timestamp': k['timestamp']
            } for k in retrieval_results["retrieved_assistant_knowledge"][:max_results]],
            
            # 添加总数统计字段
            "total_pages_found": len(retrieval_results["retrieved_pages"]),
            "total_user_knowledge_found": len(retrieval_results["retrieved_user_knowledge"]),
            "total_assistant_knowledge_found": len(retrieval_results["retrieved_assistant_knowledge"])
        }
        
        return result
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error retrieving memory: {str(e)}"
        }

@mcp.tool()
def get_user_profile(user_id: str, include_knowledge: bool = True, include_assistant_knowledge: bool = False) -> Dict[str, Any]:
    """
    获取用户的画像信息，包括个性特征、偏好和相关知识。只能读取 user_id 自己的画像。
    
    Args:
        user_id: 记忆所属用户的标识。必须原样使用消息开头 [用户标识：…] 里的值，不得自行编造或改写
        include_knowledge: 是否包括用户相关的知识条目
        include_assistant_knowledge: 是否包括助手知识库
    
    Returns:
        包含用户画像信息的字典
    """
    try:
        memoryos_instance = get_memoryos(user_id)
        # 获取用户画像
        user_profile = memoryos_instance.get_user_profile_summary()
        
        result = {
            "status": "success",
            "timestamp": get_timestamp(),
            "user_id": memoryos_instance.user_id,
            "assistant_id": memoryos_instance.assistant_id,
            "user_profile": user_profile if user_profile and user_profile.lower() != "none" else "No detailed user profile"
        }
        
        if include_knowledge:
            user_knowledge = memoryos_instance.user_long_term_memory.get_user_knowledge()
            result["user_knowledge"] = [
                {
                    "knowledge": item["knowledge"],
                    "timestamp": item["timestamp"]
                }
                for item in user_knowledge
            ]
            result["user_knowledge_count"] = len(user_knowledge)
        
        if include_assistant_knowledge:
            assistant_knowledge = memoryos_instance.get_assistant_knowledge_summary()
            result["assistant_knowledge"] = [
                {
                    "knowledge": item["knowledge"],
                    "timestamp": item["timestamp"]
                }
                for item in assistant_knowledge
            ]
            result["assistant_knowledge_count"] = len(assistant_knowledge)
        
        return result
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error getting user profile: {str(e)}"
        }

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="MemoryOS MCP Server")
    parser.add_argument(
        "--config", 
        type=str, 
        default="config.json",
        help="配置文件路径 (默认: config.json)"
    )
    
    args = parser.parse_args()
    
    global _config

    try:
        # 只读配置；各用户的 MemoryOS 实例在第一次使用该 user_id 时按需创建
        _config = load_config(args.config)
        print("MemoryOS MCP Server 已启动（按 user_id 隔离记忆）", file=sys.stderr)
        print(f"配置文件: {args.config}", file=sys.stderr)
        threading.Thread(target=_warm_embedding_model, daemon=True).start()
        
        # 启动MCP服务器 - 使用stdio传输
        mcp.run(transport="stdio")
        
    except KeyboardInterrupt:
        print("服务器被用户中断", file=sys.stderr)
    except Exception as e:
        print(f"启动服务器时发生错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()