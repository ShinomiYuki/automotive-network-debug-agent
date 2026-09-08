"""
文件用途：
- 提供信号时间序列的基础处理能力。
- 与 DBC 解码模块配合，向上提供 signal samples 与简单统计。

当前状态：
- TODO 骨架。
- 后续计划加入 min/max/change points/range check/state transitions。
"""

def summarize_signal(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "max": None}

    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
    }
