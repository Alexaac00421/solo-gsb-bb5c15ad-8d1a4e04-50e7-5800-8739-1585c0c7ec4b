"""服务端配置：全部通过环境变量注入，均有默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .auth import Operator, parse_operators


def _get(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# 开发态内置值班员凭证（仅用于本地/演示；生产必须用 SOP_OPERATORS 覆盖）。
# 三个提交人 + 一个审核人，token 仅在进程内存中比对、不落库、不写日志。
DEFAULT_OPERATORS = (
    "duty-alice:submit:dev-token-alice-2026;"
    "duty-bob:submit:dev-token-bob-2026;"
    "duty-carol:submit:dev-token-carol-2026;"
    "duty-supervisor:submit,approve:dev-token-supervisor-2026;"
    "duty-reviewer:approve:dev-token-reviewer-2026"
)


@dataclass(frozen=True)
class Settings:
    # SQLite 数据库文件路径；:/memory: 之外的路径会在启动时自动建表
    db_path: str = _get("SOP_DB_PATH", "/data/sop.db")
    # HTTP 监听地址 / 端口
    host: str = _get("SOP_HOST", "0.0.0.0")
    port: int = int(_get("SOP_PORT", "8080"))
    # SQLite 写锁等待（毫秒）
    busy_timeout_ms: int = int(_get("SOP_BUSY_TIMEOUT_MS", "5000"))
    # 值班员凭证（身份 -> 角色 -> 本地 token）；仅存进程内存、不落库
    operators: dict[str, Operator] = field(
        default_factory=lambda: parse_operators(
            _get("SOP_OPERATORS", DEFAULT_OPERATORS)))


settings = Settings()
