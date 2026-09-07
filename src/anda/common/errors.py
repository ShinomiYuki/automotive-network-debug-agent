"""
文件用途：
- 统一定义项目异常类型。
- 避免各 MCP Server 向模型暴露杂乱底层异常。

当前状态：
- 初始骨架。
"""


class AndaError(RuntimeError):
    """项目统一基础异常。"""


class TraceError(AndaError):
    """Trace 分析相关异常。"""


class TraceInputError(TraceError):
    """Trace 输入文件或查询参数无效。"""


class TraceNotFoundError(TraceError):
    """请求的 Trace Session 不存在。"""


class TraceDatabaseError(TraceError):
    """网络数据库加载或解码失败。"""
