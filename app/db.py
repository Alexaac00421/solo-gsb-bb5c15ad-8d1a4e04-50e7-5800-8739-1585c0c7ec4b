"""SQLite 连接管理与初始化。

- 读：每次操作开一条短连接（WAL 下读写不互斥）。
- 写：全局锁串行化，``BEGIN IMMEDIATE`` 立即拿写锁；写操作的整个校验+插入在
  同一事务内完成，保证"整体拒绝且不改历史"的原子性。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id  INTEGER NOT NULL REFERENCES procedures(id),
    revision      INTEGER NOT NULL,
    body          TEXT NOT NULL,
    valid_start   TEXT NOT NULL,
    valid_end     TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES versions(id),
    published_at  TEXT NOT NULL,
    UNIQUE(procedure_id, revision)
);

-- 撤回事件：只追加，不删除/改写 versions 行。
-- 一个版本至多被撤回一次（UNIQUE）；撤回时刻 withdrawn_at 由服务端记录，不可回填。
-- 双时间维语义：知悉时刻 k 下该版本可见当且仅当
-- published_at <= k 且（未撤回 或 withdrawn_at > k）。
CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL UNIQUE REFERENCES versions(id),
    withdrawn_at TEXT NOT NULL
);

-- 作业规程包：班组开工前按（作业时刻, 知悉时刻, 一组规程编号）固定。
-- 包一旦形成即不可变：各规程当时适用版本的正文等字段在 package_items 中
-- 完整留快照，之后任何更正/撤回都只作用于 versions/withdrawals，
-- 按包标识取回的内容永远保持固定时原样。
CREATE TABLE IF NOT EXISTS packages (
    id          TEXT PRIMARY KEY,
    work_time   TEXT NOT NULL,
    known_as_of TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS package_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id          TEXT NOT NULL REFERENCES packages(id),
    position            INTEGER NOT NULL,
    procedure_id        INTEGER NOT NULL REFERENCES procedures(id),
    number              TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    body                TEXT NOT NULL,
    valid_start         TEXT NOT NULL,
    valid_end           TEXT NOT NULL,
    supersedes_revision INTEGER,
    published_at        TEXT NOT NULL,
    UNIQUE(package_id, position)
);

-- 前置规程引用：每个版本各自携带一份有序的前置规程编号列表。
-- 与版本行同一事务插入、只追加不改写：首次发布省略视为空；更正省略继承
-- 被替代版本（在写入时复制，基版行不可变，故与按引用继承等价）；显式空
-- 列表清空。引用是否生效完全由所属版本的双时间维可见性决定（适用区间、
-- 发布时间、撤回），因此历史查询天然看到当时的前置列表，无需改写历史。
-- 编号在写入时不校验存在性：前置规程可以晚于引用者发布。
CREATE TABLE IF NOT EXISTS prerequisites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    position   INTEGER NOT NULL,
    number     TEXT NOT NULL,
    UNIQUE(version_id, position)
);

-- 值班审计变动日志：每个**已提交**写事务（首次发布 / 更正 / 撤回）对应一行
-- 批次（audit_batches），批次内事件（audit_events）按请求顺序排列、永不拆开。
-- 批次与事件和业务行在**同一条写事务**内插入：写事务回滚则审计行一条都不留，
-- 只读快照也永远不会看到跨事务的半截批次。游标 = 最近已完整交付批次的 seq
-- （省略 / 0 表示起始游标），只随已提交事务单调前进。
-- 升级前已存在的 versions/withdrawals 由启动回填逐事件补入（每条一个单事件
-- 批次），回填只追加本表、绝不改写任何原记录。
CREATE TABLE IF NOT EXISTS audit_batches (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_seq        INTEGER NOT NULL REFERENCES audit_batches(seq),
    position         INTEGER NOT NULL,
    operation        TEXT NOT NULL,
    procedure_number TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    event_time       TEXT NOT NULL,
    version_id       INTEGER NOT NULL REFERENCES versions(id),
    UNIQUE(batch_seq, position)
);

CREATE INDEX IF NOT EXISTS idx_versions_procedure
    ON versions(procedure_id);
CREATE INDEX IF NOT EXISTS idx_versions_query
    ON versions(published_at, valid_start, valid_end);
CREATE INDEX IF NOT EXISTS idx_withdrawals_version
    ON withdrawals(version_id);
CREATE INDEX IF NOT EXISTS idx_package_items_package
    ON package_items(package_id);
CREATE INDEX IF NOT EXISTS idx_prerequisites_version
    ON prerequisites(version_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_batch
    ON audit_events(batch_seq, position);
"""

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path, timeout=settings.busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(settings.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """幂等建表；内存库可安全重复调用。

    首次在含旧数据（升级前已存在 versions/withdrawals、但尚无审计批次）的
    库上建表后，执行一次审计日志回填：旧版本/撤回**逐条**补成单事件批次，
    按服务端时刻与原始行顺序排列。回填只向审计表追加，绝不改写 versions /
    withdrawals 原记录。
    """
    if settings.db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(settings.db_path))
        os.makedirs(parent, exist_ok=True)
    conn = _connect()
    try:
        if settings.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        backfill_legacy_audit(conn)
        conn.commit()
    finally:
        conn.close()


def backfill_legacy_audit(conn: sqlite3.Connection) -> int:
    """把升级前已存在的版本与撤回记录回填为单事件审计批次。

    幂等：仅当 ``audit_batches`` 为空且存在历史业务行时执行（一次启动至多
    回填一次）；空库或已回填过的库直接返回 0。

    回填顺序（确定性、与"按提交顺序"一致）：每个写事务只写 versions 或
    withdrawals 一张表，服务端时钟单调保证其时刻严格递增；同表内行 id 即
    提交顺序。跨表时刻相同的极端情形按"先发布后撤回"裁决（撤回不可能
    先于其目标版本）。每条旧记录一个**单事件批次**——原始请求批次无法
    事后重建，故不把多条旧记录合并。
    """
    if conn.execute("SELECT 1 FROM audit_batches LIMIT 1").fetchone():
        return 0
    rows = conn.execute(
        """
        SELECT 'version'   AS kind, v.id AS version_id, p.number AS number,
               v.revision AS revision, v.published_at AS event_time,
               0 AS kind_rank
          FROM versions v JOIN procedures p ON p.id = v.procedure_id
        UNION ALL
        SELECT 'withdrawal' AS kind, v.id AS version_id, p.number AS number,
               v.revision AS revision, w.withdrawn_at AS event_time,
               1 AS kind_rank
          FROM withdrawals w
          JOIN versions v ON v.id = w.version_id
          JOIN procedures p ON p.id = v.procedure_id
        ORDER BY event_time, kind_rank, version_id
        """
    ).fetchall()
    count = 0
    # 首版（无父版本）记 release，其余版本行记 correction；撤回行记 withdrawal。
    root_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM versions WHERE supersedes_id IS NULL")}
    for row in rows:
        operation = ("withdrawal" if row["kind"] == "withdrawal"
                     else "release" if row["version_id"] in root_ids
                     else "correction")
        cur = conn.execute(
            "INSERT INTO audit_batches (event_count) VALUES (1)")
        conn.execute(
            "INSERT INTO audit_events (batch_seq, position, operation, "
            "procedure_number, revision, event_time, version_id) "
            "VALUES (?, 0, ?, ?, ?, ?, ?)",
            (cur.lastrowid, operation, row["number"], row["revision"],
             row["event_time"], row["version_id"]))
        count += 1
    return count


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_snapshot() -> Iterator[sqlite3.Connection]:
    """只读一致性快照：显式开启一条 deferred 事务，并**立即执行一次读**把
    SQLite 的快照固定在进入时刻（deferred 事务的快照在首次读时取得，而非
    BEGIN 时）。此后事务内所有读取看到的都是同一数据库版本，不会拼接并发
    写入前后的结果（WAL 下读事务在该快照上保持到事务结束）。
    """
    conn = _connect()
    try:
        conn.execute("BEGIN")
        # 立即读真实表以获取 WAL 读锁、把读快照固定在进入时刻（SELECT 1 不
        # 访问任何表，不取读锁也不建立快照；deferred 事务的快照在首次表读时
        # 取得）。此后事务内所有读取都基于同一数据库版本。
        conn.execute("SELECT 1 FROM packages LIMIT 1").fetchone()
        yield conn
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化的写事务：异常即回滚，绝不留下半截历史。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
