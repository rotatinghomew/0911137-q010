from collections import Counter, defaultdict
import math
from statistics import mean, median


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('数值必须为有限实数')
    return value


def selected(value, group):
    return not group or value == group


def latest(rows, key, revision, visible, cutoff):
    result = {}
    for row in rows:
        if row[visible] > cutoff:
            continue
        previous = result.get(row[key])
        if previous is None or row[revision] > previous[revision]:
            result[row[key]] = row
    return list(result.values())


def output(rows, summary, notes):
    return {'rows': rows, 'summary': summary, 'notes': notes}


def run(data, options):
    cutoff, group = options['cutoff'], options['group']
    shipments = [row for row in data['shipments'] if row['known_month'] <= cutoff and row['cohort'] <= cutoff and selected(row['product'], group)]
    cases = latest(data['cases'], 'claim_id', 'known_month', 'known_month', cutoff)
    lookup = {row['claim_id']: row for row in cases}
    totals = Counter()
    for transaction in data['transactions']:
        case = lookup.get(transaction['claim_id'])
        if case is not None and transaction['known_month'] <= cutoff and transaction['effective_month'] <= cutoff:
            totals[(case['product'], case['cohort'])] += number(transaction['signed_amount'])
    rows = []
    for shipment in shipments:
        related = [row for row in cases if row['product'] == shipment['product'] and row['cohort'] == shipment['cohort']]
        paid = totals[(shipment['product'], shipment['cohort'])]
        rows.append(dict(product=shipment['product'], cohort=shipment['cohort'], development_age=cutoff - shipment['cohort'], units=number(shipment['units']), reported=len(related), open_cases=sum(row['status'] != 'closed' for row in related), paid_net=round(paid, 2), paid_per_unit=paid / shipment['units'] if shipment['units'] else None))
    return output(rows, {'出货暴露': sum(row['units'] for row in rows), '已知案件': sum(row['reported'] for row in rows), '净支付': round(sum(row['paid_net'] for row in rows), 2)}, ['当前已付金额不是最终费用或准备金。'])
