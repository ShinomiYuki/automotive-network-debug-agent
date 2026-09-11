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


class TraceNotReadyError(TraceError):
    """Trace Session 已创建，但后台索引尚未完成。"""


class TraceDatabaseError(TraceError):
    """网络数据库加载或解码失败。"""


class ConfigError(AndaError):
    """工程配置查询相关异常。"""


class ConfigInputError(ConfigError):
    """Config Workspace 路径或查询参数无效。"""


class ConfigNotFoundError(ConfigError):
    """请求的 Workspace 或配置对象不存在。"""


class ConfigNotReadyError(ConfigError):
    """Config Workspace 已创建，但后台索引尚未完成。"""


class ConfigParseError(ConfigError):
    """ARXML 或源码输入无法可靠解析。"""


class ConfigConflictError(ConfigError):
    """查询命中多个配置对象，无法确定唯一调查入口。"""


class InvestigationError(AndaError):
    """结构化调查证据包路径、内容或写入操作无效。"""
