"""分析管道批量入账冲销：批次校验、争用分配、幂等入账与集市回执更新。

业务约定（与复核口径一致，改动前先同步 tests/test_writeoff.py）：

- 批次一次提交多条冲销行，每行含借方行号、贷方行号、源回执与冲销原因，
  按活动与结算日逐行校验，只有可用行进入抵扣。
- 争用策略：同一借方行下多条有效贷方行按「源回执时间、贷方行号」的稳定顺序
  依次抵扣；余量不足的边界行按剩余量部分确认，未覆盖部分记待补差额；
  余量耗尽后的行挂待复核。不把整个争用组挂起——同组无关行照常入账。
- 幂等：批次号是幂等键，同批次重复提交（包括并发）只入账一次；
  已确认/部分确认的行按「贷方行号+源回执号」去重，部分批次重送不重复影响金额。
- 集市：批次入账回执沿借方行关联的原流水更新已确认净额与待补差额。
- 复核：失败项（已拒绝、待复核）可多选重启，只有可纠正条目会重新进入校验抵扣。
"""
from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.store import Store, store

MODULE_BATCH = "writeoff_batch"
MODULE_LINE = "writeoff_line"
MODULE_LEDGER = "debit_ledger"
MODULE_MART = "mart_flow"
MODULE_ACTIVITY = "writeoff_activity"

CENT = Decimal("0.01")

LINE_CONFIRMED = "已确认"
LINE_PARTIAL = "部分确认"
LINE_REJECTED = "已拒绝"
LINE_PENDING = "待复核"

BATCH_POSTED = "已入账"
BATCH_PARTIAL = "部分入账"
BATCH_FAILED = "全拒待复核"

# 拒绝/挂起原因码：文案 + 是否可纠正（可纠正的才能被复核重启重新处理）。
REASONS: dict[str, tuple[str, bool]] = {
    "CROSS_DAY": ("跨日：行结算日与批次结算日不一致", True),
    "DUPLICATE": ("重复：该贷方行与源回执已确认过冲销", False),
    "OVER_WRITE": ("超冲：请求金额超过借方剩余可冲量", True),
    "AMOUNT_INVALID": ("金额无效：冲销金额必须为正数", True),
    "RECEIPT_MISSING": ("回执缺失：贷方行号与源回执号必填", True),
    "ACTIVITY_MISMATCH": ("活动不一致：行活动与批次活动不符", False),
    "ACTIVITY_CLOSED": ("活动不可用：活动不存在或已关闭", False),
    "DEBIT_INVALID": ("借方行不可用：不存在或不属于本活动", False),
    "CONTENTION_SHORT": ("借方余量不足：同组争用后无可抵扣余量", True),
}

FAILURE_STATUSES = (LINE_REJECTED, LINE_PENDING)
SETTLED_STATUSES = (LINE_CONFIRMED, LINE_PARTIAL)


class WriteOffService:
    """批量冲销业务规则；测试可传入独立 Store 获得隔离状态。"""

    def __init__(self, store_obj: Store | None = None) -> None:
        self._store = store_obj or store
        self._lock = threading.Lock()

    # ---------- 查询 ----------

    def list_batches(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(reversed(self._store.rows(MODULE_BATCH)))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("批次号", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        batch = self._find_batch(batch_no)
        if batch is None:
            return None
        return self._receipt(batch)

    def list_failures(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = [line for line in self._store.rows(MODULE_LINE) if line.get("status") in FAILURE_STATUSES]
        if batch_no:
            rows = [line for line in rows if line.get("批次号") == batch_no]
        return [self._line_view(line) for line in rows]

    def list_ledger(self) -> list[dict[str, Any]]:
        return [self._ledger_view(row) for row in self._store.rows(MODULE_LEDGER)]

    def list_mart_flows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._store.rows(MODULE_MART)]

    # ---------- 批次提交 ----------

    def submit_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_no = str(values.get("批次号") or "").strip()
        activity = str(values.get("活动编号") or "").strip()
        settle_date = str(values.get("结算日") or "").strip()
        raw_lines = values.get("行") or []
        if not batch_no or not activity or not settle_date:
            return None, "缺少必填字段：批次号、活动编号、结算日"
        if not isinstance(raw_lines, list) or not raw_lines:
            return None, "批次至少需要一条冲销行"
        with self._lock:
            existing = self._find_batch(batch_no)
            if existing is not None:
                # 幂等：同批次号（含并发重复提交）只入账一次，直接返回首次回执。
                existing["重复提交次数"] = int(existing.get("重复提交次数", 0)) + 1
                receipt = self._receipt(existing)
                receipt["重复提交"] = True
                return receipt, f"批次 {batch_no} 已入账，本次为重复提交，不新增抵扣"
            batch = {
                "id": self._next_id(MODULE_BATCH),
                "批次号": batch_no,
                "活动编号": activity,
                "结算日": settle_date,
                "提交时间": datetime.now().isoformat(timespec="seconds"),
                "status": BATCH_FAILED,
                "pending": True,
                "abnormal": False,
                "重复提交次数": 0,
                "处理轮次": 1,
            }
            lines = [self._build_line(batch, raw, idx) for idx, raw in enumerate(raw_lines, start=1)]
            self._store.rows(MODULE_BATCH).append(batch)
            self._store.rows(MODULE_LINE).extend(lines)
            self._process_round(batch, lines)
            return self._receipt(batch), f"批次 {batch_no} 处理完成"

    # ---------- 复核重启 ----------

    def restart_failures(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_no = str(values.get("批次号") or "").strip()
        line_nos = [str(no) for no in (values.get("行号列表") or [])]
        corrections = values.get("纠正") or {}
        batch = self._find_batch(batch_no)
        if batch is None:
            return None, f"冲销批次 {batch_no} 不存在"
        if not line_nos:
            return None, "请先多选要重新开始的失败项"
        failures = {
            line["行号"]: line
            for line in self._batch_lines(batch_no)
            if line.get("status") in FAILURE_STATUSES
        }
        skipped = [no for no in line_nos if no not in failures]
        excluded = [no for no in line_nos if no in failures and not failures[no].get("可纠正")]
        with self._lock:
            picked: list[dict[str, Any]] = []
            for no in line_nos:
                line = failures.get(no)
                if line is None or not line.get("可纠正"):
                    continue  # 重新开始只包含可纠正条目
                fix = corrections.get(no) or {}
                for field in ("结算日", "金额", "活动编号"):
                    if fix.get(field) not in (None, ""):
                        line[field] = fix[field]
                amount = self._money(line.get("金额"))
                if amount is not None:
                    line["金额"] = self._out(amount)
                line["处理轮次"] = int(line.get("处理轮次", 1)) + 1
                picked.append(line)
            if not picked:
                return None, "所选条目都不可纠正或不是失败项，没有可重新开始的行"
            batch["处理轮次"] = int(batch.get("处理轮次", 1)) + 1
            before = self._money(batch.get("可入账净额", 0))
            self._process_round(batch, picked)
            after = self._money(batch.get("可入账净额", 0))
        result = {
            "批次号": batch_no,
            "重新开始行号": [line["行号"] for line in picked],
            "剔除不可纠正": excluded,
            "跳过非失败项": skipped,
            "新增确认金额": self._out(after - before),
            "批次": self._receipt(batch),
        }
        return result, f"批次 {batch_no} 已重新开始 {len(picked)} 条可纠正行"

    # ---------- 内部：校验与分配 ----------

    def _process_round(self, batch: dict[str, Any], lines: list[dict[str, Any]]) -> None:
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for line in lines:
            reason = self._validate_line(batch, line, seen)
            if reason is None:
                seen.add((line["贷方行号"], line["源回执号"]))
                candidates.append(line)
            else:
                self._reject(line, reason)
        groups: dict[str, list[dict[str, Any]]] = {}
        for line in candidates:
            groups.setdefault(line["借方行号"], []).append(line)
        for debit_no, group in groups.items():
            ledger = self._find_ledger(debit_no)
            remaining = self._remainder(ledger)
            # 可说明的抵扣顺序：源回执时间升序，贷方行号字典序兜底。
            ordered = sorted(group, key=lambda l: (str(l.get("源回执时间") or ""), str(l.get("贷方行号") or "")))
            for line in ordered:
                amount = self._money(line["金额"])
                if remaining <= 0:
                    self._hold(line, "CONTENTION_SHORT")
                elif amount <= remaining:
                    self._confirm(line, amount)
                    remaining -= amount
                else:
                    self._confirm(line, remaining)  # 边界行部分确认，差额记待补
                    remaining = Decimal("0")
            self._refresh_mart(debit_no)
        self._refresh_batch(batch)

    def _validate_line(self, batch: dict[str, Any], line: dict[str, Any], seen: set[tuple[str, str]]) -> str | None:
        amount = self._money(line.get("金额"))
        if amount is None or amount <= 0:
            return "AMOUNT_INVALID"
        if not line.get("贷方行号") or not line.get("源回执号"):
            return "RECEIPT_MISSING"
        if line.get("活动编号") != batch.get("活动编号"):
            return "ACTIVITY_MISMATCH"
        activity = self._find_activity(batch.get("活动编号"))
        if activity is None or activity.get("状态") != "进行中":
            return "ACTIVITY_CLOSED"
        if line.get("结算日") != batch.get("结算日"):
            return "CROSS_DAY"
        ledger = self._find_ledger(line.get("借方行号"))
        if ledger is None or ledger.get("活动编号") != batch.get("活动编号"):
            return "DEBIT_INVALID"
        if (line["贷方行号"], line["源回执号"]) in seen or self._already_settled(line):
            return "DUPLICATE"
        if amount > self._remainder(ledger):
            return "OVER_WRITE"
        return None

    def _confirm(self, line: dict[str, Any], amount: Decimal) -> None:
        ledger = self._find_ledger(line["借方行号"])
        ledger["已确认冲销"] = self._out(self._money(ledger["已确认冲销"]) + amount)
        requested = self._money(line["金额"])
        line["确认金额"] = self._out(amount)
        line["待补差额"] = self._out(requested - amount)
        line["status"] = LINE_CONFIRMED if requested == amount else LINE_PARTIAL
        line["拒绝原因"] = ""
        line["原因码"] = ""
        line["可纠正"] = False
        line["pending"] = line["status"] == LINE_PARTIAL
        line["abnormal"] = False

    def _reject(self, line: dict[str, Any], code: str) -> None:
        message, correctable = REASONS[code]
        line["status"] = LINE_REJECTED
        line["确认金额"] = 0.0
        line["待补差额"] = 0.0
        line["拒绝原因"] = message
        line["原因码"] = code
        line["可纠正"] = correctable
        line["pending"] = True
        line["abnormal"] = True

    def _hold(self, line: dict[str, Any], code: str) -> None:
        message, correctable = REASONS[code]
        line["status"] = LINE_PENDING
        line["确认金额"] = 0.0
        line["待补差额"] = self._out(self._money(line["金额"]))
        line["拒绝原因"] = message
        line["原因码"] = code
        line["可纠正"] = correctable
        line["pending"] = True
        line["abnormal"] = True

    # ---------- 内部：集市与批次汇总 ----------

    def _refresh_mart(self, debit_no: str) -> None:
        flow = next((row for row in self._store.rows(MODULE_MART) if row.get("借方行号") == debit_no), None)
        ledger = self._find_ledger(debit_no)
        if flow is None or ledger is None:
            return
        confirmed = self._money(ledger["已确认冲销"])
        pending = sum(
            (self._money(line.get("待补差额", 0)) for line in self._store.rows(MODULE_LINE)
             if line.get("借方行号") == debit_no and line.get("status") in (LINE_PARTIAL, LINE_PENDING)),
            Decimal("0"),
        )
        net = self._money(flow["原始金额"]) - confirmed
        flow["已确认净额"] = self._out(net)
        flow["待补差额"] = self._out(pending)
        if net == 0 and pending == 0:
            flow["状态"] = "已结清"
        elif pending > 0:
            flow["状态"] = "待补差"
        elif confirmed > 0:
            flow["状态"] = "部分冲销"
        else:
            flow["状态"] = "正常"

    def _refresh_batch(self, batch: dict[str, Any]) -> None:
        lines = self._batch_lines(batch["批次号"])
        confirmed = [line for line in lines if line.get("status") in SETTLED_STATUSES]
        partial = [line for line in lines if line.get("status") == LINE_PARTIAL]
        rejected = [line for line in lines if line.get("status") == LINE_REJECTED]
        held = [line for line in lines if line.get("status") == LINE_PENDING]
        batch["行数"] = len(lines)
        batch["可入账净额"] = self._out(sum((self._money(line.get("确认金额", 0)) for line in confirmed), Decimal("0")))
        batch["确认条数"] = len(confirmed)
        batch["拒绝条数"] = len(rejected)
        batch["待复核条数"] = len(held)
        if not rejected and not held and not partial:
            batch["status"] = BATCH_POSTED
        elif confirmed:
            batch["status"] = BATCH_PARTIAL
        else:
            batch["status"] = BATCH_FAILED
        batch["pending"] = any(line.get("pending") for line in lines)
        batch["abnormal"] = any(line.get("abnormal") for line in lines)

    # ---------- 内部：视图与查找 ----------

    def _receipt(self, batch: dict[str, Any]) -> dict[str, Any]:
        lines = self._batch_lines(batch["批次号"])
        debits = {line.get("借方行号") for line in lines}
        receipt = dict(batch)
        receipt["行回执"] = [self._line_view(line) for line in lines]
        receipt["集市流水"] = [
            dict(row) for row in self._store.rows(MODULE_MART) if row.get("借方行号") in debits
        ]
        return receipt

    def _line_view(self, line: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": line.get("id"),
            "批次号": line.get("批次号"),
            "行号": line.get("行号"),
            "借方行号": line.get("借方行号"),
            "贷方行号": line.get("贷方行号"),
            "金额": line.get("金额"),
            "确认金额": line.get("确认金额", 0.0),
            "待补差额": line.get("待补差额", 0.0),
            "status": line.get("status"),
            "拒绝原因": line.get("拒绝原因", ""),
            "可纠正": bool(line.get("可纠正")),
            "源回执号": line.get("源回执号"),
            "源回执时间": line.get("源回执时间"),
            "冲销原因": line.get("冲销原因"),
            "活动编号": line.get("活动编号"),
            "结算日": line.get("结算日"),
            "处理轮次": line.get("处理轮次", 1),
        }

    def _ledger_view(self, row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        view["余量"] = self._out(self._remainder(row))
        return view

    def _build_line(self, batch: dict[str, Any], raw: dict[str, Any], idx: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raw = {}
        amount = self._money(raw.get("金额"))
        return {
            "id": self._next_id(MODULE_LINE),
            "批次号": batch["批次号"],
            "行号": str(raw.get("行号") or f"L{idx}"),
            "借方行号": str(raw.get("借方行号") or "").strip(),
            "贷方行号": str(raw.get("贷方行号") or "").strip(),
            "金额": self._out(amount) if amount is not None else raw.get("金额"),
            "源回执号": str(raw.get("源回执号") or "").strip(),
            "源回执时间": str(raw.get("源回执时间") or ""),
            "冲销原因": str(raw.get("冲销原因") or ""),
            "活动编号": str(raw.get("活动编号") or batch["活动编号"]).strip(),
            "结算日": str(raw.get("结算日") or batch["结算日"]).strip(),
            "处理轮次": 1,
        }

    def _find_batch(self, batch_no: str) -> dict[str, Any] | None:
        return next((row for row in self._store.rows(MODULE_BATCH) if row.get("批次号") == batch_no), None)

    def _batch_lines(self, batch_no: str) -> list[dict[str, Any]]:
        return [line for line in self._store.rows(MODULE_LINE) if line.get("批次号") == batch_no]

    def _find_ledger(self, debit_no: str | None) -> dict[str, Any] | None:
        return next((row for row in self._store.rows(MODULE_LEDGER) if row.get("借方行号") == debit_no), None)

    def _find_activity(self, code: str | None) -> dict[str, Any] | None:
        return next((row for row in self._store.rows(MODULE_ACTIVITY) if row.get("活动编号") == code), None)

    def _already_settled(self, line: dict[str, Any]) -> bool:
        key = (line["贷方行号"], line["源回执号"])
        return any(
            other is not line
            and (other.get("贷方行号"), other.get("源回执号")) == key
            and other.get("status") in SETTLED_STATUSES
            for other in self._store.rows(MODULE_LINE)
        )

    def _remainder(self, ledger: dict[str, Any] | None) -> Decimal:
        if ledger is None:
            return Decimal("0")
        return self._money(ledger["原量"]) - self._money(ledger["已确认冲销"])

    def _next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self._store.rows(module)), default=0) + 1

    @staticmethod
    def _money(value: Any) -> Decimal | None:
        try:
            return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _out(value: Any) -> float:
        return float(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))
