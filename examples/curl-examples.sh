#!/bin/sh
# 调用示例（curl）。前置：服务已在本机启动。
# 约定时间均为 UTC、ISO-8601；区间语义为 [起, 止)。
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"

# --- 1. 首次发布 ---------------------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 2. 更正（只覆盖 3 月子区间；published_at 由服务端记录，不可回填） ---
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。"
      }'; echo

# --- 3. 回溯查询：作业时刻在 2 月（更正区间外） -> 初版 -----------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-02-15T08:00:00Z"; echo

# --- 4. 回溯查询：作业时刻在 3 月（更正区间内） -> 更正正文 -------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z"; echo

# --- 5. 知悉时刻早于任何发布 -> 空结果 ----------------------------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z&known_as_of=2025-06-01T00:00:00Z"; echo

# --- 6a. 顺序请求却与"已发布"更正重叠 -> 409 过期基版，历史不变 ----------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-20T00:00:00Z",
        "valid_end":   "2026-03-25T00:00:00Z",
        "body": "依据了旧版 r0 的已失效部分，应被拒绝"
      }'; echo

# --- 6b. 同一批次内并发更正相互冲突 -> 409，整批回滚 --------------------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z", "valid_end": "2026-05-20T00:00:00Z",
         "body": "并发更正 A"},
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-10T00:00:00Z", "valid_end": "2026-05-30T00:00:00Z",
         "body": "并发更正 B（与 A 冲突，整批拒绝）"}
      ]}'; echo

# --- 6c. 回填发布时间：published_at 为额外字段，422 拒绝 -----------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "body": "重复编号且回填发布时间",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "published_at": "2000-01-01T00:00:00Z"
      }'; echo

# --- 7. 批量更正：整批原子提交（任一条非法则全部不入库） -----------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-05-01T00:00:00Z", "valid_end": "2026-06-01T00:00:00Z",
         "body": "5 月更正"},
        {"number": "SOP-CURL-001", "base_revision": 0,
         "valid_start": "2026-06-01T00:00:00Z", "valid_end": "2026-07-01T00:00:00Z",
         "body": "6 月更正（与上一条首尾相接，允许）"}
      ]}'; echo

# --- 8. 版本历史（审计视图） --------------------------------------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CURL-001/history"; echo

# --- 9a. 两知悉时刻对比：前侧取发布之前，作业区间覆盖全年 ---------------
#       只有更正子区间（3 月）两侧版本不同；before 为 null（当时无任何版本）
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 9b. 收窄作业区间到 2-5 月：null→r0 与 null→r1 两段 -----------------
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-02-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-05-01T00:00:00Z'; echo

# --- 9c. 知悉顺序颠倒 -> 422 invalid_known_order（不写历史） -------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2030-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2025-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 9d. 空作业区间（起=止） -> 422 invalid_interval ---------------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-CURL-001/diff" \
  --data-urlencode 'known_before=2025-01-01T00:00:00Z' \
  --data-urlencode 'known_after=2030-01-01T00:00:00Z' \
  --data-urlencode 'work_start=2026-03-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-03-01T00:00:00Z'; echo

# ------------------------------------------------------------------------
# 撤回：值班员发现更正误发布时，提交"规程编号 + 更正版本号"。
# 撤回时刻 withdrawn_at 由服务端记录、不接受回填；撤回只追加事件、不删版本。
# 前置数据：r1 为上面第 2 步发布的 3 月更正。
# ------------------------------------------------------------------------

# --- 10. 撤回误发布的更正 r1 --------------------------------------------
# 成功 201，返回服务端记录的 withdrawn_at（与发布时间一样不可回填）
curl -sS -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 1 }'; echo

# --- 11a. 撤回后回溯：3 月子区间恢复父版本 r0 正文 -----------------------
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z"; echo

# --- 11b. 撤回前知悉时刻的回溯仍按原版本链 -> 历史不变 -------------------
# 知悉时刻取早于本轮服务运行的固定时刻：本脚本第 2 步的更正尚未发布，
# 故 3 月返回父版本 r0（演示"撤回不改变撤回前查询"请把该参数改为
# r1 发布之后、撤回之前的时刻，可从第 2/10 步响应中读取）。
curl -sS "$BASE_URL/api/v1/effective?work_time=2026-03-15T08:00:00Z&known_as_of=2026-10-01T00:00:00Z"; echo

# --- 11c. 历史审计：r1 行仍在，且带 withdrawn_at -------------------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-CURL-001/history"; echo

# --- 12. 撤回后父版本可在恢复区间再次更正（新的 r2 指向 r0） -------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CURL-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文（撤回误发版本后重新发布）：仍须穿戴防割手套。"
      }'; echo

# --- 13a. 重复撤回已撤回版本 -> 409 already_withdrawn（历史不变） --------
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 1 }'; echo

# --- 13b. 撤回首版 -> 409 first_revision_not_withdrawable ---------------
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 0 }'; echo

# --- 13c. 撤回不存在的版本号 -> 404 revision_not_found -------------------
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 99 }'; echo

# --- 13d. 回填撤回时刻：withdrawn_at 为额外字段 -> 422 validation_error ---
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-CURL-001", "revision": 2,
        "withdrawn_at": "2000-01-01T00:00:00Z" }'; echo

# --- 14. 批量撤回：整批原子，任一条非法全部不追加 -------------------------
# 此处第二条撤首版非法，第一条（合法）也一并回滚
curl -sS -i -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        { "number": "SOP-CURL-001", "revision": 2 },
        { "number": "SOP-CURL-001", "revision": 0 }
      ]}'; echo

# ------------------------------------------------------------------------
# 作业规程包：班组开工前按"作业时刻 + 知悉时刻 + 一组规程编号"
# 固定。包一经形成不可变：之后的更正/撤回不影响按标识取回；复核以不早于
# 包内知悉时刻、不晚于当前时刻的复核知悉时刻逐项核对是否变化。
# 知悉时刻不得晚于服务端当前时刻，故此处取运行时 UTC（先停顿 1 秒，
# 保证晚于上面的发布时刻）。
# ------------------------------------------------------------------------
sleep 1
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- 15. 为规程包准备两个规程 --------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PKG-001",
        "body": "初版正文：进入车间必须佩戴护目镜。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PKG-002",
        "body": "另一规程初版：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 16. 固定作业规程包：201 返回包标识、各规程当时版本/正文、两种时刻 ----
PKG=$(curl -sS -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2026-03-15T08:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\", \"SOP-PKG-002\"]
      }")
echo "$PKG"
PKG_ID=$(printf '%s' "$PKG" | sed -n 's/.*"id": "\([0-9a-f]\{32\}\)".*/\1/p')
echo "固定得到包标识：$PKG_ID"

# --- 17. 按标识取回：永远是固定时原样快照 --------------------------------
curl -sS "$BASE_URL/api/v1/packages/$PKG_ID"; echo

# --- 18. 固定后再更正 SOP-PKG-001 的 3 月子区间 ---------------------------
sleep 1
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PKG-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期还须穿戴防割手套。"
      }'; echo

# --- 19a. 取回包：内容保持固定时原样（仍是 r0 初版，不随后续更正变化） ----
curl -sS "$BASE_URL/api/v1/packages/$PKG_ID"; echo

# --- 19b. 复核（以当前时刻为复核知悉时刻）：逐项给出包内版本 pinned、
#         彼时有效版本 current 与 changed，并给整体 all_match=false --------
sleep 1
REVIEW_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -sS "$BASE_URL/api/v1/packages/$PKG_ID/review?known_as_of=$REVIEW_NOW"; echo

# --- 20a. 编号重复 -> 422 duplicate_package_number，整体拒绝不留包 --------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2026-03-15T08:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\", \"SOP-PKG-001\"]
      }"; echo

# --- 20b. 编号缺失 -> 404 procedure_not_found -----------------------------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2026-03-15T08:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\", \"NO-SUCH\"]
      }"; echo

# --- 20c. 作业时刻无适用正文 -> 409 no_applicable_version（不留包） -------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d "{
        \"work_time\": \"2027-06-01T00:00:00Z\",
        \"known_as_of\": \"$NOW\",
        \"numbers\": [\"SOP-PKG-001\"]
      }"; echo

# --- 20d. 知悉时刻晚于服务端当前时刻 -> 422 known_as_of_in_future --------
curl -sS -i -X POST "$BASE_URL/api/v1/packages" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_time": "2026-03-15T08:00:00Z",
        "known_as_of": "2030-01-01T00:00:00Z",
        "numbers": ["SOP-PKG-001"]
      }'; echo

# --- 20e. 复核知悉时刻早于包内知悉时刻 -> 422 review_known_too_early ------
curl -sS -i "$BASE_URL/api/v1/packages/$PKG_ID/review?known_as_of=2025-01-01T00:00:00Z"; echo

# --- 20f. 取回/复核不存在的包 -> 404 package_not_found --------------------
curl -sS -i "$BASE_URL/api/v1/packages/0123456789abcdef0123456789abcdef"; echo
curl -sS -i "$BASE_URL/api/v1/packages/0123456789abcdef0123456789abcdef/review?known_as_of=$NOW"; echo

# ------------------------------------------------------------------------
# 前置规程（本轮新增）：首次发布与更正可携带前置规程编号列表（发布省略视为
# 空；更正省略继承被替代版本，显式空列表清空；不校验存在性，前置可晚发布）。
# 递归解析接口按根编号 + 作业时刻 + 知悉时刻在同一读取快照内展开，返回
# 去重后的根及前置规程清单（前置优先，含编号、版本、正文）。
# ------------------------------------------------------------------------

# --- 21. 发布两份前置规程 --------------------------------------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-101",
        "body": "前置规程：动火作业须办理动火证。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-102",
        "body": "前置规程：进入受限空间须先通风检测。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 22. 首次发布根规程并携带前置编号列表（响应回显落库列表） --------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-001",
        "body": "根规程初版：检修作业总体要求。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-PRE-101", "SOP-PRE-102"]
      }'; echo

# --- 23. 更正省略 prerequisites -> 继承被替代版本 r0 的列表 ----------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "根规程 3 月更正：检修期加派监护。"
      }'; echo

# --- 24. 递归解析：前置优先、去重、根在最后，含编号/版本/正文 --------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 25. 显式空列表清空：4 月更正不再携带前置 -> 清单只剩根本身 ------------
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-001",
        "base_revision": 0,
        "valid_start": "2026-04-01T00:00:00Z",
        "valid_end":   "2026-05-01T00:00:00Z",
        "body": "根规程 4 月更正：不再引用前置规程。",
        "prerequisites": []
      }'; echo
curl -sS "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure?work_time=2026-04-15T08:00:00Z"; echo

# --- 26a. 前置尚未发布 -> 404 prerequisite_not_found（details 带引用链） ---
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-002",
        "body": "引用尚未发布的前置规程。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-PRE-103"]
      }'; echo
curl -sS -i "$BASE_URL/api/v1/procedures/SOP-PRE-002/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 26b. 前置发布后 -> 解析成功（前置可晚于引用者发布） --------------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-103",
        "body": "晚发布的前置规程正文。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS "$BASE_URL/api/v1/procedures/SOP-PRE-002/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 27. 引用成环 -> 409 prerequisite_cycle（details.cycle 为环路径） ------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CYC-A", "body": "A 引用 B",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CYC-B"]
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-CYC-B", "body": "B 引用 A",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-CYC-A"]
      }'; echo
curl -sS -i "$BASE_URL/api/v1/procedures/SOP-CYC-A/closure?work_time=2026-03-15T08:00:00Z"; echo

# --- 28a. 前置编号重复 -> 422 duplicate_prerequisite_number ----------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-003", "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["SOP-PRE-101", "SOP-PRE-101"]
      }'; echo

# --- 28b. 前置编号空白 -> 422 invalid_number -------------------------------
curl -sS -i -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-003", "body": "x",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": ["  "]
      }'; echo

# ------------------------------------------------------------------------
# 跨时段前置规程清单（本轮新增）：班组计划跨时段作业时，判断根规程及其递归
# 前置在整个非空作业区间 [work_start, work_end) 是否持续可用。同一读取快照
# 内按版本切换边界切成覆盖全区间、互不重叠的片段；每片段返回前置优先的去重
# 清单及版本；根/引用缺失、无适用正文、引用成环时保留该片段并标缺口链/环
# 路径（整体 available=false）；相邻片段仅在清单与错误状态完全相同时合并。
# 前置数据：SOP-PRE-001 的 r1（3 月）携带 [SOP-PRE-101, SOP-PRE-102]，
#           4 月更正 r2 清空前置。
# ------------------------------------------------------------------------

# --- 29. 全年作业区间：3 月 / 4 月两段引用不同，其余月份同为 r0 前置 --------
#       两端状态相同但被中间片段隔开 -> 不合并，共 4 段且全部 available
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure-plan" \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 30. 收窄作业区间到 3-4 月：端点裁剪到请求区间，只保留相交片段 ----------
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure-plan" \
  --data-urlencode 'work_start=2026-03-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-05-01T00:00:00Z'; echo

# --- 31. 引用不存在：SOP-PRE-002 携带尚未发布的 SOP-PRE-103（后已发布） ----
#       用其 3 月前的状态演示：再发一个只在 3 月引用不存在编号的根规程
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PLAN-GAP",
        "body": "根初版（无前置）。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z",
        "prerequisites": []
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PLAN-GAP",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "3 月版本引用了一个不存在的前置。",
        "prerequisites": ["SOP-PLAN-MISSING"]
      }'; echo
# 3 月片段 available=false 且 gaps 带缺口链 ["SOP-PLAN-GAP","SOP-PLAN-MISSING"]；
# 其余片段正常，顶层 available=false（HTTP 仍为 200）
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-PLAN-GAP/closure-plan" \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 32. 根编号不存在：保留单片段，gaps[0].code=not_found，整体不可用 --------
curl -sS --get "$BASE_URL/api/v1/procedures/NO-SUCH-PLAN/closure-plan" \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 33. 引用成环：环片段保留并给出 cycle 环路径（HTTP 200，片段不可用） ----
curl -sS --get "$BASE_URL/api/v1/procedures/SOP-CYC-A/closure-plan" \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo

# --- 34a. 空作业区间（起=止） -> 422 invalid_interval（只读、不写历史） -----
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure-plan" \
  --data-urlencode 'work_start=2026-03-01T00:00:00Z' \
  --data-urlencode 'work_end=2026-03-01T00:00:00Z'; echo

# --- 34b. 时刻缺时区 -> 422 invalid_time；缺少 work_end -> 422 --------------
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure-plan" \
  --data-urlencode 'work_start=2026-01-01T00:00:00' \
  --data-urlencode 'work_end=2027-01-01T00:00:00Z'; echo
curl -sS -i --get "$BASE_URL/api/v1/procedures/SOP-PRE-001/closure-plan" \
  --data-urlencode 'work_start=2026-01-01T00:00:00Z'; echo

# ------------------------------------------------------------------------
# 更正批量预演（本轮新增，只读）：正式提交前按单条/批量同一套字段预演整批
# 结果，须指定非空作业区间。同批按输入顺序应用，后项可引用前项拟生成版本；
# 返回各项拟生成版本号、最终前置清单，以及作业区间内提交前后实际适用版本
# 不同的最大片段。预演不写版本、不占版本号、不改变历史；预演结果不保证
# 可提交（正式提交仍按当时状态重新校验）。
# 数据：SOP-PRE-001 初版 r0 已发布（前置 [101,102]，3 月已发布 r1）。
# ------------------------------------------------------------------------

# --- 35a. 批量预演：r0->r2（9 月），再以同批拟生成的 r2 为基版 r2->r3 ------
#        省略前置 -> 继承基版列表；全年作业区间给出提交前后差异最大片段
curl -sS -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_start": "2026-01-01T00:00:00Z",
        "work_end":   "2027-01-01T00:00:00Z",
        "items": [
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-09-01T00:00:00Z",
           "valid_end":   "2026-10-01T00:00:00Z",
           "body": "九月更正（省略前置，继承基版）。"},
          {"number": "SOP-PRE-001", "base_revision": 2,
           "valid_start": "2026-09-10T00:00:00Z",
           "valid_end":   "2026-09-20T00:00:00Z",
           "body": "对同批拟生成 r2 的再更正。",
           "prerequisites": ["SOP-PRE-101"]}
        ]
      }'; echo

# --- 35b. 单条对象形式：作业区间随条目给出 -------------------------------
curl -sS -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-PRE-001", "base_revision": 0,
        "valid_start": "2026-07-01T00:00:00Z",
        "valid_end":   "2026-08-01T00:00:00Z",
        "body": "七月更正（显式清空前置）。",
        "prerequisites": [],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end":   "2027-01-01T00:00:00Z"
      }'; echo

# --- 35c. 预演只读：版本历史不变（不会因预演多出版本） -------------------
curl -sS "$BASE_URL/api/v1/procedures/SOP-PRE-001/history"; echo

# --- 36a. 第 2 条编号不存在 -> 404 not_found，details.index=1 -------------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_start": "2026-01-01T00:00:00Z",
        "work_end":   "2027-01-01T00:00:00Z",
        "items": [
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-11-01T00:00:00Z",
           "valid_end":   "2026-12-01T00:00:00Z", "body": "第 1 条合法"},
          {"number": "SOP-PRE-GONE", "base_revision": 0,
           "valid_start": "2026-11-01T00:00:00Z",
           "valid_end":   "2026-12-01T00:00:00Z", "body": "第 2 条编号不存在"}
        ]
      }'; echo

# --- 36b. 同批对同一基版的子区间重叠 -> 409 concurrent_correction_conflict
curl -sS -i -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_start": "2026-01-01T00:00:00Z",
        "work_end":   "2027-01-01T00:00:00Z",
        "items": [
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-11-01T00:00:00Z",
           "valid_end":   "2026-11-20T00:00:00Z", "body": "并发更正 A"},
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-11-10T00:00:00Z",
           "valid_end":   "2026-11-30T00:00:00Z", "body": "并发更正 B（与 A 冲突）"}
        ]
      }'; echo

# --- 36c. 落在已发布更正覆盖处 -> 409 expired_base_version（3 月已有 r1） -
curl -sS -i -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_start": "2026-01-01T00:00:00Z",
        "work_end":   "2027-01-01T00:00:00Z",
        "items": [
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-03-10T00:00:00Z",
           "valid_end":   "2026-03-20T00:00:00Z",
           "body": "依据旧版 r0 的已覆盖区间，过期基版。"}
        ]
      }'; echo

# --- 37a. 空作业区间 -> 422 invalid_interval（只读、不写历史） ------------
curl -sS -i -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "work_start": "2026-05-01T00:00:00Z",
        "work_end":   "2026-05-01T00:00:00Z",
        "items": [
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-11-01T00:00:00Z",
           "valid_end":   "2026-12-01T00:00:00Z", "body": "x"}
        ]
      }'; echo

# --- 37b. 缺少作业区间字段 / 回填 published_at -> 422 validation_error ----
curl -sS -i -X POST "$BASE_URL/api/v1/corrections/preview" \
  -H 'Content-Type: application/json' \
  -d '{
        "items": [
          {"number": "SOP-PRE-001", "base_revision": 0,
           "valid_start": "2026-11-01T00:00:00Z",
           "valid_end":   "2026-12-01T00:00:00Z", "body": "x"}
        ]
      }'; echo

# ------------------------------------------------------------------------
# 值班审计变动日志（本轮新增）：按提交顺序增量取回全站发布/更正/撤回事件。
# cursor 为上一页 next_cursor（省略/空/0 为起始游标，含升级前历史）；
# limit 为每页事件数上限（1..1000，默认 100）。同一新提交批次按请求顺序
# 排列且不可跨页拆开；游标只随已提交事务前进；空页游标不变。
# ------------------------------------------------------------------------

# --- 38. 制造提交：单条发布 + 一个批量发布批次（2 事件，请求顺序） --------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-AUDIT-001",
        "body": "审计演示初版。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{ "items": [
        {"number": "SOP-AUDIT-002", "body": "批量发布 A。",
         "valid_start": "2026-01-01T00:00:00Z",
         "valid_end":   "2027-01-01T00:00:00Z"},
        {"number": "SOP-AUDIT-003", "body": "批量发布 B。",
         "valid_start": "2026-01-01T00:00:00Z",
         "valid_end":   "2027-01-01T00:00:00Z"}
      ]}'; echo

# --- 39a. 从起始游标整页读取（事件含 operation/number/revision/event_time） -
curl -sS "$BASE_URL/api/v1/audit/events?cursor=0&limit=100" | python3 -m json.tool

# --- 39b. 只取本批演示规程的事件（用 jq 过滤；无 jq 时直接看完整页） --------
curl -sS "$BASE_URL/api/v1/audit/events?limit=100" \
  | jq '.events[] | select(.number | startswith("SOP-AUDIT"))' 2>/dev/null || true

# --- 39c. 持尾游标再读 -> 空页：next_cursor 与传入游标相同 ------------------
TAIL=$(curl -sS "$BASE_URL/api/v1/audit/events?limit=1000" \
        | sed -n 's/.*"next_cursor": "\([0-9]*\)".*/\1/p' | tail -1)
curl -sS "$BASE_URL/api/v1/audit/events?cursor=$TAIL&limit=100"; echo

# --- 40a. limit 装不下下一完整批次 -> 422 page_limit_too_small --------------
#       （上面的批量发布是 2 事件批次；找到其前一个批次 seq）
PRIOR=$(curl -sS "$BASE_URL/api/v1/audit/events?limit=1000" | python3 -c '
import json,sys
d=json.load(sys.stdin)
size={}
for e in d["events"]:
    size.setdefault(e["batch_seq"],0)
    size[e["batch_seq"]]+=1
two=next(b for b,n in size.items() if n==2)
print(two-1)')
curl -sS -i "$BASE_URL/api/v1/audit/events?cursor=$PRIOR&limit=1"; echo
# --- 40b. 同一游标调大 limit -> 整批返回，不拆开 ----------------------------
curl -sS "$BASE_URL/api/v1/audit/events?cursor=$PRIOR&limit=10"; echo

# --- 41a. 非法游标（非数字 / 不指向任何已提交批次） -> 422 invalid_cursor ---
curl -sS -i "$BASE_URL/api/v1/audit/events?cursor=not-a-number"; echo
curl -sS -i "$BASE_URL/api/v1/audit/events?cursor=99999999"; echo
# --- 41b. 非法上限（0 / 超过 1000） -> 422 invalid_page_limit ---------------
curl -sS -i "$BASE_URL/api/v1/audit/events?limit=0"; echo
curl -sS -i "$BASE_URL/api/v1/audit/events?limit=1001"; echo

# ------------------------------------------------------------------------
# 跨规程版本覆盖检查（本轮新增）：调度员安排跨规程作业前，输入非空且不重
# 复的规程编号、非空作业区间与知悉时刻；服务在同一只读快照内按全体编号
# 的版本端点切分作业区间，逐段返回每个编号的有效版本号、正文或缺口，并
# 标出因撤回导致的恢复、区间外空档与版本切换。全程只读、不写历史。
# ------------------------------------------------------------------------

# --- 42. 准备：两个规程全年适用；SOP-COV-001 在 3 月有更正 ----------------
curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-COV-001",
        "body": "初版正文：动火作业须办理动火票。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }'; echo
curl -sS -X POST "$BASE_URL/api/v1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-COV-001",
        "base_revision": 0,
        "valid_start": "2026-03-01T00:00:00Z",
        "valid_end":   "2026-04-01T00:00:00Z",
        "body": "更正正文：3 月检修期动火须双人监护。"
      }'; echo
# 知悉时刻取最后一笔写入的服务端发布时刻（此前提交全部可见）
KNOWN=$(curl -sS -X POST "$BASE_URL/api/v1/releases" \
  -H 'Content-Type: application/json' \
  -d '{
        "number": "SOP-COV-002",
        "body": "初版正文：进入装置区必须佩戴安全帽。",
        "valid_start": "2026-01-01T00:00:00Z",
        "valid_end":   "2027-01-01T00:00:00Z"
      }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["published_at"])')

# --- 43. 覆盖检查：全体端点切分为 3 段，3 月段标出版本切换 ----------------
curl -sS -X POST "$BASE_URL/api/v1/coverage/check" \
  -H 'Content-Type: application/json' \
  -d "{
        \"numbers\": [\"SOP-COV-001\", \"SOP-COV-002\"],
        \"work_start\": \"2026-01-01T00:00:00Z\",
        \"work_end\":   \"2027-01-01T00:00:00Z\",
        \"known_as_of\": \"$KNOWN\"
      }"; echo

# --- 44. 撤回 3 月更正后再查：3 月段恢复父版本并标 restored_by_withdrawal -
#       （知悉时刻取撤回之后的当前时刻；边界含等号，恰好等于撤回时刻时
#       被撤回版本仍可见）
curl -sS -X POST "$BASE_URL/api/v1/withdrawals" \
  -H 'Content-Type: application/json' \
  -d '{ "number": "SOP-COV-001", "revision": 1 }'; echo
KNOWN2=$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)
curl -sS -X POST "$BASE_URL/api/v1/coverage/check" \
  -H 'Content-Type: application/json' \
  -d "{
        \"numbers\": [\"SOP-COV-001\", \"SOP-COV-002\"],
        \"work_start\": \"2026-01-01T00:00:00Z\",
        \"work_end\":   \"2027-01-01T00:00:00Z\",
        \"known_as_of\": \"$KNOWN2\"
      }"; echo

# --- 45a. 编号重复 -> 422 duplicate_coverage_number（只读、不写历史） -----
curl -sS -i -X POST "$BASE_URL/api/v1/coverage/check" \
  -H 'Content-Type: application/json' \
  -d "{
        \"numbers\": [\"SOP-COV-001\", \"SOP-COV-001\"],
        \"work_start\": \"2026-01-01T00:00:00Z\",
        \"work_end\":   \"2027-01-01T00:00:00Z\",
        \"known_as_of\": \"$KNOWN2\"
      }"; echo

# --- 45b. 编号不存在 -> 404 procedure_not_found ---------------------------
curl -sS -i -X POST "$BASE_URL/api/v1/coverage/check" \
  -H 'Content-Type: application/json' \
  -d "{
        \"numbers\": [\"SOP-COV-001\", \"SOP-COV-NOPE\"],
        \"work_start\": \"2026-01-01T00:00:00Z\",
        \"work_end\":   \"2027-01-01T00:00:00Z\",
        \"known_as_of\": \"$KNOWN2\"
      }"; echo

# --- 45c. 知悉时刻晚于服务端当前时刻 -> 422 known_as_of_in_future ---------
curl -sS -i -X POST "$BASE_URL/api/v1/coverage/check" \
  -H 'Content-Type: application/json' \
  -d '{
        "numbers": ["SOP-COV-001"],
        "work_start": "2026-01-01T00:00:00Z",
        "work_end":   "2027-01-01T00:00:00Z",
        "known_as_of": "2999-01-01T00:00:00Z"
      }'; echo

# --- 45d. 空作业区间（起=止） -> 422 invalid_interval ---------------------
curl -sS -i -X POST "$BASE_URL/api/v1/coverage/check" \
  -H 'Content-Type: application/json' \
  -d "{
        \"numbers\": [\"SOP-COV-001\"],
        \"work_start\": \"2026-03-01T00:00:00Z\",
        \"work_end\":   \"2026-03-01T00:00:00Z\",
        \"known_as_of\": \"$KNOWN2\"
      }"; echo
