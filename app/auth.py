"""值班员本地身份凭证：基于进程内配置的 Bearer token 鉴权。

凭证**只在本地配置**（环境变量 ``SOP_OPERATORS``，启动时读入进程内存）：
token 不落库、不写日志、不随响应回显。每个身份（``Operator``）携带一个或两个
角色：

* ``submit``：可提交待审更正提案；
* ``approve``：可批准/拒绝他人提交的待审提案（提交人与审核人不得相同）。

既有直接更正、回溯及审计接口不需要凭证（契约保持可用）；仅"待审更正"相关
写接口要求出示有效身份。
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

ROLE_SUBMIT = "submit"
ROLE_APPROVE = "approve"
_ALL_ROLES = (ROLE_SUBMIT, ROLE_APPROVE)


@dataclass(frozen=True)
class Operator:
    """一名值班员：名称、角色集合与其本地凭证 token。"""
    name: str
    roles: frozenset[str]
    token: str

    def can(self, role: str) -> bool:
        return role in self.roles


def parse_operators(raw: str) -> dict[str, Operator]:
    """解析 ``SOP_OPERATORS`` 配置为 ``token -> Operator`` 映射。

    格式（分号分隔多条）：``名称:角色:token[;名称:角色:token...]``；
    ``角色`` 为逗号分隔的 ``submit``/``approve``（``submit,approve`` 表示
    兼具两种角色）。配置非法（空字段、未知角色、重名、重 token）在启动期
    直接抛 ``ValueError``——fail-closed，绝不静默忽略某条凭证。
    """
    by_token: dict[str, Operator] = {}
    names: set[str] = set()
    for pos, chunk in enumerate(raw.split(";")):
        text = chunk.strip()
        if not text:
            raise ValueError(
                f"SOP_OPERATORS 第 {pos + 1} 条为空：每条必须是 "
                f"'名称:角色:token'，请删除多余的分号")
        parts = text.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"SOP_OPERATORS 第 {pos + 1} 条 {text!r} 格式非法：必须恰好 "
                f"包含两段冒号，形如 '名称:角色:token'")
        name, roles_raw, token = (p.strip() for p in parts)
        if not name:
            raise ValueError(
                f"SOP_OPERATORS 第 {pos + 1} 条缺少值班员名称")
        if not token:
            raise ValueError(
                f"SOP_OPERATORS 中值班员 {name!r} 的 token 不能为空")
        role_names = {r.strip() for r in roles_raw.split(",") if r.strip()}
        if not role_names or any(r not in _ALL_ROLES for r in role_names):
            raise ValueError(
                f"SOP_OPERATORS 中值班员 {name!r} 的角色 {roles_raw!r} 非法："
                f"只允许 {','.join(_ALL_ROLES)}（逗号分隔）")
        if name in names:
            raise ValueError(f"SOP_OPERATORS 中值班员名称 {name!r} 重复")
        if any(hmac.compare_digest(token, existing)
               for existing in by_token):
            raise ValueError(
                f"SOP_OPERATORS 中值班员 {name!r} 的 token 与其他值班员重复")
        names.add(name)
        by_token[token] = Operator(name=name, roles=frozenset(role_names),
                                   token=token)
    if not by_token:
        raise ValueError("SOP_OPERATORS 不能为空：待审更正接口至少需要一名值班员")
    return by_token


def authenticate(authorization: str | None,
                 operators: dict[str, Operator]) -> Operator | None:
    """从 ``Authorization: Bearer <token>`` 解析值班员身份。

    头缺失/方案不是 Bearer/token 未配置均返回 ``None``（调用方按 401
    invalid_credentials 拒绝）。比对使用恒定时间比较，避免计时侧信道。
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer" or not token:
        return None
    token = token.strip()
    for known, operator in operators.items():
        if hmac.compare_digest(token, known):
            return operator
    return None
