import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


YEAR = 2026
OFFICER = Actor("officer", "guarantor_officer")
REVIEWER = Actor("reviewer", "guarantor_reviewer")
ADMIN = Actor("admin", "guarantee_admin")


def setup_world(service):
    service.create_guarantor(ADMIN, {"code": "G01", "name": "市融资担保公司"})
    service.create_guarantor(ADMIN, {"code": "G02", "name": "省再担保集团"})
    service.configure_quota(ADMIN, {"year": YEAR, "total_quota": 1000000})


def submit(service, batch_no, amount, guarantor="G01", record_id=None):
    payload = {"batch_no": batch_no, "guarantor_code": guarantor, "amount": amount, "year": YEAR}
    if record_id is not None:
        payload["record_id"] = record_id
    return service.submit_compensation(OFFICER, payload)


class GuaranteeServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        setup_world(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_submit_preoccupies_and_review_confirms(self):
        batch = submit(self.service, "B-001", 300000)
        self.assertEqual(batch["status"], "pending")
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["pending_amount"], 300000)
        self.assertEqual(overview["available_amount"], 700000)

        confirmed = self.service.review_batch(REVIEWER, batch["id"], True, {"review_note": "材料齐全"})
        self.assertEqual(confirmed["status"], "confirmed")
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["pending_amount"], 0)
        self.assertEqual(overview["confirmed_amount"], 300000)
        self.assertEqual(overview["available_amount"], 700000)

    def test_reject_releases_preoccupation(self):
        batch = submit(self.service, "B-002", 400000)
        self.service.review_batch(REVIEWER, batch["id"], False, {})
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["occupied_amount"], 0)
        self.assertEqual(overview["available_amount"], 1000000)

    def test_quota_overflow_rejected(self):
        submit(self.service, "B-003", 800000)
        with self.assertRaises(Conflict):
            submit(self.service, "B-004", 300000)
        # 失败后没有残留占用
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["occupied_amount"], 800000)

    def test_resubmit_same_batch_no_restores_without_double_occupy(self):
        first = submit(self.service, "B-005", 250000)
        # 模拟写入失败后的重试：同样批次号返回原批次，不重复占用
        again = submit(self.service, "B-005", 250000)
        self.assertTrue(again["idempotent_hit"])
        self.assertEqual(again["id"], first["id"])
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["pending_amount"], 250000)
        self.assertEqual(overview["pending_batches"], 1)
        # 同批次号但金额不一致应拒绝
        with self.assertRaises(Conflict):
            submit(self.service, "B-005", 999999)

    def test_recovery_partial_excess_and_duplicate_serial(self):
        batch = self.service.review_batch(REVIEWER, submit(self.service, "B-006", 300000)["id"], True, {})
        r1 = self.service.register_recovery(OFFICER, batch["id"], {"serial_no": "TX-1", "amount": 120000})
        self.assertEqual(r1["applied_amount"], 120000)
        self.assertEqual(r1["refunded_amount"], 0)
        self.assertEqual(r1["remaining_after"], 180000)  # 不足留差额

        r2 = self.service.register_recovery(OFFICER, batch["id"], {"serial_no": "TX-2", "amount": 200000})
        self.assertEqual(r2["applied_amount"], 180000)
        self.assertEqual(r2["refunded_amount"], 20000)  # 超额退回
        self.assertEqual(r2["remaining_after"], 0)

        # 同一流水重复提交：返回原回款，不重复冲减
        dup = self.service.register_recovery(OFFICER, batch["id"], {"serial_no": "TX-1", "amount": 120000})
        self.assertTrue(dup["idempotent_hit"])
        self.assertEqual(dup["id"], r1["id"])
        view = self.service.get_batch(OFFICER, batch["id"])
        self.assertEqual(view["recovered_amount"], 300000)
        self.assertEqual(view["outstanding_amount"], 0)

        # 流水号必须全局唯一
        another = self.service.review_batch(REVIEWER, submit(self.service, "B-007", 50000)["id"], True, {})
        with self.assertRaises(Conflict):
            self.service.register_recovery(OFFICER, another["id"], {"serial_no": "TX-1", "amount": 10})

    def test_recovery_only_after_confirm(self):
        batch = submit(self.service, "B-008", 50000)
        with self.assertRaises(Conflict):
            self.service.register_recovery(OFFICER, batch["id"], {"serial_no": "TX-9", "amount": 10000})

    def test_concurrent_submit_first_wins(self):
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(slot, batch_no):
            actor = Actor("officer-%s" % slot, "guarantor_officer")
            barrier.wait()
            try:
                outcomes[slot] = ("ok", submit(self.service, batch_no, 800000,
                                               guarantor="G01" if slot == 1 else "G02")["id"])
            except Conflict as exc:
                outcomes[slot] = ("conflict", str(exc))

        t1 = threading.Thread(target=worker, args=(1, "B-C1"))
        t2 = threading.Thread(target=worker, args=(2, "B-C2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        results = sorted(outcomes.values())
        self.assertEqual(results[0][0], "conflict")
        self.assertEqual(results[1][0], "ok")
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["occupied_amount"], 800000)  # 只有一笔占用，无超卖

    def test_defaulted_plan_voids_pending_and_recalculates(self):
        from tests.test_workflow import CREATE_DATA, FLOW
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-G-9001", CREATE_DATA)
        for action, role, data, _ in FLOW[:3]:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.assertEqual(record["state"], "active")

        pending = submit(self.service, "B-P1", 200000, record_id=record["id"])
        confirmed = submit(self.service, "B-CF", 100000, record_id=record["id"])
        self.service.review_batch(REVIEWER, confirmed["id"], True, {})

        record = self.service.act(Actor("servicer1", "servicer"), record["id"], record["version"],
                                  "default", {"default_reason": "再次违约"})
        self.assertEqual(record["state"], "defaulted")

        view = self.service.get_batch(ADMIN, pending["id"])
        self.assertEqual(view["status"], "voided")
        # 已确认批次继续有效，旧的待确认占用被释放
        self.assertEqual(self.service.get_batch(ADMIN, confirmed["id"])["status"], "confirmed")
        overview = {q["year"]: q for q in self.service.quota_overview(ADMIN, YEAR)}[YEAR]
        self.assertEqual(overview["occupied_amount"], 100000)
        self.assertEqual(overview["available_amount"], 900000)

        # 作废后不能再复核
        with self.assertRaises(Conflict):
            self.service.review_batch(REVIEWER, pending["id"], True, {})
        # 方案失效后不能再提交代偿
        with self.assertRaises(Conflict):
            submit(self.service, "B-P2", 10000, record_id=record["id"])

    def test_stats_include_batches_recoveries_and_difference(self):
        batch = self.service.review_batch(REVIEWER, submit(self.service, "B-S1", 300000)["id"], True, {})
        self.service.register_recovery(OFFICER, batch["id"], {"serial_no": "TX-S", "amount": 350000})
        stats = self.service.guarantee_stats(ADMIN, YEAR)
        self.assertEqual(stats["confirmed_amount"] if "confirmed_amount" in stats else stats["occupied_amount"], 300000)
        self.assertEqual(stats["recoveries"]["applied_amount"], 300000)
        self.assertEqual(stats["recoveries"]["refunded_amount"], 50000)
        g01 = next(x for x in stats["by_guarantor"] if x["guarantor_code"] == "G01")
        self.assertEqual(g01["outstanding_amount"], 0)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_guarantor(Actor("x", "guarantor_officer"), {"code": "GX", "name": "X"})
        with self.assertRaises(PermissionDenied):
            self.service.configure_quota(Actor("x", "guarantor_officer"), {"total_quota": 1})
        with self.assertRaises(PermissionDenied):
            self.service.review_batch(Actor("x", "guarantor_officer"), 1, True, {})
        with self.assertRaises(PermissionDenied):
            self.service.submit_compensation(
                Actor("x", "guarantor_reviewer"),
                {"batch_no": "B-X", "guarantor_code": "G01", "amount": 1, "year": YEAR})

    def test_unknown_guarantor_and_quota_validation(self):
        with self.assertRaises(Exception):
            submit(self.service, "B-U1", 10, guarantor="NOPE")
        payload = {"batch_no": "B-U2", "guarantor_code": "G01", "amount": 10, "year": 2099}
        with self.assertRaises(Exception):
            self.service.submit_compensation(OFFICER, payload)
