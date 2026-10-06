"""端到端测试：覆盖双时间维查询、更正裁剪与四类整体拒绝。

运行：python -m unittest discover -s tests -v
（需先安装 requirements-dev.txt，仅 API 测试需要 fastapi/httpx）
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# 每个测试用独立临时库，必须在导入 app.config 之前设置
_tmpdir = tempfile.mkdtemp(prefix="sop-test-")
os.environ["SOP_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["SOP_PORT"] = "8099"

from app import service  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.errors import DomainError  # noqa: E402
from app.time_utils import now_canonical, parse_canonical  # noqa: E402

init_db()


def canon(value: str) -> str:
    """测试辅助：把无小数秒的时间转成与存储一致的规范形式。"""
    return parse_canonical(value)


def reset_db() -> None:
    """每个用例重建空库，保证用例彼此独立、可重复运行。"""
    import sqlite3
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            "DELETE FROM audit_events; DELETE FROM audit_batches; "
            "DELETE FROM prerequisites; "
            "DELETE FROM package_items; DELETE FROM packages; "
            "DELETE FROM withdrawals; "
            "DELETE FROM versions; DELETE FROM procedures; "
            "DELETE FROM sqlite_sequence;")
        conn.commit()
    finally:
        conn.close()

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_us(dt: datetime) -> str:
    """带微秒的 UTC 串（用于服务端发布时刻边界）。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


t0 = iso(_T - timedelta(days=1))      # 知悉时刻早于发布
t1 = iso(_T)                          # 区间起点
t2 = iso(_T + timedelta(days=30))
t3 = iso(_T + timedelta(days=60))
t4 = iso(_T + timedelta(days=90))
t5 = iso(_T + timedelta(days=365))    # 区间终点（不含）
future = iso(_T + timedelta(days=400))


def item(number: str, body: str = "v0", start: str = t1, end: str = t5,
         base: int | None = None, prerequisites=None):
    data = {"number": number, "body": body,
            "valid_start": start, "valid_end": end}
    if base is not None:
        data["base_revision"] = base
    if prerequisites is not None:
        data["prerequisites"] = prerequisites
    return SimpleNamespace(**data)


def release(number: str, start: str = t1, end: str = t5, body: str = "初版",
            prerequisites=None):
    return service.publish_releases(
        [item(number, body, start, end, prerequisites=prerequisites)])


def correct(number: str, base: int, start: str, end: str, body: str = "更正",
            prerequisites=None):
    return service.submit_corrections(
        [item(number, body, start, end, base, prerequisites=prerequisites)])


def witem(number: str, revision: int):
    return SimpleNamespace(number=number, revision=revision)


def withdraw(number: str, revision: int):
    return service.withdraw_corrections([witem(number, revision)])


def numbers_at(work: str, known: str) -> dict[str, dict]:
    return {x["number"]: x for x in service.query_effective(work, known)["items"]}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        release("DUP-1")
        with self.assertRaises(DomainError) as ctx:
            release("DUP-1")
        self.assertEqual(ctx.exception.code, "duplicate_number")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_duplicate_in_same_batch(self):
        with self.assertRaises(DomainError) as ctx:
            service.publish_releases([item("DUP-BATCH"), item("DUP-BATCH")])
        self.assertEqual(ctx.exception.code, "duplicate_number")
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_invalid_intervals(self):
        # 起 >= 止
        with self.assertRaises(DomainError) as ctx:
            release("BAD-IV-1", start=t3, end=t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        # 空区间 [t1, t1)
        with self.assertRaises(DomainError):
            release("BAD-IV-2", start=t1, end=t1)

    def test_naive_time_rejected(self):
        with self.assertRaises(DomainError):
            service.publish_releases([item("BAD-TZ", start="2026-01-01T00:00:00",
                                           end=t5)])


class QueryTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_basic_visibility(self):
        release("Q-1")
        # 作业时刻在区间外 -> 无
        self.assertEqual(numbers_at(t5, future), {})
        self.assertEqual(numbers_at(iso(_T - timedelta(seconds=1)), future), {})
        # 区间内可见
        row = numbers_at(t2, future)["Q-1"]
        self.assertEqual(row["revision"], 0)
        self.assertEqual(row["body"], "初版")
        # 知悉时刻早于发布 -> 空
        self.assertEqual(numbers_at(t2, t0), {})

    def test_known_as_of_defaults_now(self):
        release("Q-2")  # 发布时间 = 现在，作业区间在 2026
        row = numbers_at(t2, None)["Q-2"]
        self.assertEqual(row["revision"], 0)

    def test_offset_timezone_normalized_to_utc(self):
        service.publish_releases([item("Q-TZ", start=t1, end=t5)])
        # +08:00 的 08:00 == UTC 00:00，即区间起点（含），可见
        row = numbers_at("2026-01-01T08:00:00+08:00", future)["Q-TZ"]
        self.assertEqual(row["revision"], 0)
        # 终点（不含）：UTC 2027-01-01 00:00 == +08:00 08:00
        self.assertNotIn("Q-TZ", numbers_at("2027-01-01T08:00:00+08:00", future))


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        release("C-1", body="初版")

    def test_correction_covers_only_subinterval(self):
        correct("C-1", 0, t2, t3, "三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=29)), future)["C-1"]["body"], "初版")
        self.assertEqual(numbers_at(t2, future)["C-1"]["body"], "三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=59)), future)["C-1"]["body"], "三月版")
        self.assertEqual(numbers_at(t3, future)["C-1"]["body"], "初版")  # 止不含

    def test_query_before_correction_published_unchanged(self):
        published = correct("C-1", 0, t2, t3, "三月版")["published_at"]
        # 用更正发布时刻本身（published_at <= known 含等号）刚可见；前 1 微秒不可见
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        # 作业时刻同样规范化到 UTC 微秒串，避免与初版发布时刻误比
        self.assertEqual(numbers_at(canon(t2), before)["C-1"]["body"], "初版")
        self.assertEqual(numbers_at(canon(t2), published)["C-1"]["body"], "三月版")

    def test_concurrent_overlap_rejected(self):
        # 顺序提交且与"已发布"的更正重叠：客户依据的是旧版 r0 的当前生效区间，
        # 属于过期基版（应改以 r1 为被替代版本），历史保持原样。
        correct("C-1", 0, t2, t3, "三月版")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T + timedelta(days=40)),
                    iso(_T + timedelta(days=50)), "冲突")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        # 历史不变：该日期仍是第一条更正正文
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["C-1"]["body"], "三月版")

    def test_overlap_inside_same_batch_is_conflict(self):
        # 同一批次（并发提交）内两条更正相互冲突 -> concurrent_correction_conflict
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections([
                item("C-1", "A", t2,
                     iso(_T + timedelta(days=50)), base=0),
                item("C-1", "B", iso(_T + timedelta(days=40)),
                     iso(_T + timedelta(days=55)), base=0),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)
        # 整批回滚：均不存在
        self.assertEqual(numbers_at(t2, future)["C-1"]["revision"], 0)

    def test_adjacent_intervals_allowed(self):
        correct("C-1", 0, t2, t3, "二月")
        correct("C-1", 0, t3, t4, "三月")  # 首尾相接不算重叠
        self.assertEqual(numbers_at(t3, future)["C-1"]["body"], "三月")

    def test_interval_outside_base_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T - timedelta(days=1)), t2, "越界")
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, t4, iso(_T + timedelta(days=400)), "越界")
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_expired_base_version(self):
        # 基版区间被全部覆盖后再以其为基版 -> expired_base_version
        correct("C-1", 0, t1, t3, "上半段")
        correct("C-1", 0, t3, t5, "下半段")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, t2, t3, "基版已全覆盖")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["remaining_intervals"], [])

    def test_expired_base_when_partial_overlap_existing_child(self):
        correct("C-1", 0, t2, t4, "长更正")
        # 与其部分重叠（依据了旧版 r0 的当前生效区间之外的部分）
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 0, iso(_T + timedelta(days=20)),
                    iso(_T + timedelta(days=40)), "重叠")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        remaining = ctx.exception.details["remaining_intervals"]
        self.assertEqual([(r["start"], r["end"]) for r in remaining],
                         [(canon(t1), canon(t2)), (canon(t4), canon(t5))])

    def test_missing_procedure_and_base(self):
        with self.assertRaises(DomainError) as ctx:
            correct("NOPE", 0, t1, t2)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 9, t1, t2)
        self.assertEqual(ctx.exception.code, "base_version_not_found")

    def test_chained_correction_and_depth(self):
        correct("C-1", 0, t2, t4, "r1")
        correct("C-1", 1, t3, t4, "r2")          # r1 内的子区间
        correct("C-1", 1, t2, t3, "r3")          # r1 剩余的另一半
        # t2..t3 -> r3（最深链 v0<-v1<-v3）
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["C-1"]["body"], "r3")
        # t3..t4 -> r2
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["C-1"]["body"], "r2")
        # r1 已被 r2/r3 完整覆盖，再以 r1 为基版 -> 过期
        with self.assertRaises(DomainError) as ctx:
            correct("C-1", 1, iso(_T + timedelta(days=40)),
                    iso(_T + timedelta(days=45)), "x")
        self.assertEqual(ctx.exception.code, "expired_base_version")
        # r0 在 t4..t5 仍生效，可继续更正
        correct("C-1", 0, t4, t5, "r4")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=100)),
                                    future)["C-1"]["body"], "r4")

    def test_batch_atomic_rollback(self):
        # 第二条与第一条区间重叠（同为对 r0 的并发更正）-> 第一条也不得入库
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections([
                item("C-1", "五月", iso(_T + timedelta(days=120)),
                     iso(_T + timedelta(days=150)), base=0),
                item("C-1", "冲突", iso(_T + timedelta(days=130)),
                     iso(_T + timedelta(days=140)), base=0),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)
        # 第一条不存在：该时间段回到初版
        self.assertEqual(numbers_at(iso(_T + timedelta(days=130)),
                                    future)["C-1"]["body"], "初版")

    def test_batch_with_chain_inside(self):
        # 同批次：先更正 r0 得到 r1，再以 r1 为基版
        res = service.submit_corrections([
            item("C-1", "r1", t2, t4, base=0),
            item("C-1", "r2", t3, t4, base=1),
        ])
        self.assertEqual([x["revision"] for x in res["items"]], [1, 2])
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["C-1"]["body"], "r2")

    def test_published_at_is_server_defined_and_returned(self):
        res = correct("C-1", 0, t2, t3)
        self.assertTrue(res["published_at"].endswith("Z"))
        history = service.list_history("C-1")
        self.assertEqual(history["count"], 2)
        self.assertEqual([v["revision"] for v in history["items"]], [0, 1])
        self.assertEqual(history["items"][1]["supersedes_revision"], 0)

    def test_multiple_procedures_in_one_query(self):
        release("C-2", body="另一个规程")
        correct("C-1", 0, t2, t3, "C1三月")
        rows = numbers_at(t2, future)
        self.assertEqual(rows["C-1"]["body"], "C1三月")
        self.assertEqual(rows["C-2"]["body"], "另一个规程")


class WithdrawalTests(unittest.TestCase):
    """更正撤回：只追加事件、双时间维可见性、拒绝条件与并发确定性。"""

    def setUp(self):
        reset_db()
        release("W-1", body="初版")

    def test_withdraw_restores_parent_body_afterward(self):
        correct("W-1", 0, t2, t3, "三月版")
        wd = withdraw("W-1", 1)
        self.assertEqual(wd["items"][0]["withdrawn_at"], wd["withdrawn_at"])
        self.assertEqual(wd["items"][0]["supersedes_revision"], 0)
        self.assertEqual(wd["items"][0]["body"], "三月版")
        self.assertEqual(wd["items"][0]["body"], "三月版")
        # 撤回后（默认知悉时刻=现在，晚于撤回）：子区间恢复父版本正文
        self.assertEqual(numbers_at(t2, future)["W-1"]["revision"], 0)
        self.assertEqual(numbers_at(t2, future)["W-1"]["body"], "初版")
        # 区间外本来就不受影响
        self.assertEqual(numbers_at(iso(_T + timedelta(days=100)),
                                    future)["W-1"]["revision"], 0)

    def test_past_known_as_of_unchanged_after_withdrawal(self):
        published = correct("W-1", 0, t2, t3, "三月版")["published_at"]
        withdrawn_at = withdraw("W-1", 1)["withdrawn_at"]
        p_dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        w_dt = datetime.strptime(withdrawn_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before_pub = iso_us(p_dt - timedelta(microseconds=1))
        after_pub_before_wd = iso_us(p_dt + timedelta(microseconds=1))
        at_wd = iso_us(w_dt)
        after_wd = iso_us(w_dt + timedelta(microseconds=1))
        # 更正发布前：无更正
        self.assertEqual(numbers_at(canon(t2), before_pub)["W-1"]["body"], "初版")
        # 发布后、撤回前：仍按原版本链看到更正
        self.assertEqual(numbers_at(canon(t2), after_pub_before_wd)["W-1"]["body"],
                         "三月版")
        # 撤回时刻本身：withdrawn_at > known 为严格大于，等于时尚未撤回 -> 仍可见
        self.assertEqual(numbers_at(canon(t2), at_wd)["W-1"]["body"], "三月版")
        # 撤回后：恢复初版
        self.assertEqual(numbers_at(canon(t2), after_wd)["W-1"]["body"], "初版")

    def test_withdrawn_version_remains_in_history_with_timestamp(self):
        correct("W-1", 0, t2, t3, "三月版")
        withdrawn_at = withdraw("W-1", 1)["withdrawn_at"]
        history = service.list_history("W-1")
        self.assertEqual(history["count"], 2)          # 行不删除
        revs = {v["revision"]: v for v in history["items"]}
        self.assertIsNone(revs[0]["withdrawn_at"])
        self.assertEqual(revs[1]["withdrawn_at"], withdrawn_at)
        self.assertTrue(revs[1]["published_at"].endswith("Z"))
        self.assertEqual(revs[1]["supersedes_revision"], 0)

    def test_first_revision_cannot_withdraw(self):
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 0)
        self.assertEqual(ctx.exception.code,
                         "first_revision_not_withdrawable")
        self.assertEqual(ctx.exception.http_status, 409)
        # 历史不变
        self.assertEqual(service.list_history("W-1")["count"], 1)

    def test_double_withdraw_rejected(self):
        correct("W-1", 0, t2, t3, "三月版")
        first_at = withdraw("W-1", 1)["withdrawn_at"]
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "already_withdrawn")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["withdrawn_at"], first_at)

    def test_withdraw_version_with_children_rejected(self):
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "version_has_children")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["child_revisions"], [2])
        # 历史不变：r1/r2 仍生效
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["body"], "r2")

    def test_leaf_withdraw_restores_immediate_parent(self):
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        # 撤回叶子 r2：其区间恢复为"父版本 r1"的正文（r1 当前仍生效）
        withdraw("W-1", 2)
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["body"], "r1")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["revision"], 1)
        # 但 r1 "已有子更正"（r2 行仍保留在历史中）-> 仍不可撤回
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "version_has_children")
        history = service.list_history("W-1")
        self.assertEqual(
            {v["revision"]: v["withdrawn_at"] is not None for v in history["items"]},
            {0: False, 1: False, 2: True})
        # r2 撤回后其区间回归 r1，r1 可在该恢复区间再次更正
        correct("W-1", 1, t3, t4, "r3")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=75)),
                                    future)["W-1"]["body"], "r3")

    def test_parent_still_blocked_by_withdrawn_child_row(self):
        # 撤回只追加事件不删行：子更正即使已撤回，仍是"已有子更正"，
        # 父版本不能撤回；被撤回叶子的区间恢复为该父版本正文。
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        withdraw("W-1", 2)
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 1)
        self.assertEqual(ctx.exception.code, "version_has_children")

    def test_re_correct_parent_on_restored_interval(self):
        correct("W-1", 0, t2, t3, "错误的三月版")
        withdraw("W-1", 1)
        # 撤回后父版本 r0 在恢复区间可再次更正
        correct("W-1", 0, t2, t3, "正确的三月版")
        self.assertEqual(numbers_at(iso(_T + timedelta(days=45)),
                                    future)["W-1"]["body"], "正确的三月版")
        history = service.list_history("W-1")
        self.assertEqual([v["revision"] for v in history["items"]], [0, 1, 2])
        self.assertIsNone(history["items"][2]["withdrawn_at"])
        self.assertIsNotNone(history["items"][1]["withdrawn_at"])

    def test_cannot_base_correction_on_withdrawn_version(self):
        correct("W-1", 0, t2, t4, "r1")
        withdraw("W-1", 1)
        with self.assertRaises(DomainError) as ctx:
            correct("W-1", 1, t2, t3, "以撤回版本为基版")
        self.assertEqual(ctx.exception.code, "base_version_withdrawn")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_withdraw_missing_procedure_and_revision(self):
        with self.assertRaises(DomainError) as ctx:
            withdraw("NOPE", 0)
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(DomainError) as ctx:
            withdraw("W-1", 7)
        self.assertEqual(ctx.exception.code, "revision_not_found")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_batch_atomic_rollback(self):
        correct("W-1", 0, t2, t3, "r1")
        correct("W-1", 0, t3, t4, "r2")
        # 第一条可撤（叶子 r1），第二条撤首版 -> 整批回滚，r1 不被撤回
        with self.assertRaises(DomainError) as ctx:
            service.withdraw_corrections([witem("W-1", 1), witem("W-1", 0)])
        self.assertEqual(ctx.exception.code,
                         "first_revision_not_withdrawable")
        self.assertEqual(ctx.exception.details["index"], 1)
        self.assertIsNone(
            service.list_history("W-1")["items"][1]["withdrawn_at"])
        self.assertEqual(numbers_at(t2, future)["W-1"]["body"], "r1")

    def test_batch_leaf_and_parent_parent_rejected(self):
        correct("W-1", 0, t2, t4, "r1")
        correct("W-1", 1, t3, t4, "r2")
        # 同一批中撤 r2（合法）后再撤 r1：r1 仍"已有子更正"（r2 行不删除）
        # -> version_has_children，整批回滚，r2 也不撤回。
        with self.assertRaises(DomainError) as ctx:
            service.withdraw_corrections([witem("W-1", 2), witem("W-1", 1)])
        self.assertEqual(ctx.exception.code, "version_has_children")
        self.assertEqual(ctx.exception.details["index"], 1)
        self.assertIsNone(
            service.list_history("W-1")["items"][2]["withdrawn_at"])

    def test_withdrawal_then_correction_no_double_body(self):
        # 并发确定性：撤回事务先提交，随后的更正在恢复区间内成功；
        # 任何知悉时刻下 t2..t3 至多一份当前正文。
        correct("W-1", 0, t2, t3, "旧更正")
        wd = withdraw("W-1", 1)["withdrawn_at"]
        res = correct("W-1", 0, t2, t3, "新更正")
        wd_dt = datetime.strptime(wd, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        # 撤回瞬间（含）：旧更正仍生效
        self.assertEqual(numbers_at(canon(t2), canon(wd))["W-1"]["revision"], 1)
        # 撤回之后 1 微秒、新更正之前：恢复父版本
        self.assertEqual(numbers_at(canon(t2),
                                    iso_us(wd_dt + timedelta(microseconds=1))
                                    )["W-1"]["revision"], 0)
        # 新更正后：新更正（r2），不存在两份正文
        self.assertEqual(numbers_at(canon(t2), future)["W-1"]["revision"], 2)
        self.assertEqual(numbers_at(canon(t2), future)["W-1"]["body"], "新更正")

    def test_diff_excludes_withdrawn_for_later_known_time(self):
        published = correct("W-1", 0, t2, t3, "三月版")["published_at"]
        withdrawn_at = withdraw("W-1", 1)["withdrawn_at"]
        p_dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        w_dt = datetime.strptime(withdrawn_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        # 两侧都在撤回之后：全年都是 r0，无变化
        res = service.compare_interval(
            "W-1", iso_us(w_dt + timedelta(microseconds=1)),
            future, t1, t5)
        self.assertEqual(res["count"], 0)
        # 前侧在发布之后撤回之前（r1 可见），后侧在撤回之后（恢复 r0）
        res = service.compare_interval(
            "W-1", iso_us(p_dt + timedelta(microseconds=1)),
            iso_us(w_dt + timedelta(microseconds=1)), t1, t5)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"]["revision"], 1)
        self.assertEqual(seg["after"]["revision"], 0)
        self.assertEqual(seg["after"]["body"], "初版")


class CompareTests(unittest.TestCase):
    """两知悉时刻作业区间对比。"""

    def setUp(self):
        reset_db()
        release("D-1", body="初版")

    def test_before_release_whole_window_null_to_versions(self):
        # 前侧知悉时刻早于任何发布：三段（r1 更正段把两侧同为 null->r0 的段隔开）
        correct("D-1", 0, t2, t3, "三月版")
        res = service.compare_interval("D-1", t0, future, t1, t5)
        self.assertEqual(res["count"], 3)
        segs = [(s["start"], s["end"],
                 s["before"], s["after"]["revision"]) for s in res["segments"]]
        self.assertEqual([s[2] for s in segs], [None, None, None])
        self.assertEqual([s[3] for s in segs], [0, 1, 0])
        self.assertEqual([(s[0], s[1]) for s in segs],
                         [(canon(t1), canon(t2)),
                          (canon(t2), canon(t3)),
                          (canon(t3), canon(t5))])
        self.assertEqual(res["segments"][1]["after"]["body"], "三月版")

    def test_correction_between_known_times_single_segment(self):
        published = correct("D-1", 0, t2, t3, "三月版")["published_at"]
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        res = service.compare_interval("D-1", before, future, t1, t5)
        self.assertEqual(res["count"], 1)
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"]["revision"], 0)
        self.assertEqual(seg["before"]["body"], "初版")
        self.assertEqual(seg["after"]["revision"], 1)
        self.assertEqual(seg["after"]["body"], "三月版")

    def test_visibility_boundary_published_at_inclusive(self):
        # 前侧取更正发布时刻本身（published_at <= known 含等号）-> 已可见，无差异
        published = correct("D-1", 0, t2, t3, "三月版")["published_at"]
        res = service.compare_interval("D-1", published, future, t1, t5)
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["segments"], [])

    def test_same_body_new_revision_still_a_change(self):
        correct("D-1", 0, t2, t3, "初版")   # 正文与 r0 完全相同
        res = service.compare_interval("D-1", t0, future, t1, t5)
        changed = [s for s in res["segments"]
                   if s["before"] is None and s["after"]["revision"] == 1]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["after"]["body"], "初版")

    def test_partial_work_window_clipped(self):
        correct("D-1", 0, t2, t3, "三月版")
        ws = iso(_T + timedelta(days=15))
        we = iso(_T + timedelta(days=45))
        res = service.compare_interval("D-1", t0, future, ws, we)
        self.assertEqual(res["count"], 2)
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(ws), canon(t2)), (canon(t2), canon(we))])

    def test_chained_corrections_separate_segments(self):
        correct("D-1", 0, t2, t4, "r1")
        correct("D-1", 1, t3, t4, "r2")
        res = service.compare_interval("D-1", t0, future, t1, t5)
        pairs = [(s["start"], s["after"]["revision"]) for s in res["segments"]]
        self.assertEqual(pairs,
                         [(canon(t1), 0), (canon(t2), 1), (canon(t3), 2),
                          (canon(t4), 0)])
        # 前侧全空；两段 after=r0 被不同版本隔开，不得跨段合并
        self.assertTrue(all(s["before"] is None for s in res["segments"]))

    def test_no_change_returns_empty(self):
        correct("D-1", 0, t2, t3, "三月版")
        # 两个知悉时刻都在更正发布之后（须严格先后；future = T+400d）
        after1 = iso(_T + timedelta(days=300))
        res = service.compare_interval("D-1", after1, future, t1, t5)
        self.assertEqual(res["count"], 0)
        # 作业区间完全在任何适用区间之外：两侧均无版本，也不返回
        res = service.compare_interval(
            "D-1", t0, future, iso(_T + timedelta(days=800)),
            iso(_T + timedelta(days=900)))
        self.assertEqual(res["count"], 0)

    def test_offset_timezone_normalized(self):
        correct("D-1", 0, t2, t3, "三月版")
        res = service.compare_interval(
            "D-1",
            "2025-12-31T16:00:00-08:00",       # == 2026-01-01T00:00:00Z（早于发布）
            future,
            "2026-01-31T08:00:00+08:00",       # == 2026-01-31T00:00:00Z == t2（含）
            "2026-03-02T08:00:00+08:00")       # == 2026-03-02T00:00:00Z == t3（不含）
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["segments"][0]["after"]["revision"], 1)

    def test_error_unknown_number(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("NOPE", t0, future, t1, t2)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_error_naive_time_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval(
                "D-1", "2026-01-01T00:00:00", future, t1, t2)
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "known_before")
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval(
                "D-1", t0, future, "2026-01-01T00:00:00", t2)
        self.assertEqual(ctx.exception.details["field"], "work_start")

    def test_error_known_order_reversed_or_equal(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", future, t0, t1, t2)
        self.assertEqual(ctx.exception.code, "invalid_known_order")
        self.assertEqual(ctx.exception.http_status, 422)
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", future, future, t1, t2)
        self.assertEqual(ctx.exception.code, "invalid_known_order")

    def test_error_empty_work_interval(self):
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", t0, future, t2, t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            service.compare_interval("D-1", t0, future, t3, t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_read_only_on_error(self):
        # 非法请求前后历史完全一致
        before = service.list_history("D-1")
        with self.assertRaises(DomainError):
            service.compare_interval("D-1", future, t0, t1, t2)
        self.assertEqual(service.list_history("D-1"), before)

    def test_merge_adjacent_helper(self):
        segs = service._merge_adjacent([
            (t1, t2, None, 0), (t2, t3, None, 0), (t3, t4, 0, 0),
            (t4, t5, 0, 1)])
        self.assertEqual(segs, [(t1, t3, None, 0), (t3, t4, 0, 0),
                                (t4, t5, 0, 1)])


class PackageTests(unittest.TestCase):
    """作业规程包：固定、原样取回、复核及各类整体拒绝。"""

    def setUp(self):
        reset_db()
        release("P-1", body="P1初版")
        release("P-2", body="P2初版")

    def _now_after_writes(self) -> str:
        # 服务端发布时钟单调递增；略作停顿确保后续 now 严格晚于发布时刻
        import time as _time
        _time.sleep(0.002)
        return now_canonical()

    def _package(self, numbers, work=t2, known=None):
        return service.create_package(work, known or self._now_after_writes(),
                                      numbers)

    def test_create_returns_id_versions_bodies_and_times(self):
        known = self._now_after_writes()
        res = self._package(["P-2", "P-1"], known=known)
        self.assertRegex(res["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(res["count"], 2)
        # 保留调用方传入的编号顺序（复核按相同位置逐项核对）
        self.assertEqual([i["number"] for i in res["items"]], ["P-2", "P-1"])
        self.assertEqual([i["revision"] for i in res["items"]], [0, 0])
        self.assertEqual(res["items"][0]["body"], "P2初版")
        self.assertEqual(res["items"][1]["body"], "P1初版")
        self.assertEqual(res["known_as_of"], canon(known))
        self.assertEqual(res["work_time"], canon(t2))
        self.assertTrue(res["created_at"].endswith("Z"))

    def test_get_returns_same_snapshot(self):
        res = self._package(["P-1"])
        got = service.get_package(res["id"])
        self.assertEqual(got, res)

    def test_missing_number_rejected_entirely(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1", "NOPE"])
        self.assertEqual(ctx.exception.code, "procedure_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details,
                         {"number": "NOPE", "index": 1})
        # 不留包
        import sqlite3
        conn = sqlite3.connect(settings.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM packages")
                             .fetchone()[0], 0)
        finally:
            conn.close()

    def test_duplicate_number_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1", "P-1"])
        self.assertEqual(ctx.exception.code, "duplicate_package_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_empty_number_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["   "])
        self.assertEqual(ctx.exception.code, "invalid_number")

    def test_no_applicable_version_at_work_time(self):
        # 作业时刻在任何适用区间之外（t5 为终点，不含）
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1"], work=t5)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["number"], "P-1")

    def test_no_applicable_version_when_not_yet_published(self):
        # 知悉时刻早于发布 -> 无已发布版本
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1"], known=t0)
        self.assertEqual(ctx.exception.code, "no_applicable_version")

    def test_known_as_of_in_future_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._package(["P-1"], known=future)
        self.assertEqual(ctx.exception.code, "known_as_of_in_future")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_naive_time_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            service.create_package("2026-01-01T00:00:00",
                                   self._now_after_writes(), ["P-1"])
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "work_time")
        with self.assertRaises(DomainError) as ctx:
            service.create_package(t2, "2026-01-01T00:00:00", ["P-1"])
        self.assertEqual(ctx.exception.details["field"], "known_as_of")

    def test_package_pins_applicable_correction_at_fix_time(self):
        correct("P-1", 0, t2, t3, "P1三月版")
        res = self._package(["P-1"], work=t2)
        item = res["items"][0]
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["body"], "P1三月版")
        self.assertEqual(item["supersedes_revision"], 0)

    def test_package_immutable_after_later_correction(self):
        res = self._package(["P-1"], work=t2)
        # 固定之后再更正同一作业时刻
        correct("P-1", 0, t2, t3, "P1后来的三月版")
        got = service.get_package(res["id"])
        self.assertEqual(got["items"][0]["revision"], 0)
        self.assertEqual(got["items"][0]["body"], "P1初版")
        self.assertEqual(got["known_as_of"], res["known_as_of"])

    def test_package_immutable_after_withdrawal(self):
        correct("P-1", 0, t2, t3, "误发三月版")
        res = self._package(["P-1"], work=t2)
        withdraw("P-1", 1)
        # 撤回不影响包快照
        got = service.get_package(res["id"])
        self.assertEqual(got["items"][0]["revision"], 1)
        self.assertEqual(got["items"][0]["body"], "误发三月版")

    def test_review_all_match(self):
        res = self._package(["P-1", "P-2"], work=t2)
        review = service.review_package(res["id"], self._now_after_writes())
        self.assertTrue(review["all_match"])
        self.assertEqual(review["count"], 2)
        for it in review["items"]:
            self.assertFalse(it["changed"])
            self.assertEqual(it["pinned"]["revision"],
                             it["current"]["revision"])
            self.assertEqual(it["pinned"]["body"], it["current"]["body"])

    def test_review_detects_later_correction(self):
        res = self._package(["P-1"], work=t2)
        correct("P-1", 0, t2, t3, "新三月版")
        review = service.review_package(res["id"], self._now_after_writes())
        self.assertFalse(review["all_match"])
        it = review["items"][0]
        self.assertTrue(it["changed"])
        self.assertEqual(it["pinned"]["revision"], 0)
        self.assertEqual(it["current"]["revision"], 1)
        self.assertEqual(it["current"]["body"], "新三月版")

    def test_review_detects_withdrawal_restoring_parent(self):
        correct("P-1", 0, t2, t3, "三月版")
        res = self._package(["P-1"], work=t2)
        self.assertEqual(res["items"][0]["revision"], 1)
        withdraw("P-1", 1)
        review = service.review_package(res["id"], self._now_after_writes())
        it = review["items"][0]
        self.assertTrue(it["changed"])
        self.assertEqual(it["pinned"]["revision"], 1)
        self.assertEqual(it["current"]["revision"], 0)
        self.assertEqual(it["current"]["body"], "P1初版")
        self.assertFalse(review["all_match"])

    def test_review_at_package_known_as_of_matches(self):
        # 以包内知悉时刻本身复核（不早于，含等号）-> 完全一致
        correct("P-1", 0, t2, t3, "三月版")
        known = self._now_after_writes()
        res = self._package(["P-1"], work=t2, known=known)
        review = service.review_package(res["id"], res["known_as_of"])
        self.assertTrue(review["all_match"])
        self.assertEqual(review["items"][0]["current"]["revision"],
                         review["items"][0]["pinned"]["revision"])

    def test_review_known_earlier_than_package_rejected(self):
        res = self._package(["P-1"], work=t2)
        with self.assertRaises(DomainError) as ctx:
            service.review_package(res["id"], t0)
        self.assertEqual(ctx.exception.code, "review_known_too_early")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_review_known_in_future_rejected(self):
        res = self._package(["P-1"], work=t2)
        with self.assertRaises(DomainError) as ctx:
            service.review_package(res["id"], future)
        self.assertEqual(ctx.exception.code, "known_as_of_in_future")

    def test_review_unknown_package(self):
        with self.assertRaises(DomainError) as ctx:
            service.review_package("deadbeef", self._now_after_writes())
        self.assertEqual(ctx.exception.code, "package_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        with self.assertRaises(DomainError) as ctx:
            service.get_package("deadbeef")
        self.assertEqual(ctx.exception.code, "package_not_found")

    def test_review_snapshot_consistent_under_concurrent_write(self):
        # 复核在单一只读快照内完成：复核快照建立之后并发提交的写入，对该次
        # 复核完全不可见（不会拼出"半新半旧"的结果）；快照之外的新读取才能
        # 看到并发更正。用真实线程在读者持有快照期间由写路径提交。
        import threading
        import time as _time
        from app.db import read_snapshot
        from app import service as svc

        correct("P-1", 0, t2, t3, "三月版")
        known = self._now_after_writes()
        pkg = self._package(["P-1", "P-2"], work=t2, known=known)
        seen: dict = {}

        def reader() -> None:
            with read_snapshot() as conn:
                # 进入时快照已固定；让出窗口给写线程提交并发更正
                _time.sleep(0.3)
                best, _ = svc._resolve_effective(
                    conn, svc._fetch_procedure(conn, "P-2"), t2, known)
                seen["revision"] = best["revision"]

        th = threading.Thread(target=reader)
        th.start()
        _time.sleep(0.1)
        try:
            correct("P-2", 0, t2, t3, "P2并发三月版")
        finally:
            th.join()
        # 同一快照内解析：看不到快照之后的写入 -> 仍是 r0
        self.assertEqual(seen["revision"], 0)
        # 快照之外的新连接则能看到新更正 r1
        with read_snapshot() as conn:
            best, _ = svc._resolve_effective(
                conn, svc._fetch_procedure(conn, "P-2"), t2,
                self._now_after_writes())
            self.assertEqual(best["revision"], 1)

    def test_review_function_atomic_snapshot_under_concurrent_write(self):
        # 直接调用 review_package：复核快照建立后并发提交的更正不可见；快照
        # 之外的复核才看到。验证整个复核不与并发写入拼接（无混合结果）。
        import threading
        import time as _time
        from app.db import read_snapshot
        from app import service as svc

        known = self._now_after_writes()
        pkg_id = self._package(["P-1", "P-2"], work=t2, known=known)["id"]
        seen: dict = {}

        def reviewer() -> None:
            with read_snapshot() as conn:
                _time.sleep(0.3)     # 持有快照，窗口内写线程提交
                # 复用复核的逐项解析逻辑，但全部跑在该快照上
                pkg, rows = svc._package_rows(conn, pkg_id)
                revs = {}
                for row in rows:
                    proc = svc._fetch_procedure_by_id(conn, row["procedure_id"])
                    best, _ = svc._resolve_effective(
                        conn, proc, pkg["work_time"],
                        self._now_after_writes())
                    revs[row["number"]] = best["revision"]
                seen["revs"] = revs

        th = threading.Thread(target=reviewer)
        th.start()
        _time.sleep(0.1)
        correct("P-2", 0, t2, t3, "P2并发三月版")
        th.join()
        # 快照在更正提交前固定 -> P-2 仍解析为 r0，未与并发写入拼接
        self.assertEqual(seen["revs"], {"P-1": 0, "P-2": 0})
        # 快照之外的正式复核随后可见 r1 并判该项变化
        review = service.review_package(pkg_id, self._now_after_writes())
        by_no = {i["number"]: i for i in review["items"]}
        self.assertTrue(by_no["P-2"]["changed"])
        self.assertEqual(by_no["P-2"]["current"]["revision"], 1)
        self.assertFalse(by_no["P-1"]["changed"])


class PrerequisiteTests(unittest.TestCase):
    """前置规程：发布/更正携带前置编号列表，按版本生效，递归解析闭包。"""

    def setUp(self):
        reset_db()

    def _closure(self, number: str, work: str = t2, known: str = future):
        return service.resolve_closure(number, work, known)

    def _closure_numbers(self, number: str, work: str = t2,
                         known: str = future) -> list[str]:
        return [i["number"] for i in self._closure(number, work, known)["items"]]

    # ---- 写入：携带 / 省略 / 继承 / 清空 --------------------------------

    def test_release_with_prerequisites_returned_and_stored(self):
        res = release("PRE-W-1", prerequisites=["PRE-A", "PRE-B"])
        self.assertEqual(res["items"][0]["prerequisites"], ["PRE-A", "PRE-B"])
        history = service.list_history("PRE-W-1")
        self.assertEqual(history["items"][0]["prerequisites"],
                         ["PRE-A", "PRE-B"])

    def test_release_omitting_prerequisites_defaults_empty(self):
        res = release("PRE-W-2")
        self.assertEqual(res["items"][0]["prerequisites"], [])
        self.assertEqual(
            service.list_history("PRE-W-2")["items"][0]["prerequisites"], [])

    def test_correction_inherits_base_list_when_omitted(self):
        release("PRE-W-3", prerequisites=["PRE-A", "PRE-B"])
        res = correct("PRE-W-3", 0, t2, t3, "三月版")
        self.assertEqual(res["items"][0]["prerequisites"], ["PRE-A", "PRE-B"])
        history = {v["revision"]: v for v in
                   service.list_history("PRE-W-3")["items"]}
        self.assertEqual(history[0]["prerequisites"], ["PRE-A", "PRE-B"])
        self.assertEqual(history[1]["prerequisites"], ["PRE-A", "PRE-B"])

    def test_correction_explicit_empty_list_clears(self):
        release("PRE-W-4", prerequisites=["PRE-A"])
        res = correct("PRE-W-4", 0, t2, t3, "三月版", prerequisites=[])
        self.assertEqual(res["items"][0]["prerequisites"], [])
        history = {v["revision"]: v for v in
                   service.list_history("PRE-W-4")["items"]}
        self.assertEqual(history[0]["prerequisites"], ["PRE-A"])
        self.assertEqual(history[1]["prerequisites"], [])

    def test_correction_explicit_list_replaces(self):
        release("PRE-W-5", prerequisites=["PRE-A"])
        res = correct("PRE-W-5", 0, t2, t3, "三月版",
                      prerequisites=["PRE-B", "PRE-C"])
        self.assertEqual(res["items"][0]["prerequisites"],
                         ["PRE-B", "PRE-C"])
        # 链式：r2 省略 -> 继承 r1 的显式列表，而非 r0 的
        res2 = correct("PRE-W-5", 1, t2, t3, "三月再更正")
        self.assertEqual(res2["items"][0]["prerequisites"],
                         ["PRE-B", "PRE-C"])

    def test_old_style_items_without_field_still_work(self):
        # 旧调用方构造的对象没有 prerequisites 属性：按省略处理
        release("PRE-W-6")
        res = correct("PRE-W-6", 0, t2, t3, "三月版")
        self.assertEqual(res["items"][0]["prerequisites"], [])

    # ---- 写入校验：空白 / 重复 / 批量原子 --------------------------------

    def test_blank_prerequisite_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            release("PRE-BAD-1", prerequisites=["PRE-A", "  "])
        self.assertEqual(ctx.exception.code, "invalid_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["field"], "prerequisites")
        self.assertEqual(ctx.exception.details["position"], 1)
        with self.assertRaises(DomainError) as ctx:
            release("PRE-BAD-2", prerequisites=[""])
        self.assertEqual(ctx.exception.code, "invalid_number")

    def test_duplicate_prerequisite_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            release("PRE-BAD-3", prerequisites=["PRE-A", "PRE-B", "PRE-A"])
        self.assertEqual(ctx.exception.code, "duplicate_prerequisite_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["number"], "PRE-A")
        self.assertEqual(ctx.exception.details["position"], 2)
        # 更正中同样校验
        release("PRE-BAD-4")
        with self.assertRaises(DomainError) as ctx:
            correct("PRE-BAD-4", 0, t2, t3, "x",
                    prerequisites=["PRE-A", "PRE-A"])
        self.assertEqual(ctx.exception.code, "duplicate_prerequisite_number")

    def test_invalid_prerequisite_batch_atomic(self):
        # 批量发布：第二条前置重复 -> 整批回滚，第一条也不入库
        with self.assertRaises(DomainError) as ctx:
            service.publish_releases([
                item("PRE-BAT-1", prerequisites=["PRE-A"]),
                item("PRE-BAT-2", prerequisites=["PRE-X", "PRE-X"]),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_prerequisite_number")
        self.assertEqual(ctx.exception.details["index"], 1)
        with self.assertRaises(DomainError):
            service.list_history("PRE-BAT-1")
        # 批量更正同理：第二条非法 -> 第一条更正也不入库
        release("PRE-BAT-3")
        with self.assertRaises(DomainError):
            service.submit_corrections([
                item("PRE-BAT-3", "ok", t2, t3, base=0),
                item("PRE-BAT-3", "bad", t3, t4, base=0,
                     prerequisites=[" "]),
            ])
        self.assertEqual(service.list_history("PRE-BAT-3")["count"], 1)

    def test_reference_may_be_published_later(self):
        # 写入时不校验存在性：引用尚未发布的编号合法
        release("PRE-LATE-ROOT", prerequisites=["PRE-LATE"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-LATE-ROOT")
        self.assertEqual(ctx.exception.code, "prerequisite_not_found")
        # 前置发布后解析成功
        release("PRE-LATE", body="前置正文")
        items = self._closure("PRE-LATE-ROOT")["items"]
        self.assertEqual([i["number"] for i in items],
                         ["PRE-LATE", "PRE-LATE-ROOT"])
        self.assertEqual(items[0]["body"], "前置正文")

    # ---- 递归解析：顺序 / 去重 / 版本选取 --------------------------------

    def test_closure_prerequisites_first_root_last_dedup(self):
        # 菱形：ROOT -> [B, C]，B -> [D]，C -> [D]
        release("PRE-D", body="D正文")
        release("PRE-B", body="B正文", prerequisites=["PRE-D"])
        release("PRE-C", body="C正文", prerequisites=["PRE-D"])
        release("PRE-ROOT", body="根正文",
                prerequisites=["PRE-B", "PRE-C", "PRE-D"])
        res = self._closure("PRE-ROOT")
        self.assertEqual(res["root"], "PRE-ROOT")
        self.assertEqual(res["count"], 4)
        # 前置优先、根最后；D 被三处引用只出现一次
        self.assertEqual([i["number"] for i in res["items"]],
                         ["PRE-D", "PRE-B", "PRE-C", "PRE-ROOT"])
        self.assertEqual(res["items"][0]["body"], "D正文")
        self.assertEqual(res["items"][-1]["body"], "根正文")
        # 清单项含编号、版本、正文与各自携带的前置列表
        self.assertEqual(res["items"][-1]["revision"], 0)
        self.assertEqual(res["items"][-1]["prerequisites"],
                         ["PRE-B", "PRE-C", "PRE-D"])
        self.assertEqual(res["items"][0]["prerequisites"], [])
        self.assertEqual(res["work_time"], canon(t2))
        self.assertEqual(res["known_as_of"], canon(future))

    def test_closure_deep_chain(self):
        release("PRE-L3", body="L3")
        release("PRE-L2", body="L2", prerequisites=["PRE-L3"])
        release("PRE-L1", body="L1", prerequisites=["PRE-L2"])
        release("PRE-L0", body="L0", prerequisites=["PRE-L1"])
        self.assertEqual(self._closure_numbers("PRE-L0"),
                         ["PRE-L3", "PRE-L2", "PRE-L1", "PRE-L0"])

    def test_closure_uses_effective_version_at_work_time(self):
        # 前置规程自身在作业时刻适用的是更正版本
        release("PRE-E", body="前置初版")
        correct("PRE-E", 0, t2, t3, "前置三月版")
        release("PRE-R", body="根", prerequisites=["PRE-E"])
        # 作业时刻在 3 月（t2..t3）-> 前置取 r1
        items = self._closure("PRE-R", work=t2)["items"]
        self.assertEqual(items[0]["revision"], 1)
        self.assertEqual(items[0]["body"], "前置三月版")
        # 作业时刻在 4 月（t3..t4）-> 前置回到 r0
        items = self._closure("PRE-R", work=t3)["items"]
        self.assertEqual(items[0]["revision"], 0)
        self.assertEqual(items[0]["body"], "前置初版")

    def test_closure_references_follow_version_and_known_time(self):
        # 引用随版本的适用区间与发布时间变化：根 r0 带 [PRE-1]，
        # 3 月更正 r1 改为 []（清空）
        release("PRE-1", body="前置")
        release("PRE-R2", body="根初版", prerequisites=["PRE-1"])
        published = correct("PRE-R2", 0, t2, t3, "根三月版",
                            prerequisites=[])["published_at"]
        # 3 月作业：根的适用版本是 r1（已清空前置）-> 只有根
        self.assertEqual(self._closure_numbers("PRE-R2", work=t2),
                         ["PRE-R2"])
        # 2 月作业：根的适用版本是 r0 -> 带前置
        self.assertEqual(
            self._closure_numbers("PRE-R2",
                                  work=iso(_T + timedelta(days=15))),
            ["PRE-1", "PRE-R2"])
        # 知悉时刻早于更正发布：3 月作业仍按 r0 -> 带前置（历史不变）
        dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
        before = iso_us(dt - timedelta(microseconds=1))
        self.assertEqual(self._closure_numbers("PRE-R2", work=t2,
                                               known=before),
                         ["PRE-1", "PRE-R2"])

    def test_closure_references_follow_withdrawal(self):
        # 撤回更正后，其前置列表随之失效，恢复父版本携带的列表
        release("PRE-1", body="前置")
        release("PRE-R3", body="根初版", prerequisites=["PRE-1"])
        correct("PRE-R3", 0, t2, t3, "根三月版", prerequisites=[])
        self.assertEqual(self._closure_numbers("PRE-R3", work=t2),
                         ["PRE-R3"])
        withdraw("PRE-R3", 1)
        self.assertEqual(self._closure_numbers("PRE-R3", work=t2),
                         ["PRE-1", "PRE-R3"])

    def test_closure_known_as_of_defaults_now(self):
        release("PRE-N1", body="前置")
        release("PRE-N2", body="根", prerequisites=["PRE-N1"])
        res = service.resolve_closure("PRE-N2", t2, None)
        self.assertEqual([i["number"] for i in res["items"]],
                         ["PRE-N1", "PRE-N2"])

    # ---- 缺口与环：整体失败 ----------------------------------------------

    def test_root_not_found(self):
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-NOPE")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details["number"], "PRE-NOPE")

    def test_missing_reference_reports_gap_path(self):
        release("PRE-G1", prerequisites=["PRE-G2", "PRE-G-MISSING"])
        release("PRE-G2", prerequisites=["PRE-G3-MISSING"])
        # 先展开 PRE-G2 -> 缺口在 PRE-G3-MISSING，引用链带到缺口
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-G1")
        self.assertEqual(ctx.exception.code, "prerequisite_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details["number"], "PRE-G3-MISSING")
        self.assertEqual(ctx.exception.details["path"],
                         ["PRE-G1", "PRE-G2", "PRE-G3-MISSING"])

    def test_no_applicable_version_root_and_reference(self):
        # 根在作业时刻无适用正文
        release("PRE-X1", start=t2, end=t3)
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-X1", work=t4)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["number"], "PRE-X1")
        self.assertEqual(ctx.exception.details["path"], ["PRE-X1"])
        # 前置在作业时刻无适用正文（前置区间只覆盖 3 月，作业在 4 月）
        release("PRE-X2", start=t2, end=t3, body="前置仅 3 月")
        release("PRE-X3", prerequisites=["PRE-X2"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-X3", work=t4)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.details["number"], "PRE-X2")
        self.assertEqual(ctx.exception.details["path"],
                         ["PRE-X3", "PRE-X2"])
        # 知悉时刻早于前置发布：同样视为当时无适用正文
        release("PRE-X4")
        release("PRE-X5", prerequisites=["PRE-X4"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-X5", known=t0)
        self.assertEqual(ctx.exception.code, "no_applicable_version")
        self.assertEqual(ctx.exception.details["number"], "PRE-X5")

    def test_cycle_returns_cycle_path(self):
        release("PRE-CY-A", prerequisites=["PRE-CY-B"])
        release("PRE-CY-B", prerequisites=["PRE-CY-C"])
        release("PRE-CY-C", prerequisites=["PRE-CY-A"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-CY-A")
        self.assertEqual(ctx.exception.code, "prerequisite_cycle")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.details["cycle"],
                         ["PRE-CY-A", "PRE-CY-B", "PRE-CY-C", "PRE-CY-A"])
        # 从环外引用环：环路径从入环处开始
        release("PRE-CY-ROOT", prerequisites=["PRE-CY-B"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-CY-ROOT")
        self.assertEqual(ctx.exception.details["cycle"],
                         ["PRE-CY-B", "PRE-CY-C", "PRE-CY-A", "PRE-CY-B"])

    def test_self_cycle(self):
        release("PRE-CY-S", prerequisites=["PRE-CY-S"])
        with self.assertRaises(DomainError) as ctx:
            self._closure("PRE-CY-S")
        self.assertEqual(ctx.exception.code, "prerequisite_cycle")
        self.assertEqual(ctx.exception.details["cycle"],
                         ["PRE-CY-S", "PRE-CY-S"])

    def test_invalid_time_rejected(self):
        release("PRE-T1")
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure("PRE-T1", "2026-01-01T00:00:00", future)
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "work_time")
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure("PRE-T1", t2, "2026-01-01T00:00:00")
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "known_as_of")


class ClosurePlanTests(unittest.TestCase):
    """跨时段作业：根及递归前置在整个作业区间的逐片段可用性。"""

    def setUp(self):
        reset_db()

    def _plan(self, number: str, start: str = t1, end: str = t5,
              known: str | None = future) -> dict:
        return service.resolve_closure_plan(number, start, end, known)

    def _segs(self, number: str, start: str = t1, end: str = t5,
              known: str | None = future):
        """精简片段为 (起, 止, 是否可用, [(编号, 版本)...], 缺口链, 环)。"""
        res = self._plan(number, start, end, known)
        out = []
        for s in res["segments"]:
            out.append((s["start"], s["end"], s["available"],
                        [(i["number"], i["revision"]) for i in s["items"]],
                        [(g["code"], g["number"], tuple(g["path"]))
                         for g in s["gaps"]],
                        tuple(s["cycle"]) if s["cycle"] else None))
        return res, out

    # ---- 切分与可用性 ----------------------------------------------------

    def test_single_segment_when_nothing_changes(self):
        release("PL-A", body="A")
        release("PL-R", body="R", prerequisites=["PL-A"])
        res, segs = self._segs("PL-R")
        self.assertTrue(res["available"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(segs, [(canon(t1), canon(t5), True,
                                 [("PL-A", 0), ("PL-R", 0)], [], None)])
        self.assertEqual(res["work_start"], canon(t1))
        self.assertEqual(res["work_end"], canon(t5))
        self.assertEqual(res["known_as_of"], canon(future))
        self.assertEqual(res["root"], "PL-R")

    def test_root_version_switch_splits_segments(self):
        # 根 3 月适用 r1（前置不同），其余 r0
        release("PL-A", body="A")
        release("PL-B", body="B")
        release("PL-R1", body="R0", prerequisites=["PL-A"])
        correct("PL-R1", 0, t2, t3, "R1", prerequisites=["PL-B"])
        res, segs = self._segs("PL-R1")
        self.assertTrue(res["available"])
        self.assertEqual([(s[0], s[1]) for s in segs],
                         [(canon(t1), canon(t2)),
                          (canon(t2), canon(t3)),
                          (canon(t3), canon(t5))])
        self.assertEqual(segs[0][3], [("PL-A", 0), ("PL-R1", 0)])
        self.assertEqual(segs[1][3], [("PL-B", 0), ("PL-R1", 1)])
        self.assertEqual(segs[2][3], [("PL-A", 0), ("PL-R1", 0)])
        for s in segs:
            self.assertTrue(s[2])

    def test_prerequisite_version_switch_splits_segments(self):
        # 根全年恒定；前置 3 月切到更正版本
        release("PL-P", body="P0")
        correct("PL-P", 0, t2, t3, "P1")
        release("PL-R2", body="R", prerequisites=["PL-P"])
        res, segs = self._segs("PL-R2")
        self.assertTrue(res["available"])
        self.assertEqual([(s[0], s[1]) for s in segs],
                         [(canon(t1), canon(t2)),
                          (canon(t2), canon(t3)),
                          (canon(t3), canon(t5))])
        self.assertEqual(segs[0][3], [("PL-P", 0), ("PL-R2", 0)])
        self.assertEqual(segs[1][3], [("PL-P", 1), ("PL-R2", 0)])
        self.assertEqual(segs[2][3], [("PL-P", 0), ("PL-R2", 0)])

    def test_segments_cover_full_window_without_gaps_or_overlap(self):
        release("PL-X", body="X")
        correct("PL-X", 0, t2, t3, "X1")
        correct("PL-X", 1, t2, iso(_T + timedelta(days=40)), "X2")
        release("PL-R3", body="R", prerequisites=["PL-X"])
        res = self._plan("PL-R3")
        # 片段首尾相接、恰好覆盖整个作业区间
        self.assertEqual(res["segments"][0]["start"], canon(t1))
        self.assertEqual(res["segments"][-1]["end"], canon(t5))
        for a, b in zip(res["segments"], res["segments"][1:]):
            self.assertEqual(a["end"], b["start"])

    def test_window_clipped_to_requested_interval(self):
        # 前置更正覆盖 t2..t4；作业区间只问 t3..t5 -> 两段，端点裁剪到区间
        release("PL-Q", body="Q0")
        correct("PL-Q", 0, t2, t4, "Q1")
        release("PL-R4", body="R", prerequisites=["PL-Q"])
        res, segs = self._segs("PL-R4", t3, t5)
        self.assertEqual([(s[0], s[1]) for s in segs],
                         [(canon(t3), canon(t4)), (canon(t4), canon(t5))])

    def test_changes_outside_window_do_not_split(self):
        # 前置适用到 2027 年中；其更正完全落在作业区间 [t1, t5) 之后
        far_end = iso(_T + timedelta(days=400))
        release("PL-O", body="O0", start=t1, end=far_end)
        correct("PL-O", 0, t5, far_end, "O-outside")
        release("PL-R5", body="R", prerequisites=["PL-O"])
        res, segs = self._segs("PL-R5", t1, t5)
        self.assertEqual(res["count"], 1)
        self.assertEqual(segs[0][:2], (canon(t1), canon(t5)))

    def test_equal_end_segments_separated_by_middle_version_do_not_merge(self):
        # 根 r0 全年；3 月更正 r1 与 r0 携带完全相同前置、但版本不同
        # -> 片段按版本区分不合并；两端相同但不相邻，也不得跨段合并
        release("PL-M", body="M")
        release("PL-R6", body="R0", prerequisites=["PL-M"])
        correct("PL-R6", 0, t2, t3, "R1", prerequisites=["PL-M"])
        res, segs = self._segs("PL-R6")
        self.assertEqual(res["count"], 3)
        self.assertEqual(segs[0][3], segs[2][3])
        self.assertNotEqual(segs[0][3], segs[1][3])

    def test_adjacent_segments_with_identical_state_merge(self):
        # 前置引用只在 3 月存在：2 月与 4 月片段状态完全相同（只有根 r0），
        # 但被 3 月隔开。另构造真正首尾相接且状态相同的情况：前置在一个根
        # 版本恒定的子区间两端无变化（多余端点不出现在非相交版本上）。
        release("PL-K", body="K")
        release("PL-RK", body="R", prerequisites=["PL-K"])
        # 对无关规程的更正不得为本闭包引入边界
        release("PL-OTHER", body="x")
        correct("PL-OTHER", 0, t2, t3, "x1")
        res, segs = self._segs("PL-RK")
        self.assertEqual(res["count"], 1)
        self.assertEqual(segs[0][:2], (canon(t1), canon(t5)))
        # 缺口相同且首尾相接 -> 合并：根在 [t1,t2) 与 [t3,t5) 外无正文，
        # 中间 3 月有正文；这里改为"引用的前置在整个窗口外恒为同一缺口"，
        # 全窗口应为单一缺口片段。
        release("PL-RGG", body="R", prerequisites=["PL-GG-MISS"])
        res, segs = self._segs("PL-RGG")
        self.assertEqual(res["count"], 1)
        self.assertFalse(res["available"])
        self.assertEqual(segs[0][:3], (canon(t1), canon(t5), False))
        self.assertEqual(segs[0][4],
                         [("prerequisite_not_found", "PL-GG-MISS",
                           ("PL-RGG", "PL-GG-MISS"))])

    def test_same_gap_at_both_ends_merges(self):
        # 前置只在 3 月有适用正文；前后两段同为该缺口、彼此不相邻，
        # 验证缺口段保留且边界正确。
        release("PL-G", body="仅三月", start=t2, end=t3)
        release("PL-R7", body="R", prerequisites=["PL-G"])
        res, segs = self._segs("PL-R7")
        self.assertFalse(res["available"])
        self.assertEqual(res["count"], 3)
        gap = ("no_applicable_version", "PL-G", ("PL-R7", "PL-G"))
        self.assertEqual(segs[0], (canon(t1), canon(t2), False, [], [gap], None))
        self.assertTrue(segs[1][2])
        self.assertEqual(segs[1][3], [("PL-G", 0), ("PL-R7", 0)])
        self.assertEqual(segs[2], (canon(t3), canon(t5), False, [], [gap], None))

    def test_root_no_applicable_version_segments(self):
        # 根自身只覆盖 3 月：区间外片段以根缺口保留
        release("PL-R8", body="仅三月", start=t2, end=t3)
        res, segs = self._segs("PL-R8")
        self.assertFalse(res["available"])
        root_gap = ("no_applicable_version", "PL-R8", ("PL-R8",))
        self.assertEqual(segs[0], (canon(t1), canon(t2), False, [],
                                   [root_gap], None))
        self.assertEqual(segs[1], (canon(t2), canon(t3), True,
                                   [("PL-R8", 0)], [], None))
        self.assertEqual(segs[2], (canon(t3), canon(t5), False, [],
                                   [root_gap], None))

    def test_missing_root_keeps_segment_with_gap(self):
        res, segs = self._segs("PL-NO-SUCH")
        self.assertFalse(res["available"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(segs[0], (canon(t1), canon(t5), False, [],
                                   [("not_found", "PL-NO-SUCH",
                                     ("PL-NO-SUCH",))], None))

    def test_missing_reference_only_in_subinterval(self):
        # 根 3 月更正引用一个不存在的编号，其余片段只有根
        release("PL-R9", body="R0", prerequisites=[])
        correct("PL-R9", 0, t2, t3, "R1", prerequisites=["PL-MISSING"])
        res, segs = self._segs("PL-R9")
        self.assertFalse(res["available"])
        self.assertEqual(res["count"], 3)
        gap = ("prerequisite_not_found", "PL-MISSING",
               ("PL-R9", "PL-MISSING"))
        self.assertEqual(segs[0][3], [("PL-R9", 0)])
        self.assertEqual(segs[1], (canon(t2), canon(t3), False, [],
                                   [gap], None))
        self.assertEqual(segs[2][3], [("PL-R9", 0)])

    def test_missing_deep_reference_reports_gap_chain(self):
        release("PL-H1", prerequisites=["PL-H2"])
        release("PL-H2", prerequisites=["PL-H3-GONE"])
        res, segs = self._segs("PL-H1")
        gap = segs[0][4][0]
        self.assertEqual(gap, ("prerequisite_not_found", "PL-H3-GONE",
                               ("PL-H1", "PL-H2", "PL-H3-GONE")))
        self.assertFalse(segs[0][2])

    def test_cycle_only_in_subinterval_keeps_all_segments(self):
        # 根 r0 无前置；3 月 r1 引用 BB，BB 又引用根 -> 成环仅在 3 月
        release("PL-CY", body="R0", prerequisites=[])
        correct("PL-CY", 0, t2, t3, "R1", prerequisites=["PL-CYB"])
        release("PL-CYB", body="B", prerequisites=["PL-CY"])
        res, segs = self._segs("PL-CY")
        self.assertFalse(res["available"])
        self.assertEqual(res["count"], 3)
        self.assertEqual(segs[0][3], [("PL-CY", 0)])
        # 环段：环上之前已完成的前置（无）+ 环路径，根未入列
        self.assertFalse(segs[1][2])
        self.assertEqual(segs[1][4], [])
        self.assertEqual(segs[1][5], ("PL-CY", "PL-CYB", "PL-CY"))
        self.assertEqual(segs[1][3], [])
        self.assertEqual(segs[2][3], [("PL-CY", 0)])

    def test_cycle_segment_keeps_partial_prerequisites(self):
        # 根 -> [D(正常), CY(与 CYB 互引)]：D 已先完成并保留在环段清单中
        release("PL-D", body="D")
        release("PL-CA", body="A", prerequisites=["PL-CBB"])
        release("PL-CBB", body="B", prerequisites=["PL-CA"])
        release("PL-RC", body="R", prerequisites=["PL-D", "PL-CA"])
        res, segs = self._segs("PL-RC")
        seg = segs[0]
        self.assertFalse(seg[2])
        self.assertEqual(seg[3], [("PL-D", 0)])       # 环外正常前置保留
        self.assertEqual(seg[5], ("PL-CA", "PL-CBB", "PL-CA"))

    def test_diamond_dedup_prerequisites_first(self):
        # 菱形引用：去重、前置优先、根最后，在每个片段都成立
        release("PL-DD", body="D")
        release("PL-DB", body="B", prerequisites=["PL-DD"])
        release("PL-DC", body="C", prerequisites=["PL-DD"])
        release("PL-DR", body="R",
                prerequisites=["PL-DB", "PL-DC", "PL-DD"])
        res, segs = self._segs("PL-DR")
        self.assertEqual(segs[0][3],
                         [("PL-DD", 0), ("PL-DB", 0), ("PL-DC", 0),
                          ("PL-DR", 0)])

    def test_withdrawal_restores_segment_boundaries(self):
        # 根 3 月更正清空前置；撤回后 3 月恢复父版本前置列表
        release("PL-W", body="W")
        release("PL-RW", body="R0", prerequisites=["PL-W"])
        correct("PL-RW", 0, t2, t3, "R1", prerequisites=[])
        # 撤回前快照（知悉时刻取未来）：3 月只有根
        _, segs = self._segs("PL-RW")
        self.assertEqual(segs[1][3], [("PL-RW", 1)])
        withdraw("PL-RW", 1)
        # 撤回后：全部片段均为 [W, R r0]，相同状态合并为单一片段
        res, segs = self._segs("PL-RW")
        self.assertTrue(res["available"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(segs[0], (canon(t1), canon(t5), True,
                                   [("PL-W", 0), ("PL-RW", 0)], [], None))

    def test_not_yet_published_whole_window_is_single_gap(self):
        release("PL-RZ", body="R")
        # 知悉时刻早于一切发布：没有任何版本端点 -> 单片段、根无适用正文
        res, segs = self._segs("PL-RZ", known=t0)
        self.assertFalse(res["available"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(segs[0][:3], (canon(t1), canon(t5), False))
        self.assertEqual(segs[0][4],
                         [("no_applicable_version", "PL-RZ", ("PL-RZ",))])

    def test_segment_items_carry_full_payload(self):
        release("PL-F", body="前置全文")
        release("PL-RF", body="根全文", prerequisites=["PL-F"])
        res = self._plan("PL-RF")
        pre, root = res["segments"][0]["items"]
        for it in (pre, root):
            for key in ("number", "revision", "body", "valid_start",
                        "valid_end", "supersedes_revision", "published_at",
                        "prerequisites"):
                self.assertIn(key, it)
        self.assertEqual(pre["prerequisites"], [])
        self.assertEqual(root["prerequisites"], ["PL-F"])
        self.assertEqual(root["supersedes_revision"], None)

    def test_known_as_of_defaults_now(self):
        release("PL-N1", body="A")
        release("PL-NR", body="R", prerequisites=["PL-N1"])
        res = service.resolve_closure_plan("PL-NR", t1, t5, None)
        self.assertTrue(res["available"])
        self.assertTrue(res["known_as_of"].endswith("Z"))

    def test_half_open_boundaries_at_exact_endpoints(self):
        # 前置更正 [t2, t3)：t2（含）取 r1，t3（不含）回到 r0
        release("PL-BD", body="P0")
        correct("PL-BD", 0, t2, t3, "P1")
        release("PL-RBD", body="R", prerequisites=["PL-BD"])
        at_t2 = service.resolve_closure_plan(
            "PL-RBD", t2, iso(_T + timedelta(days=31)), future)
        self.assertEqual(
            [i["revision"] for i in at_t2["segments"][0]["items"]
             if i["number"] == "PL-BD"], [1])
        at_t3 = service.resolve_closure_plan(
            "PL-RBD", t3, iso(_T + timedelta(days=61)), future)
        self.assertEqual(
            [i["revision"] for i in at_t3["segments"][0]["items"]
             if i["number"] == "PL-BD"], [0])
        # 空区间（起=止）明确拒绝
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure_plan("PL-RBD", t2, t2, future)
        self.assertEqual(ctx.exception.code, "invalid_interval")

    # ---- 非法入参：拒绝且不写历史 ---------------------------------------

    def test_invalid_interval_rejected(self):
        release("PL-E1")
        with self.assertRaises(DomainError) as ctx:
            self._plan("PL-E1", t3, t3)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            self._plan("PL-E1", t3, t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_invalid_time_rejected(self):
        release("PL-E2")
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure_plan("PL-E2", "2026-01-01T00:00:00",
                                         t5, future)
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "work_start")
        with self.assertRaises(DomainError) as ctx:
            service.resolve_closure_plan("PL-E2", t1, t5,
                                         "2026-01-01T00:00:00")
        self.assertEqual(ctx.exception.details["field"], "known_as_of")

    def test_blank_root_is_missing_gap_not_crash(self):
        res, segs = self._segs("   ")
        self.assertFalse(res["available"])
        self.assertEqual(segs[0][4][0][0], "not_found")

    def test_read_only_on_error(self):
        release("PL-E3")
        before = service.list_history("PL-E3")
        with self.assertRaises(DomainError):
            self._plan("PL-E3", t5, t1)
        self.assertEqual(service.list_history("PL-E3"), before)


class CoverageCheckTests(unittest.TestCase):
    """跨规程版本覆盖检查：逐段返回各编号有效版本/缺口与切换、恢复标记。"""

    def setUp(self):
        reset_db()

    def _now(self) -> str:
        # 服务端发布时钟单调递增；略作停顿确保知悉时刻不晚于当前时刻
        import time as _time
        _time.sleep(0.002)
        return now_canonical()

    def _check(self, numbers, start=t1, end=t5, known=None):
        return service.check_coverage(numbers, start, end,
                                      known or self._now())

    # ---- 基本切分与标记 --------------------------------------------------

    def test_single_constant_coverage(self):
        release("C-1", body="唯一版本")
        res = self._check(["C-1"])
        self.assertEqual(res["count"], 1)
        self.assertTrue(res["fully_covered"])
        self.assertEqual(res["numbers"], ["C-1"])
        self.assertEqual(res["work_start"], canon(t1))
        self.assertEqual(res["work_end"], canon(t5))
        self.assertEqual(res["known_as_of"], canon(res["known_as_of"]))
        seg = res["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t1), canon(t5)))
        self.assertFalse(seg["version_switch"])
        self.assertFalse(seg["has_gap"])
        self.assertFalse(seg["restored"])
        it = seg["items"][0]
        self.assertEqual(it["number"], "C-1")
        self.assertEqual(it["revision"], 0)
        self.assertEqual(it["body"], "唯一版本")
        self.assertEqual(it["valid_start"], canon(t1))
        self.assertEqual(it["valid_end"], canon(t5))
        self.assertIsNone(it["supersedes_revision"])
        self.assertTrue(it["published_at"].endswith("Z"))
        self.assertFalse(it["gap"])
        self.assertFalse(it["restored_by_withdrawal"])
        self.assertFalse(it["switched"])

    def test_union_of_endpoints_splits_and_marks_switches(self):
        release("C-A", body="A0")
        correct("C-A", 0, t2, t3, "A1")          # A 在 3 月切换
        release("C-B", body="B0")
        correct("C-B", 0, t3, t4, "B1")          # B 在 4-5 月切换
        res = self._check(["C-A", "C-B"])
        self.assertTrue(res["fully_covered"])
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(t1), canon(t2)), (canon(t2), canon(t3)),
                          (canon(t3), canon(t4)), (canon(t4), canon(t5))])
        self.assertEqual(
            [[(i["number"], i["revision"]) for i in s["items"]]
             for s in res["segments"]],
            [[("C-A", 0), ("C-B", 0)], [("C-A", 1), ("C-B", 0)],
             [("C-A", 0), ("C-B", 1)], [("C-A", 0), ("C-B", 0)]])
        # 版本切换：首段 False，其后每段相对前一段整组均有变化
        self.assertEqual([s["version_switch"] for s in res["segments"]],
                         [False, True, True, True])
        # 逐项 switched 只标实际切换的编号
        self.assertEqual(
            [[i["switched"] for i in s["items"]] for s in res["segments"]],
            [[False, False], [True, False], [True, True], [False, True]])
        self.assertEqual(res["segments"][1]["items"][0]["body"], "A1")
        self.assertEqual(
            res["segments"][1]["items"][0]["supersedes_revision"], 0)

    def test_numbers_order_preserved(self):
        release("C-Z", body="Z")
        release("C-Y", body="Y")
        res = self._check(["C-Z", "C-Y"])
        self.assertEqual(res["numbers"], ["C-Z", "C-Y"])
        self.assertEqual([i["number"] for i in res["segments"][0]["items"]],
                         ["C-Z", "C-Y"])

    def test_window_clipped_and_outside_versions_ignored(self):
        release("C-O", body="O0")
        correct("C-O", 0, t2, t4, "O1")
        # 作业区间只问 [t3, t5)：端点裁剪到区间
        res = self._check(["C-O"], start=t3, end=t5)
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(t3), canon(t4)), (canon(t4), canon(t5))])
        self.assertEqual([s["items"][0]["revision"] for s in res["segments"]],
                         [1, 0])
        # 完全落在作业区间之外的版本不产生端点
        far = iso(_T + timedelta(days=400))
        release("C-OUT", body="x", start=t1, end=far)
        correct("C-OUT", 0, t5, far, "区间外更正")
        res = self._check(["C-OUT"], start=t1, end=t5)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["segments"][0]["items"][0]["revision"], 0)

    # ---- 缺口（区间外空档） ------------------------------------------------

    def test_gap_segments_for_uncovered_intervals(self):
        release("C-G", body="仅三月", start=t2, end=t3)
        res = self._check(["C-G"])
        self.assertFalse(res["fully_covered"])
        self.assertEqual(res["count"], 3)
        self.assertEqual([s["has_gap"] for s in res["segments"]],
                         [True, False, True])
        gap_item = res["segments"][0]["items"][0]
        self.assertTrue(gap_item["gap"])
        self.assertIsNone(gap_item["revision"])
        self.assertIsNone(gap_item["body"])
        self.assertIsNone(gap_item["published_at"])
        self.assertFalse(gap_item["restored_by_withdrawal"])
        self.assertEqual(res["segments"][1]["items"][0]["revision"], 0)
        # 两端同为缺口但不相邻 -> 不跨段合并
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(t1), canon(t2)), (canon(t2), canon(t3)),
                          (canon(t3), canon(t5))])

    def test_known_as_of_before_publish_yields_gaps(self):
        release("C-N", body="x")
        res = self._check(["C-N"], known=t0)
        self.assertEqual(res["count"], 1)
        self.assertFalse(res["fully_covered"])
        self.assertTrue(res["segments"][0]["items"][0]["gap"])

    def test_unpublished_correction_does_not_split(self):
        pub0 = release("C-U", body="初版")["published_at"]
        correct("C-U", 0, t2, t3, "尚不知悉的更正")
        # 知悉时刻取 r0 发布时刻：r1 尚未发布，不产生端点
        res = self._check(["C-U"], known=pub0)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["segments"][0]["items"][0]["revision"], 0)

    # ---- 撤回恢复 ----------------------------------------------------------

    def test_withdrawal_restoration_marked_and_not_merged(self):
        release("C-W", body="父版本")
        correct("C-W", 0, t2, t3, "误发更正")
        withdraw("C-W", 1)
        res = self._check(["C-W"])
        # 已撤回版本的端点把"恢复父版本"区间精确圈出；两侧同为 r0 但恢复
        # 标记不同 -> 整组结果不同、不合并
        self.assertEqual([(s["start"], s["end"]) for s in res["segments"]],
                         [(canon(t1), canon(t2)), (canon(t2), canon(t3)),
                          (canon(t3), canon(t5))])
        self.assertEqual([s["items"][0]["revision"] for s in res["segments"]],
                         [0, 0, 0])
        self.assertEqual([s["items"][0]["restored_by_withdrawal"]
                          for s in res["segments"]], [False, True, False])
        self.assertEqual([s["restored"] for s in res["segments"]],
                         [False, True, False])
        self.assertTrue(res["fully_covered"])

    def test_restoration_not_marked_before_withdrawal(self):
        release("C-WB", body="父版本")
        correct("C-WB", 0, t2, t3, "更正")
        pub1 = service.list_history("C-WB")["items"][1]["published_at"]
        withdraw("C-WB", 1)
        # 知悉时刻取更正发布时刻（撤回尚未发生）：看到更正版本、无恢复标记
        res = self._check(["C-WB"], known=pub1)
        self.assertEqual([s["items"][0]["revision"] for s in res["segments"]],
                         [0, 1, 0])
        self.assertFalse(any(s["restored"] for s in res["segments"]))

    def test_adjacent_identical_groups_merge(self):
        # 两段相邻的"撤回恢复"区间（r1、r2 先后发布又先后撤回）合并为一段
        release("C-M", body="父版本")
        correct("C-M", 0, t2, t3, "误发一")
        withdraw("C-M", 1)
        correct("C-M", 0, t3, t4, "误发二")
        withdraw("C-M", 2)
        res = self._check(["C-M"])
        self.assertEqual(
            [(s["start"], s["end"], s["items"][0]["restored_by_withdrawal"])
             for s in res["segments"]],
            [(canon(t1), canon(t2), False),
             (canon(t2), canon(t4), True),
             (canon(t4), canon(t5), False)])

    def test_recorrection_after_withdrawal_clears_restored_flag(self):
        # 撤回后在恢复区间再次更正：新更正覆盖处不再是"恢复"
        release("C-RC", body="父版本")
        correct("C-RC", 0, t2, t3, "误发")
        withdraw("C-RC", 1)
        correct("C-RC", 0, t2, t3, "重新发布的更正")
        res = self._check(["C-RC"])
        self.assertEqual([s["items"][0]["revision"] for s in res["segments"]],
                         [0, 2, 0])
        self.assertFalse(any(s["restored"] for s in res["segments"]))

    # ---- 拒绝情形（均只读、不写历史） --------------------------------------

    def test_empty_numbers_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            service.check_coverage([], t1, t5, self._now())
        self.assertEqual(ctx.exception.code, "validation_error")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_duplicate_numbers_rejected(self):
        release("C-D")
        with self.assertRaises(DomainError) as ctx:
            self._check(["C-D", "C-D"])
        self.assertEqual(ctx.exception.code, "duplicate_coverage_number")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details, {"number": "C-D", "index": 1})

    def test_blank_number_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self._check(["   "])
        self.assertEqual(ctx.exception.code, "invalid_number")

    def test_unknown_number_rejected_404(self):
        release("C-K")
        with self.assertRaises(DomainError) as ctx:
            self._check(["C-K", "C-NOPE"])
        self.assertEqual(ctx.exception.code, "procedure_not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details,
                         {"number": "C-NOPE", "index": 1})

    def test_invalid_times_rejected(self):
        release("C-T")
        with self.assertRaises(DomainError) as ctx:
            self._check(["C-T"], start="2026-01-01T00:00:00")
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "work_start")
        with self.assertRaises(DomainError) as ctx:
            self._check(["C-T"], start=t3, end=t2)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            self._check(["C-T"], known="2026-01-01T00:00:00")
        self.assertEqual(ctx.exception.code, "invalid_time")
        self.assertEqual(ctx.exception.details["field"], "known_as_of")

    def test_known_as_of_in_future_rejected(self):
        release("C-F")
        with self.assertRaises(DomainError) as ctx:
            self._check(["C-F"], known=future)
        self.assertEqual(ctx.exception.code, "known_as_of_in_future")
        self.assertEqual(ctx.exception.http_status, 422)

    def test_read_only_does_not_change_history(self):
        release("C-R", body="初版")
        correct("C-R", 0, t2, t3, "更正")
        withdraw("C-R", 1)
        before = service.list_history("C-R")
        feed_before = service.read_change_feed(None, None)
        self._check(["C-R"])
        self._check(["C-R"], known=t0)
        self.assertEqual(service.list_history("C-R"), before)
        self.assertEqual(service.read_change_feed(None, None), feed_before)


def pitem(number: str, base: int, start: str, end: str, body: str = "预演更正",
          prerequisites=None):
    return item(number, body, start, end, base, prerequisites=prerequisites)


class CorrectionPreviewTests(unittest.TestCase):
    """更正批量预演：只读、不占版本号；校验与正式提交逐项一致。"""

    def setUp(self):
        reset_db()
        release("PV-1", body="初版-1", prerequisites=["PV-PRE"])
        release("PV-2", body="初版-2")

    def _preview(self, items, start=t1, end=t5):
        return service.preview_corrections(items, start, end)

    def test_single_preview_predicts_revision_and_segment(self):
        res = self._preview([pitem("PV-1", 0, t2, t3, "三月版")])
        self.assertEqual(res["count"], 1)
        it = res["items"][0]
        self.assertEqual(it["index"], 0)
        self.assertEqual(it["revision"], 1)
        self.assertEqual(it["supersedes_revision"], 0)
        self.assertEqual(it["prerequisites"], ["PV-PRE"])     # 省略继承
        self.assertNotIn("published_at", it)
        ch = res["changes"][0]
        self.assertEqual(ch["number"], "PV-1")
        self.assertEqual(ch["count"], 1)
        seg = ch["segments"][0]
        self.assertEqual((seg["start"], seg["end"]), (canon(t2), canon(t3)))
        self.assertEqual(seg["before"], {"revision": 0, "body": "初版-1"})
        self.assertEqual(seg["after"], {"revision": 1, "body": "三月版"})

    def test_preview_does_not_write_or_consume_revision(self):
        before = service.list_history("PV-1")
        self._preview([pitem("PV-1", 0, t2, t3, "三月版")])
        self.assertEqual(service.list_history("PV-1"), before)
        # 正式提交拿到的版本号仍是 1（预演未占用）
        committed = correct("PV-1", 0, t2, t3, "正式三月版")
        self.assertEqual(committed["items"][0]["revision"], 1)

    def test_batch_applied_in_input_order_later_references_earlier(self):
        res = self._preview([
            pitem("PV-1", 0, t2, t3, "r1"),
            pitem("PV-1", 1, iso(_T + timedelta(days=33)),
                  iso(_T + timedelta(days=40)), "r2"),
        ])
        self.assertEqual([(x["index"], x["revision"], x["supersedes_revision"])
                          for x in res["items"]],
                         [(0, 1, 0), (1, 2, 1)])
        # 3 月子区间被切成 r1 / r2 / r1 三段，两侧版本不同
        sides = [(s["before"]["revision"], s["after"]["revision"])
                 for s in res["changes"][0]["segments"]]
        self.assertEqual(sides, [(0, 1), (0, 2), (0, 1)])

    def test_chained_preview_revision_starts_after_existing_max(self):
        correct("PV-1", 0, t2, t3, "已发布 r1")
        res = self._preview([pitem("PV-1", 0, t3, t4, "下一条")])
        self.assertEqual(res["items"][0]["revision"], 2)

    def test_multiple_procedures_changes_in_first_appearance_order(self):
        res = self._preview([
            pitem("PV-2", 0, t3, t4, "PV-2 更正"),
            pitem("PV-1", 0, t2, t3, "PV-1 更正"),
        ])
        self.assertEqual([c["number"] for c in res["changes"]],
                         ["PV-2", "PV-1"])

    def test_no_change_outside_work_window(self):
        # 更正落在 3 月；只问 6-7 月作业区间 -> changes 为空但仍返回拟生成版本
        res = self._preview([pitem("PV-1", 0, t2, t3, "三月")],
                            start=iso(_T + timedelta(days=150)),
                            end=iso(_T + timedelta(days=180)))
        self.assertEqual(res["items"][0]["revision"], 1)
        self.assertEqual(res["changes"][0]["count"], 0)
        self.assertEqual(res["changes"][0]["segments"], [])

    def test_segments_clipped_to_work_window(self):
        res = self._preview([pitem("PV-1", 0, t2, t4, "跨段更正")],
                            start=t3, end=t4)
        segs = res["changes"][0]["segments"]
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0]["start"], segs[0]["end"]),
                         (canon(t3), canon(t4)))

    def test_prerequisites_inherit_clear_and_replace(self):
        inherited = self._preview([pitem("PV-1", 0, t3, t4, "x")])
        self.assertEqual(inherited["items"][0]["prerequisites"], ["PV-PRE"])
        cleared = self._preview(
            [pitem("PV-1", 0, t3, t4, "x", prerequisites=[])])
        self.assertEqual(cleared["items"][0]["prerequisites"], [])
        replaced = self._preview(
            [pitem("PV-1", 0, t3, t4, "x",
                   prerequisites=["PV-PRE", "PV-OTHER"])])
        self.assertEqual(replaced["items"][0]["prerequisites"],
                         ["PV-PRE", "PV-OTHER"])

    def test_error_missing_procedure_with_index(self):
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0, t3, t4, "ok"),
                           pitem("PV-NOPE", 0, t3, t4, "bad")])
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_error_base_version_not_found(self):
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 7, t3, t4, "x")])
        self.assertEqual(ctx.exception.code, "base_version_not_found")
        self.assertEqual(ctx.exception.http_status, 404)

    def test_error_interval_outside_base(self):
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0, t2, future, "x")])
        self.assertEqual(ctx.exception.code, "invalid_interval")

    def test_error_expired_base_version(self):
        correct("PV-1", 0, t2, t3, "已占用 3 月")
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0,
                                 iso(_T + timedelta(days=25)),
                                 iso(_T + timedelta(days=35)), "x")])
        self.assertEqual(ctx.exception.code, "expired_base_version")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertIn("remaining_intervals", ctx.exception.details)

    def test_error_concurrent_overlap_in_batch(self):
        with self.assertRaises(DomainError) as ctx:
            self._preview([
                pitem("PV-1", 0, iso(_T + timedelta(days=120)),
                      iso(_T + timedelta(days=140)), "A"),
                pitem("PV-1", 0, iso(_T + timedelta(days=130)),
                      iso(_T + timedelta(days=150)), "B"),
            ])
        self.assertEqual(ctx.exception.code,
                         "concurrent_correction_conflict")
        self.assertEqual(ctx.exception.details["index"], 1)

    def test_error_withdrawn_base(self):
        correct("PV-1", 0, t2, t3, "误发布")
        withdraw("PV-1", 1)
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 1,
                                 iso(_T + timedelta(days=33)),
                                 iso(_T + timedelta(days=35)), "x")])
        self.assertEqual(ctx.exception.code, "base_version_withdrawn")

    def test_error_blank_and_duplicate_prerequisite(self):
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0, t3, t4, "x",
                                 prerequisites=["  "])])
        self.assertEqual(ctx.exception.code, "invalid_number")
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0, t3, t4, "x",
                                 prerequisites=["Q", "Q"])])
        self.assertEqual(ctx.exception.code,
                         "duplicate_prerequisite_number")

    def test_error_invalid_work_window_and_time(self):
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0, t3, t4, "x")],
                          start=t4, end=t4)
        self.assertEqual(ctx.exception.code, "invalid_interval")
        with self.assertRaises(DomainError) as ctx:
            self._preview([pitem("PV-1", 0, t3, t4, "x")],
                          start="2026-01-01T00:00:00", end=t5)
        self.assertEqual(ctx.exception.code, "invalid_time")

    def test_error_is_read_only(self):
        before = service.list_history("PV-1")
        with self.assertRaises(DomainError):
            self._preview([pitem("PV-1", 0, t2, t3, "ok"),
                           pitem("PV-1", 0,
                                 iso(_T + timedelta(days=25)),
                                 iso(_T + timedelta(days=35)), "overlap")])
        self.assertEqual(service.list_history("PV-1"), before)

    def test_error_matches_actual_submit(self):
        """预演拒绝的批次，正式提交也拒绝且错误码一致（反之亦然）。"""
        items = [pitem("PV-1", 0,
                       iso(_T + timedelta(days=120)),
                       iso(_T + timedelta(days=140)), "A"),
                 pitem("PV-1", 0,
                       iso(_T + timedelta(days=130)),
                       iso(_T + timedelta(days=150)), "B")]
        with self.assertRaises(DomainError) as pv:
            self._preview(items)
        with self.assertRaises(DomainError) as sub:
            service.submit_corrections(items)
        self.assertEqual(pv.exception.code, sub.exception.code)
        self.assertEqual(pv.exception.details.get("index"),
                         sub.exception.details.get("index"))

    def test_preview_then_later_concurrent_change_not_committable(self):
        """预演结果不保证可提交：预演后历史变化，正式提交按当时状态重新校验。"""
        items = [pitem("PV-1", 0, t2, t3, "预演时合法")]
        # 预演时基版剩余区间充足
        self.assertEqual(self._preview(items)["items"][0]["revision"], 1)
        # 另一值班员先提交了同一区间的更正
        correct("PV-1", 0, t2, t3, "抢先发布")
        with self.assertRaises(DomainError) as ctx:
            service.submit_corrections(items)
        self.assertEqual(ctx.exception.code, "expired_base_version")


class ApiTests(unittest.TestCase):
    """FastAPI 层：仅在依赖可用时运行。"""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            from app.main import app
        except Exception:
            raise unittest.SkipTest("fastapi/httpx 未安装，跳过 API 测试")
        cls.client = TestClient(app)

    def _n(self):
        # 编号全库唯一
        return f"API-{id(self)}-{self._seq}"

    def setUp(self):
        reset_db()
        self._seq = ApiTests._seq = getattr(ApiTests, "_seq", 0) + 1

    def test_full_flow_over_http(self):
        c = self.client
        n = self._n()
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "A", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["items"][0]["revision"], 0)

        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "B"})
        self.assertEqual(r.status_code, 201)

        r = c.get("/api/v1/effective", params={"work_time": t2})
        self.assertEqual(r.status_code, 200)
        found = [i for i in r.json()["items"] if i["number"] == n]
        self.assertEqual(found[0]["body"], "B")

        # 顺序提交且与已发布更正重叠 -> 409 过期基版
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": iso(_T + timedelta(days=40)),
            "valid_end": iso(_T + timedelta(days=50)), "body": "X"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "expired_base_version")

        # 同批冲突 -> 409 并发更正冲突
        r = c.post("/api/v1/corrections", json={"items": [
            {"number": n, "base_revision": 0,
             "valid_start": iso(_T + timedelta(days=120)),
             "valid_end": iso(_T + timedelta(days=140)), "body": "P"},
            {"number": n, "base_revision": 0,
             "valid_start": iso(_T + timedelta(days=130)),
             "valid_end": iso(_T + timedelta(days=150)), "body": "Q"},
        ]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"],
                         "concurrent_correction_conflict")

        # 重复编号 409
        r = c.post("/api/v1/releases", json={
            "number": n, "body": "A2", "valid_start": t1, "valid_end": t5})
        self.assertEqual(r.status_code, 409)

        # 历史
        r = c.get(f"/api/v1/procedures/{n}/history")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 2)
        r = c.get("/api/v1/procedures/NO-SUCH/history")
        self.assertEqual(r.status_code, 404)

    def test_extra_field_published_at_forbidden(self):
        r = self.client.post("/api/v1/releases", json={
            "number": self._n(), "body": "A",
            "valid_start": t1, "valid_end": t5,
            "published_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

    def test_diff_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})

        # 前侧早于发布 -> 三段 null->r0/r1/r0
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": t0, "known_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([s["after"]["revision"] for s in body["segments"]],
                         [0, 1, 0])
        self.assertTrue(all(s["before"] is None
                            for s in body["segments"]))

        # 编号不存在 -> 404，不区分入参先后
        r = c.get("/api/v1/procedures/NO-SUCH/diff", params={
            "known_before": t0, "known_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

        # 知悉顺序颠倒 -> 422 invalid_known_order
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": future, "known_after": t0,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_known_order")

        # 空作业区间 -> 422 invalid_interval
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": t0, "known_after": future,
            "work_start": t2, "work_end": t2})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")

        # 无时区 -> 422 invalid_time
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": "2026-01-01T00:00:00", "known_after": future,
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")

        # 缺少必填参数 -> 422 validation_error
        r = c.get(f"/api/v1/procedures/{n}/diff", params={
            "known_before": t0, "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)

    def test_withdraw_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})

        # 撤回前：3 月 -> 三月版
        r = c.get("/api/v1/effective", params={"work_time": t2})
        self.assertEqual([i for i in r.json()["items"] if i["number"] == n][0]["body"],
                         "三月版")

        # 撤回 r1
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 1})
        self.assertEqual(r.status_code, 201, r.text)
        payload = r.json()
        self.assertTrue(payload["withdrawn_at"].endswith("Z"))
        item = payload["items"][0]
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["supersedes_revision"], 0)
        self.assertEqual(item["withdrawn_at"], payload["withdrawn_at"])

        # 撤回后：3 月恢复初版
        r = c.get("/api/v1/effective", params={"work_time": t2})
        found = [i for i in r.json()["items"] if i["number"] == n][0]
        self.assertEqual(found["revision"], 0)
        self.assertEqual(found["body"], "初版")

        # 历史仍有 r1，且带 withdrawn_at
        r = c.get(f"/api/v1/procedures/{n}/history")
        revs = {v["revision"]: v for v in r.json()["items"]}
        self.assertEqual(len(revs), 2)
        self.assertIsNotNone(revs[1]["withdrawn_at"])

        # 重复撤回 -> 409 already_withdrawn
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "already_withdrawn")

        # 首版 -> 409
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 0})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"],
                         "first_revision_not_withdrawable")

        # 编号不存在 -> 404；版本号不存在 -> 404 revision_not_found
        r = c.post("/api/v1/withdrawals", json={"number": "NO-SUCH", "revision": 1})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 99})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "revision_not_found")

        # 额外字段（试图回填撤回时刻）-> 422 validation_error
        r = c.post("/api/v1/withdrawals", json={
            "number": n, "revision": 1,
            "withdrawn_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 撤回后父版本可在恢复区间再次更正
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "修正后的三月版"})
        self.assertEqual(r.status_code, 201, r.text)
        r = c.get("/api/v1/effective", params={"work_time": t2})
        found = [i for i in r.json()["items"] if i["number"] == n][0]
        self.assertEqual(found["body"], "修正后的三月版")

    def test_withdraw_children_and_batch_conflict_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t4, "body": "r1"})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 1,
            "valid_start": t3, "valid_end": t4, "body": "r2"})

        # 有子更正的 r1 不可撤回
        r = c.post("/api/v1/withdrawals", json={"number": n, "revision": 1})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "version_has_children")
        self.assertEqual(r.json()["error"]["details"]["child_revisions"], [2])

        # 以已撤回版本为基版更正 -> 409 base_version_withdrawn
        c.post("/api/v1/withdrawals", json={"number": n, "revision": 2})
        r = c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 2,
            "valid_start": t3, "valid_end": t4, "body": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "base_version_withdrawn")

        # 批量：用一个全新规程，第二条非法（首版）-> 整批回滚，
        # 第一条（合法叶子更正）的撤回也不生效。
        n2 = n + "-B"
        c.post("/api/v1/releases", json={
            "number": n2, "body": "初版", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n2, "base_revision": 0,
            "valid_start": t2, "valid_end": t4, "body": "r1"})
        c.post("/api/v1/corrections", json={
            "number": n2, "base_revision": 1,
            "valid_start": t3, "valid_end": t4, "body": "r2"})
        r = c.post("/api/v1/withdrawals", json={"items": [
            {"number": n2, "revision": 2},
            {"number": n2, "revision": 0},
        ]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["details"]["index"], 1)
        hist = {v["revision"]: v for v in
                c.get(f"/api/v1/procedures/{n2}/history").json()["items"]}
        self.assertIsNone(hist[2]["withdrawn_at"])

    def test_health_and_empty_result(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        r = self.client.get("/api/v1/effective",
                            params={"work_time": "2001-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)
        self.assertEqual(r.json()["items"], [])

    def test_packages_over_http(self):
        c = self.client
        n1, n2 = self._n(), self._n() + "-2"
        c.post("/api/v1/releases", json={
            "number": n1, "body": "初版", "valid_start": t1,
            "valid_end": t5})
        # 知悉时刻取最后一次发布的时刻（保证两个规程均已发布且 <= 当前时刻）
        pub = c.post("/api/v1/releases", json={
            "number": n2, "body": "另一个", "valid_start": t1,
            "valid_end": t5}).json()["published_at"]
        # 以发布时刻为知悉时刻（必然 <= 服务端当前时刻，且已可见）
        body = {"work_time": t2, "known_as_of": pub,
                "numbers": [n2, n1]}
        r = c.post("/api/v1/packages", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        pkg = r.json()
        self.assertRegex(pkg["id"], r"^[0-9a-f]{32}$")
        # 保留调用方传入的编号顺序
        self.assertEqual([i["number"] for i in pkg["items"]], [n2, n1])
        self.assertEqual(pkg["items"][1]["body"], "初版")
        package_id = pkg["id"]

        # 原样取回
        r = c.get(f"/api/v1/packages/{package_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), pkg)

        # 固定后更正：取回内容不变；复核显示变化
        c.post("/api/v1/corrections", json={
            "number": n1, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "三月版"})
        r = c.get(f"/api/v1/packages/{package_id}")
        self.assertEqual(
            [i for i in r.json()["items"] if i["number"] == n1][0]["body"],
            "初版")

        r = c.get(f"/api/v1/packages/{package_id}/review",
                  params={"known_as_of": now_canonical()})
        self.assertEqual(r.status_code, 200, r.text)
        review = r.json()
        changed = {i["number"]: i for i in review["items"]
                   if i["changed"]}
        self.assertIn(n1, changed)
        self.assertEqual(changed[n1]["pinned"]["revision"], 0)
        self.assertEqual(changed[n1]["current"]["revision"], 1)
        self.assertFalse(review["all_match"])
        self.assertEqual(review["package_known_as_of"], canon(pub))

        # 不存在的包 -> 404（取回与复核一致）
        r = c.get("/api/v1/packages/nope")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "package_not_found")
        r = c.get("/api/v1/packages/nope/review", params={"known_as_of": pub})
        self.assertEqual(r.status_code, 404)

    def test_packages_reject_over_http(self):
        c = self.client
        n = self._n()
        pub = c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1,
            "valid_end": t5}).json()["published_at"]

        # 编号重复 -> 422 duplicate_package_number
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n, n]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "duplicate_package_number")

        # 编号缺失 -> 404 procedure_not_found
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n, "NOPE"]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "procedure_not_found")

        # 作业时刻无适用正文 -> 409 no_applicable_version
        r = c.post("/api/v1/packages", json={
            "work_time": t5, "known_as_of": pub, "numbers": [n]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "no_applicable_version")

        # 知悉时刻在未来 -> 422 known_as_of_in_future
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": future, "numbers": [n]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "known_as_of_in_future")

        # 缺时区 -> 422 invalid_time
        r = c.post("/api/v1/packages", json={
            "work_time": "2026-01-01T00:00:00",
            "known_as_of": pub, "numbers": [n]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")

        # 空编号列表 / 缺字段 / 额外字段 -> 422 validation_error
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": []})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "numbers": [n]})
        self.assertEqual(r.status_code, 422)
        r = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n],
            "created_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(r.status_code, 422)

        # 复核知悉时刻早于包内知悉时刻 -> 422 review_known_too_early
        pkg = c.post("/api/v1/packages", json={
            "work_time": t2, "known_as_of": pub, "numbers": [n]}).json()
        r = c.get(f"/api/v1/packages/{pkg['id']}/review",
                  params={"known_as_of": t0})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "review_known_too_early")
        # 复核缺少必填参数 -> 422
        r = c.get(f"/api/v1/packages/{pkg['id']}/review")
        self.assertEqual(r.status_code, 422)

    def test_prerequisites_and_closure_over_http(self):
        c = self.client
        pre = self._n() + "-PRE"
        root = self._n() + "-ROOT"
        c.post("/api/v1/releases", json={
            "number": pre, "body": "前置正文", "valid_start": t1,
            "valid_end": t5})
        # 首次发布携带前置编号列表
        r = c.post("/api/v1/releases", json={
            "number": root, "body": "根初版", "valid_start": t1,
            "valid_end": t5, "prerequisites": [pre]})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["items"][0]["prerequisites"], [pre])

        # 更正省略 -> 继承被替代版本的前置列表
        r = c.post("/api/v1/corrections", json={
            "number": root, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "根三月版"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["items"][0]["prerequisites"], [pre])

        # 递归解析：前置优先、根最后，含编号/版本/正文
        r = c.get(f"/api/v1/procedures/{root}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["root"], root)
        self.assertEqual([i["number"] for i in body["items"]], [pre, root])
        self.assertEqual(body["items"][0]["body"], "前置正文")
        self.assertEqual(body["items"][1]["revision"], 1)  # 3 月适用更正版
        self.assertEqual(body["items"][1]["prerequisites"], [pre])

        # 历史审计：每个版本各自的前置列表
        r = c.get(f"/api/v1/procedures/{root}/history")
        revs = {v["revision"]: v for v in r.json()["items"]}
        self.assertEqual(revs[0]["prerequisites"], [pre])
        self.assertEqual(revs[1]["prerequisites"], [pre])

        # 显式空列表清空：4 月起的更正不再携带前置
        r = c.post("/api/v1/corrections", json={
            "number": root, "base_revision": 0,
            "valid_start": t3, "valid_end": t4, "body": "根四月版",
            "prerequisites": []})
        self.assertEqual(r.json()["items"][0]["prerequisites"], [])
        r = c.get(f"/api/v1/procedures/{root}/closure",
                  params={"work_time": t3})
        self.assertEqual([i["number"] for i in r.json()["items"]], [root])

    def test_closure_errors_over_http(self):
        c = self.client
        n = self._n()
        # 根不存在 -> 404 not_found
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

        # 引用编号不存在 -> 404 prerequisite_not_found（details 带引用链）
        c.post("/api/v1/releases", json={
            "number": n, "body": "根", "valid_start": t1, "valid_end": t5,
            "prerequisites": ["NO-SUCH-PRE"]})
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 404)
        err = r.json()["error"]
        self.assertEqual(err["code"], "prerequisite_not_found")
        self.assertEqual(err["details"]["number"], "NO-SUCH-PRE")
        self.assertEqual(err["details"]["path"], [n, "NO-SUCH-PRE"])

        # 作业时刻无适用正文 -> 409 no_applicable_version
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": t5})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "no_applicable_version")

        # 引用成环 -> 409 prerequisite_cycle（details 带环路径）
        a, b = self._n() + "-A", self._n() + "-B"
        c.post("/api/v1/releases", json={
            "number": a, "body": "A", "valid_start": t1, "valid_end": t5,
            "prerequisites": [b]})
        c.post("/api/v1/releases", json={
            "number": b, "body": "B", "valid_start": t1, "valid_end": t5,
            "prerequisites": [a]})
        r = c.get(f"/api/v1/procedures/{a}/closure",
                  params={"work_time": t2})
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "prerequisite_cycle")
        self.assertEqual(err["details"]["cycle"], [a, b, a])

        # 时间缺时区 -> 422 invalid_time；缺 work_time -> 422 validation_error
        r = c.get(f"/api/v1/procedures/{n}/closure",
                  params={"work_time": "2026-01-01T00:00:00"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")
        r = c.get(f"/api/v1/procedures/{n}/closure")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 前置编号空白/重复 -> 422；旧式请求（不带 prerequisites）仍可用
        r = c.post("/api/v1/releases", json={
            "number": self._n(), "body": "x", "valid_start": t1,
            "valid_end": t5, "prerequisites": [" "]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_number")
        r = c.post("/api/v1/releases", json={
            "number": self._n(), "body": "x", "valid_start": t1,
            "valid_end": t5, "prerequisites": ["P", "P"]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "duplicate_prerequisite_number")
        r = c.post("/api/v1/releases", json={
            "number": self._n() + "-OLD", "body": "旧式", "valid_start": t1,
            "valid_end": t5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["items"][0]["prerequisites"], [])

    def test_closure_plan_over_http(self):
        c = self.client
        pre, pre2, root = (self._n() + "-P1", self._n() + "-P2",
                           self._n() + "-ROOT")
        c.post("/api/v1/releases", json={
            "number": pre, "body": "前置", "valid_start": t1,
            "valid_end": t5})
        c.post("/api/v1/releases", json={
            "number": pre2, "body": "前置2", "valid_start": t1,
            "valid_end": t5})
        c.post("/api/v1/releases", json={
            "number": root, "body": "根初版", "valid_start": t1,
            "valid_end": t5, "prerequisites": [pre]})
        # 3 月更正：根换版本并改为引用前置 2
        c.post("/api/v1/corrections", json={
            "number": root, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "根三月版",
            "prerequisites": [pre2]})

        r = c.get(f"/api/v1/procedures/{root}/closure-plan", params={
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["root"], root)
        self.assertTrue(body["available"])
        self.assertEqual(body["count"], 3)
        self.assertEqual(
            [[i["number"] for i in s["items"]] for s in body["segments"]],
            [[pre, root], [pre2, root], [pre, root]])
        self.assertEqual(body["segments"][1]["items"][1]["revision"], 1)
        for seg in body["segments"]:
            self.assertTrue(seg["available"])
            self.assertEqual(seg["gaps"], [])
            self.assertIsNone(seg["cycle"])
        # 片段覆盖全区间、首尾相接
        self.assertEqual(body["segments"][0]["start"], canon(t1))
        self.assertEqual(body["segments"][-1]["end"], canon(t5))
        for a, b in zip(body["segments"], body["segments"][1:]):
            self.assertEqual(a["end"], b["start"])

        # 端点已列入根索引
        self.assertIn("GET  /api/v1/procedures/{number}/closure-plan",
                      c.get("/").json()["endpoints"])

    def test_closure_plan_gaps_and_cycle_over_http(self):
        c = self.client
        n = self._n()
        # 3 月更正引用不存在的编号：片段保留并标缺口链、整体不可用（仍 200）
        c.post("/api/v1/releases", json={
            "number": n, "body": "根", "valid_start": t1, "valid_end": t5,
            "prerequisites": []})
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "根三月",
            "prerequisites": ["NO-SUCH-PRE"]})
        r = c.get(f"/api/v1/procedures/{n}/closure-plan",
                  params={"work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["available"])
        middle = body["segments"][1]
        self.assertFalse(middle["available"])
        self.assertEqual(middle["gaps"], [{
            "code": "prerequisite_not_found",
            "number": "NO-SUCH-PRE",
            "path": [n, "NO-SUCH-PRE"]}])
        self.assertIsNone(middle["cycle"])
        self.assertTrue(body["segments"][0]["available"])

        # 根编号不存在：单片段、not_found 缺口、整体不可用
        r = c.get("/api/v1/procedures/NO-SUCH-ROOT/closure-plan",
                  params={"work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["segments"][0]["gaps"][0]["code"], "not_found")

        # 成环：环片段带 cycle 环路径
        a, b = self._n() + "-A", self._n() + "-B"
        c.post("/api/v1/releases", json={
            "number": a, "body": "A", "valid_start": t1, "valid_end": t5,
            "prerequisites": [b]})
        c.post("/api/v1/releases", json={
            "number": b, "body": "B", "valid_start": t1, "valid_end": t5,
            "prerequisites": [a]})
        r = c.get(f"/api/v1/procedures/{a}/closure-plan",
                  params={"work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200)
        seg = r.json()["segments"][0]
        self.assertFalse(seg["available"])
        self.assertEqual(seg["cycle"], [a, b, a])

    def test_closure_plan_bad_params_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "根", "valid_start": t1, "valid_end": t5})
        # 空作业区间 -> 422 invalid_interval（不写历史）
        r = c.get(f"/api/v1/procedures/{n}/closure-plan",
                  params={"work_start": t2, "work_end": t2})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")
        # 起 > 止
        r = c.get(f"/api/v1/procedures/{n}/closure-plan",
                  params={"work_start": t5, "work_end": t1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")
        # 缺时区 -> 422 invalid_time
        r = c.get(f"/api/v1/procedures/{n}/closure-plan", params={
            "work_start": "2026-01-01T00:00:00", "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")
        r = c.get(f"/api/v1/procedures/{n}/closure-plan", params={
            "work_start": t1, "work_end": t5,
            "known_as_of": "2026-01-01T00:00:00"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["details"]["field"],
                         "known_as_of")
        # 缺少必填参数 -> 422 validation_error
        r = c.get(f"/api/v1/procedures/{n}/closure-plan",
                  params={"work_start": t1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")
        # 错误不写历史
        self.assertEqual(c.get(f"/api/v1/procedures/{n}/history")
                         .json()["count"], 1)

    def test_correction_preview_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5,
            "prerequisites": ["PRE-X"]})

        # 批量预演：同批先 r0->r1（3 月），再 r1->r2（3 月 10-20 日）
        r = c.post("/api/v1/corrections/preview", json={
            "work_start": t1, "work_end": t5,
            "items": [
                {"number": n, "base_revision": 0,
                 "valid_start": t2, "valid_end": t3, "body": "三月"},
                {"number": n, "base_revision": 1,
                 "valid_start": iso(_T + timedelta(days=33)),
                 "valid_end": iso(_T + timedelta(days=40)),
                 "body": "三月再更正"}]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([(x["index"], x["revision"]) for x in body["items"]],
                         [(0, 1), (1, 2)])
        # 省略继承被替代版本前置
        self.assertEqual(body["items"][0]["prerequisites"], ["PRE-X"])
        self.assertEqual(body["items"][1]["prerequisites"], ["PRE-X"])
        sides = [(s["before"]["revision"], s["after"]["revision"])
                 for s in body["changes"][0]["segments"]]
        self.assertEqual(sides, [(0, 1), (0, 2), (0, 1)])
        for key in ("work_start", "work_end", "known_as_of"):
            self.assertIn(key, body)

        # 预演不写历史：仍只有 r0，正式提交拿到 r1
        self.assertEqual(c.get(f"/api/v1/procedures/{n}/history")
                         .json()["items"][-1]["revision"], 0)

        # 单条对象形式（作业区间随条目给出）
        r = c.post("/api/v1/corrections/preview", json={
            "number": n, "base_revision": 0,
            "valid_start": t3, "valid_end": t4, "body": "四月",
            "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["revision"], 1)
        self.assertEqual(body["changes"][0]["segments"][0]["after"],
                         {"revision": 1, "body": "四月"})
        self.assertEqual(c.get(f"/api/v1/procedures/{n}/history")
                         .json()["items"][-1]["revision"], 0)

    def test_correction_preview_errors_over_http(self):
        c = self.client
        n = self._n()
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})

        # 同批并发更正冲突 -> 409，details.index 指出出错位置（第 2 条）
        r = c.post("/api/v1/corrections/preview", json={
            "work_start": t1, "work_end": t5,
            "items": [
                {"number": n, "base_revision": 0,
                 "valid_start": iso(_T + timedelta(days=120)),
                 "valid_end": iso(_T + timedelta(days=140)), "body": "A"},
                {"number": n, "base_revision": 0,
                 "valid_start": iso(_T + timedelta(days=130)),
                 "valid_end": iso(_T + timedelta(days=150)),
                 "body": "B(冲突)"}]})
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "concurrent_correction_conflict")
        self.assertEqual(err["details"]["index"], 1)

        # 过期基版（占用 3 月的更正已发布）-> 409 expired_base_version，
        # 错误原因与正式提交一致且带 remaining_intervals
        c.post("/api/v1/corrections", json={
            "number": n, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "已发布"})
        r = c.post("/api/v1/corrections/preview", json={
            "work_start": t1, "work_end": t5,
            "items": [{"number": n, "base_revision": 0,
                       "valid_start": iso(_T + timedelta(days=25)),
                       "valid_end": iso(_T + timedelta(days=35)),
                       "body": "落在已覆盖处"}]})
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "expired_base_version")
        self.assertIn("remaining_intervals", err["details"])

        # 编号不存在 -> 404 not_found
        r = c.post("/api/v1/corrections/preview", json={
            "work_start": t1, "work_end": t5,
            "items": [{"number": "NO-SUCH-PV", "base_revision": 0,
                       "valid_start": t2, "valid_end": t3, "body": "x"}]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

        # 空作业区间 -> 422 invalid_interval
        r = c.post("/api/v1/corrections/preview", json={
            "work_start": t2, "work_end": t2,
            "items": [{"number": n, "base_revision": 0,
                       "valid_start": t3, "valid_end": t4, "body": "x"}]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")

        # 缺少作业区间字段 -> 422 validation_error
        r = c.post("/api/v1/corrections/preview", json={
            "items": [{"number": n, "base_revision": 0,
                       "valid_start": t3, "valid_end": t4, "body": "x"}]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 额外字段（如回填发布时刻）-> 422 validation_error
        r = c.post("/api/v1/corrections/preview", json={
            "work_start": t1, "work_end": t5,
            "items": [{"number": n, "base_revision": 0,
                       "valid_start": t3, "valid_end": t4, "body": "x",
                       "published_at": t1}]})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 全程只读：本用例仅正式提交过 1 条更正（r0、r1 共 2 行），预演不增加
        self.assertEqual(c.get(f"/api/v1/procedures/{n}/history")
                         .json()["count"], 2)
        self.assertEqual(c.get(f"/api/v1/procedures/{n}/history")
                         .json()["items"][-1]["revision"], 1)

    def test_coverage_check_over_http(self):
        c = self.client
        n1, n2 = self._n() + "-1", self._n() + "-2"
        c.post("/api/v1/releases", json={
            "number": n1, "body": "A0", "valid_start": t1, "valid_end": t5})
        c.post("/api/v1/corrections", json={
            "number": n1, "base_revision": 0,
            "valid_start": t2, "valid_end": t3, "body": "A1"})
        # 知悉时刻取最后一笔写入的服务端发布时刻：此前提交全部可见
        pub = c.post("/api/v1/releases", json={
            "number": n2, "body": "B0", "valid_start": t1,
            "valid_end": t5}).json()["published_at"]

        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1, n2], "work_start": t1, "work_end": t5,
            "known_as_of": pub})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["numbers"], [n1, n2])
        self.assertTrue(body["fully_covered"])
        self.assertEqual(body["count"], 3)
        self.assertEqual([s["start"] for s in body["segments"]],
                         [canon(t1), canon(t2), canon(t3)])
        self.assertEqual(
            [[i["revision"] for i in s["items"]] for s in body["segments"]],
            [[0, 0], [1, 0], [0, 0]])
        self.assertEqual([s["version_switch"] for s in body["segments"]],
                         [False, True, True])
        self.assertEqual(body["segments"][1]["items"][0]["body"], "A1")

        # 编号重复 -> 422 duplicate_coverage_number
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1, n1], "work_start": t1, "work_end": t5,
            "known_as_of": pub})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"],
                         "duplicate_coverage_number")

        # 编号不存在 -> 404 procedure_not_found
        r = c.post("/api/v1/coverage/check", json={
            "numbers": ["NO-SUCH"], "work_start": t1, "work_end": t5,
            "known_as_of": pub})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "procedure_not_found")

        # 空编号列表 / 缺 known_as_of / 额外字段 -> 422 validation_error
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [], "work_start": t1, "work_end": t5,
            "known_as_of": pub})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1], "work_start": t1, "work_end": t5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1], "work_start": t1, "work_end": t5,
            "known_as_of": pub, "created_at": pub})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "validation_error")

        # 知悉时刻晚于服务端当前时刻 -> 422 known_as_of_in_future
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1], "work_start": t1, "work_end": t5,
            "known_as_of": future})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "known_as_of_in_future")

        # 空作业区间 -> 422 invalid_interval；缺时区 -> 422 invalid_time
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1], "work_start": t2, "work_end": t2,
            "known_as_of": pub})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_interval")
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1], "work_start": "2026-01-01T00:00:00",
            "work_end": t5, "known_as_of": pub})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_time")

        # 全程只读：两次调用后历史不变（仍 r0/r1 两个版本 + n2 的 r0）
        r = c.post("/api/v1/coverage/check", json={
            "numbers": [n1, n2], "work_start": t1, "work_end": t5,
            "known_as_of": pub})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get(f"/api/v1/procedures/{n1}/history")
                         .json()["count"], 2)
        self.assertEqual(c.get(f"/api/v1/procedures/{n2}/history")
                         .json()["count"], 1)

    def test_audit_feed_over_http(self):
        c = self.client
        n = self._n()
        # 空库：起始游标空页
        r = c.get("/api/v1/audit/events")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["events"], [])
        self.assertEqual(r.json()["next_cursor"], "0")
        self.assertFalse(r.json()["has_more"])

        # 首次发布（单事件批次）
        c.post("/api/v1/releases", json={
            "number": n, "body": "初版", "valid_start": t1, "valid_end": t5})
        # 批量更正 2 条（同一请求顺序，一个 2 事件批次）
        r = c.post("/api/v1/corrections", json={"items": [
            {"number": n, "base_revision": 0,
             "valid_start": t2, "valid_end": t3, "body": "三月"},
            {"number": n, "base_revision": 1,
             "valid_start": iso(_T + timedelta(days=33)),
             "valid_end": iso(_T + timedelta(days=40)), "body": "三月中"}]})
        self.assertEqual(r.status_code, 201)
        # 撤回 r2（单事件批次）
        c.post("/api/v1/withdrawals", json={"number": n, "revision": 2})

        r = c.get("/api/v1/audit/events", params={"limit": 10})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(
            [(e["operation"], e["number"], e["revision"])
             for e in body["events"]],
            [("release", n, 0), ("correction", n, 1),
             ("correction", n, 2), ("withdrawal", n, 2)])
        # 服务端时刻齐备且为 UTC 微秒串
        for e in body["events"]:
            self.assertRegex(e["event_time"],
                             r"^\d{4}-\d{2}-\d{2}T.*\.\d{6}Z$")
        # 同批两条更正 batch_seq 相同、position 为 0/1
        self.assertEqual(body["events"][1]["batch_seq"],
                         body["events"][2]["batch_seq"])
        self.assertEqual([body["events"][1]["position"],
                          body["events"][2]["position"]], [0, 1])
        self.assertFalse(body["has_more"])

        # 翻页：limit=1 装不下 2 事件批次 -> 422 page_limit_too_small，
        # 游标仍可原样重试（不前进）
        first_cursor = body["events"][0]["batch_seq"]
        r = c.get("/api/v1/audit/events",
                  params={"cursor": str(first_cursor), "limit": 1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "page_limit_too_small")
        self.assertEqual(r.json()["error"]["details"]["batch_size"], 2)
        # 调大 limit 后同一游标可取回后续（2 事件批次 + 撤回共 3 事件）
        r = c.get("/api/v1/audit/events",
                  params={"cursor": str(first_cursor), "limit": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            [(e["operation"], e["revision"]) for e in r.json()["events"]],
            [("correction", 1), ("correction", 2), ("withdrawal", 2)])
        self.assertFalse(r.json()["has_more"])

        # 空页保持游标不变
        tail_cursor = r.json()["next_cursor"]
        r = c.get("/api/v1/audit/events", params={"cursor": tail_cursor})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["events"], [])
        self.assertEqual(r.json()["next_cursor"], tail_cursor)

        # 端点已列入根索引
        self.assertIn("GET  /api/v1/audit/events",
                      c.get("/").json()["endpoints"])

    def test_audit_feed_bad_params_over_http(self):
        c = self.client
        for bad in ("abc", "-1", "999999"):
            r = c.get("/api/v1/audit/events", params={"cursor": bad})
            self.assertEqual(r.status_code, 422, bad)
            self.assertEqual(r.json()["error"]["code"], "invalid_cursor")
        for bad in ("0", "-5", "1001", "x"):
            r = c.get("/api/v1/audit/events", params={"limit": bad})
            self.assertEqual(r.status_code, 422, bad)
            self.assertEqual(r.json()["error"]["code"], "invalid_page_limit")


class AuditFeedTests(unittest.TestCase):
    """值班审计变动日志：按提交顺序增量取回，批次原子分页。"""

    def setUp(self):
        reset_db()

    def test_empty_feed_from_origin(self):
        page = service.read_change_feed(None, None)
        self.assertEqual(page["count"], 0)
        self.assertEqual(page["events"], [])
        self.assertEqual(page["cursor"], "0")
        self.assertEqual(page["next_cursor"], "0")
        self.assertFalse(page["has_more"])
        # 默认上限
        self.assertEqual(page["limit"], 100)
        # 空页用游标 0 重试，仍是空页、游标不变
        again = service.read_change_feed("0", None)
        self.assertEqual(again["next_cursor"], "0")
        self.assertEqual(again["events"], [])

    def test_events_cover_release_correction_withdrawal_order(self):
        r1 = release("AUD-A")["published_at"]
        r2 = service.publish_releases([item("AUD-B"), item("AUD-C")])["published_at"]
        c1 = correct("AUD-A", 0, t2, t3, "更正A1")["published_at"]
        wd = withdraw("AUD-A", 1)["withdrawn_at"]
        page = service.read_change_feed(None, 100)
        ops = [(e["operation"], e["number"], e["revision"])
               for e in page["events"]]
        self.assertEqual(ops, [
            ("release", "AUD-A", 0),
            ("release", "AUD-B", 0),
            ("release", "AUD-C", 0),
            ("correction", "AUD-A", 1),
            ("withdrawal", "AUD-A", 1),
        ])
        times = {"release": {"AUD-A": r1, "AUD-B": r2, "AUD-C": r2},
                 "correction": {"AUD-A": c1},
                 "withdrawal": {"AUD-A": wd}}
        for e in page["events"]:
            self.assertEqual(e["event_time"],
                             times[e["operation"]][e["number"]])
            self.assertRegex(e["event_time"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertFalse(page["has_more"])
        # 末页 next_cursor 固定，再读为空页且游标不变
        tail = service.read_change_feed(page["next_cursor"])
        self.assertEqual(tail["events"], [])
        self.assertEqual(tail["next_cursor"], page["next_cursor"])
        self.assertFalse(tail["has_more"])

    def test_batch_kept_together_atomic_and_ordered(self):
        # 一次批量首次发布 3 条 -> 一个 3 事件批次，顺序=请求顺序
        service.publish_releases([item("BD-1"), item("BD-2"), item("BD-3")])
        # limit=2 装不下 3 事件的批次 -> 明确报错，游标不前进、无事件
        with self.assertRaises(DomainError) as ctx:
            service.read_change_feed(None, 2)
        self.assertEqual(ctx.exception.code, "page_limit_too_small")
        self.assertEqual(ctx.exception.http_status, 422)
        self.assertEqual(ctx.exception.details["batch_size"], 3)
        self.assertEqual(ctx.exception.details["limit"], 2)
        # limit=3 整批返回，且批次内 position 为 0/1/2
        page = service.read_change_feed(None, 3)
        self.assertEqual([e["number"] for e in page["events"]],
                         ["BD-1", "BD-2", "BD-3"])
        self.assertEqual([e["position"] for e in page["events"]], [0, 1, 2])
        self.assertEqual(len({e["batch_seq"] for e in page["events"]}), 1)
        self.assertFalse(page["has_more"])

    def test_multi_batch_pagination_no_dup_no_gap(self):
        # 三个事务：单事件批次 + 2 事件批次 + 单事件批次
        release("PG-1")
        service.publish_releases([item("PG-2"), item("PG-3")])
        release("PG-4")
        seen: list[tuple] = []
        cursor = None
        pages = 0
        while True:
            page = service.read_change_feed(cursor, 2)
            pages += 1
            for e in page["events"]:
                key = (e["batch_seq"], e["position"])
                self.assertNotIn(key, seen)          # 不重现
                seen.append(key)
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual([(e["number"], e["revision"])
                          for e in self._all_events()],
                         [("PG-1", 0), ("PG-2", 0), ("PG-3", 0), ("PG-4", 0)])
        self.assertEqual(len(seen), 4)               # 不遗漏
        # 第 2 页恰好取 2 事件批次（不会把后面单事件也带上：limit=2）
        self.assertGreaterEqual(pages, 3)

    @staticmethod
    def _all_events() -> list:
        return service.read_change_feed(None, 1000)["events"]

    def test_rollback_leaves_no_audit_batch(self):
        # 批量撤回第二条非法（首版）-> 整批回滚，审计批次也不存在
        release("RB-1")
        correct("RB-1", 0, t2, t3, "更正")
        with self.assertRaises(DomainError):
            service.withdraw_corrections(
                [witem("RB-1", 1), witem("RB-1", 0)])
        events = self._all_events()
        self.assertEqual([e["operation"] for e in events],
                         ["release", "correction"])

    def test_invalid_cursor(self):
        release("IC-1")
        for bad in ("abc", "-1", "1.5", "xyz9"):
            with self.assertRaises(DomainError) as ctx:
                service.read_change_feed(bad, 10)
            self.assertEqual(ctx.exception.code, "invalid_cursor")
        # 语法合法但不指向任何批次（未提交事务不产生批次，游标不前进）
        with self.assertRaises(DomainError) as ctx:
            service.read_change_feed("999999", 10)
        self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_invalid_limit(self):
        for bad in ("0", "-1", "1001", "abc", "1.0"):
            with self.assertRaises(DomainError) as ctx:
                service.read_change_feed(None, bad)
            self.assertEqual(ctx.exception.code, "invalid_page_limit")
            self.assertEqual(ctx.exception.details["max"], 1000)

    def test_concurrent_commits_during_paging_no_dup_no_gap(self):
        # 翻页期间并发写入：新提交要么整批可见、要么整批不可见，
        # 全量走完全部页后不重现、不遗漏。
        release("CC-0")
        import threading

        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                name = f"CC-W-{i:03d}"
                try:
                    service.publish_releases([item(name)])
                except DomainError:
                    pass
                i += 1

        t = threading.Thread(target=writer)
        t.start()
        seen: set[tuple] = set()
        cursor = None
        for _ in range(50):
            page = service.read_change_feed(cursor, 3)
            for e in page["events"]:
                self.assertNotIn((e["batch_seq"], e["position"]), seen)
                seen.add((e["batch_seq"], e["position"]))
            cursor = page["next_cursor"]
        stop.set()
        t.join()
        # 写入全部完成后继续翻到尾，与全量事件集合逐一对齐
        while True:
            page = service.read_change_feed(cursor, 3)
            for e in page["events"]:
                self.assertNotIn((e["batch_seq"], e["position"]), seen)
                seen.add((e["batch_seq"], e["position"]))
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        total = service.read_change_feed(None, 1000)
        all_keys = {(e["batch_seq"], e["position"]) for e in total["events"]}
        self.assertEqual(seen, all_keys)


class AuditBackfillTests(unittest.TestCase):
    """升级前已有版本与撤回记录：启动回填从起始游标完整可读、不改原记录。"""

    def test_backfill_legacy_rows_as_singleton_events(self):
        import sqlite3
        import tempfile
        from app.db import _SCHEMA, _connect, _write_lock, backfill_legacy_audit

        path = tempfile.mktemp(prefix="sop-backfill-", suffix=".db")
        # 模拟"升级前"：旧库只有 procedures/versions/withdrawals。
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE procedures (id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                procedure_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                body TEXT NOT NULL, valid_start TEXT NOT NULL,
                valid_end TEXT NOT NULL, supersedes_id INTEGER,
                published_at TEXT NOT NULL,
                UNIQUE(procedure_id, revision));
            CREATE TABLE withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL UNIQUE, withdrawn_at TEXT NOT NULL);
            """)
        legacy.execute(
            "INSERT INTO procedures (number, created_at) VALUES "
            "('OLD-1','2026-01-01T00:00:00.000000Z'),"
            "('OLD-2','2026-01-02T00:00:00.000000Z')")
        legacy.execute(
            "INSERT INTO versions (procedure_id, revision, body, valid_start, "
            "valid_end, supersedes_id, published_at) VALUES "
            "(1,0,'r0','2026-01-01T00:00:00.000000Z',"
            "'2027-01-01T00:00:00.000000Z',NULL,'2026-01-01T00:00:00.000000Z'),"
            "(2,0,'r0','2026-01-01T00:00:00.000000Z',"
            "'2027-01-01T00:00:00.000000Z',NULL,'2026-01-02T00:00:00.000000Z'),"
            "(1,1,'r1','2026-03-01T00:00:00.000000Z',"
            "'2026-04-01T00:00:00.000000Z',1,'2026-02-01T00:00:00.000000Z')")
        legacy.execute(
            "INSERT INTO withdrawals (version_id, withdrawn_at) "
            "VALUES (3,'2026-05-01T00:00:00.000000Z')")
        legacy.commit()
        legacy.close()

        # settings 为 frozen dataclass：用 object.__setattr__ 临时指向旧库。
        old_path = settings.db_path
        try:
            object.__setattr__(settings, "db_path", path)
            conn = _connect()
            try:
                conn.executescript(_SCHEMA)
                with _write_lock:
                    n = backfill_legacy_audit(conn)
                conn.commit()
                self.assertEqual(n, 4)
                # 回填幂等：第二次为空操作
                self.assertEqual(backfill_legacy_audit(conn), 0)
            finally:
                conn.close()

            page = service.read_change_feed(None, 1000)
            self.assertEqual(
                [(e["operation"], e["number"], e["revision"])
                 for e in page["events"]],
                [("release", "OLD-1", 0),
                 ("release", "OLD-2", 0),
                 ("correction", "OLD-1", 1),
                 ("withdrawal", "OLD-1", 1)])
            # 每个旧记录都是单事件批次（原请求批次无法事后重建）
            check = sqlite3.connect(path)
            try:
                sizes = [r[0] for r in check.execute(
                    "SELECT event_count FROM audit_batches ORDER BY seq")]
                self.assertEqual(sizes, [1, 1, 1, 1])
                # 原记录未被改写
                self.assertEqual(check.execute(
                    "SELECT COUNT(*) FROM versions").fetchone()[0], 3)
                self.assertEqual(check.execute(
                    "SELECT COUNT(*) FROM withdrawals").fetchone()[0], 1)
            finally:
                check.close()
            # 升级后新提交的事务正常追加在回填批次之后
            service.publish_releases([
                SimpleNamespace(number="NEW-1", body="b",
                                valid_start=t1, valid_end=t5,
                                prerequisites=None)])
            page2 = service.read_change_feed(None, 1000)
            last = page2["events"][-1]
            self.assertEqual((last["operation"], last["number"]),
                             ("release", "NEW-1"))
            self.assertGreater(int(last["batch_seq"]), 4)
        finally:
            object.__setattr__(settings, "db_path", old_path)

    def test_init_db_upgrades_legacy_database(self):
        import sqlite3
        import tempfile
        from app.db import init_db

        path = tempfile.mktemp(prefix="sop-upgrade-", suffix=".db")
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE procedures (id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                procedure_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                body TEXT NOT NULL, valid_start TEXT NOT NULL,
                valid_end TEXT NOT NULL, supersedes_id INTEGER,
                published_at TEXT NOT NULL,
                UNIQUE(procedure_id, revision));
            CREATE TABLE withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL UNIQUE, withdrawn_at TEXT NOT NULL);
            INSERT INTO procedures (number, created_at)
            VALUES ('UPG-1','2026-01-01T00:00:00.000000Z');
            INSERT INTO versions (procedure_id, revision, body, valid_start,
                valid_end, supersedes_id, published_at)
            VALUES (1,0,'r0','2026-01-01T00:00:00.000000Z',
                '2027-01-01T00:00:00.000000Z',NULL,
                '2026-01-01T00:00:00.000000Z');
            """)
        legacy.commit()
        legacy.close()

        old_path = settings.db_path
        try:
            object.__setattr__(settings, "db_path", path)
            init_db()                       # 建表 + 回填
            init_db()                       # 再启动一次：幂等不重复回填
            page = service.read_change_feed(None, 10)
            self.assertEqual(
                [(e["operation"], e["number"], e["revision"])
                 for e in page["events"]],
                [("release", "UPG-1", 0)])
        finally:
            object.__setattr__(settings, "db_path", old_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
