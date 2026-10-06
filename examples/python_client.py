#!/usr/bin/env python3
"""调用示例：仅依赖标准库，直接运行即可走一遍完整业务场景。

前置：服务已在本机启动（docker compose up --build 或 uvicorn app.main:app）。
用法：python examples/python_client.py [BASE_URL]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    number = "SOP-DEMO-001"

    # 1) 首次发布：全年适用的初版正文
    status, res = call("POST", "/api/v1/releases", {
        "number": number,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    show("1. 首次发布", status, res)

    # 2) 更正：仅覆盖 3 月这一子区间；发布时间由服务端记录
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。",
    })
    show("2. 提交更正（子区间）", status, res)
    published_r1 = res["items"][0]["published_at"]

    # 3) 查询：2 月（更正区间之外）-> 仍是初版
    status, res = call("GET", "/api/v1/effective?work_time=2026-02-15T08:00:00Z")
    show("3. 作业时刻 2026-02-15（更正区间外）", status, res)

    # 4) 查询：3 月（更正区间之内）-> 更正正文
    status, res = call("GET", "/api/v1/effective?work_time=2026-03-15T08:00:00Z")
    show("4. 作业时刻 2026-03-15（更正区间内）", status, res)

    # 5) 回溯：知悉时刻取 2025 年（任何版本都尚未发布）-> 空结果
    status, res = call("GET",
                       "/api/v1/effective?work_time=2026-03-15T08:00:00Z"
                       "&known_as_of=2025-06-01T00:00:00Z")
    show("5. 知悉时刻早于发布 -> 空", status, res)

    # 6a) 顺序请求，却与"已发布"更正区间重叠 -> 409 过期基版，历史不变
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-20T00:00:00Z",
        "valid_end": "2026-03-25T00:00:00Z",
        "body": "依据了旧版 r0 的已失效部分，必须被拒绝。",
    })
    show("6a. 过期基版（顺序重叠已发布更正，整体拒绝）", status, res)

    # 6b) 同一批次内两条对 r0 的并发更正相互重叠 -> 409，整批回滚
    status, res = call("POST", "/api/v1/corrections", {"items": [
        {"number": number, "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z",
         "valid_end": "2026-05-20T00:00:00Z", "body": "并发更正 A"},
        {"number": number, "base_revision": 0,
         "valid_start": "2026-05-10T00:00:00Z",
         "valid_end": "2026-05-30T00:00:00Z", "body": "并发更正 B（与 A 冲突）"},
    ]})
    show("6b. 并发更正冲突（同批重叠，整批回滚）", status, res)

    # 6c) 试图回填发布时间：额外字段被拒绝（published_at 只由服务端记录）
    status, res = call("POST", "/api/v1/releases", {
        "number": number,
        "body": "重复编号且回填发布时间",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "published_at": "2000-01-01T00:00:00Z",
    })
    show("6c. 回填发布时间/重复编号被拒绝", status, res)

    # 7) 以 r1 为基版做"更正之更正"（链式）
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 1,
        "valid_start": "2026-03-10T00:00:00Z",
        "valid_end": "2026-03-20T00:00:00Z",
        "body": "再更正：3 月 10-20 日改为全套防割装备。",
    })
    show("7. 链式更正 r1 -> r2", status, res)

    status, res = call("GET",
                       "/api/v1/procedures/" + number + "/history")
    show("8. 版本历史", status, res)

    # 9) 两知悉时刻作业区间对比（见文件下方 _diff_examples）
    _diff_examples(number, published_r1)

    # 10) 撤回误发布的更正（独立规程演示，见 _withdrawal_examples）
    _withdrawal_examples()

    # 11) 作业规程包的固定、取回与复核（独立规程演示，见 _package_examples）
    _package_examples()

    # 12) 前置规程（本轮新增）：携带列表发布/更正，递归解析前置清单
    _prerequisite_examples()

    # 13) 跨时段前置规程清单：逐片段判断全作业区间的持续可用性
    _closure_plan_examples()

    # 14) 更正批量预演（本轮新增）：只读，不落库、不占版本号
    _correction_preview_examples()

    # 15) 值班审计变动日志：游标增量拉取（从起始游标读到尾）
    _audit_feed_examples()

    # 16) 跨规程版本覆盖检查（本轮新增）：调度员安排跨规程作业前核对版本覆盖
    _coverage_examples()


def _coverage_examples() -> None:
    """跨规程版本覆盖检查：逐段返回各编号有效版本/缺口与切换、恢复标记。"""
    na, nb = "SOP-DEMO-COV-1", "SOP-DEMO-COV-2"

    # 16.1) 准备：两个规程全年适用；COV-1 在 3 月有一条更正
    call("POST", "/api/v1/releases", {
        "number": na, "body": "初版正文：动火作业须办理动火票。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z"})
    call("POST", "/api/v1/corrections", {
        "number": na, "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期动火须双人监护。"})
    # 知悉时刻取最后一笔写入的服务端发布时刻（此前提交全部可见）
    _, res = call("POST", "/api/v1/releases", {
        "number": nb, "body": "初版正文：进入装置区必须佩戴安全帽。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z"})
    known = res["published_at"]

    # 16.2) 覆盖检查：全体端点把全年切成 3 段，3 月段 COV-1 切到 r1，
    #       逐项 switched 与片段 version_switch 标出版本切换
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": [na, nb],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "known_as_of": known})
    show("16.2 覆盖检查（3 段，3 月段标版本切换）", status, res)

    # 16.3) 撤回 3 月更正后再查：3 月段恢复父版本 r0，
    #       标 restored_by_withdrawal（恢复边界不算版本切换）。
    #       知悉时刻取撤回之后的当前时刻（边界含等号：恰好等于撤回时刻时
    #       被撤回版本仍可见）。
    call("POST", "/api/v1/withdrawals", {"number": na, "revision": 1})
    from datetime import datetime, timezone
    known2 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": [na, nb],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "known_as_of": known2})
    show("16.3 撤回后再查（恢复片段标 restored_by_withdrawal）", status, res)

    # 16.4) 区间外空档：COV-3 只覆盖上半年，下半年片段 gap=true、
    #       片段 has_gap=true、顶层 fully_covered=false
    _, res = call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-COV-3", "body": "仅上半年有效的临时规程。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2026-07-01T00:00:00Z"})
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": ["SOP-DEMO-COV-3"],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "known_as_of": res["published_at"]})
    show("16.4 区间外空档（下半年 gap，fully_covered=false）", status, res)

    # 16.5) 拒绝情形（均只读、不写历史）
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": [na, na],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "known_as_of": known2})
    show("16.5a 编号重复 -> 422 duplicate_coverage_number", status, res)
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": [na, "SOP-DEMO-COV-NOPE"],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "known_as_of": known2})
    show("16.5b 编号不存在 -> 404 procedure_not_found", status, res)
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": [na],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "known_as_of": "2999-01-01T00:00:00Z"})
    show("16.5c 知悉时刻晚于当前 -> 422 known_as_of_in_future", status, res)
    status, res = call("POST", "/api/v1/coverage/check", {
        "numbers": [na],
        "work_start": "2026-03-01T00:00:00Z",
        "work_end": "2026-03-01T00:00:00Z",
        "known_as_of": known2})
    show("16.5d 空作业区间 -> 422 invalid_interval", status, res)


def _audit_feed_examples() -> None:
    """值班审计变动日志：按提交顺序游标分页，批次不跨页。"""
    import urllib.parse

    audit_root = "SOP-DEMO-AUDIT-001"

    # 15.1) 制造若干提交：首版（单事件批次）+ 批量发布（一个多事件批次，
    #       事件按请求顺序）+ 更正 + 撤回
    call("POST", "/api/v1/releases", {
        "number": audit_root, "body": "审计演示初版。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z"})
    call("POST", "/api/v1/releases", {"items": [
        {"number": "SOP-DEMO-AUDIT-002", "body": "批量发布 A。",
         "valid_start": "2026-01-01T00:00:00Z",
         "valid_end": "2027-01-01T00:00:00Z"},
        {"number": "SOP-DEMO-AUDIT-003", "body": "批量发布 B。",
         "valid_start": "2026-01-01T00:00:00Z",
         "valid_end": "2027-01-01T00:00:00Z"},
    ]})
    call("POST", "/api/v1/corrections", {
        "number": audit_root, "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "审计演示 3 月更正。"})
    call("POST", "/api/v1/withdrawals", {"number": audit_root, "revision": 1})

    # 15.2) 从起始游标开始整页拉取：每页 limit 个事件；同一提交批次不拆开，
    #       next_cursor 只随已完整交付的批次前进。
    q = urllib.parse.urlencode({"cursor": "0", "limit": 50})
    status, res = call("GET", f"/api/v1/audit/events?{q}")
    show("15.2 审计变动日志（起始游标，一页取全部演示事件）", status,
         {"cursor": res["cursor"], "next_cursor": res["next_cursor"],
          "has_more": res["has_more"], "count": res["count"],
          "events": [{"operation": e["operation"], "number": e["number"],
                      "revision": e["revision"], "event_time": e["event_time"],
                      "batch_seq": e["batch_seq"], "position": e["position"]}
                     for e in res["events"]]})
    next_cursor = res["next_cursor"]

    # 15.3) 持尾游标再读：空页，next_cursor 与游标相同、has_more=false
    q = urllib.parse.urlencode({"cursor": next_cursor, "limit": 50})
    status, res = call("GET", f"/api/v1/audit/events?{q}")
    show("15.3 尾游标再读 -> 空页（游标保持不变）", status, res)

    # 15.4) 每页上限装不下下一完整批次 -> 422 page_limit_too_small，
    #       游标不前进；调大 limit 后以同一游标重试即可。
    #       先找到批量发布批次（2 事件）之前的游标位点。
    status, full = call("GET", "/api/v1/audit/events?limit=100")
    batch_seqs = {e["batch_seq"] for e in full["events"]}
    # 批量发布是其中唯一含 2 个事件的批次
    sizes = {bs: sum(1 for e in full["events"] if e["batch_seq"] == bs)
             for bs in batch_seqs}
    two_event_batch = next(bs for bs, n in sizes.items() if n == 2)
    prior_cursor = str(two_event_batch - 1)
    q = urllib.parse.urlencode({"cursor": prior_cursor, "limit": 1})
    status, res = call("GET", f"/api/v1/audit/events?{q}")
    show("15.4a limit=1 装不下 2 事件批次 -> 422 page_limit_too_small",
         status, res)
    q = urllib.parse.urlencode({"cursor": prior_cursor, "limit": 10})
    status, res = call("GET", f"/api/v1/audit/events?{q}")
    show("15.4b 同一游标调大 limit -> 整批取回（批次内按请求顺序）", status,
         [e for e in res["events"] if e["batch_seq"] == two_event_batch])

    # 15.5) 非法游标 / 非法上限
    status, res = call("GET", "/api/v1/audit/events?cursor=not-a-number")
    show("15.5a 非法游标 -> 422 invalid_cursor", status, res)
    status, res = call("GET", "/api/v1/audit/events?cursor=99999999")
    show("15.5b 游标不指向任何已提交批次 -> 422 invalid_cursor", status, res)
    status, res = call("GET", "/api/v1/audit/events?limit=0")
    show("15.5c limit=0 -> 422 invalid_page_limit", status, res)
    status, res = call("GET", "/api/v1/audit/events?limit=1001")
    show("15.5d limit=1001 -> 422 invalid_page_limit", status, res)


def _correction_preview_examples() -> None:
    """更正批量预演：提交前预演整批结果，只读、不写版本、不占版本号。"""
    root = "SOP-DEMO-PREVIEW-001"
    other = "SOP-DEMO-PREVIEW-002"

    call("POST", "/api/v1/releases", {
        "number": root,
        "body": "根规程初版：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-DEMO-PRE-101"],   # 已在 12 步发布
    })
    call("POST", "/api/v1/releases", {
        "number": other,
        "body": "另一规程初版：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })

    # 14.1) 批量预演：先 r0->r1（3 月），再以同批拟生成的 r1 为基版 r1->r2
    #       （3 月 10-20 日）；另一规程 5 月更正并显式清空前置。
    status, res = call("POST", "/api/v1/corrections/preview", {
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "items": [
            {"number": root, "base_revision": 0,
             "valid_start": "2026-03-01T00:00:00Z",
             "valid_end": "2026-04-01T00:00:00Z",
             "body": "三月更正：检修期加派监护。"},                # 省略 -> 继承前置
            {"number": root, "base_revision": 1,
             "valid_start": "2026-03-10T00:00:00Z",
             "valid_end": "2026-03-20T00:00:00Z",
             "body": "对拟生成 r1 的再更正：全套防割装备。"},      # 后项引用前项
            {"number": other, "base_revision": 0,
             "valid_start": "2026-05-01T00:00:00Z",
             "valid_end": "2026-06-01T00:00:00Z",
             "body": "五月更正。", "prerequisites": []},           # 显式清空
        ],
    })
    show("14.1 批量预演（拟生成版本号 + 最终前置清单 + 提交前后最大片段）",
         status, res)

    # 14.2) 预演只读：版本历史仍只有 r0，版本号未被预演占用
    status, hist = call("GET", f"/api/v1/procedures/{root}/history")
    show("14.2 预演后历史不变（仍只有 r0，r1/r2 未占用）", status,
         [v["revision"] for v in hist["items"]])

    # 14.3) 单条对象形式：作业区间随条目一起给出
    status, res = call("POST", "/api/v1/corrections/preview", {
        "number": root, "base_revision": 0,
        "valid_start": "2026-07-01T00:00:00Z",
        "valid_end": "2026-08-01T00:00:00Z",
        "body": "七月更正。",
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    show("14.3 单条对象形式预演", status, res)

    # 14.4) 任一项非法：指出位置（details.index=1）与现有错误原因，不返回部分结果
    status, res = call("POST", "/api/v1/corrections/preview", {
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "items": [
            {"number": root, "base_revision": 0,
             "valid_start": "2026-09-01T00:00:00Z",
             "valid_end": "2026-10-01T00:00:00Z", "body": "第 1 条合法"},
            {"number": "SOP-DEMO-PREVIEW-GONE", "base_revision": 0,
             "valid_start": "2026-09-01T00:00:00Z",
             "valid_end": "2026-10-01T00:00:00Z", "body": "第 2 条编号不存在"},
        ],
    })
    show("14.4a 第 2 条编号不存在 -> 404 not_found（index=1）", status, res)

    # 14.5) 同批对同一基版的子区间重叠 -> 409，与正式提交同码同因
    status, res = call("POST", "/api/v1/corrections/preview", {
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "items": [
            {"number": root, "base_revision": 0,
             "valid_start": "2026-09-01T00:00:00Z",
             "valid_end": "2026-09-20T00:00:00Z", "body": "并发更正 A"},
            {"number": root, "base_revision": 0,
             "valid_start": "2026-09-10T00:00:00Z",
             "valid_end": "2026-09-30T00:00:00Z", "body": "并发更正 B（冲突）"},
        ],
    })
    show("14.4b 同批重叠 -> 409 concurrent_correction_conflict（index=1）",
         status, res)

    # 14.6) 非法作业区间 / 缺字段 / 回填发布时刻
    status, res = call("POST", "/api/v1/corrections/preview", {
        "work_start": "2026-05-01T00:00:00Z",
        "work_end": "2026-05-01T00:00:00Z",
        "items": [{"number": root, "base_revision": 0,
                   "valid_start": "2026-09-01T00:00:00Z",
                   "valid_end": "2026-10-01T00:00:00Z", "body": "x"}]})
    show("14.5a 空作业区间 -> 422 invalid_interval", status, res)
    status, res = call("POST", "/api/v1/corrections/preview", {
        "items": [{"number": root, "base_revision": 0,
                   "valid_start": "2026-09-01T00:00:00Z",
                   "valid_end": "2026-10-01T00:00:00Z", "body": "x"}]})
    show("14.5b 缺少作业区间字段 -> 422 validation_error", status, res)
    status, res = call("POST", "/api/v1/corrections/preview", {
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "items": [{"number": root, "base_revision": 0,
                   "valid_start": "2026-09-01T00:00:00Z",
                   "valid_end": "2026-10-01T00:00:00Z", "body": "x",
                   "published_at": "2000-01-01T00:00:00Z"}]})
    show("14.5c 回填发布时刻（额外字段）-> 422 validation_error", status, res)

    # 14.7) 预演结果不保证可提交：先对一个空闲区间预演成功，随后他人抢先
    #       占用同一区间，正式提交仍按提交时状态重新校验而失败。
    win = {"valid_start": "2026-11-01T00:00:00Z",
           "valid_end": "2026-12-01T00:00:00Z"}
    status, preview = call("POST", "/api/v1/corrections/preview", {
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
        "items": [{"number": root, "base_revision": 0, **win,
                   "body": "预演时该区间空闲，拟生成新版本。"}]})
    show("14.6a 预演成功（拟生成版本号，只反映预演当时快照）", status,
         {"revision": preview["items"][0]["revision"]} if status == 200 else preview)
    call("POST", "/api/v1/corrections", {
        "number": root, "base_revision": 0, **win,
        "body": "他人抢先发布的十一月更正。"})
    status, res = call("POST", "/api/v1/corrections", {
        "number": root, "base_revision": 0, **win,
        "body": "与预演相同的载荷；正式提交时该区间已被占用。"})
    show("14.6b 正式提交仍按当时状态校验 -> 409 expired_base_version",
         status, res)



def _withdrawal_examples() -> None:
    """撤回示例：撤回只追加事件，撤回前历史不变，撤回后区间恢复父版本正文。"""
    number = "SOP-DEMO-WD-001"

    # 10.1) 发布首版 + 一条覆盖 3 月的更正
    call("POST", "/api/v1/releases", {
        "number": number,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    published = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "误发布的更正正文。",
    })[1]["items"][0]["published_at"]

    # 10.2) 撤回 r1：只提交编号与更正版本号；withdrawn_at 由服务端记录
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 1})
    show("10.2 撤回误发布更正 r1（withdrawn_at 服务端记录）", status, res)
    withdrawn_at = res["items"][0]["withdrawn_at"]

    # 10.3) 撤回后回溯：3 月区间恢复父版本 r0 正文
    status, res = call("GET",
                       "/api/v1/effective?work_time=2026-03-15T08:00:00Z")
    show("10.3 撤回后回溯（3 月恢复初版）", status,
         [i for i in res["items"] if i["number"] == number])

    # 10.4) 撤回前知悉时刻的回溯仍按原版本链 -> 仍是 r1（历史不变）
    status, res = call(
        "GET",
        "/api/v1/effective?work_time=2026-03-15T08:00:00Z"
        f"&known_as_of={published}")
    show("10.4 以撤回前知悉时刻回溯（仍看到被撤回的 r1）", status,
         [i for i in res["items"] if i["number"] == number])

    # 10.5) 历史审计：r1 行仍在，且带 withdrawn_at
    status, res = call("GET", f"/api/v1/procedures/{number}/history")
    show("10.5 版本历史（r1 仍在并带 withdrawn_at）", status, res)

    # 10.6) 撤回后父版本可在恢复的区间再次更正
    status, res = call("POST", "/api/v1/corrections", {
        "number": number,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "重新发布的正确更正正文。",
    })
    show("10.6 在恢复区间以 r0 为基版再次更正（r2）", status, res)

    # 10.7) 各类拒绝：重复撤回 / 撤首版 / 版本不存在 / 回填撤回时刻
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 1})
    show("10.7a 重复撤回 -> 409 already_withdrawn", status, res)
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 0})
    show("10.7b 撤回首版 -> 409 first_revision_not_withdrawable", status, res)
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 99})
    show("10.7c 版本号不存在 -> 404 revision_not_found", status, res)
    status, res = call("POST", "/api/v1/withdrawals",
                       {"number": number, "revision": 2,
                        "withdrawn_at": "2000-01-01T00:00:00Z"})
    show("10.7d 回填撤回时刻 -> 422 validation_error", status, res)

    # 10.8) 两知悉时刻对比：撤回前一刻 vs 撤回后一刻，3 月段 r1 -> r0
    import urllib.parse
    from datetime import datetime, timedelta, timezone

    def us_iso(canonical: str, delta_microseconds: int) -> str:
        dt = datetime.strptime(canonical, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        return (dt + timedelta(microseconds=delta_microseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    q = urllib.parse.urlencode({
        "known_before": published,                      # 撤回前：r1 可见
        "known_after": us_iso(withdrawn_at, 1),         # 撤回后：恢复 r0
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("10.8 撤回前后两知悉时刻对比（仅 3 月段 r1→r0）", status, res)


def _package_examples() -> None:
    """作业规程包：开工前固定、原样取回、之后更正/撤回下的复核。"""
    import time
    from datetime import datetime, timezone

    def utc_now_iso() -> str:
        # 停顿 1 秒，保证知悉时刻严格晚于紧接着的发布时刻（服务端记录）
        time.sleep(1)
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    n1, n2 = "SOP-DEMO-PKG-001", "SOP-DEMO-PKG-002"
    call("POST", "/api/v1/releases", {
        "number": n1,
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    call("POST", "/api/v1/releases", {
        "number": n2,
        "body": "另一规程初版：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })

    # 11.1) 固定作业规程包：作业时刻 + 知悉时刻 + 编号列表
    known_fix = utc_now_iso()
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1, n2],
    })
    show("11.1 固定作业规程包（201，返回标识/版本/正文/两种时刻）", status, res)
    package_id = res["id"]
    fixed_revision = res["items"][0]["revision"]

    # 11.2) 按标识原样取回
    status, res = call("GET", f"/api/v1/packages/{package_id}")
    show("11.2 按标识取回（固定时原样）", status, res)

    # 11.3) 固定之后对 n1 发布 3 月更正
    status, res = call("POST", "/api/v1/corrections", {
        "number": n1,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。",
    })
    show("11.3 固定后再更正 n1", status, res)

    # 11.4) 再次取回：包内容保持固定时原样（仍是初版 r0）
    status, res = call("GET", f"/api/v1/packages/{package_id}")
    show("11.4 取回包（后续更正不影响包快照）", status, res)
    assert res["items"][0]["revision"] == fixed_revision

    # 11.5) 复核：以当前时刻为复核知悉时刻 -> n1 changed，整体 all_match=false
    status, res = call(
        "GET", f"/api/v1/packages/{package_id}/review"
               f"?known_as_of={utc_now_iso()}")
    show("11.5 复核（n1 包内 r0、彼时 r1，整体不再一致）", status, res)

    # 11.6) 以包内知悉时刻本身复核（含等号，不早于即可）-> 完全一致
    status, res = call(
        "GET", f"/api/v1/packages/{package_id}/review"
               f"?known_as_of={known_fix}")
    show("11.6 以包内知悉时刻复核（all_match=true）", status, res)

    # 11.7) 各类拒绝
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1, n1]})
    show("11.7a 编号重复 -> 422 duplicate_package_number", status, res)
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1, "NO-SUCH"]})
    show("11.7b 编号缺失 -> 404 procedure_not_found", status, res)
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2027-06-01T08:00:00Z",
        "known_as_of": known_fix,
        "numbers": [n1]})
    show("11.7c 作业时刻无适用正文 -> 409 no_applicable_version", status, res)
    status, res = call("POST", "/api/v1/packages", {
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": "2030-01-01T00:00:00Z",
        "numbers": [n1]})
    show("11.7d 知悉时刻在未来 -> 422 known_as_of_in_future", status, res)
    status, res = call(
        "GET", "/api/v1/packages/deadbeefdeadbeefdeadbeefdeadbeef"
               "/review?known_as_of=" + known_fix)
    show("11.7e 包不存在 -> 404 package_not_found", status, res)
    status, res = call(
        "GET", f"/api/v1/packages/{package_id}/review"
               "?known_as_of=2025-01-01T00:00:00Z")
    show("11.7f 复核知悉时刻早于包内知悉时刻 -> 422 review_known_too_early",
         status, res)


def _prerequisite_examples() -> None:
    """前置规程：发布/更正携带前置编号列表，递归解析根及前置规程清单。"""
    pre1, pre2 = "SOP-DEMO-PRE-101", "SOP-DEMO-PRE-102"
    root = "SOP-DEMO-PRE-001"

    # 12.1) 先发布两份前置规程（也可先引用、后发布，见 12.6）
    call("POST", "/api/v1/releases", {
        "number": pre1,
        "body": "前置规程：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    call("POST", "/api/v1/releases", {
        "number": pre2,
        "body": "前置规程：进入受限空间须先通风检测。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })

    # 12.2) 首次发布根规程并携带前置编号列表（响应回显落库后的列表）
    status, res = call("POST", "/api/v1/releases", {
        "number": root,
        "body": "根规程初版：检修作业总体要求。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [pre1, pre2],
    })
    show("12.2 首次发布携带前置规程编号列表", status, res)

    # 12.3) 更正省略 prerequisites -> 继承被替代版本 r0 的列表
    status, res = call("POST", "/api/v1/corrections", {
        "number": root,
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "根规程 3 月更正：检修期加派监护。",
    })
    show("12.3 更正省略前置列表 -> 继承被替代版本", status, res)

    # 12.4) 递归解析：前置优先、去重、根在最后，含编号/版本/正文
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.4 前置规程清单（3 月作业，根取更正版 r1）", status, res)

    # 12.5) 显式空列表清空：4 月更正不再携带前置 -> 清单只剩根本身
    call("POST", "/api/v1/corrections", {
        "number": root,
        "base_revision": 0,
        "valid_start": "2026-04-01T00:00:00Z",
        "valid_end": "2026-05-01T00:00:00Z",
        "body": "根规程 4 月更正：不再引用前置规程。",
        "prerequisites": [],
    })
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure"
               "?work_time=2026-04-15T08:00:00Z")
    show("12.5 显式空列表清空（4 月作业，清单只剩根）", status, res)

    # 12.6) 前置可晚于引用者发布：先引用后发布，解析从缺口变为成功
    late_root, late_pre = "SOP-DEMO-PRE-002", "SOP-DEMO-PRE-103"
    call("POST", "/api/v1/releases", {
        "number": late_root,
        "body": "引用尚未发布的前置规程。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [late_pre],
    })
    status, res = call(
        "GET", f"/api/v1/procedures/{late_root}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.6a 前置尚未发布 -> 404 prerequisite_not_found（带引用链）",
         status, res)
    call("POST", "/api/v1/releases", {
        "number": late_pre,
        "body": "晚发布的前置规程正文。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    status, res = call(
        "GET", f"/api/v1/procedures/{late_root}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.6b 前置发布后 -> 解析成功", status, res)

    # 12.7) 引用成环 -> 409 prerequisite_cycle，返回环路径、不给部分清单
    cyc_a, cyc_b = "SOP-DEMO-CYC-A", "SOP-DEMO-CYC-B"
    call("POST", "/api/v1/releases", {
        "number": cyc_a, "body": "A 引用 B",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [cyc_b]})
    call("POST", "/api/v1/releases", {
        "number": cyc_b, "body": "B 引用 A",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [cyc_a]})
    status, res = call(
        "GET", f"/api/v1/procedures/{cyc_a}/closure"
               "?work_time=2026-03-15T08:00:00Z")
    show("12.7 引用成环 -> 409 prerequisite_cycle（details.cycle 为环路径）",
         status, res)

    # 12.8) 列表形状校验：重复编号 -> 422；空白编号 -> 422
    status, res = call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-PRE-003", "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [pre1, pre1]})
    show("12.8a 前置编号重复 -> 422 duplicate_prerequisite_number",
         status, res)
    status, res = call("POST", "/api/v1/releases", {
        "number": "SOP-DEMO-PRE-003", "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": ["  "]})
    show("12.8b 前置编号空白 -> 422 invalid_number", status, res)


def _closure_plan_examples() -> None:
    """跨时段前置规程清单：根及递归前置在整个作业区间的逐片段可用性。"""
    import urllib.parse

    pre1, pre2 = "SOP-DEMO-PLAN-101", "SOP-DEMO-PLAN-102"
    root = "SOP-DEMO-PLAN-001"
    call("POST", "/api/v1/releases", {
        "number": pre1, "body": "前置规程 101。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    call("POST", "/api/v1/releases", {
        "number": pre2, "body": "前置规程 102。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
    })
    call("POST", "/api/v1/releases", {
        "number": root,
        "body": "根规程初版：携带前置 101。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": [pre1],
    })
    # 3 月：根切到 r1，改带前置 102；4 月：r2 清空前置
    call("POST", "/api/v1/corrections", {
        "number": root, "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "根规程 3 月更正：改带前置 102。",
        "prerequisites": [pre2],
    })
    call("POST", "/api/v1/corrections", {
        "number": root, "base_revision": 0,
        "valid_start": "2026-04-01T00:00:00Z",
        "valid_end": "2026-05-01T00:00:00Z",
        "body": "根规程 4 月更正：清空前置。",
        "prerequisites": [],
    })

    # 13.1) 全年作业区间：逐片段给出前置优先去重清单及版本，全部可用
    q = urllib.parse.urlencode({
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{root}/closure-plan?{q}")
    show("13.1 跨时段清单（全年：r0→3月r1换前置→4月r2清空→恢复r0）",
         status, res)

    # 13.2) 收窄到 3-4 月：端点裁剪到请求区间
    q = urllib.parse.urlencode({
        "work_start": "2026-03-01T00:00:00Z",
        "work_end": "2026-05-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{root}/closure-plan?{q}")
    show("13.2 作业区间收窄到 3-5 月（边界裁剪）", status, res)

    # 13.3) 仅 3 月引用不存在编号的根规程：片段保留 + 缺口链，整体不可用
    gap_root = "SOP-DEMO-PLAN-GAP"
    call("POST", "/api/v1/releases", {
        "number": gap_root, "body": "根初版（无前置）。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z",
        "prerequisites": []})
    call("POST", "/api/v1/corrections", {
        "number": gap_root, "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end": "2026-04-01T00:00:00Z",
        "body": "3 月版本引用不存在的前置。",
        "prerequisites": ["SOP-DEMO-PLAN-MISSING"]})
    q = urllib.parse.urlencode({
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET",
                       f"/api/v1/procedures/{gap_root}/closure-plan?{q}")
    show("13.3 引用缺失（3 月片段带缺口链，HTTP 200、整体 available=false）",
         status, res)

    # 13.4) 根编号不存在：单片段保留，gaps[0].code=not_found
    q = urllib.parse.urlencode({
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call(
        "GET", "/api/v1/procedures/SOP-DEMO-PLAN-NOPE/closure-plan?" + q)
    show("13.4 根编号不存在（保留片段 + not_found 缺口链）", status, res)

    # 13.5) 引用成环：环片段保留并给出 cycle 环路径
    ca, cb = "SOP-DEMO-PLAN-CA", "SOP-DEMO-PLAN-CB"
    call("POST", "/api/v1/releases", {
        "number": ca, "body": "A",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z", "prerequisites": [cb]})
    call("POST", "/api/v1/releases", {
        "number": cb, "body": "B",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end": "2027-01-01T00:00:00Z", "prerequisites": [ca]})
    status, res = call("GET", f"/api/v1/procedures/{ca}/closure-plan?" + q)
    show("13.5 引用成环（片段保留 + cycle 环路径，整体不可用）", status, res)

    # 13.6) 非法请求：空区间 422 invalid_interval；缺时区 422 invalid_time；
    #       缺参数 422 validation_error（均只读、不写历史）
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure-plan"
               "?work_start=2026-03-01T00:00:00Z&work_end=2026-03-01T00:00:00Z")
    show("13.6a 空作业区间 -> 422 invalid_interval", status, res)
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure-plan"
               "?work_start=2026-01-01T00:00:00&work_end=2027-01-01T00:00:00Z")
    show("13.6b 缺时区 -> 422 invalid_time", status, res)
    status, res = call(
        "GET", f"/api/v1/procedures/{root}/closure-plan"
               "?work_start=2026-01-01T00:00:00Z")
    show("13.6c 缺少 work_end -> 422 validation_error", status, res)


def _diff_examples(number: str, published_r1: str) -> None:
    """对比接口示例：known_before / known_after 卡在 r1 发布时刻前后一微秒。"""
    import urllib.parse
    from datetime import datetime, timedelta, timezone

    def us_iso(canonical: str, delta_microseconds: int) -> str:
        dt = datetime.strptime(canonical, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        return (dt + timedelta(microseconds=delta_microseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    # 9a) 全年作业区间：只有 3 月这一段在两侧分别是 r0 / r1；
    #     两侧同为 r0 的相邻部分不返回
    q = urllib.parse.urlencode({
        "known_before": us_iso(published_r1, -1),
        "known_after": us_iso(published_r1, 1),
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9a. 两知悉时刻对比（全年区间，仅 3 月段 r0→r1）", status, res)

    # 9b) 前侧取很早以前（一切版本均未发布）：before=null 逐段列出。
    #     作业区间只问 2-5 月，其中 r1 还被第 7 步的 r2 切成 r1/r2/r1 三段
    q = urllib.parse.urlencode({
        "known_before": "2025-01-01T00:00:00Z",
        "known_after": "2030-01-01T00:00:00Z",
        "work_start": "2026-02-01T00:00:00Z",
        "work_end": "2026-05-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9b. 前侧无任何可见版本（before 为 null 也逐段列出）", status, res)

    # 9c) 知悉顺序颠倒 -> 422 invalid_known_order，不写历史
    q = urllib.parse.urlencode({
        "known_before": "2030-01-01T00:00:00Z",
        "known_after": "2025-01-01T00:00:00Z",
        "work_start": "2026-01-01T00:00:00Z",
        "work_end": "2027-01-01T00:00:00Z",
    })
    status, res = call("GET", f"/api/v1/procedures/{number}/diff?{q}")
    show("9c. 知悉顺序颠倒被拒绝", status, res)


if __name__ == "__main__":
    main()
