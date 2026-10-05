import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
TO_ACTIVE = [('assess', 'intake_officer', {'assessment_note': '收入波动'}), ('approve', 'underwriter', {'exception_approved': False}), ('activate', 'servicer', {'borrower_ack': True})]


class GuaranteeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.admin = Actor("root", "admin")
        self.officer = Actor("officer-1", "guarantee_officer")
        self.reviewer = Actor("uw-1", "underwriter")
        self.service.create_quota_pool(self.admin, {"year": 2026, "total_amount": 10000.0})
        self.service.create_agency(self.admin, {"code": "GA01", "name": "城投担保"})
        self.service.create_agency(self.admin, {"code": "GA02", "name": "惠民担保"})

    def tearDown(self):
        self.temp.cleanup()

    def _active_record(self, reference="MORT-90001"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        for action, role, data in TO_ACTIVE:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        return record

    def _pool(self):
        return self.service.list_quota_pools(self.admin)[0]

    def test_submit_preoccupies_and_confirm_activates(self):
        record = self._active_record()
        batch, created = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 4000.0, "year": 2026})
        self.assertTrue(created)
        self.assertEqual(batch["status"], "pre_occupied")
        self.assertEqual(self._pool()["available"], 6000.0)
        confirmed = self.service.confirm_compensation(self.reviewer, batch["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        again = self.service.confirm_compensation(self.reviewer, batch["id"])
        self.assertEqual(again["status"], "confirmed")
        pool = self._pool()
        self.assertEqual(pool["pre_occupied"], 0.0)
        self.assertEqual(pool["confirmed"], 4000.0)
        self.assertEqual(pool["available"], 6000.0)

    def test_shared_quota_first_come_first_served(self):
        record = self._active_record()
        self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 7000.0, "year": 2026})
        with self.assertRaises(Conflict):
            self.service.submit_compensation(Actor("officer-2", "guarantee_officer"), record["id"], {"batch_no": "B-2", "agency_code": "GA02", "amount": 4000.0, "year": 2026})

    def test_concurrent_submission_first_wins(self):
        record = self._active_record()
        successes, failures = [], []

        def submit(tag):
            try:
                self.service.submit_compensation(Actor("officer-%s" % tag, "guarantee_officer"), record["id"], {"batch_no": "B-C-%s" % tag, "agency_code": "GA01", "amount": 7000.0, "year": 2026})
                successes.append(tag)
            except Conflict:
                failures.append(tag)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self._pool()["pre_occupied"], 7000.0)

    def test_retry_same_batch_restores_without_double_occupation(self):
        record = self._active_record()
        first, created = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 4000.0, "year": 2026})
        replay, created_again = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 4000.0, "year": 2026})
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(self._pool()["pre_occupied"], 4000.0)
        with self.assertRaises(Conflict):
            self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 5000.0, "year": 2026})

    def test_recovery_matching_shortfall_and_overflow(self):
        record = self._active_record()
        batch, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 6000.0, "year": 2026})
        self.service.confirm_compensation(self.reviewer, batch["id"])
        first, created = self.service.post_recovery(self.officer, batch["id"], {"flow_no": "F-1", "amount": 2500.0})
        self.assertTrue(created)
        self.assertEqual(first["applied"], 2500.0)
        self.assertEqual(first["refunded"], 0.0)
        self.assertEqual(first["remaining"], 3500.0)
        replay, created = self.service.post_recovery(self.officer, batch["id"], {"flow_no": "F-1", "amount": 2500.0})
        self.assertFalse(created)
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["remaining"], 3500.0)
        with self.assertRaises(Conflict):
            self.service.post_recovery(self.officer, batch["id"], {"flow_no": "F-1", "amount": 2600.0})
        overflow, _ = self.service.post_recovery(self.officer, batch["id"], {"flow_no": "F-2", "amount": 5000.0})
        self.assertEqual(overflow["applied"], 3500.0)
        self.assertEqual(overflow["refunded"], 1500.0)
        self.assertEqual(overflow["remaining"], 0.0)
        detail = self.service.get_compensation(self.officer, batch["id"])
        self.assertEqual(detail["outstanding"], 0.0)
        self.assertEqual(len(detail["recoveries"]), 2)
        pool = self._pool()
        self.assertEqual(pool["recovered"], 6000.0)
        self.assertEqual(pool["available"], 10000.0)

    def test_recovery_requires_confirmed_batch(self):
        record = self._active_record()
        batch, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 2000.0, "year": 2026})
        with self.assertRaises(Conflict):
            self.service.post_recovery(self.officer, batch["id"], {"flow_no": "F-1", "amount": 100.0})

    def test_default_voids_unconfirmed_batches(self):
        record = self._active_record()
        pending, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 2000.0, "year": 2026})
        confirmed, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-2", "agency_code": "GA02", "amount": 3000.0, "year": 2026})
        self.service.confirm_compensation(self.reviewer, confirmed["id"])
        record = self.service.act(Actor("servicer-1", "servicer"), record["id"], record["version"], "default", {"default_reason": "借款人失联"})
        self.assertEqual(record["state"], "defaulted")
        self.assertEqual(self.service.get_compensation(self.officer, pending["id"])["status"], "voided")
        self.assertEqual(self.service.get_compensation(self.officer, confirmed["id"])["status"], "confirmed")
        pool = self._pool()
        self.assertEqual(pool["pre_occupied"], 0.0)
        self.assertEqual(pool["confirmed"], 3000.0)
        self.assertEqual(pool["available"], 7000.0)
        with self.assertRaises(Conflict):
            self.service.confirm_compensation(self.reviewer, pending["id"])
        timeline = self.service.timeline(self.admin, record["id"])
        self.assertEqual(timeline[-1]["details"]["voided_batches"], 1)
        # 方案失效作废重算后，可以按新批次重新提交
        resubmitted, created = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-3", "agency_code": "GA01", "amount": 7000.0, "year": 2026})
        self.assertTrue(created)
        self.assertEqual(self._pool()["available"], 0.0)

    def test_cure_voids_unconfirmed_batches(self):
        record = self._active_record()
        batch, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 2000.0, "year": 2026})
        record = self.service.act(Actor("servicer-1", "servicer"), record["id"], record["version"], "cure", {"arrears_cleared": True})
        self.assertEqual(record["state"], "cured")
        self.assertEqual(self.service.get_compensation(self.officer, batch["id"])["status"], "voided")
        self.assertEqual(self._pool()["available"], 10000.0)

    def test_submit_requires_active_or_defaulted_record(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-90002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 1000.0, "year": 2026})

    def test_permissions(self):
        record = self._active_record("MORT-90003")
        with self.assertRaises(PermissionDenied):
            self.service.create_quota_pool(self.officer, {"year": 2027, "total_amount": 5000.0})
        with self.assertRaises(PermissionDenied):
            self.service.create_agency(self.officer, {"code": "GA03", "name": "越权机构"})
        with self.assertRaises(PermissionDenied):
            self.service.submit_compensation(self.reviewer, record["id"], {"batch_no": "B-9", "agency_code": "GA01", "amount": 1000.0, "year": 2026})
        batch, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-10", "agency_code": "GA01", "amount": 1000.0, "year": 2026})
        with self.assertRaises(PermissionDenied):
            self.service.confirm_compensation(self.officer, batch["id"])

    def test_duplicate_pool_agency_and_unknown_agency(self):
        with self.assertRaises(Conflict):
            self.service.create_quota_pool(self.admin, {"year": 2026, "total_amount": 1.0})
        with self.assertRaises(Conflict):
            self.service.create_agency(self.admin, {"code": "GA01", "name": "重复登记"})
        record = self._active_record()
        with self.assertRaises(NotFound):
            self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-X", "agency_code": "GA99", "amount": 100.0, "year": 2026})
        with self.assertRaises(NotFound):
            self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-Y", "agency_code": "GA01", "amount": 100.0, "year": 2027})

    def test_stats_show_batches_recoveries_and_outstanding(self):
        record = self._active_record()
        batch, _ = self.service.submit_compensation(self.officer, record["id"], {"batch_no": "B-1", "agency_code": "GA01", "amount": 6000.0, "year": 2026})
        self.service.confirm_compensation(self.reviewer, batch["id"])
        self.service.post_recovery(self.officer, batch["id"], {"flow_no": "F-1", "amount": 2500.0})
        stats = self.service.stats(self.admin)
        self.assertEqual(stats["records"]["active"], 1)
        self.assertEqual(stats["guarantee"]["batches"]["confirmed"], 1)
        self.assertEqual(stats["guarantee"]["recoveries"]["applied"], 2500.0)
        self.assertEqual(stats["guarantee"]["recoveries"]["outstanding"], 3500.0)
        self.assertEqual(stats["quota_pools"][0]["available"], 6500.0)
