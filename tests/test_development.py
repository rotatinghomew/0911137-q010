import copy
import json
import unittest

from claims_development.application import ROOT
from claims_development.domain import MIN_IBNR_TAIL, backtest, run


def base_tables(months=30):
    """两个产品线、多个批次，每批 3 件报案，结案与重开按固定模式出现。"""
    shipments = []
    cases = []
    transactions = []
    for product, offset in [('甲', 0), ('乙', 1)]:
        for cohort in range(1, months + 1):
            shipments.append(dict(product=product, cohort=cohort, units=100,
                                  warranty_months=12, known_month=cohort))
            for index in range(3):
                claim = f'{product}-{cohort}-{index}'
                report = cohort + 2 + index
                cases.append(dict(claim_id=claim, product=product, cohort=cohort,
                                  reported_month=report, status='open',
                                  status_at=report, known_month=report))
                cases.append(dict(claim_id=claim, product=product, cohort=cohort,
                                  reported_month=report, status='closed',
                                  status_at=report + 2, known_month=report + 2))
                transactions.append(dict(transaction=claim + '-p', claim_id=claim,
                                         signed_amount=100 + offset,
                                         effective_month=report + 1,
                                         known_month=report + 2))
                if index == 0:
                    cases.append(dict(claim_id=claim, product=product, cohort=cohort,
                                      reported_month=report, status='reopened',
                                      status_at=report + 5, known_month=report + 6))
                    cases.append(dict(claim_id=claim, product=product, cohort=cohort,
                                      reported_month=report, status='closed',
                                      status_at=report + 8, known_month=report + 8))
                    transactions.append(dict(transaction=claim + '-p2', claim_id=claim,
                                             signed_amount=20,
                                             effective_month=report + 5,
                                             known_month=report + 6))
                    transactions.append(dict(transaction=claim + '-r', claim_id=claim,
                                             signed_amount=-5, effective_month=report + 6,
                                             known_month=report + 6))
    return dict(shipments=shipments, cases=cases, transactions=transactions, policies=[])


class DevelopmentTests(unittest.TestCase):
    def test_unreported_cases_are_not_zero_at_early_cutoff(self):
        tables = base_tables()
        early = run(tables, {'cutoff': 18, 'group': '甲'})
        row = next(item for item in early['rows'] if item['cohort'] == 18)
        # 至少要让参照曲线在本例中提供非零的尚未报案厚度。
        self.assertGreater(row['ibnr_cases'], 0)
        self.assertGreater(row['reserve'], 0)
        self.assertGreater(row['ultimate_net'], row['paid_net'])

    def test_late_report_uses_business_month_not_visibility(self):
        tables = base_tables()
        late = dict(claim_id='late', product='甲', cohort=1, status='open',
                    reported_month=25, status_at=25, known_month=25)
        tables['cases'].append(late)
        tables['transactions'].append(dict(transaction='late-p', claim_id='late',
                                           signed_amount=100, effective_month=26,
                                           known_month=26))
        before = run(tables, {'cutoff': 24, 'group': '甲'})
        after = run(tables, {'cutoff': 26, 'group': '甲'})
        before_row = next(item for item in before['rows'] if item['cohort'] == 1)
        after_row = next(item for item in after['rows'] if item['cohort'] == 1)
        # 关账 24 时这条业务月 25 的报案还不可见；到 26 才进入案件数与支付。
        self.assertEqual(before_row['reported'], 3)
        self.assertEqual(after_row['reported'], 4)
        self.assertEqual(after_row['paid_net'] - before_row['paid_net'], 100)
        self.assertLess(before['provenance']['visible_claims'], after['provenance']['visible_claims'])

    def test_reopening_is_same_case_not_extra_exposure(self):
        tables = base_tables()
        result = run(tables, {'cutoff': 30, 'group': '甲'})
        first = next(item for item in result['rows'] if item['cohort'] == 1)
        self.assertEqual(first['units'], 100)
        self.assertEqual(first['reported'], 3)
        self.assertEqual(first['reopened_cases'], 1)
        self.assertEqual(first['reopen_gross'], 0)

    def test_late_recovery_reduces_net_but_never_case_count(self):
        tables = base_tables()
        tables['transactions'].append(dict(transaction='late-r', claim_id='甲-1-0',
                                           signed_amount=-200, effective_month=20,
                                           known_month=20))
        result = run(tables, {'cutoff': 30, 'group': '甲'})
        row = next(item for item in result['rows'] if item['cohort'] == 1)
        self.assertEqual(row['reported'], 3)
        self.assertEqual(row['recovered'], 205)
        self.assertEqual(row['reserve'], 0)
        self.assertEqual(row['reserve_raw'], 0)

    def test_unusual_recovery_reduces_net_and_never_creates_reserve(self):
        tables = base_tables()
        tables['transactions'].append(dict(transaction='big-r', claim_id='甲-3-0',
                                           signed_amount=-100000, effective_month=25,
                                           known_month=25))
        result = run(tables, {'cutoff': 30, 'group': '甲'})
        row = next(item for item in result['rows'] if item['cohort'] == 3)
        # 异常追回不制造负准备金，也不重复计为未来费用；它直接压低净费用。
        self.assertEqual(row['reserve'], 0)
        self.assertEqual(row['reserve_raw'], 0)
        self.assertLess(row['paid_net'], 0)
        self.assertEqual(row['ultimate_net'], row['paid_net'])

    def test_future_recovery_exceeding_future_payment_keeps_signed_raw(self):
        tables = base_tables()
        adjusted = []
        for row in tables['transactions']:
            if row['transaction'].endswith('-p2'):
                adjusted.append({**row, 'signed_amount': 5})
            elif row['transaction'].endswith('-r'):
                adjusted.append({**row, 'signed_amount': -300})
            else:
                adjusted.append(row)
        tables['transactions'] = adjusted
        result = run(tables, {'cutoff': 18, 'group': '甲'})
        estimated = [row for row in result['rows'] if row['status'] == 'estimated']
        self.assertTrue(any(row['reserve_raw'] is not None and row['reserve_raw'] < 0
                            for row in estimated))
        self.assertTrue(all(row['reserve'] >= 0 for row in estimated))

    def test_new_product_without_reference_stays_indeterminate(self):
        tables = base_tables()
        tables['shipments'].append(dict(product='丙', cohort=20, units=50,
                                        warranty_months=12, known_month=20))
        tables['cases'].append(dict(claim_id='丙-1', product='丙', cohort=20,
                                    reported_month=22, status='open',
                                    status_at=22, known_month=22))
        tables['transactions'].append(dict(transaction='丙-1-p', claim_id='丙-1',
                                           signed_amount=80, effective_month=23,
                                           known_month=23))
        result = run(tables, {'cutoff': 24, 'group': ''})
        row = next(item for item in result['rows'] if item['product'] == '丙')
        self.assertEqual(row['status'], 'indeterminate')
        self.assertIsNone(row['reserve'])
        self.assertIsNone(row['ultimate_net'])
        self.assertEqual(row['comparability'], 'no_reference')
        self.assertEqual(row['paid_net'], 80)
        summary = result['summary']
        self.assertEqual(summary['未估计批次净支付'], 80)
        self.assertNotIn(80, [summary['已估计最终净费用']])
        self.assertAlmostEqual(summary['已估计准备金'], sum(
            item['reserve'] for item in result['rows'] if item['status'] == 'estimated'), places=2)

    def test_policy_change_reports_tempo_separately_from_final_cost(self):
        tables = base_tables()
        tables['policies'] = [
            dict(product='甲', version='v1', start_month=1, end_month=15),
            dict(product='甲', version='v2', start_month=15, end_month=None),
        ]
        # v2 把结案时点整体推迟 12 个月，每案金额不变。
        adjusted = []
        for row in tables['cases']:
            if row['product'] == '甲' and row['cohort'] >= 15 and row['status'] != 'open':
                row = {**row, 'status_at': row['status_at'] + 12,
                       'known_month': row['known_month'] + 12}
            adjusted.append(row)
        tables['cases'] = adjusted
        result = run(tables, {'cutoff': 40, 'group': '甲'})
        change = result['diagnostics']['policy_changes'][0]
        self.assertEqual(change['classification'], 'tempo')
        self.assertEqual(change['cost_ratio'], 1.0)
        self.assertGreater(change['tempo_ratio'], 1.0)

    def test_policy_change_is_indeterminate_before_batches_mature(self):
        tables = base_tables()
        tables['policies'] = [
            dict(product='甲', version='v1', start_month=1, end_month=15),
            dict(product='甲', version='v2', start_month=15, end_month=None),
        ]
        result = run(tables, {'cutoff': 20, 'group': '甲'})
        v2 = next(row for row in result['diagnostics']['regimes']
                  if row['policy_version'] == 'v2')
        self.assertEqual(v2['maturity'], 'immature')
        change = result['diagnostics']['policy_changes'][0]
        self.assertEqual(change['classification'], 'indeterminate')
        self.assertIsNone(change['cost_ratio'])

    def test_policy_split_keeps_separate_reference(self):
        tables = base_tables()
        tables['policies'] = [
            dict(product='甲', version='v1', start_month=1, end_month=15),
            dict(product='甲', version='v2', start_month=15, end_month=None),
        ]
        result = run(tables, {'cutoff': 40, 'group': '甲'})
        versions = {row['policy_version'] for row in result['rows']}
        self.assertEqual(versions, {'v1', 'v2'})
        recent = next(row for row in result['rows'] if row['cohort'] >= 15)
        self.assertIn(recent['comparability'], {'comparable', 'thin_same_policy'})

    def test_settled_batch_closes_ibnr(self):
        tables = base_tables()
        result = run(tables, {'cutoff': 40, 'group': '甲'})
        settled = [row for row in result['rows'] if row['status'] == 'settled']
        self.assertTrue(settled)
        for row in settled:
            self.assertEqual(row['ibnr_cases'], 0)
            self.assertEqual(row['reserve'], 0)
            self.assertEqual(row['ultimate_net'], row['paid_net'])

    def test_backtest_does_not_leak_future_accounting(self):
        tables = base_tables()
        as_of = run(tables, {'cutoff': 20, 'group': '甲'})
        report = backtest(tables, {'cutoff': 20, 'evaluate': 40, 'group': '甲'})
        self.assertEqual(report['provenance']['as_of_visible']['transactions'],
                         as_of['provenance']['visible_transactions'])
        self.assertGreater(report['provenance']['evaluate_visible']['transactions'],
                           report['provenance']['as_of_visible']['transactions'])
        for record in report['rows']:
            self.assertAlmostEqual(
                record['estimated_ultimate'] - record['paid_net_as_of'],
                record['estimated_reserve'], places=2)

    def test_ranges_are_compatible_and_ordered(self):
        tables = base_tables()
        result = run(tables, {'cutoff': 20, 'group': ''})
        total = result['ranges']['total']
        self.assertLessEqual(total['joint_low'], total['central'])
        self.assertLessEqual(total['central'], total['joint_high'])
        self.assertLessEqual(total['additive_low'], total['joint_low'])
        self.assertLessEqual(total['joint_high'], total['additive_high'])

    def test_policy_effective_from_policy_start(self):
        tables = base_tables()
        tables['policies'] = [dict(product='甲', version='v1', start_month=10, end_month=None)]
        result = run(tables, {'cutoff': 20, 'group': '甲'})
        early = next(row for row in result['rows'] if row['cohort'] < 10)
        self.assertEqual(early['policy_version'], '未覆盖')

    def test_fixture_is_deterministic_and_does_not_leak_nan(self):
        fixture = json.loads((ROOT / 'fixtures/data.json').read_text())
        original = copy.deepcopy(fixture['tables'])
        first = run(fixture['tables'], {'cutoff': 24, 'group': ''})
        second = run(fixture['tables'], {'cutoff': 24, 'group': ''})
        self.assertEqual(first, second)
        self.assertEqual(original, fixture['tables'])
        json.dumps(first, allow_nan=False, ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()
