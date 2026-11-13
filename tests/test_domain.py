import copy
import json
import unittest

from claims_development.application import ROOT
from claims_development.domain import run


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'fixtures/data.json').read_text())
        self.data = {key: [] for key in self.fixture['tables']}

    def test_fixture_is_deterministic_and_not_mutated(self):
        original = copy.deepcopy(self.fixture['tables'])
        options = {'cutoff': self.fixture['meta']['default_cutoff'], 'group': ''}
        first = run(self.fixture['tables'], options)
        second = run(self.fixture['tables'], options)
        self.assertEqual(first, second)
        self.assertEqual(original, self.fixture['tables'])
        self.assertTrue(first['rows'])
        json.dumps(first, allow_nan=False)

    def sample(self):
        self.data['shipments'] = [dict(product='甲', cohort=1, units=100, warranty_months=12, known_month=1)]
        first = dict(claim_id='c', product='甲', cohort=1, reported_month=2, status='open', status_at=2, known_month=2)
        self.data['cases'] = [first, {**first, 'status': 'closed', 'status_at': 3, 'known_month': 4}, {**first, 'status': 'reopened', 'status_at': 5, 'known_month': 6}]
        self.data['transactions'] = [dict(transaction='p', claim_id='c', signed_amount=200, effective_month=3, known_month=4), dict(transaction='r', claim_id='c', signed_amount=-50, effective_month=5, known_month=6)]

    def test_reopening_does_not_duplicate_exposure(self):
        self.sample()
        row = run(self.data, {'cutoff': 6, 'group': ''})['rows'][0]
        self.assertEqual((row['units'], row['reported'], row['open_cases']), (100, 1, 1))
        self.assertEqual(row['paid_net'], 150)

    def test_close_and_late_recovery_asof(self):
        self.sample()
        row = run(self.data, {'cutoff': 4, 'group': ''})['rows'][0]
        self.assertEqual(row['paid_net'], 200)
        self.assertEqual(row['open_cases'], 0)
        self.assertEqual(row['development_age'], 3)

    def test_report_before_cash_is_not_payment(self):
        self.sample()
        row = run(self.data, {'cutoff': 2, 'group': '甲'})['rows'][0]
        self.assertEqual(row['reported'], 1)
        self.assertEqual(row['paid_net'], 0)
        self.assertEqual(row['paid_per_unit'], 0)
