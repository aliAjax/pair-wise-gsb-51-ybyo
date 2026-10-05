import unittest

from src.guarantees import GuaranteeRules


class GuaranteeRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = GuaranteeRules()

    def test_recovery_below_outstanding_leaves_difference(self):
        applied, refunded = self.rules.allocate(300000.0, 100000.0)
        self.assertEqual((applied, refunded), (100000.0, 0.0))

    def test_recovery_excess_is_refunded(self):
        applied, refunded = self.rules.allocate(200000.0, 250000.0)
        self.assertEqual((applied, refunded), (200000.0, 50000.0))

    def test_recovery_exact_amount(self):
        applied, refunded = self.rules.allocate(1200.0, 1200.0)
        self.assertEqual((applied, refunded), (1200.0, 0.0))

    def test_validate_batch_requires_amount(self):
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            self.rules.validate_batch({"batch_no": "B1", "guarantor_code": "G01", "amount": 0})
