"""服务端配置：全部通过环境变量注入，均有默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# 本地登记的值班员凭证（默认值，开箱即用，仅供演示/本地部署）：
# 每位值班员一段，段间用 ";" 分隔；段内字段用 ":" 分隔，依次为
# 工号、姓名、本地凭证（token）、角色清单（角色间用 "," 分隔）。
# 角色：submit=可提交待审更正，review=可批准/拒绝待审更正。
# 生产部署应通过 SOP_OPERATORS 环境变量整体替换为本站登记内容。
DEFAULT_OPERATORS = (
    "duty-alice:Alice:alice-token:submit,review;"
    "duty-bob:Bob:bob-token:submit,review;"
    "duty-carol:Carol:carol-token:submit;"
    "reviewer-dave:Dave:dave-token:review"
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
    # 本地值班员凭证登记本（格式见 DEFAULT_OPERATORS 注释）
    operators_raw: str = _get("SOP_OPERATORS", DEFAULT_OPERATORS)


settings = Settings()
