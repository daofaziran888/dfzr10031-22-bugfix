"""批量入账冲销场景测试：争用分配、集市回执、幂等重送、异常拒绝与复核重启。

运行：cd backend && python3 -m unittest tests.test_writeoff -v
"""
from __future__ import annotations

import threading
import unittest

from app.services.writeoff import WriteOffService
from app.store import Store

ACT = "ACT-2026-11"
SETTLE = "2026-11-20"


def make_service() -> WriteOffService:
    """每个用例一份独立的内存仓库，互不影响。"""
    return WriteOffService(Store())


def line(line_no, debit, credit, amount, receipt, receipt_time, settle=SETTLE, activity=ACT, reason="重复入账"):
    return {
        "行号": line_no,
        "借方行号": debit,
        "贷方行号": credit,
        "金额": amount,
        "源回执号": receipt,
        "源回执时间": receipt_time,
        "冲销原因": reason,
        "活动编号": activity,
        "结算日": settle,
    }


def submit(service, batch_no, lines, activity=ACT, settle=SETTLE):
    receipt, message = service.submit_batch(
        {"批次号": batch_no, "活动编号": activity, "结算日": settle, "行": lines}
    )
    return receipt, message


def line_of(receipt, line_no):
    return next(l for l in receipt["行回执"] if l["行号"] == line_no)


def ledger_remaining(service, debit_no):
    row = next(r for r in service.list_ledger() if r["借方行号"] == debit_no)
    return row["余量"]


def ledger_confirmed(service, debit_no):
    row = next(r for r in service.list_ledger() if r["借方行号"] == debit_no)
    return row["已确认冲销"]


def mart_flow(service, debit_no):
    return next(r for r in service.list_mart_flows() if r["借方行号"] == debit_no)


class WriteOffTestCase(unittest.TestCase):
    def assertLedgerInvariant(self, service):
        """同一借方行的冲销总量不能超过原量。"""
        for row in service.list_ledger():
            self.assertLessEqual(row["已确认冲销"], row["原量"], row["借方行号"])
            self.assertGreaterEqual(row["余量"], 0, row["借方行号"])


class ContentionTest(WriteOffTestCase):
    def test_contention_partial_confirm_by_explainable_order(self):
        """借方余量 100，两条有效贷方各 70：按源回执时间先确认一条，边界行部分确认。"""
        service = make_service()
        # 故意把回执时间晚的行放在载荷前面，验证抵扣顺序看回执时间而非提交顺序。
        receipt, _ = submit(service, "WO-C1", [
            line("L2", "DBT-1001", "CR-02", 70, "RCPT-002", "2026-11-20 10:01:00"),
            line("L1", "DBT-1001", "CR-01", 70, "RCPT-001", "2026-11-20 10:00:00"),
        ])
        first, second = line_of(receipt, "L1"), line_of(receipt, "L2")
        self.assertEqual(first["status"], "已确认")
        self.assertEqual(first["确认金额"], 70.0)
        self.assertEqual(second["status"], "部分确认")
        self.assertEqual(second["确认金额"], 30.0)
        self.assertEqual(second["待补差额"], 40.0)
        # 借方台账：冲销总量等于原量，余量归零。
        self.assertEqual(ledger_confirmed(service, "DBT-1001"), 100.0)
        self.assertEqual(ledger_remaining(service, "DBT-1001"), 0.0)
        # 集市：沿原流水更新已确认净额与待补差额。
        flow = mart_flow(service, "DBT-1001")
        self.assertEqual(flow["已确认净额"], 0.0)
        self.assertEqual(flow["待补差额"], 40.0)
        self.assertEqual(flow["状态"], "待补差")
        # 批次净额 = 70 + 30。
        self.assertEqual(receipt["可入账净额"], 100.0)
        self.assertEqual(receipt["status"], "部分入账")
        self.assertLedgerInvariant(service)

    def test_unrelated_line_in_same_group_posts_independently(self):
        """同组加入一条无关的有效冲销：争用结果不变，无关行独立入账。"""
        service = make_service()
        receipt, _ = submit(service, "WO-C2", [
            line("L1", "DBT-1001", "CR-01", 70, "RCPT-001", "2026-11-20 10:00:00"),
            line("L2", "DBT-1001", "CR-02", 70, "RCPT-002", "2026-11-20 10:01:00"),
            line("L3", "DBT-1002", "CR-03", 20, "RCPT-003", "2026-11-20 10:02:00"),
        ])
        self.assertEqual(line_of(receipt, "L1")["确认金额"], 70.0)
        self.assertEqual(line_of(receipt, "L2")["确认金额"], 30.0)
        third = line_of(receipt, "L3")
        self.assertEqual(third["status"], "已确认")
        self.assertEqual(third["确认金额"], 20.0)
        # 集市两条原流水各自更新。
        flow1, flow2 = mart_flow(service, "DBT-1001"), mart_flow(service, "DBT-1002")
        self.assertEqual((flow1["已确认净额"], flow1["待补差额"]), (0.0, 40.0))
        self.assertEqual((flow2["已确认净额"], flow2["待补差额"]), (30.0, 0.0))
        self.assertEqual(flow2["状态"], "部分冲销")
        self.assertEqual(receipt["可入账净额"], 120.0)
        self.assertLedgerInvariant(service)

    def test_line_beyond_exhausted_remainder_goes_pending(self):
        """余量耗尽后的行挂待复核（可纠正），不计确认金额。"""
        service = make_service()
        receipt, _ = submit(service, "WO-C3", [
            line("L1", "DBT-1001", "CR-01", 50, "RCPT-001", "2026-11-20 10:00:00"),
            line("L2", "DBT-1001", "CR-02", 50, "RCPT-002", "2026-11-20 10:01:00"),
            line("L3", "DBT-1001", "CR-03", 50, "RCPT-003", "2026-11-20 10:02:00"),
        ])
        held = line_of(receipt, "L3")
        self.assertEqual(held["status"], "待复核")
        self.assertEqual(held["确认金额"], 0.0)
        self.assertEqual(held["待补差额"], 50.0)
        self.assertTrue(held["可纠正"])
        flow = mart_flow(service, "DBT-1001")
        self.assertEqual((flow["已确认净额"], flow["待补差额"]), (0.0, 50.0))
        self.assertLedgerInvariant(service)


class RejectReasonTest(WriteOffTestCase):
    def _submit_anomaly_batch(self, service):
        submit(service, "WO-A", [line("A1", "DBT-1003", "CR-A1", 30, "RCPT-A1", "2026-11-20 09:00:00")])
        receipt, _ = submit(service, "WO-B", [
            line("L1", "DBT-1003", "CR-B1", 20, "RCPT-B1", "2026-11-20 10:00:00", settle="2026-11-21"),
            line("L2", "DBT-1003", "CR-A1", 30, "RCPT-A1", "2026-11-20 10:01:00"),
            line("L3", "DBT-1002", "CR-B3", 60, "RCPT-B3", "2026-11-20 10:02:00"),
            line("L4", "DBT-1002", "CR-B4", 20, "RCPT-B4", "2026-11-20 10:03:00"),
        ])
        return receipt

    def test_cross_day_duplicate_and_over_write_lines(self):
        """跨日、重复、超冲分别给出拒绝原因；可入账净额只含有效行。"""
        service = make_service()
        receipt = self._submit_anomaly_batch(service)
        l1, l2, l3, l4 = (line_of(receipt, no) for no in ("L1", "L2", "L3", "L4"))
        self.assertEqual(l1["status"], "已拒绝")
        self.assertIn("跨日", l1["拒绝原因"])
        self.assertTrue(l1["可纠正"])
        self.assertEqual(l2["status"], "已拒绝")
        self.assertIn("重复", l2["拒绝原因"])
        self.assertFalse(l2["可纠正"])
        self.assertEqual(l3["status"], "已拒绝")
        self.assertIn("超冲", l3["拒绝原因"])
        self.assertTrue(l3["可纠正"])
        self.assertEqual(l4["status"], "已确认")
        self.assertEqual(l4["确认金额"], 20.0)
        # 可入账净额只算有效行。
        self.assertEqual(receipt["可入账净额"], 20.0)
        self.assertEqual(receipt["拒绝条数"], 3)
        # 失败清单与可纠正清单。
        failures = service.list_failures("WO-B")
        self.assertEqual({f["行号"] for f in failures}, {"L1", "L2", "L3"})
        correctable = {f["行号"] for f in failures if f["可纠正"]}
        self.assertEqual(correctable, {"L1", "L3"})
        # 集市：DBT-1003 净额 50（WO-A 确认 30），DBT-1002 净额 30。
        self.assertEqual(mart_flow(service, "DBT-1003")["已确认净额"], 50.0)
        self.assertEqual(mart_flow(service, "DBT-1002")["已确认净额"], 30.0)
        self.assertLedgerInvariant(service)

    def test_restart_only_correctable_entries(self):
        """复核多选重新开始：只处理可纠正条目，不可纠正的被剔除。"""
        service = make_service()
        self._submit_anomaly_batch(service)
        result, message = service.restart_failures({
            "批次号": "WO-B",
            "行号列表": ["L1", "L2", "L3"],
            "纠正": {"L1": {"结算日": SETTLE}, "L3": {"金额": 30}},
        })
        self.assertIsNotNone(result, message)
        self.assertEqual(result["重新开始行号"], ["L1", "L3"])
        self.assertEqual(result["剔除不可纠正"], ["L2"])
        self.assertEqual(result["新增确认金额"], 50.0)
        batch = result["批次"]
        l1, l2, l3 = (line_of(batch, no) for no in ("L1", "L2", "L3"))
        self.assertEqual(l1["status"], "已确认")
        self.assertEqual(l1["确认金额"], 20.0)
        self.assertEqual(l1["处理轮次"], 2)
        self.assertEqual(l3["status"], "已确认")
        self.assertEqual(l3["确认金额"], 30.0)
        self.assertEqual(l2["status"], "已拒绝")  # 不可纠正，维持原状
        self.assertIn("重复", l2["拒绝原因"])
        self.assertEqual(batch["可入账净额"], 70.0)
        # 台账：DBT-1003 = 30 + 20，DBT-1002 = 20 + 30。
        self.assertEqual(ledger_confirmed(service, "DBT-1003"), 50.0)
        self.assertEqual(ledger_confirmed(service, "DBT-1002"), 50.0)
        self.assertEqual(mart_flow(service, "DBT-1002")["状态"], "已结清")
        self.assertLedgerInvariant(service)

    def test_restart_rejects_when_nothing_correctable(self):
        """只选中不可纠正条目时，重新开始不产生任何处理。"""
        service = make_service()
        self._submit_anomaly_batch(service)
        result, message = service.restart_failures({"批次号": "WO-B", "行号列表": ["L2"]})
        self.assertIsNone(result)
        self.assertIn("不可纠正", message)
        self.assertEqual(ledger_confirmed(service, "DBT-1003"), 30.0)

    def test_resubmit_anomaly_batch_adds_no_deduction(self):
        """跨日/重复/超冲批次重送：不新增抵扣，台账与集市保持不变。"""
        service = make_service()
        self._submit_anomaly_batch(service)
        before = {row["借方行号"]: row["已确认冲销"] for row in service.list_ledger()}
        again, message = submit(service, "WO-B", [
            line("L1", "DBT-1003", "CR-B1", 20, "RCPT-B1", "2026-11-20 10:00:00", settle="2026-11-21"),
            line("L2", "DBT-1003", "CR-A1", 30, "RCPT-A1", "2026-11-20 10:01:00"),
            line("L3", "DBT-1002", "CR-B3", 60, "RCPT-B3", "2026-11-20 10:02:00"),
            line("L4", "DBT-1002", "CR-B4", 20, "RCPT-B4", "2026-11-20 10:03:00"),
        ])
        self.assertTrue(again["重复提交"])
        after = {row["借方行号"]: row["已确认冲销"] for row in service.list_ledger()}
        self.assertEqual(before, after)
        self.assertEqual(mart_flow(service, "DBT-1002")["已确认净额"], 30.0)
        self.assertLedgerInvariant(service)


class IdempotencyTest(WriteOffTestCase):
    def test_resubmit_same_batch_adds_no_deduction(self):
        """同一批次重复提交：只记一次已核验的贷方余额，不新增抵扣。"""
        service = make_service()
        first, _ = submit(service, "WO-R", [
            line("L1", "DBT-1001", "CR-01", 40, "RCPT-001", "2026-11-20 10:00:00"),
        ])
        self.assertEqual(first["可入账净额"], 40.0)
        again, message = submit(service, "WO-R", [
            line("L1", "DBT-1001", "CR-01", 40, "RCPT-001", "2026-11-20 10:00:00"),
        ])
        self.assertTrue(again["重复提交"])
        self.assertIn("重复提交", message)
        self.assertEqual(again["重复提交次数"], 1)
        self.assertEqual(ledger_confirmed(service, "DBT-1001"), 40.0)
        self.assertEqual(mart_flow(service, "DBT-1001")["已确认净额"], 60.0)
        batches, total = service.list_batches()
        self.assertEqual(total, 1)
        self.assertLedgerInvariant(service)

    def test_concurrent_duplicate_batch_posts_once(self):
        """同一冲销批次同时提交两次：只入账一次。"""
        service = make_service()
        payload = {"批次号": "WO-Z", "活动编号": ACT, "结算日": SETTLE, "行": [
            line("L1", "DBT-1001", "CR-01", 25, "RCPT-001", "2026-11-20 10:00:00"),
        ]}
        receipts = []

        def worker():
            receipt, _ = service.submit_batch(dict(payload))
            receipts.append(receipt)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(ledger_confirmed(service, "DBT-1001"), 25.0)
        self.assertEqual(sum(1 for r in receipts if r.get("重复提交")), 1)
        _, total = service.list_batches()
        self.assertEqual(total, 1)
        self.assertLedgerInvariant(service)

    def test_partial_resend_skips_confirmed_lines(self):
        """部分批次重送：已确认明细按贷方行号+源回执去重，不重复影响金额。"""
        service = make_service()
        submit(service, "WO-P1", [
            line("L1", "DBT-1002", "CR-P1", 20, "RCPT-P1", "2026-11-20 10:00:00"),
        ])
        receipt, _ = submit(service, "WO-P2", [
            line("L1", "DBT-1002", "CR-P1", 20, "RCPT-P1", "2026-11-20 10:00:00"),
            line("L2", "DBT-1002", "CR-P2", 15, "RCPT-P2", "2026-11-20 10:01:00"),
        ])
        resent, fresh = line_of(receipt, "L1"), line_of(receipt, "L2")
        self.assertEqual(resent["status"], "已拒绝")
        self.assertIn("重复", resent["拒绝原因"])
        self.assertEqual(fresh["status"], "已确认")
        # 台账只增加新行的 15，重送行不重复影响金额。
        self.assertEqual(ledger_confirmed(service, "DBT-1002"), 35.0)
        self.assertEqual(mart_flow(service, "DBT-1002")["已确认净额"], 15.0)
        self.assertLedgerInvariant(service)


if __name__ == "__main__":
    unittest.main()
