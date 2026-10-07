"""值班员本地凭证登记与鉴权。

凭证**在本地配置**（环境变量 ``SOP_OPERATORS``，见 :mod:`app.config`），
不经过任何外部服务：服务启动时把登记本解析为 token -> 值班员的内存表。
每位值班员登记为一段 ``工号:姓名:token:角色清单``（段间 ``;``，角色间
``,``），角色有：

* ``submit``：可提交待审更正提案；
* ``review``：可批准 / 拒绝待审更正提案。

提案 HTTP 接口用 ``Authorization: Bearer <token>``（或等价的
``X-Operator-Token: <token>`` 请求头）出示凭证；凭证缺失/无法识别 ->
401 invalid_credentials，角色不符 -> 403 forbidden。提交人与审核人
不得相同的业务规则在服务层按工号判定。

测试或配置热更新可调用 :func:`load_operators` 重新解析登记本。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import DEFAULT_OPERATORS, settings
from .errors import DomainError

if TYPE_CHECKING:  # 服务层只在类型注解处使用，不引入 fastapi 依赖
    from fastapi import Request


@dataclass(frozen=True)
class Operator:
    """一名通过本地凭证识别出的值班员。"""
    operator_id: str
    name: str
    token: str
    roles: frozenset[str]

    def can_submit(self) -> bool:
        return "submit" in self.roles

    def can_review(self) -> bool:
        return "review" in self.roles


_VALID_ROLES = frozenset({"submit", "review"})

# token -> 值班员；工号唯一性由解析时校验。
_by_token: dict[str, Operator] = {}
_by_id: dict[str, Operator] = {}


class OperatorConfigError(ValueError):
    """SOP_OPERATORS 登记本格式非法（启动即失败，避免"以为有凭证"）。"""


def parse_operators(raw: str) -> list[Operator]:
    """解析登记本字符串为值班员列表；格式非法或凭证重复时抛错。

    每个非空段必须是 ``工号:姓名:token:角色`` 四字段；角色至少含一个
    合法角色（submit/review）；工号与 token 必须各自全局唯一。
    """
    operators: list[Operator] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    for pos, segment in enumerate(raw.split(";")):
        text = segment.strip()
        if not text:
            continue
        fields = text.split(":")
        where = f"第 {pos + 1} 段 {text!r}"
        if len(fields) != 4:
            raise OperatorConfigError(
                f"值班员凭证配置非法：{where} 必须是 "
                f"'工号:姓名:token:角色清单' 四个 ':' 分隔字段")
        operator_id, name, token, roles_raw = (f.strip() for f in fields)
        if not operator_id or not name or not token:
            raise OperatorConfigError(
                f"值班员凭证配置非法：{where} 的工号、姓名、token 均不能为空")
        roles = frozenset(r.strip() for r in roles_raw.split(","))
        if "" in roles or not roles or not roles <= _VALID_ROLES:
            raise OperatorConfigError(
                f"值班员凭证配置非法：{where} 的角色清单只能包含 "
                f"submit/review 且至少一个，实际为 {sorted(roles)!r}")
        if operator_id in seen_ids:
            raise OperatorConfigError(
                f"值班员凭证配置非法：工号 {operator_id} 重复登记")
        if token in seen_tokens:
            raise OperatorConfigError(
                f"值班员凭证配置非法：token {token!r} 被重复登记（"
                f"工号 {operator_id}）")
        seen_ids.add(operator_id)
        seen_tokens.add(token)
        operators.append(Operator(operator_id, name, token, roles))
    if not operators:
        raise OperatorConfigError(
            "值班员凭证配置非法：SOP_OPERATORS 未登记任何值班员，"
            "待审更正接口至少需要一名带 submit/review 角色的值班员")
    return operators


def load_operators(raw: str | None = None) -> list[Operator]:
    """（重新）加载登记本到进程内内存表；返回加载后的值班员列表。"""
    global _by_token, _by_id
    parsed = parse_operators(raw if raw is not None else settings.operators_raw)
    _by_token = {o.token: o for o in parsed}
    _by_id = {o.operator_id: o for o in parsed}
    return list(parsed)


def by_token(token: str | None) -> Operator | None:
    return _by_token.get(token) if token else None


def by_id(operator_id: str) -> Operator | None:
    return _by_id.get(operator_id)


def extract_token(request: "Request") -> str | None:
    """从请求中取出凭证：Authorization: Bearer <token> 或 X-Operator-Token。

    两个头同时出现且不一致时视为无效（返回哨兵 ``__mismatch__``）。
    """
    auth = request.headers.get("authorization")
    header_token: str | None = None
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            header_token = parts[1].strip()
        else:
            return "__invalid__"
    direct = request.headers.get("x-operator-token")
    direct = direct.strip() if direct else None
    if header_token is not None and direct is not None:
        return header_token if header_token == direct else "__mismatch__"
    return header_token if header_token is not None else direct


def authenticate(request: "Request") -> Operator:
    """识别值班员：凭证缺失/无法识别 -> 401 invalid_credentials。"""
    token = extract_token(request)
    operator = by_token(None if token in (None, "__invalid__", "__mismatch__")
                        else token)
    if operator is None:
        raise DomainError(
            "invalid_credentials",
            "身份凭证缺失或无效：待审更正接口须通过 "
            "'Authorization: Bearer <token>'（或 X-Operator-Token 头）"
            "出示本地登记的值班员凭证",
            http_status=401)
    return operator


def require_operator(request: "Request", role: str) -> Operator:
    """识别值班员并要求其具备指定角色，否则 403 forbidden。"""
    operator = authenticate(request)
    if role not in operator.roles:
        raise DomainError(
            "forbidden",
            f"值班员 {operator.operator_id}（{operator.name}）缺少 {role} 权限，"
            f"不能执行该操作",
            http_status=403,
            details={"operator_id": operator.operator_id,
                     "roles": sorted(operator.roles), "required": role})
    return operator


# 模块导入即加载默认/环境配置；配置非法会在导入（服务启动）时立即暴露。
load_operators()
# 供测试与文档引用默认登记本（例如未设置 SOP_OPERATORS 时的演示 token）。
DEFAULT_OPERATOR_RAW = DEFAULT_OPERATORS
