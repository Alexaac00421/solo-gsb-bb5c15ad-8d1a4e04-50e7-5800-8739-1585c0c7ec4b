"""领域服务：首次发布、更正提交、撤回、双时间维回溯查询。

双时间维语义
------------
一条 version 行同时携带两个互不相同的时间维：

* 适用时间（valid time）：``[valid_start, valid_end)``，正文在作业现场的生效区间；
* 发布时间（transaction time）：``published_at``，服务端落库时刻，不可回填。

更正只新增行（子版本），不修改任何旧行，因此"更正发布前的查询结果"天然保持原样：
查询时以知悉时刻 ``known_as_of`` 过滤 ``published_at <= known_as_of`` 即可。

撤回
----
撤回也是**只追加事件**（``withdrawals`` 表），从不删除或改写 versions 行：
撤回时刻 ``withdrawn_at`` 由服务端记录、不接受回填。某版本在知悉时刻 k 下
可见（当时仍生效）当且仅当 ``published_at <= k`` 且
（未撤回 或 ``withdrawn_at >= k``）——两侧边界均含等号，与发布时刻
``published_at <= k`` 的约定对称。因此：

* 撤回前知悉时刻的回溯与两知悉时刻对比仍按原版本链还原（历史不变）；
* 撤回后知悉时刻下该更正不再可见，其适用子区间恢复显示父版本正文；
* 仅"当前仍生效、尚无子更正"的叶子更正可撤回：首版、已撤回版本、
  已有子更正的版本一律拒绝（撤回不删行，子更正即使已撤回也仍在历史中）；
  撤回叶子更正后，其父版本可在恢复的区间内再次更正。

版本树
------
- 首版：``revision=0``、``supersedes_id IS NULL``；
- 更正：指向被替代版本 ``supersedes_id``，编号为该规程下递增的 revision；
- 父版本的"仍生效区间" = 自身区间 − 其全部**当前仍生效**（未撤回）子更正区间；
- 查询时在所有可见候选中沿 supersedes 链取"最深"版本，即为作业时刻有效正文。

作业规程包
----------
班组开工前按（作业时刻、知悉时刻、一组规程编号）固定一个不可变快照：

- 固定：在单条写事务的一致快照上为每个编号解析当时适用版本，把正文连同
  版本号/区间/父版本/发布时刻整包复制进 ``packages``/``package_items``；
  编号重复、编号缺失、任一规程无适用正文、知悉时刻晚于当前时刻均整体拒绝、
  不留包。
- 取回：只返回固定时的快照行，之后的更正/撤回永不改变它。
- 复核：在显式只读快照事务内以复核知悉时刻（不早于包内知悉时刻、不晚于
  当前时刻）按原作业时刻逐项重新解析，给出包内版本 pinned、彼时有效版本
  current 与 changed，并汇总整体 all_match；是否变化只看版本号。

前置规程
--------
每个版本可携带一份有序的前置规程编号列表（``prerequisites`` 表，随版本行
同事务插入、只追加不改写）：

- 首次发布省略该字段视为空列表；更正省略则继承被替代版本的列表（基版行
  不可变，写入时复制即等价于按引用继承），显式空列表表示清空；
- 写入时只校验"无空白、无重复"，不校验编号存在性——前置可晚于引用者发布；
- 引用是否生效由所属版本的双时间维可见性决定，因此随适用区间、发布时间
  与撤回自然变化，历史查询看到当时的列表，无需改写任何历史；
- 递归解析（resolve_closure）在同一只读快照内按现有回溯语义展开根规程
  的传递闭包，前置优先、去重返回；缺口（根/引用不存在、当时无适用正文）
  整体失败并说明引用链，引用成环返回环路径、不返回部分清单。
- 跨时段解析（resolve_closure_plan）把单时刻闭包推广到非空作业区间：
  在同一只读快照内收集根及递归可引用规程的可见版本端点切分区间，逐片段
  复用闭包解析；片段解析器对缺口/环不抛异常而保留片段（部分前置清单 +
  缺口链/环路径），相邻片段仅在清单与错误状态完全相同时合并。

更正批量预演
------------
值班员在正式提交多条更正前，可先按"现有单条/批量更正的同一套字段与校验"
预演整批结果（preview_corrections）：

- 必须指定一个非空作业区间 ``[work_start, work_end)``；预演在**同一只读
  快照**上把当时数据完整复制进一个内存库，随后在该副本上运行与正式提交
  **逐行相同**的应用逻辑（``_apply_corrections``）——同批条目按输入顺序
  生效，后项可引用前项在副本内拟生成的版本；
- 逐项沿用当前基版剩余区间、同批重叠、正文非空及前置编号的校验与"省略
  继承 / 显式清空"规则；任一项非法即抛出与正式提交**完全相同**的领域错误
  （含 ``details.index`` 指明位置），不返回任何部分成功结果；
- 全部合法时返回各项拟生成版本号与最终前置清单，以及指定作业区间内
  "提交前（当前快照）"与"提交后（应用本批之后）"实际适用版本不同的
  **最大片段**（片段两侧各为 ``{revision, body}`` 或 ``null``，仅版本号
  不同即为变化，边界切分与合并沿用两知悉时刻对比的约定）；
- 预演**全程只读**：不写版本、不占版本号、不推进服务端时钟、不改变历史
  （连正式提交与预演共用的应用逻辑也跑在副本上）；正式提交仍须按提交时
  的当时状态重新校验，预演结果不保证可提交。

跨规程版本覆盖检查
------------------
调度员安排跨规程作业前，用 check_coverage 在同一只读快照内按全体编号的
版本端点切分作业区间，逐段返回每个编号的有效版本号、正文或缺口，并标出
因撤回导致的恢复、区间外空档与版本切换；相邻段仅在整组结果完全相同时
合并。编号不存在、时间非法或知悉时刻晚于当前时刻均拒绝；全程只读。
"""
from __future__ import annotations

import sqlite3
import uuid

from . import errors
from .db import read_conn, read_snapshot, write_tx
from .errors import DomainError
from .time_utils import TimeParseError, now_canonical, parse_canonical
# ---------------------------------------------------------------- 服务端时钟

# 写操作由全局锁串行化；时钟同样保证单调递增，避免连续两次发布落在同一微秒
# 而破坏 published_at 的先后可分辨性（客户端禁止回填，只允许使用该时钟）。
_last_published_at = ""


def _server_now() -> str:
    global _last_published_at
    ts = now_canonical()
    if ts <= _last_published_at:
        # 极端情况下同一微秒内连续发布：向前推进 1 微秒
        from datetime import datetime, timedelta
        dt = datetime.strptime(_last_published_at,
                               "%Y-%m-%dT%H:%M:%S.%fZ") + timedelta(microseconds=1)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _last_published_at = ts
    return ts

# ---------------------------------------------------------------- 区间工具


def _subtract_intervals(base: tuple[str, str],
                        holes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``[base)`` 减去 holes 的并集，返回若干不相交区间（按起点排序）。"""
    pieces = [base]
    for hs, he in sorted(holes):
        next_pieces: list[tuple[str, str]] = []
        for ps, pe in pieces:
            if he <= ps or hs >= pe:
                next_pieces.append((ps, pe))          # 不相交
            else:
                if hs > ps:
                    next_pieces.append((ps, hs))      # 左侧残余
                if he < pe:
                    next_pieces.append((he, pe))      # 右侧残余
        pieces = next_pieces
    return pieces


def _covered_by(interval: tuple[str, str],
                pieces: list[tuple[str, str]]) -> bool:
    s, e = interval
    for ps, pe in pieces:
        if ps <= s and e <= pe:
            return True
    return False


def _first_overlap(interval: tuple[str, str],
                   others: list[tuple[str, str]]) -> tuple[str, str] | None:
    s, e = interval
    for os_, oe in others:
        if s < oe and os_ < e:
            return os_, oe
    return None


def _validated_interval(start_raw: str, end_raw: str,
                        index: int | None) -> tuple[str, str]:
    """规范化区间并校验 [start, end) 非空；时间不合法抛 invalid_interval。"""
    try:
        start = parse_canonical(start_raw, "valid_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "valid_start", **({"index": index} if index is not None else {})})
    try:
        end = parse_canonical(end_raw, "valid_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "valid_end", **({"index": index} if index is not None else {})})
    if not start < end:
        raise errors.invalid_interval(
            start, end, "适用区间非法：起点必须严格早于终点（区间为 [起, 止) 且长度大于 0）",
            index)
    return start, end


def _clean_text(value: str, field: str, index: int | None) -> str:
    text = value.strip()
    if not text:
        details = {"field": field}
        if index is not None:
            details["index"] = index
        raise DomainError("invalid_body", f"{field} 不能为空字符串", 422, details)
    return text


def _clean_number(value: str, index: int | None) -> str:
    number = value.strip()
    if not number:
        details: dict = {"field": "number"}
        if index is not None:
            details["index"] = index
        raise DomainError("invalid_number", "规程编号不能为空字符串", 422, details)
    return number


def _clean_prerequisites(value, index: int | None) -> list[str] | None:
    """规范化前置规程编号列表；``None`` 表示请求未携带该字段。

    返回 ``None``（未携带）或去重前的有序编号列表。只校验形状：空白编号
    -> 422 invalid_number；重复编号 -> 422 duplicate_prerequisite_number。
    不校验编号是否存在——前置规程允许晚于引用者发布，存在性在递归解析
    （回溯查询）时按当时数据判定。
    """
    if value is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for pos, raw in enumerate(value):
        number = raw.strip() if isinstance(raw, str) else ""
        if not number:
            details: dict = {"field": "prerequisites", "position": pos}
            if index is not None:
                details["index"] = index
            raise DomainError("invalid_number", "前置规程编号不能为空字符串",
                              422, details)
        if number in seen:
            raise errors.duplicate_prerequisite_number(number, index, pos)
        seen.add(number)
        cleaned.append(number)
    return cleaned

# ---------------------------------------------------------------- 行读取


def _fetch_procedure(conn: sqlite3.Connection,
                     number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM procedures WHERE number = ?", (number,)).fetchone()


def _fetch_procedure_by_id(conn: sqlite3.Connection,
                           proc_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM procedures WHERE id = ?", (proc_id,)).fetchone()


def _fetch_version(conn: sqlite3.Connection, proc_id: int,
                   revision: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE procedure_id = ? AND revision = ?",
        (proc_id, revision)).fetchone()


def _children(conn: sqlite3.Connection,
              version_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE supersedes_id = ? ORDER BY valid_start",
        (version_id,)).fetchall()


def _withdrawn_map(conn: sqlite3.Connection,
                   version_ids: list[int] | None = None) -> dict[int, str]:
    """version_id -> withdrawn_at（仅含已撤回版本）。

    不传 version_ids 时取全表（单规程场景版本量很小）。
    """
    if version_ids is not None:
        if not version_ids:
            return {}
        placeholders = ",".join("?" * len(version_ids))
        rows = conn.execute(
            f"SELECT version_id, withdrawn_at FROM withdrawals "
            f"WHERE version_id IN ({placeholders})",
            tuple(version_ids)).fetchall()
    else:
        rows = conn.execute(
            "SELECT version_id, withdrawn_at FROM withdrawals").fetchall()
    return {r["version_id"]: r["withdrawn_at"] for r in rows}


def _withdrawn_at(conn: sqlite3.Connection, version_id: int) -> str | None:
    row = conn.execute(
        "SELECT withdrawn_at FROM withdrawals WHERE version_id = ?",
        (version_id,)).fetchone()
    return None if row is None else row["withdrawn_at"]


# ---------------------------------------------------------------- 前置规程存储


def _insert_prerequisites(conn: sqlite3.Connection, version_id: int,
                          numbers: list[str]) -> None:
    """把一份有序前置编号列表挂到版本行上（与版本同事务插入，只追加）。"""
    for pos, number in enumerate(numbers):
        conn.execute(
            "INSERT INTO prerequisites (version_id, position, number) "
            "VALUES (?, ?, ?)", (version_id, pos, number))


def _insert_audit_batch(conn: sqlite3.Connection,
                        entries: list[dict]) -> int:
    """把一次已提交写事务的全部变动作为**一个批次**写入审计日志。

    必须与业务行在同一条写事务内调用：事务回滚时审计批次一并消失，只读
    快照因此永远看不到"业务行已落库但审计缺失"或反之的中间状态。批次内
    事件按 ``entries`` 顺序（即请求顺序）排列。每个 entry 含：

    ``operation``（release/correction/withdrawal）、``number``、``revision``、
    ``event_time``（该版本的服务端发布时刻，撤回事件为撤回时刻）、
    ``version_id``（对应 versions 行）。
    """
    cur = conn.execute(
        "INSERT INTO audit_batches (event_count) VALUES (?)",
        (len(entries),))
    batch_seq = cur.lastrowid
    for position, e in enumerate(entries):
        conn.execute(
            "INSERT INTO audit_events (batch_seq, position, operation, "
            "procedure_number, revision, event_time, version_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch_seq, position, e["operation"], e["number"],
             e["revision"], e["event_time"], e["version_id"]))
    return batch_seq


def _prerequisites_of(conn: sqlite3.Connection, version_id: int) -> list[str]:
    """某版本携带的前置规程编号列表（按写入顺序）；无记录即空列表。"""
    rows = conn.execute(
        "SELECT number FROM prerequisites WHERE version_id = ? "
        "ORDER BY position", (version_id,)).fetchall()
    return [r["number"] for r in rows]


def _prerequisites_map(conn: sqlite3.Connection,
                       version_ids: list[int]) -> dict[int, list[str]]:
    """批量取多个版本的前置列表（审计视图用）；无记录的版本不在返回中。"""
    if not version_ids:
        return {}
    placeholders = ",".join("?" * len(version_ids))
    rows = conn.execute(
        f"SELECT version_id, number FROM prerequisites "
        f"WHERE version_id IN ({placeholders}) "
        f"ORDER BY version_id, position", tuple(version_ids)).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["version_id"], []).append(r["number"])
    return out


def _remaining(conn: sqlite3.Connection,
               base: sqlite3.Row) -> list[tuple[str, str]]:
    """基版在其当前生效区间内尚未被子更正覆盖的子区间集合。

    已撤回的子更正不再占用父版本区间（撤回后该区间恢复父版本正文，可再次更正），
    故这里只扣除**仍生效**（未撤回）的子更正。
    """
    withdrawn = _withdrawn_map(conn)
    holes = [(c["valid_start"], c["valid_end"])
             for c in _children(conn, base["id"])
             if c["id"] not in withdrawn]
    return _subtract_intervals((base["valid_start"], base["valid_end"]), holes)

# ---------------------------------------------------------------- 首次发布


def publish_releases(items: list) -> dict:
    """首次发布一个或多个规程；整批原子提交。

    每条可携带 ``prerequisites`` 前置规程编号列表；省略视为空列表。
    编号存在性不在写入时校验（前置可晚于引用者发布）。
    """
    normalized: list[tuple[str, str, str, str, list[str]]] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        number = _clean_number(item.number, i)
        body = _clean_text(item.body, "body", i)
        start, end = _validated_interval(item.valid_start, item.valid_end, i)
        # 旧请求/旧调用方没有该字段：按"省略"处理，视为空列表
        prerequisites = _clean_prerequisites(
            getattr(item, "prerequisites", None), i) or []
        if number in seen:
            raise errors.duplicate_number(number, i)
        seen.add(number)
        normalized.append((number, body, start, end, prerequisites))

    with write_tx() as conn:
        published_at = _server_now()
        created: list[dict] = []
        for i, (number, _body, _s, _e, _p) in enumerate(normalized):
            if _fetch_procedure(conn, number) is not None:
                raise errors.duplicate_number(number, i)
        for number, body, start, end, prerequisites in normalized:
            cur = conn.execute(
                "INSERT INTO procedures (number, created_at) VALUES (?, ?)",
                (number, published_at))
            proc_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO versions (procedure_id, revision, body, "
                "valid_start, valid_end, supersedes_id, published_at) "
                "VALUES (?, 0, ?, ?, ?, NULL, ?)",
                (proc_id, body, start, end, published_at))
            _insert_prerequisites(conn, cur.lastrowid, prerequisites)
            created.append({"number": number, "revision": 0, "body": body,
                            "valid_start": start, "valid_end": end,
                            "published_at": published_at,
                            "prerequisites": prerequisites,
                            "_version_id": cur.lastrowid})
        # 整次批量首次发布作为一个审计批次（同事务，事件按请求顺序）。
        _insert_audit_batch(conn, [
            {"operation": "release", "number": e["number"],
             "revision": e["revision"], "event_time": published_at,
             "version_id": e["_version_id"]}
            for e in created])
    for e in created:
        e.pop("_version_id", None)
    return {"published_at": published_at, "items": created}

# ---------------------------------------------------------------- 更正提交


def submit_corrections(items: list) -> dict:
    """提交一条或多条更正；任一非法则整批回滚。

    同批次内条目按顺序处理，因此第 k 条可以以同批次更早插入的新版本为基版
    （此时 revision 号为可预知的该规程当前最大号 +1）。
    """
    with write_tx() as conn:
        published_at = _server_now()
        applied = _apply_corrections(conn, items, published_at)
        # 整批更正作为一个审计批次：同批条目按请求顺序、后项可引用前项，
        # 审计事件顺序与批次内生成顺序一致（与业务行同事务）。
        _insert_audit_batch(conn, [
            {"operation": "correction", "number": e["number"],
             "revision": e["revision"], "event_time": published_at,
             "version_id": e["_version_id"]}
            for e in applied])
    # 正式提交的响应形状保持不变：每项回填服务端发布时刻、不带预演用的 index
    # 与内部行 id。
    created = [{k: v for k, v in entry.items()
                if k not in ("index", "_version_id")}
               | {"published_at": published_at}
              for entry in applied]
    return {"published_at": published_at, "items": created}


def _apply_corrections(conn: sqlite3.Connection, items: list,
                       published_at: str) -> list[dict]:
    """在给定连接/事务上按输入顺序逐条应用更正，返回拟/已生成版本清单。

    正式提交与批量预演共用**完全相同**的本函数，保证预演沿用与单条/批量
    更正一致的全部校验（剩余区间、同批重叠、正文、前置编号及省略继承/显式
    清空）；预演调用方传入的是只读快照的内存副本，故即使本函数含 INSERT
    也不会写真实历史、不占真实版本号。调用方负责外层事务（异常即整体回滚）。
    """
    # (proc_id, base_revision) -> 本批次内已占用的子区间
    pending: dict[tuple[int, int], list[tuple[str, str]]] = {}
    created: list[dict] = []

    for i, item in enumerate(items):
        number = _clean_number(item.number, i)
        body = _clean_text(item.body, "body", i)
        sub = _validated_interval(item.valid_start, item.valid_end, i)
        # 省略 -> 继承被替代版本的前置列表；显式空列表 -> 清空
        prerequisites = _clean_prerequisites(
            getattr(item, "prerequisites", None), i)

        proc = _fetch_procedure(conn, number)
        if proc is None:
            raise errors.procedure_not_found(number, i)
        proc_id = proc["id"]
        base = _fetch_version(conn, proc_id, item.base_revision)
        if base is None:
            raise errors.base_version_not_found(number, item.base_revision, i)

        # 1) 子区间必须整体落在基版区间内（非法区间优先于冲突判定）。
        base_iv = (base["valid_start"], base["valid_end"])
        if not (base_iv[0] <= sub[0] and sub[1] <= base_iv[1]):
            raise errors.invalid_interval(
                sub[0], sub[1],
                f"非法区间：更正子区间 [{sub[0]}, {sub[1]}) 超出被替代版本 "
                f"r{item.base_revision} 的适用区间 [{base_iv[0]}, {base_iv[1]})",
                i)

        # 1.5) 已撤回版本不再是任何更正的基版：其区间已恢复由父版本持有，
        #      若要改正文请改以父版本（当前生效版本）为被替代版本。
        base_withdrawn_at = _withdrawn_at(conn, base["id"])
        if base_withdrawn_at is not None:
            raise errors.base_version_withdrawn(
                number, item.base_revision, base_withdrawn_at, i)

        # 2) 同一被替代版本的并发更正不得在生效区间上相互冲突。
        #    先于"剩余区间"检查：同批次更早条目虽已插入当前事务、
        #    尚非已发布历史，对它们的重叠属于并发更正，而非过期基版。
        key = (proc_id, item.base_revision)
        clash = _first_overlap(sub, pending.get(key, []))
        if clash is not None:
            raise errors.concurrent_conflict(number, item.base_revision,
                                             clash, i)

        # 3) 扣除已发布子更正后的剩余生效区间；子区间不落在其中 -> 过期基版。
        #    （步骤 1 已保证子区间在基版区间内，故未被剩余区间覆盖的部分
        #    必然与某个已发布子更正重叠，即客户依据的是已被替代的旧版。）
        remaining = _remaining(conn, base)
        if not _covered_by(sub, remaining):
            raise errors.expired_base(number, item.base_revision, remaining, i)

        row = conn.execute(
            "SELECT COALESCE(MAX(revision), -1) + 1 AS next_revision "
            "FROM versions WHERE procedure_id = ?", (proc_id,)).fetchone()
        new_revision = row["next_revision"]

        # 省略时继承被替代版本的前置列表：基版行不可变，写入时复制与
        # 按引用继承完全等价，且保持"只追加、不改写历史"。
        if prerequisites is None:
            prerequisites = _prerequisites_of(conn, base["id"])

        cur = conn.execute(
            "INSERT INTO versions (procedure_id, revision, body, "
            "valid_start, valid_end, supersedes_id, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (proc_id, new_revision, body, sub[0], sub[1],
             base["id"], published_at))
        _insert_prerequisites(conn, cur.lastrowid, prerequisites)
        pending.setdefault(key, []).append(sub)
        created.append({"index": i, "number": number, "revision": new_revision,
                        "supersedes_revision": item.base_revision,
                        "body": body, "valid_start": sub[0],
                        "valid_end": sub[1], "prerequisites": prerequisites,
                        "_version_id": cur.lastrowid})
    return created

# ---------------------------------------------------------------- 更正批量预演


def preview_corrections(items: list, work_start_raw: str,
                        work_end_raw: str) -> dict:
    """正式提交前预演一批更正：只读，不写版本、不占版本号、不改变历史。

    入参与 ``POST /api/v1/corrections`` 完全一致（同样支持单条对象/批量），
    另须指定一个**非空作业区间** ``[work_start, work_end)``。预演在同一只读
    快照的内存副本上运行与正式提交逐行相同的 :func:`_apply_corrections`：
    同批条目按输入顺序应用，后项可引用前项拟生成的版本；逐项沿用当前基版
    剩余区间、同批重叠、正文及前置编号（省略继承 / 显式清空）的校验。

    全部合法时返回：

    * ``items``：各项（按输入顺序，带 ``index``）拟生成的版本号 ``revision``
      与最终前置清单 ``prerequisites``（及编号、基版、子区间、正文）；
    * ``changes``：本批涉及的每个规程（按编号在输入中首次出现的顺序）在指定
      作业区间内，提交前（当前快照）与提交后（应用本批之后）实际适用版本
      不同的最大片段；片段两侧各为 ``{"revision", "body"}`` 或 ``null``，
      是否变化只看版本号，切分/合并沿用两知悉时刻对比（第 6 节）的约定。

    任一项非法则抛出与正式提交完全相同的领域错误（``details.index`` 指明
    位置），不返回部分成功结果。预演不推进服务端单调时钟；随后正式提交仍须
    按提交时的当时状态重新校验，预演结果不保证可提交。
    """
    try:
        work_start = parse_canonical(work_start_raw, "work_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_start"})
    try:
        work_end = parse_canonical(work_end_raw, "work_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_end"})
    if not work_start < work_end:
        raise errors.invalid_interval(
            work_start, work_end,
            "作业区间非法：work_start 必须严格早于 work_end（区间为 [起, 止) 且长度大于 0）")

    # 预演全程只读：取墙上时钟作为"提交前"快照基准（不触碰写路径的共享
    # 单调状态，避免在只读事务内引入任何写侧副作用）。
    known_as_of = now_canonical()
    with read_snapshot() as snapshot:
        sim = _in_memory_clone(snapshot)
        try:
            created = _apply_corrections(sim, items, known_as_of)

            numbers: list[str] = []
            seen: set[str] = set()
            for entry in created:
                if entry["number"] not in seen:
                    seen.add(entry["number"])
                    numbers.append(entry["number"])
            changes = [
                _preview_changes_for(snapshot, sim, number,
                                     work_start, work_end, known_as_of)
                for number in numbers]
        finally:
            sim.close()

    # 预演响应不带内部行 id（拟生成行只存在于副本内）。
    preview_items = [{k: v for k, v in e.items() if k != "_version_id"}
                     for e in created]
    return {"work_start": work_start, "work_end": work_end,
            "known_as_of": known_as_of, "count": len(created),
            "items": preview_items, "changes": changes}


def _in_memory_clone(conn: sqlite3.Connection) -> sqlite3.Connection:
    """把只读快照当时的全部业务表复制进一个全新的内存库（同 schema）。

    预演在该副本上运行与正式提交逐行相同的应用逻辑，因此不写真实历史、
    不占真实版本号；行主键连同 AUTOINCREMENT 序列（sqlite_sequence）一并
    复制，保证副本内 ``lastrowid`` 与外键引用关系和真实库一致。
    """
    from .db import _SCHEMA

    clone = sqlite3.connect(":memory:")
    try:
        clone.row_factory = sqlite3.Row
        clone.execute("PRAGMA foreign_keys = OFF")
        clone.executescript(_SCHEMA)
        tables = ["procedures", "versions", "withdrawals", "packages",
                  "package_items", "prerequisites",
                  "audit_batches", "audit_events"]
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            columns = rows[0].keys()
            placeholders = ",".join("?" * len(columns))
            col_list = ",".join(columns)
            clone.executemany(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                [tuple(r[c] for c in columns) for r in rows])
        # 复制 AUTOINCREMENT 序列，副本内新行的 rowid 不与拷入行冲突。
        # 源库从未向 AUTOINCREMENT 表插过行时 sqlite_sequence 表尚不存在。
        if conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='sqlite_sequence'").fetchone():
            seq_rows = conn.execute(
                "SELECT name, seq FROM sqlite_sequence").fetchall()
            for r in seq_rows:
                clone.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                    (r["name"], r["seq"]))
        clone.commit()
    except Exception:
        clone.close()
        raise
    return clone


def _preview_changes_for(before_conn: sqlite3.Connection,
                         after_conn: sqlite3.Connection, number: str,
                         work_start: str, work_end: str,
                         known_as_of: str) -> dict:
    """单个规程在作业区间内"提交前 vs 提交后（本批应用后）"的版本差异片段。

    两侧均以预演时刻为知悉时刻（本批更正视为"此刻发布"）：提交前取只读
    快照当时的可见版本，提交后取内存副本（含本批拟生成版本）的可见版本。
    切分、合并与版本相等判定完全沿用两知悉时刻对比（compare_interval）。
    """
    proc = _fetch_procedure(before_conn, number)
    proc_id = proc["id"]
    before_rows = _visible_versions(before_conn, proc_id, known_as_of)
    after_rows = _visible_versions(after_conn, proc_id, known_as_of)

    # 片段解析需要沿完整 supersedes 链计算深度（含对某侧不可见的版本），
    # 故按两侧并集建索引；版本 id 在各自库内唯一，而副本新生成的版本 id
    # 在其所属规程下不与既有 id 相同，故并集内不会发生跨库键冲突。
    all_rows = {v["id"]: v for v in before_rows}
    for v in after_rows:
        all_rows.setdefault(v["id"], v)
    depth_cache: dict[int, int] = {}

    bounds = {work_start, work_end}
    for v in all_rows.values():
        if v["valid_end"] <= work_start or work_end <= v["valid_start"]:
            continue                        # 与作业区间不相交
        bounds.add(max(v["valid_start"], work_start))
        bounds.add(min(v["valid_end"], work_end))
    ordered = sorted(bounds)

    raw: list[tuple[str, str, int | None, int | None]] = []
    for s, e in zip(ordered, ordered[1:]):
        lv = _effective_at(s, before_rows, all_rows, depth_cache)
        rv = _effective_at(s, after_rows, all_rows, depth_cache)
        raw.append((s, e, lv["id"] if lv is not None else None,
                    rv["id"] if rv is not None else None))

    segments: list[dict] = []
    for s, e, lid, rid in _merge_adjacent(raw):
        if lid == rid:
            continue
        lv = all_rows.get(lid) if lid is not None else None
        rv = all_rows.get(rid) if rid is not None else None
        segments.append({
            "start": s,
            "end": e,
            "before": None if lv is None else {
                "revision": lv["revision"], "body": lv["body"]},
            "after": None if rv is None else {
                "revision": rv["revision"], "body": rv["body"]},
        })

    return {"number": number, "count": len(segments), "segments": segments}


# ---------------------------------------------------------------- 更正撤回


def withdraw_corrections(items: list) -> dict:
    """撤回一条或多条误发布的更正；整批原子，任一非法则整批回滚。

    撤回只向 withdrawals 追加事件行（withdrawn_at 由服务端时钟记录、不可回填），
    绝不删除或改写 versions 行。拒绝条件（命中即整体拒绝、不改历史）：

    1. 规程编号不存在 -> 404 not_found；
    2. 版本号不存在 -> 404 revision_not_found；
    3. 首版（revision=0）-> 409 first_revision_not_withdrawable；
    4. 已撤回版本 -> 409 already_withdrawn（响应带服务端记录的 withdrawn_at）；
    5. 已有子更正的版本 -> 409 version_has_children（只能撤回叶子更正）。

    写操作经全局写锁串行化，并在同一 BEGIN IMMEDIATE 事务内"校验 + 追加事件"，
    因此撤回与并发更正按确定的全局次序先后生效，同一区间不会并存两份当前正文。
    """
    with write_tx() as conn:
        withdrawn_at = _server_now()
        created: list[dict] = []

        for i, item in enumerate(items):
            number = _clean_number(item.number, i)
            revision = item.revision

            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.procedure_not_found(number, i)
            proc_id = proc["id"]
            version = _fetch_version(conn, proc_id, revision)
            if version is None:
                raise errors.revision_not_found(number, revision, i)

            # 3) 首版只能更正，不能撤回。
            if version["supersedes_id"] is None:
                raise errors.first_revision_not_withdrawable(number, revision, i)

            # 4) 已撤回：撤回事件只追加一次；UNIQUE(version_id) 是最终防线。
            existing = _withdrawn_at(conn, version["id"])
            if existing is not None:
                raise errors.already_withdrawn(number, revision, existing, i)

            # 5) 已有子更正：撤回只追加事件、子更正行永不删除，因此只要版本
            #    曾被更正（子行存在，无论其自身是否已撤回）就拒绝撤回本版本；
            #    可撤回的是当前仍生效的"叶子"更正，撤回后其区间恢复直接父版本。
            children = _children(conn, version["id"])
            if children:
                child_revisions = sorted(c["revision"] for c in children)
                raise errors.version_has_children(
                    number, revision, child_revisions, i)

            conn.execute(
                "INSERT INTO withdrawals (version_id, withdrawn_at) "
                "VALUES (?, ?)",
                (version["id"], withdrawn_at))
            created.append({
                "number": number,
                "revision": revision,
                "supersedes_revision": _revision_of(conn, version["supersedes_id"]),
                "body": version["body"],
                "valid_start": version["valid_start"],
                "valid_end": version["valid_end"],
                "published_at": version["published_at"],
                "withdrawn_at": withdrawn_at,
                "_version_id": version["id"],
            })

        # 整批撤回作为一个审计批次（事件按请求顺序，与撤回事件行同事务）；
        # 撤回事件的服务端时刻取 withdrawn_at。
        _insert_audit_batch(conn, [
            {"operation": "withdrawal", "number": e["number"],
             "revision": e["revision"], "event_time": withdrawn_at,
             "version_id": e["_version_id"]}
            for e in created])
        for e in created:
            e.pop("_version_id", None)

    return {"withdrawn_at": withdrawn_at, "items": created}


def _revision_of(conn: sqlite3.Connection, version_id: int) -> int:
    row = conn.execute("SELECT revision FROM versions WHERE id = ?",
                       (version_id,)).fetchone()
    return row["revision"]

# ------------------------------------------------------------ 值班审计变动日志

DEFAULT_FEED_LIMIT = 100
MAX_FEED_LIMIT = 1000


def _parse_cursor(raw: str | None) -> int:
    """解析不透明游标：省略/空串/0 表示起始游标；否则须为正整数串。"""
    if raw is None or not str(raw).strip():
        return 0
    text = str(raw).strip()
    if not text.isdigit():
        raise errors.invalid_cursor(text)
    value = int(text)
    if value < 0:                       # isdigit 已挡住负号，防御性保留
        raise errors.invalid_cursor(text)
    return value


def _parse_feed_limit(raw) -> int:
    if raw is None:
        return DEFAULT_FEED_LIMIT
    text = str(raw).strip()
    if not text.isdigit() or not (1 <= int(text) <= MAX_FEED_LIMIT):
        raise errors.invalid_page_limit(text)
    return int(text)


def read_change_feed(cursor_raw: str | None, limit_raw=None) -> dict:
    """按提交顺序增量取回全站规程变动（值班审计）。

    * ``cursor``：上一页响应的 ``next_cursor``；省略/空/``"0"`` 为起始游标，
      从升级前已存在的第一条版本/撤回事件开始完整可读。游标只随**已提交**
      事务（批次）前进——页内每一批都是完整交付，空页保持游标不变。
    * ``limit``：每页事件数上限（默认 100、最大 1000）。同一新提交批次内的
      事件按请求顺序排列且**不可跨页拆开**；若游标后的下一批本身大于
      ``limit``，返回 ``422 page_limit_too_small``（游标不前进、无事件）。

    全程使用进入时固定的只读快照：并发写入或翻页期间的新提交要么整批可见、
    要么整批不可见，配合单调批次游标，不会重现、不会遗漏。
    """
    cursor = _parse_cursor(cursor_raw)
    limit = _parse_feed_limit(limit_raw)

    with read_snapshot() as conn:
        if cursor > 0 and conn.execute(
                "SELECT 1 FROM audit_batches WHERE seq = ?",
                (cursor,)).fetchone() is None:
            raise errors.invalid_cursor(str(cursor))

        # 取下一批起的批次序列（快照内一致）；limit+1 用于判定是否还有后续。
        batches = conn.execute(
            "SELECT seq, event_count FROM audit_batches WHERE seq > ? "
            "ORDER BY seq LIMIT ?",
            (cursor, limit + 1)).fetchall()

        # 同一批次不可拆开：游标后的第一个批次就装不下 -> 明确报错，
        # 不返回任何事件、游标保持不变。
        if batches and batches[0]["event_count"] > limit:
            raise errors.page_limit_too_small(
                limit, batches[0]["seq"], batches[0]["event_count"])

        included: list[sqlite3.Row] = []
        used = 0
        for b in batches:
            if used + b["event_count"] > limit:
                break
            included.append(b)
            used += b["event_count"]
        has_more = len(included) < len(batches)

        events: list[dict] = []
        next_cursor = cursor
        if included:
            seqs = [b["seq"] for b in included]
            placeholders = ",".join("?" * len(seqs))
            rows = conn.execute(
                f"SELECT * FROM audit_events WHERE batch_seq IN "
                f"({placeholders}) ORDER BY batch_seq, position",
                tuple(seqs)).fetchall()
            events = [{
                "operation": r["operation"],
                "number": r["procedure_number"],
                "revision": r["revision"],
                "event_time": r["event_time"],
                "batch_seq": r["batch_seq"],
                "position": r["position"],
            } for r in rows]
            next_cursor = included[-1]["seq"]

    return {"cursor": str(cursor), "next_cursor": str(next_cursor),
            "limit": limit, "has_more": has_more,
            "count": len(events), "events": events}

# ---------------------------------------------------------------- 回溯查询


def _version_payload(row: sqlite3.Row, number: str,
                     parent_rev: int | None) -> dict:
    """一个版本对外呈现的完整字段（规程包快照也按此形状留档/取回）。"""
    return {
        "number": number,
        "revision": row["revision"],
        "body": row["body"],
        "valid_start": row["valid_start"],
        "valid_end": row["valid_end"],
        "supersedes_revision": parent_rev,
        "published_at": row["published_at"],
    }


def _serialize(row: sqlite3.Row,
               parent_rev: dict[int, int],
               withdrawn_at_by_id: dict[int, str] | None = None) -> dict:
    pid = row["supersedes_id"]
    out = _version_payload(row, row["number"],
                           parent_rev.get(pid) if pid is not None else None)
    # 审计视图追加撤回时刻（仅历史接口传入该映射）；回溯接口输出约定保持不变。
    if withdrawn_at_by_id is not None:
        out["withdrawn_at"] = withdrawn_at_by_id.get(row["id"])
    return out


def _depth_of(version_id: int, by_id: dict[int, sqlite3.Row],
              depth_cache: dict[int, int]) -> int:
    """版本在替代链（correction chain）上的深度：首版为 1，每被更正一次 +1。

    作业时刻有效版本即所有覆盖该时刻的可见版本中链上最深者。
    """
    chain: list[int] = []
    cur_id: int | None = version_id
    while cur_id is not None and cur_id not in depth_cache:
        chain.append(cur_id)
        parent = by_id[cur_id]["supersedes_id"]
        cur_id = parent if parent is not None else None
    base = depth_cache[cur_id] if cur_id is not None else 0
    for vid in reversed(chain):
        base += 1
        depth_cache[vid] = base
    return depth_cache[version_id]


def _effective_at(t: str, candidates: list[sqlite3.Row],
                  by_id: dict[int, sqlite3.Row],
                  depth_cache: dict[int, int]) -> sqlite3.Row | None:
    """作业时刻 t 处沿更正链取最深的可见版本；无版本覆盖返回 None。

    左闭右开：valid_start <= t < valid_end。同深度仅作确定性兜底
    （同一父版本的更正区间互不重叠，正常数据下同深度候选只会有一个）。
    """
    best: sqlite3.Row | None = None
    best_depth = 0
    for r in candidates:
        if r["valid_start"] <= t < r["valid_end"]:
            d = _depth_of(r["id"], by_id, depth_cache)
            if (best is None or d > best_depth
                    or (d == best_depth
                        and (r["revision"], r["published_at"])
                        > (best["revision"], best["published_at"]))):
                best, best_depth = r, d
    return best


def _visible_versions(conn: sqlite3.Connection, proc_id: int,
                      known_as_of: str) -> list[sqlite3.Row]:
    """知悉时刻下"当时仍生效"的版本：已发布且（未撤回或撤回晚于该时刻）。

    边界与发布侧对称（均为含等号）：``published_at <= k`` 时版本已发布可见，
    ``withdrawn_at >= k`` 时撤回事件尚未发生、版本仍生效；严格晚于撤回时刻
    （``withdrawn_at < k``）才视为已撤回。因此对撤回之前（含撤回瞬间）的知悉
    时刻，被撤回版本仍在版本链中可见，撤回前的回溯与区间对比结果保持原样。
    """
    return conn.execute(
        """
        SELECT v.* FROM versions v
        LEFT JOIN withdrawals w ON w.version_id = v.id
         WHERE v.procedure_id = ?
           AND v.published_at <= ?
           AND (w.version_id IS NULL OR w.withdrawn_at >= ?)
        """,
        (proc_id, known_as_of, known_as_of)).fetchall()


def _resolve_effective(conn: sqlite3.Connection, proc: sqlite3.Row,
                       work_time: str, known_as_of: str
                       ) -> tuple[sqlite3.Row | None, int | None]:
    """某规程在 (作业时刻, 知悉时刻) 下实际适用的版本及其父版本号。

    沿现有双时间维约定：仅取该知悉时刻可见（已发布且未撤回或撤回不早于该
    时刻）的版本，在覆盖作业时刻（左闭右开）的候选中沿 supersedes 链取最深者。
    无适用正文时版本为 None。固定与复核共用本函数，语义与回溯查询完全一致。
    """
    visible = _visible_versions(conn, proc["id"], known_as_of)
    by_id = {v["id"]: v for v in visible}
    depth_cache: dict[int, int] = {}
    best = _effective_at(work_time, visible, by_id, depth_cache)
    if best is None:
        return None, None
    parent_rev: int | None = None
    if best["supersedes_id"] is not None:
        parent = by_id.get(best["supersedes_id"])
        if parent is not None:
            parent_rev = parent["revision"]
        else:
            # 理论上最深候选的父版本必然可见；兜底从全量版本读取父版本号。
            parent_rev = _revision_of(conn, best["supersedes_id"])
    return best, parent_rev


def query_effective(work_time_raw: str, known_as_of_raw: str | None) -> dict:
    """返回截至知悉时刻已发布、且在作业时刻有效的各规程正文与版本。"""
    try:
        work_time = parse_canonical(work_time_raw, "work_time")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422, {"field": "work_time"})
    if known_as_of_raw is None or not str(known_as_of_raw).strip():
        known_as_of = now_canonical()
    else:
        try:
            known_as_of = parse_canonical(known_as_of_raw, "known_as_of")
        except TimeParseError as exc:
            raise DomainError("invalid_time", str(exc), 422,
                              {"field": "known_as_of"})

    with read_conn() as conn:
        rows = conn.execute(
            """
            SELECT v.*, p.number AS number
              FROM versions v
              JOIN procedures p ON p.id = v.procedure_id
              LEFT JOIN withdrawals w ON w.version_id = v.id
             WHERE v.published_at <= ?
               AND (w.version_id IS NULL OR w.withdrawn_at >= ?)
               AND v.valid_start <= ?
               AND ? < v.valid_end
             ORDER BY p.number
            """,
            (known_as_of, known_as_of, work_time, work_time)).fetchall()
        if not rows:
            return {"work_time": work_time, "known_as_of": known_as_of,
                    "count": 0, "items": []}

        # 取候选涉及的全部版本，构建父链并计算深度。
        proc_ids = {r["procedure_id"] for r in rows}
        all_versions: list[sqlite3.Row] = []
        for proc_id in proc_ids:
            all_versions.extend(conn.execute(
                "SELECT * FROM versions WHERE procedure_id = ?",
                (proc_id,)).fetchall())

        by_id = {v["id"]: v for v in all_versions}
        parent_rev = {v["id"]: v["revision"] for v in all_versions}
        depth_cache: dict[int, int] = {}

        best: dict[int, sqlite3.Row] = {}
        best_depth: dict[int, int] = {}
        for r in rows:
            pid = r["procedure_id"]
            d = _depth_of(r["id"], by_id, depth_cache)
            cur = best.get(pid)
            if (cur is None or d > best_depth[pid]
                    # 同深度理论上不会出现（覆盖互不重叠）；以下仅为确定性兜底
                    or (d == best_depth[pid]
                        and (r["revision"], r["published_at"])
                        > (cur["revision"], cur["published_at"]))):
                best[pid] = r
                best_depth[pid] = d

        items = sorted((_serialize(r, parent_rev) for r in best.values()),
                       key=lambda x: x["number"])

    return {"work_time": work_time, "known_as_of": known_as_of,
            "count": len(items), "items": items}

# ---------------------------------------------------------------- 前置规程递归解析


def resolve_closure(number_raw: str, work_time_raw: str,
                    known_as_of_raw: str | None) -> dict:
    """班组按一份规程准备作业：连同该时点有效的前置规程一起核对。

    输入根规程编号、作业时刻与知悉时刻，按现有回溯语义（知悉时刻可见性、
    左闭右开适用区间、撤回边界，与 /effective 完全一致）在**同一只读快照**
    内从根规程的有效版本出发，沿其携带的前置编号列表递归解析，返回去重后
    的根及前置规程清单：

    * 每个规程取 (作业时刻, 知悉时刻) 下的适用版本，引用的是**该版本**所
      携带的前置列表——引用随版本的适用区间、发布时间与撤回而变化；
    * 清单按"前置优先"排列（任一规程排在其前置之后），根规程在最后；
      同一编号被多处引用只出现一次（按首次完成解析的位置）；
    * 缺口即整体失败、不返回部分清单：根不存在 -> 404 not_found；引用
      编号不存在 -> 404 prerequisite_not_found；任一规程当时无适用正文
      -> 409 no_applicable_version（details 均带引用链 path 说明缺口）；
    * 引用成环 -> 409 prerequisite_cycle，details.cycle 给出环路径。
    """
    number = number_raw.strip()
    try:
        work_time = parse_canonical(work_time_raw, "work_time")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_time"})
    if known_as_of_raw is None or not str(known_as_of_raw).strip():
        known_as_of = now_canonical()
    else:
        try:
            known_as_of = parse_canonical(known_as_of_raw, "known_as_of")
        except TimeParseError as exc:
            raise DomainError("invalid_time", str(exc), 422,
                              {"field": "known_as_of"})

    with read_snapshot() as conn:
        proc = _fetch_procedure(conn, number) if number else None
        if proc is None:
            raise DomainError("not_found",
                              f"规程编号 {number or '(空)'} 不存在",
                              404, {"number": number})
        result = _resolve_closure_at(conn, number, work_time, known_as_of)

    if result["gaps"] or result["cycle"] is not None:
        # 单时刻解析：任一缺口/环即整体失败、不返回部分清单（历史约定）。
        raise _closure_error(result["gaps"], result["cycle"],
                             work_time, known_as_of)

    items = result["items"]
    return {"root": number, "work_time": work_time,
            "known_as_of": known_as_of,
            "count": len(items), "items": items}


class _ClosureStop(Exception):
    """片段内闭包解析首次遇到缺口/成环：停止展开，保留已完成部分。"""

    def __init__(self, gaps: list[dict], cycle: list[str] | None):
        super().__init__("closure unresolved")
        self.gaps = gaps
        self.cycle = cycle


def _closure_error(gaps: list[dict], cycle: list[str] | None,
                   work_time: str, known_as_of: str) -> DomainError:
    """把片段内首次缺口/环转成与单时刻 /closure 完全一致的领域错误。"""
    if cycle is not None:
        return errors.prerequisite_cycle(cycle)
    gap = gaps[0]
    if gap["code"] == "prerequisite_not_found":
        return errors.prerequisite_not_found(gap["number"], gap["path"])
    if gap["code"] == "not_found":
        return DomainError("not_found",
                           f"规程编号 {gap['number'] or '(空)'} 不存在",
                           404, {"number": gap["number"]})
    return errors.closure_no_applicable_version(
        gap["number"], work_time, known_as_of, gap["path"])


def _resolve_closure_at(conn: sqlite3.Connection, root_number: str,
                        work_time: str, known_as_of: str) -> dict:
    """在给定读取快照上解析根规程在 (作业时刻, 知悉时刻) 下的前置闭包。

    迭代式后序遍历（显式栈，深链不触发递归上限）：前置优先、按编号去重、
    根在最后。本函数**不抛异常**——根/引用缺失、无适用正文或引用成环时停止
    后续展开，把已完成的前置项原样保留在 ``items``，并在 ``gaps``（缺口链
    列表）/``cycle``（环路径）中标记，供区间接口逐片段保留缺口；根编号不
    存在时以 ``not_found`` 缺口保留（不假设根一定存在）。调用方需要"整体
    失败"语义时用 :func:`_closure_error` 转换。
    """
    items: list[dict] = []
    gaps: list[dict] = []
    cycle: list[str] | None = None
    state: dict[str, str] = {}          # 编号 -> visiting / done
    path: list[str] = []                # 当前解析栈（引用链，供环与缺口说明）
    frames: list[dict] = []             # 显式栈：深链不触发递归上限

    def push(num: str) -> None:
        nonlocal cycle
        proc = _fetch_procedure(conn, num)
        if proc is None:
            chain = path + [num]
            # 根缺失（frames 为空，code=not_found）与引用缺失（code=
            # prerequisite_not_found）都作为片段缺口保留。
            code = "prerequisite_not_found" if frames else "not_found"
            gaps.append({"code": code, "number": num, "path": chain})
            raise _ClosureStop(gaps, None)
        best, parent_rev = _resolve_effective(
            conn, proc, work_time, known_as_of)
        if best is None:
            gaps.append({"code": "no_applicable_version",
                         "number": num, "path": path + [num]})
            raise _ClosureStop(gaps, None)
        state[num] = "visiting"
        path.append(num)
        frames.append({"number": num, "best": best,
                       "parent_rev": parent_rev,
                       "prerequisites": _prerequisites_of(conn, best["id"]),
                       "next": 0})

    try:
        push(root_number)
        while frames:
            frame = frames[-1]
            descended = False
            while frame["next"] < len(frame["prerequisites"]):
                pre = frame["prerequisites"][frame["next"]]
                frame["next"] += 1
                pre_state = state.get(pre)
                if pre_state == "done":
                    continue                    # 已解析过：去重，不重复展开
                if pre_state == "visiting":
                    # 环：从首次出现到当前栈顶再回到它；停止展开、保留片段
                    cycle = path[path.index(pre):] + [pre]
                    raise _ClosureStop(gaps, cycle)
                push(pre)
                descended = True
                break
            if descended:
                continue
            # 全部前置已就位：本规程出栈入列（前置优先，根最后）
            frames.pop()
            path.pop()
            state[frame["number"]] = "done"
            payload = _version_payload(frame["best"], frame["number"],
                                       frame["parent_rev"])
            payload["prerequisites"] = frame["prerequisites"]
            items.append(payload)
    except _ClosureStop as stop:
        gaps = stop.gaps
        cycle = stop.cycle

    return {"items": items, "gaps": gaps, "cycle": cycle}


def _closure_signature(result: dict) -> dict:
    """片段解析结果的"完全相同"判定键：清单（含版本/正文/区间/前置列表）
    与错误状态（缺口链/环路径）逐一可比时即视为同一状态。

    相邻片段仅在该键相等时合并——版本号相同但正文/区间/引用列表不同、
    或缺口链/环不同，都不合并。
    """
    return {"items": result["items"], "gaps": result["gaps"],
            "cycle": result["cycle"]}

# ---------------------------------------------------------------- 跨时段前置闭包


def resolve_closure_plan(number_raw: str,
                         work_start_raw: str,
                         work_end_raw: str,
                         known_as_of_raw: str | None) -> dict:
    """班组跨时段作业：判断根规程及其递归前置在整个作业区间是否持续可用。

    输入根编号、非空半开作业区间 ``[work_start, work_end)`` 与带时区的知悉
    时刻，在**同一只读快照**内按现有版本回溯与前置关系语义，找出根及递归
    引用的全部版本切换边界，把作业区间切成覆盖全区间、互不重叠的片段；
    逐片段在片段起点解析前置闭包（前置优先、按编号去重、根在最后）。

    与单时刻 :func:`resolve_closure` 的"缺口即整体失败"不同，本接口**保留
    每个片段**：根/引用编号不存在、某规程在片段内无适用正文、引用成环时，
    该片段照样返回（含成环前已完成的前置优先部分清单），并以 ``gaps``
    （缺口链：code/number/path）或 ``cycle``（环路径）标记，``available``
    为 false；顶层 ``available`` 仅在全部片段均无缺口/环时为 true。

    切分边界取"结构上可传递引用到的全部规程"在该知悉时刻下**可见版本**的
    适用区间端点（与作业区间求交）：引用随版本变化只可能发生在这些端点上，
    故片段内部闭包恒定；区间外的版本/变动不产生端点、不影响结果。相邻片段
    仅在清单（含版本/正文/区间/各自前置列表）与错误状态（缺口链/环路径）
    完全相同时合并。非法区间或时间直接拒绝，全程只读、不写历史。
    """
    number = number_raw.strip()
    try:
        work_start = parse_canonical(work_start_raw, "work_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_start"})
    try:
        work_end = parse_canonical(work_end_raw, "work_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_end"})
    if not work_start < work_end:
        raise errors.invalid_interval(
            work_start, work_end,
            "作业区间非法：work_start 必须严格早于 work_end（区间为 [起, 止) 且长度大于 0）")
    if known_as_of_raw is None or not str(known_as_of_raw).strip():
        known_as_of = now_canonical()
    else:
        try:
            known_as_of = parse_canonical(known_as_of_raw, "known_as_of")
        except TimeParseError as exc:
            raise DomainError("invalid_time", str(exc), 422,
                              {"field": "known_as_of"})

    with read_snapshot() as conn:
        root_proc = _fetch_procedure(conn, number) if number else None

        # 1) 沿"版本携带的前置编号"做结构性传递展开，收集作业区间内可能参与
        #    闭包的全部规程。根编号不存在时无任何版本/端点可收集：作业区间
        #    作为单一片段保留，逐片段解析时以根缺口标记（available=false）。
        reached: dict[int, sqlite3.Row] = {}
        visible_by_proc: dict[int, list[sqlite3.Row]] = {}
        if root_proc is not None:
            queue: list[sqlite3.Row] = [root_proc]
            while queue:
                proc = queue.pop()
                if proc["id"] in reached:
                    continue
                reached[proc["id"]] = proc
                visible = _visible_versions(conn, proc["id"], known_as_of)
                visible_by_proc[proc["id"]] = visible
                # 该规程任一可见版本携带过的前置编号都纳入结构性传递展开：不同
                # 版本可能在不同片段携带不同前置列表（含撤回后恢复的父版本列表）。
                referenced: set[str] = set()
                for v in visible:
                    referenced.update(_prerequisites_of(conn, v["id"]))
                for pre in referenced:
                    pre_proc = _fetch_procedure(conn, pre)
                    if pre_proc is not None and pre_proc["id"] not in reached:
                        queue.append(pre_proc)

        # 2) 收集与作业区间相交的全部可见版本端点：片段内部各规程的适用版本
        #    与其携带的前置列表均恒定（端点之外的变动不影响结果）。
        bounds = {work_start, work_end}
        for visible in visible_by_proc.values():
            for v in visible:
                if v["valid_end"] <= work_start or work_end <= v["valid_start"]:
                    continue                        # 与作业区间不相交
                bounds.add(max(v["valid_start"], work_start))
                bounds.add(min(v["valid_end"], work_end))
        ordered = sorted(bounds)

        # 3) 逐片段（以左闭右开片段起点为代表作业时刻）解析闭包；缺口/环不再
        #    整体失败，而是保留片段并标记。
        raw_segments: list[tuple[str, str, dict]] = []
        for s, e in zip(ordered, ordered[1:]):
            result = _resolve_closure_at(conn, number, s, known_as_of)
            raw_segments.append((s, e, result))

        # 4) 合并首尾相接且"清单 + 错误状态"完全相同的相邻片段。
        segments: list[dict] = []
        for s, e, result in raw_segments:
            signature = _closure_signature(result)
            if (segments and segments[-1]["end"] == s
                    and segments[-1]["_signature"] == signature):
                segments[-1]["end"] = e
                continue
            segments.append({"start": s, "end": e,
                             "available": not (result["gaps"]
                                               or result["cycle"] is not None),
                             "count": len(result["items"]),
                             "items": result["items"],
                             "gaps": result["gaps"],
                             "cycle": result["cycle"],
                             "_signature": signature})
        for seg in segments:
            seg.pop("_signature", None)

    return {"root": number,
            "work_start": work_start,
            "work_end": work_end,
            "known_as_of": known_as_of,
            "count": len(segments),
            "available": all(seg["available"] for seg in segments),
            "segments": segments}


# ---------------------------------------------------------------- 跨规程版本覆盖检查


def check_coverage(numbers_raw, work_start_raw: str, work_end_raw: str,
                   known_as_of_raw: str) -> dict:
    """调度员安排跨规程作业前的版本覆盖检查（只读，不写任何历史）。

    输入非空且不重复的规程编号、非空作业区间 ``[work_start, work_end)`` 与
    知悉时刻（必填，不得晚于服务端当前时刻）。在**同一只读快照**内：

    * 收集各编号在该知悉时刻前已发布版本的适用区间端点（含已撤回版本的
      端点——"因撤回恢复父版本"的区间边界只能由它们圈定），与作业区间
      求交后排序，把作业区间切成首尾相接、互不重叠的左闭右开片段；
    * 逐段以片段起点为代表作业时刻，按与 /effective 完全一致的可见性
      （``published_at <= k`` 且未撤回或 ``withdrawn_at >= k``）与最深链
      语义解析每个编号的有效版本：有则给出版本号与正文，无则记为缺口
      （``gap=true``，即该编号在片段内无适用正文的空档）；
    * 标记：``restored_by_withdrawal``（该片段的有效版本是因更正被撤回
      而恢复的祖先版本）、``switched``（相对上一片段发生版本切换）；
      片段级汇总 ``version_switch`` / ``has_gap`` / ``restored``，顶层
      ``fully_covered`` 仅在全区间全部编号均有适用正文时为 true；
    * 相邻片段仅在整组结果（全部编号的有效版本与恢复标记）完全相同时
      合并；被中间状态隔开的相同片段不跨段合并。

    编号不存在（404 procedure_not_found）、编号重复（422
    duplicate_coverage_number）、时间非法（422 invalid_time /
    invalid_interval）、知悉时刻晚于服务端当前时刻（422
    known_as_of_in_future）均直接拒绝；全程只读、不改变历史。
    """
    try:
        work_start = parse_canonical(work_start_raw, "work_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_start"})
    try:
        work_end = parse_canonical(work_end_raw, "work_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_end"})
    if not work_start < work_end:
        raise errors.invalid_interval(
            work_start, work_end,
            "作业区间非法：work_start 必须严格早于 work_end（区间为 [起, 止) 且长度大于 0）")
    try:
        known_as_of = parse_canonical(known_as_of_raw, "known_as_of")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "known_as_of"})

    if not numbers_raw:
        raise DomainError("validation_error",
                          "规程编号列表不能为空：覆盖检查至少需要一个规程编号",
                          422, {"field": "numbers"})
    cleaned: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(numbers_raw):
        number = raw.strip() if isinstance(raw, str) else ""
        if not number:
            raise DomainError("invalid_number", "规程编号不能为空字符串",
                              422, {"field": "numbers", "index": i})
        if number in seen:
            raise errors.duplicate_coverage_number(number, i)
        seen.add(number)
        cleaned.append(number)

    with read_snapshot() as conn:
        # 只读路径使用不触碰写状态的墙上时钟（与复核接口同理）。
        server_now = now_canonical()
        if known_as_of > server_now:
            raise errors.known_as_of_in_future(known_as_of, server_now)

        procs: list[sqlite3.Row] = []
        for i, number in enumerate(cleaned):
            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.coverage_procedure_not_found(number, i)
            procs.append(proc)

        # 知悉时刻前已发布的全部版本（含已撤回版本，附撤回时刻）：可见版本
        # 端点决定有效版本的切换点，已撤回版本端点决定"恢复"标记的边界。
        per_number: list[dict] = []
        bounds = {work_start, work_end}
        for proc in procs:
            rows = conn.execute(
                """
                SELECT v.*, w.withdrawn_at AS withdrawn_at
                  FROM versions v
                  LEFT JOIN withdrawals w ON w.version_id = v.id
                 WHERE v.procedure_id = ? AND v.published_at <= ?
                """,
                (proc["id"], known_as_of)).fetchall()
            for v in rows:
                if v["valid_end"] <= work_start or work_end <= v["valid_start"]:
                    continue                        # 与作业区间不相交
                bounds.add(max(v["valid_start"], work_start))
                bounds.add(min(v["valid_end"], work_end))
            per_number.append({
                "rows": rows,
                "by_id": {v["id"]: v for v in rows},
                "visible": [r for r in rows
                            if r["withdrawn_at"] is None
                            or r["withdrawn_at"] >= known_as_of],
                "depth_cache": {},
            })
        ordered = sorted(bounds)

        # 逐段解析：每个编号取片段起点的有效版本与"撤回恢复"标记。
        # 恢复判定：忽略撤回可见性时的最深版本是已撤回版本（撤回时刻早于
        # 知悉时刻），此时实际生效的是其祖先版本，即"因撤回导致的恢复"。
        raw_segments: list[tuple[str, str, tuple]] = []
        for s, e in zip(ordered, ordered[1:]):
            group = []
            for entry in per_number:
                best = _effective_at(s, entry["visible"], entry["by_id"],
                                     entry["depth_cache"])
                deepest = _effective_at(s, entry["rows"], entry["by_id"],
                                        entry["depth_cache"])
                restored = (best is not None and deepest is not None
                            and deepest["withdrawn_at"] is not None
                            and deepest["withdrawn_at"] < known_as_of)
                group.append((best["id"] if best is not None else None,
                              restored))
            raw_segments.append((s, e, tuple(group)))

        # 相邻段仅在整组结果（全部编号的有效版本与恢复标记）完全相同时合并。
        merged: list[list] = []
        for s, e, group in raw_segments:
            if merged and merged[-1][1] == s and merged[-1][2] == group:
                merged[-1][1] = e
            else:
                merged.append([s, e, group])

        segments: list[dict] = []
        prev_group: tuple | None = None
        for s, e, group in merged:
            items: list[dict] = []
            for idx, (number, entry) in enumerate(zip(cleaned, per_number)):
                vid, restored = group[idx]
                switched = (prev_group is not None
                            and vid != prev_group[idx][0])
                row = entry["by_id"].get(vid) if vid is not None else None
                if row is None:
                    items.append({"number": number, "revision": None,
                                  "body": None, "valid_start": None,
                                  "valid_end": None,
                                  "supersedes_revision": None,
                                  "published_at": None,
                                  "gap": True,
                                  "restored_by_withdrawal": False,
                                  "switched": switched})
                    continue
                parent_rev = None
                if row["supersedes_id"] is not None:
                    parent = entry["by_id"].get(row["supersedes_id"])
                    if parent is not None:
                        parent_rev = parent["revision"]
                items.append({"number": number, "revision": row["revision"],
                              "body": row["body"],
                              "valid_start": row["valid_start"],
                              "valid_end": row["valid_end"],
                              "supersedes_revision": parent_rev,
                              "published_at": row["published_at"],
                              "gap": False,
                              "restored_by_withdrawal": restored,
                              "switched": switched})
            segments.append({
                "start": s, "end": e,
                "version_switch": (prev_group is not None
                                   and any(it["switched"] for it in items)),
                "has_gap": any(it["gap"] for it in items),
                "restored": any(it["restored_by_withdrawal"]
                                for it in items),
                "items": items})
            prev_group = group

    return {"numbers": cleaned,
            "work_start": work_start,
            "work_end": work_end,
            "known_as_of": known_as_of,
            "count": len(segments),
            "fully_covered": all(not seg["has_gap"] for seg in segments),
            "segments": segments}


# ---------------------------------------------------------------- 历史查看


def list_history(number_raw: str) -> dict:
    """返回某规程全部版本（管理用审计视图，不做知悉时刻裁剪）。"""
    number = number_raw.strip()
    with read_conn() as conn:
        proc = _fetch_procedure(conn, number) if number else None
        if proc is None:
            raise DomainError("not_found", f"规程编号 {number or '(空)'} 不存在",
                              404, {"number": number})
        rows = conn.execute(
            """
            SELECT v.*, p.number AS number
              FROM versions v
              JOIN procedures p ON p.id = v.procedure_id
             WHERE v.procedure_id = ?
             ORDER BY v.revision
            """, (proc["id"],)).fetchall()
        parent_rev: dict[int, int] = {}
        if rows:
            ids = [r["supersedes_id"] for r in rows if r["supersedes_id"] is not None]
            if ids:
                placeholders = ",".join("?" * len(ids))
                for prow in conn.execute(
                        f"SELECT id, revision FROM versions WHERE id IN ({placeholders})",
                        ids):
                    parent_rev[prow["id"]] = prow["revision"]
        # 撤回版本与撤回时刻对审计可见（撤回只追加事件、版本行仍在）。
        withdrawn = _withdrawn_map(conn, [r["id"] for r in rows])
        items = [_serialize(r, parent_rev, withdrawn) for r in rows]
        # 审计视图同时给出每个版本各自携带的前置规程编号列表（随版本留档，
        # 不改写历史）；回溯接口输出约定保持不变、不含该字段。
        prereq_map = _prerequisites_map(conn, [r["id"] for r in rows])
        for row, out in zip(rows, items):
            out["prerequisites"] = prereq_map.get(row["id"], [])
    return {"number": number, "count": len(items), "items": items}


# ---------------------------------------------------------------- 两知悉时刻区间对比


def _merge_adjacent(segments: list[tuple[str, str, int | None, int | None]]
                    ) -> list[tuple[str, str, int | None, int | None]]:
    """合并首尾相接且两侧版本完全相同的相邻片段（区间均为左闭右开）。"""
    merged: list[tuple[str, str, int | None, int | None]] = []
    for seg in segments:
        s, e, left, right = seg
        if merged and merged[-1][1] == s and merged[-1][2:] == (left, right):
            merged[-1] = (merged[-1][0], e, left, right)
        else:
            merged.append(seg)
    return merged


def compare_interval(number_raw: str,
                     known_before_raw: str,
                     known_after_raw: str,
                     work_start_raw: str,
                     work_end_raw: str) -> dict:
    """比较同一规程在先后两个知悉时刻、对作业区间 [work_start, work_end) 的
    实际适用内容，逐段返回两侧可见版本不同的最大片段。

    各侧状态按发布时间（published_at <= 知悉时刻）沿更正链还原：
    覆盖同一作业时刻的候选版本中，链上最深者为该时刻的有效版本；
    无任何可见版本覆盖时该侧记为空（revision/body 均为 null）。
    """
    # 入参全部校验通过后再查库；任何非法输入均只读、不写历史。
    number = number_raw.strip()
    try:
        work_start = parse_canonical(work_start_raw, "work_start")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_start"})
    try:
        work_end = parse_canonical(work_end_raw, "work_end")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "work_end"})
    if not work_start < work_end:
        raise errors.invalid_interval(
            work_start, work_end,
            "作业区间非法：work_start 必须严格早于 work_end（区间为 [起, 止) 且长度大于 0）")
    try:
        known_before = parse_canonical(known_before_raw, "known_before")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "known_before"})
    try:
        known_after = parse_canonical(known_after_raw, "known_after")
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422,
                          {"field": "known_after"})
    if not known_before < known_after:
        raise errors.invalid_known_order(known_before, known_after)

    with read_conn() as conn:
        proc = _fetch_procedure(conn, number) if number else None
        if proc is None:
            raise DomainError("not_found", f"规程编号 {number or '(空)'} 不存在",
                              404, {"number": number})

        before_rows = _visible_versions(conn, proc["id"], known_before)
        after_rows = _visible_versions(conn, proc["id"], known_after)

        # 任一版本（含对某侧不可见的版本）的区间端点都可能是有效正文切换点，
        # 故用两侧并集的全部端点切分作业区间；片段内部两侧版本各自恒定。
        all_rows = {v["id"]: v for v in before_rows}
        for v in after_rows:
            all_rows.setdefault(v["id"], v)
        depth_cache: dict[int, int] = {}

        bounds = {work_start, work_end}
        for v in all_rows.values():
            if v["valid_end"] <= work_start or work_end <= v["valid_start"]:
                continue                        # 与作业区间不相交
            bounds.add(max(v["valid_start"], work_start))
            bounds.add(min(v["valid_end"], work_end))
        ordered = sorted(bounds)

        raw: list[tuple[str, str, int | None, int | None]] = []
        for s, e in zip(ordered, ordered[1:]):
            lv = _effective_at(s, before_rows, all_rows, depth_cache)
            rv = _effective_at(s, after_rows, all_rows, depth_cache)
            raw.append((s, e, lv["id"] if lv is not None else None,
                        rv["id"] if rv is not None else None))

        # 合并相邻且两侧版本相同的片段，再剔除两侧版本相同（含均为空）的片段。
        segments: list[dict] = []
        for s, e, lid, rid in _merge_adjacent(raw):
            if lid == rid:
                continue
            lv = all_rows.get(lid) if lid is not None else None
            rv = all_rows.get(rid) if rid is not None else None
            segments.append({
                "start": s,
                "end": e,
                "before": None if lv is None else {
                    "revision": lv["revision"], "body": lv["body"]},
                "after": None if rv is None else {
                    "revision": rv["revision"], "body": rv["body"]},
            })

    return {"number": number,
            "known_before": known_before,
            "known_after": known_after,
            "work_start": work_start,
            "work_end": work_end,
            "count": len(segments),
            "segments": segments}


# ---------------------------------------------------------------- 作业规程包


def _package_time(value: str, field: str) -> str:
    """解析固定/复核入参中的时刻（必须带时区）。"""
    try:
        return parse_canonical(value, field)
    except TimeParseError as exc:
        raise DomainError("invalid_time", str(exc), 422, {"field": field})


def create_package(work_time_raw: str, known_as_of_raw: str,
                   numbers: list) -> dict:
    """按作业时刻、知悉时刻与一组规程编号固定作业规程包。

    整体语义（任一不满足即**整体拒绝、不留包**）：

    * 编号列表非空且不得重复（空列表在请求模型层 422，重复 422
      duplicate_package_number）；
    * 每个编号都必须存在（404 procedure_not_found）；
    * 在包知悉时刻下、对作业时刻，每个规程都必须有已发布且生效（未撤回）
      的适用正文（沿用左闭右开与撤回边界），否则 409 no_applicable_version；
    * 知悉时刻不得晚于服务端当前时刻（422 known_as_of_in_future）。

    全部校验与快照写入在同一条 BEGIN IMMEDIATE 事务内完成，读取的是该
    事务的一致快照，不会拼接并发写入前后的结果；失败即回滚、不留包。
    """
    work_time = _package_time(work_time_raw, "work_time")
    known_as_of = _package_time(known_as_of_raw, "known_as_of")

    cleaned: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(numbers):
        number = raw.strip() if isinstance(raw, str) else ""
        if not number:
            raise DomainError("invalid_number", "规程编号不能为空字符串",
                              422, {"field": "numbers", "index": i})
        if number in seen:
            raise errors.duplicate_package_number(number, i)
        seen.add(number)
        cleaned.append(number)

    with write_tx() as conn:
        # 服务端当前时刻取自事务内：知悉时刻晚于该时刻即视为"未来"而拒绝，
        # 固定时刻 created_at 同样由服务端单调时钟记录、接口不接受回填。
        server_now = _server_now()
        if known_as_of > server_now:
            raise errors.known_as_of_in_future(known_as_of, server_now)

        resolved: list[tuple[int, dict]] = []
        for i, number in enumerate(cleaned):
            proc = _fetch_procedure(conn, number)
            if proc is None:
                raise errors.package_procedure_not_found(number, i)
            best, parent_rev = _resolve_effective(
                conn, proc, work_time, known_as_of)
            if best is None:
                raise errors.no_applicable_version(
                    number, work_time, known_as_of, i)
            payload = _version_payload(best, number, parent_rev)
            resolved.append((proc["id"], payload))

        package_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO packages (id, work_time, known_as_of, created_at, "
            "item_count) VALUES (?, ?, ?, ?, ?)",
            (package_id, work_time, known_as_of, server_now, len(resolved)))
        for position, (proc_id, payload) in enumerate(resolved):
            conn.execute(
                "INSERT INTO package_items (package_id, position, procedure_id, "
                "number, revision, body, valid_start, valid_end, "
                "supersedes_revision, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (package_id, position, proc_id, payload["number"],
                 payload["revision"], payload["body"], payload["valid_start"],
                 payload["valid_end"], payload["supersedes_revision"],
                 payload["published_at"]))

    return _package_response(package_id, work_time, known_as_of, server_now,
                             [payload for _, payload in resolved])


def _package_rows(conn: sqlite3.Connection,
                  package_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    pkg = conn.execute("SELECT * FROM packages WHERE id = ?",
                       (package_id,)).fetchone()
    if pkg is None:
        raise errors.package_not_found(package_id)
    items = conn.execute(
        "SELECT * FROM package_items WHERE package_id = ? ORDER BY position",
        (package_id,)).fetchall()
    return pkg, items


def _item_payload(row: sqlite3.Row) -> dict:
    return {"number": row["number"], "revision": row["revision"],
            "body": row["body"], "valid_start": row["valid_start"],
            "valid_end": row["valid_end"],
            "supersedes_revision": row["supersedes_revision"],
            "published_at": row["published_at"]}


def _package_response(package_id: str, work_time: str, known_as_of: str,
                      created_at: str, items: list[dict]) -> dict:
    return {"id": package_id, "work_time": work_time,
            "known_as_of": known_as_of, "created_at": created_at,
            "count": len(items), "items": items}


def get_package(package_id_raw: str) -> dict:
    """按标识取回包内容：永远返回固定时留下的快照（含正文），不受之后的
    更正、撤回影响。包不存在 -> 404 package_not_found。
    """
    package_id = package_id_raw.strip()
    with read_snapshot() as conn:
        pkg, rows = _package_rows(conn, package_id)
        items = [_item_payload(r) for r in rows]
        return _package_response(pkg["id"], pkg["work_time"],
                                 pkg["known_as_of"], pkg["created_at"], items)


def review_package(package_id_raw: str, review_known_raw: str) -> dict:
    """复核作业规程包：以复核知悉时刻按原作业时刻逐项核对包内版本与彼时
    有效版本是否一致。

    复核知悉时刻必须不早于包内知悉时刻、且不晚于服务端当前时刻；
    包不存在 -> 404。包读取与对每个规程的版本解析在同一只读快照事务内
    完成，保证"整体是否仍一致"基于同一数据版本、不拼接并发写入。
    """
    package_id = package_id_raw.strip()
    review_known = _package_time(review_known_raw, "known_as_of")

    with read_snapshot() as conn:
        # 只读路径必须使用不触碰写状态/写锁的墙上时钟：写路径的 _server_now
        # 依赖共享单调状态，在本读事务持有快照期间若并发写事务获取写锁，会令
        # 本连接的读事务被隐式结束、从而破坏快照隔离。读侧取"当前时刻"与
        # /effective 缺省知悉时刻一致，直接读墙上时钟即可。
        server_now = now_canonical()
        if review_known > server_now:
            raise errors.known_as_of_in_future(review_known, server_now)

        pkg, pinned_rows = _package_rows(conn, package_id)
        package_known = pkg["known_as_of"]
        work_time = pkg["work_time"]
        if review_known < package_known:
            raise errors.review_known_too_early(review_known, package_known)

        items: list[dict] = []
        all_match = True
        for row in pinned_rows:
            proc = _fetch_procedure_by_id(conn, row["procedure_id"])
            best, parent_rev = (None, None)
            if proc is not None:
                best, parent_rev = _resolve_effective(
                    conn, proc, work_time, review_known)
            current = (None if best is None
                       else _version_payload(best, row["number"], parent_rev))
            pinned = _item_payload(row)
            # 是否变化只看版本号（与两知悉时刻对比约定一致）；规程在复核时刻
            # 已无任何适用正文（current 为 null）也视为发生了变化。
            changed = current is None or current["revision"] != row["revision"]
            if changed:
                all_match = False
            items.append({"number": row["number"], "pinned": pinned,
                          "current": current, "changed": changed})

    return {"id": package_id, "work_time": work_time,
            "package_known_as_of": package_known,
            "known_as_of": review_known,
            "created_at": pkg["created_at"],
            "count": len(items),
            "all_match": all_match,
            "items": items}
