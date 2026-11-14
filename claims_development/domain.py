"""质保费用发展：批次最终净费用与剩余准备金。

口径要点：

- 所有时点均为业务月。``known_month`` 是信息何时对分析可见，``status_at`` /
  ``effective_month`` 是业务发生时点。某个关账时点只使用两列都不晚于该时点的记录。
- 案件以 ``claim_id`` 去重；``reopened`` 是同一案件的状态变化，不是新案件。报告晚到
  用 ``reported_month`` 判断，不用记录进入可见层的月份来替代。
- ``signed_amount`` 为正表示支付、为负表示追回。毛支付、追回与净额分列；追回不冲掉
  案件计数，剩余费用可以为负，但账务准备金不取负值。
- 准备金分解为：已报案未结案件的剩余费用、尚未报案案件（IBNR）、结案后重开以及预计
  未来追回。没有合格成熟参照的批次保持 ``None``，不以零费用代替。
- 成熟参照按服务政策版本分层；版本样本不足时退回产品线并标记 ``policy_truncated``，
  同时用发展节奏与单位成本分开诊断，避免把政策造成的处理速度变化误当最终成本变化。
"""

from collections import defaultdict
import math
from statistics import mean, stdev


SCENARIOS = ('favorable', 'central', 'prudent', 'conservative')
MIN_POOL_BATCHES = 2
MIN_POOL_CLAIMS = 5
MIN_IBNR_TAIL = 0.02
REOPEN_PRIOR_STRENGTH = 20.0
WITHIN_LINE_CORRELATION = 0.5
ACROSS_LINE_CORRELATION = 0.25
TEMPO_TOLERANCE = 0.10


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('数值必须为有限实数')
    return float(value)


def month_value(value):
    result = number(value)
    if result != int(result):
        raise ValueError('业务时点必须为整数月')
    return int(result)


def policy_at(policies, product, at):
    candidates = [
        row for row in policies
        if row['product'] == product
        and row['start_month'] <= at
        and (row.get('end_month') is None or at < row['end_month'])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row['start_month'], str(row.get('version', ''))))


def _quantile(values, q):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _mean(values, default=0.0):
    return mean(values) if values else default


def _stdev(values, default=0.0):
    return stdev(values) if len(values) > 1 else default


def _r2(value):
    return None if value is None else round(value, 2)


def _r4(value):
    return None if value is None else round(value, 4)


def _field(row, name):
    if not isinstance(row, dict) or name not in row:
        raise ValueError(f'资料缺少字段 {name}')
    return row[name]


def load_context(tables, cutoff, group):
    """读取某个关账时点可见的批次暴露、案件状态、账务流水与服务政策。"""
    cutoff = month_value(cutoff)
    shipments = []
    for row in tables.get('shipments', []):
        product = _field(row, 'product')
        cohort = month_value(_field(row, 'cohort'))
        known = month_value(_field(row, 'known_month'))
        units = number(_field(row, 'units'))
        warranty = month_value(_field(row, 'warranty_months'))
        if units < 0 or warranty < 0:
            raise ValueError('出货暴露与质保期不能为负')
        if known <= cutoff and cohort <= cutoff and (not group or product == group):
            shipments.append(dict(product=product, cohort=cohort, units=units,
                                  warranty_months=warranty, known_month=known))
    keys = {(row['product'], row['cohort']) for row in shipments}
    if len(keys) != len(shipments):
        raise ValueError('同一批次存在重复出货记录')

    policies = []
    for row in tables.get('policies', []):
        start = month_value(_field(row, 'start_month'))
        end = row.get('end_month')
        end = None if end is None else month_value(end)
        if start <= cutoff:
            policies.append(dict(product=_field(row, 'product'), version=str(_field(row, 'version')),
                                 start_month=start, end_month=end))

    def regime(product, cohort):
        return (policy_at(policies, product, cohort) or {}).get('version', '未覆盖')

    case_rows = defaultdict(list)
    skipped_cases = 0
    for index, row in enumerate(tables.get('cases', [])):
        claim_id = _field(row, 'claim_id')
        product = _field(row, 'product')
        cohort = month_value(_field(row, 'cohort'))
        reported = month_value(_field(row, 'reported_month'))
        status = _field(row, 'status')
        status_at = month_value(_field(row, 'status_at'))
        known = month_value(_field(row, 'known_month'))
        if status not in {'open', 'closed', 'reopened'}:
            raise ValueError('未知案件状态')
        if reported > cutoff or known > cutoff or status_at > cutoff or (product, cohort) not in keys:
            skipped_cases += 1
            continue
        case_rows[claim_id].append((status_at, known, index, row))

    states = {}
    for claim_id, rows in case_rows.items():
        rows.sort(key=lambda item: (item[0], item[1], item[2]))
        batches = {(row['product'], month_value(row['cohort'])) for _, _, _, row in rows}
        if len(batches) > 1:
            raise ValueError('同一案件对应多个批次')
        last = rows[-1][3]
        closed_at = min((item[3]['status_at'] for item in rows if item[3]['status'] == 'closed'), default=None)
        reopened_at = min((item[3]['status_at'] for item in rows if item[3]['status'] == 'reopened'), default=None)
        ever_reopened = reopened_at is not None
        status = last['status']
        if status == 'closed':
            state = 'closed_reopened' if ever_reopened else 'closed'
        else:
            state = 'open_reopened' if ever_reopened else 'open'
        states[claim_id] = dict(
            claim_id=claim_id,
            product=last['product'],
            cohort=month_value(last['cohort']),
            reported_month=min(item[3]['reported_month'] for item in rows),
            closed_at=closed_at,
            reopened_at=reopened_at,
            ever_reopened=ever_reopened,
            status=status,
            state=state,
            rows=rows,
            transactions=[],
            gross=0.0, gross_pre=0.0, gross_post=0.0,
            recovered=0.0, recovery_pre=0.0, recovery_post=0.0,
            net=0.0,
        )

    transactions = []
    skipped_transactions = 0
    for row in tables.get('transactions', []):
        claim_id = _field(row, 'claim_id')
        amount = number(_field(row, 'signed_amount'))
        effective = month_value(_field(row, 'effective_month'))
        known = month_value(_field(row, 'known_month'))
        claim = states.get(claim_id)
        if claim is None or known > cutoff or effective > cutoff:
            skipped_transactions += 1
            continue
        item = dict(transaction=row.get('transaction'), claim_id=claim_id,
                    amount=amount, effective_month=effective, known_month=known)
        transactions.append(item)
        claim['transactions'].append(item)
        if amount > 0:
            claim['gross'] += amount
            if claim['closed_at'] is not None and effective > claim['closed_at']:
                claim['gross_post'] += amount
            else:
                claim['gross_pre'] += amount
        elif amount < 0:
            recovery = -amount
            claim['recovered'] += recovery
            if claim['closed_at'] is not None and effective > claim['closed_at']:
                claim['recovery_post'] += recovery
            else:
                claim['recovery_pre'] += recovery
        claim['net'] += amount

    by_batch = defaultdict(list)
    for claim in states.values():
        by_batch[(claim['product'], claim['cohort'])].append(claim)

    # 已观测到的最晚发展年龄只用于校验“完成发展”的年龄门槛，不参与金额估计。
    event_ages = []
    report_lags = []
    reopen_delays = []
    for claim in states.values():
        report_lag = claim['reported_month'] - claim['cohort']
        event_ages.append(report_lag)
        report_lags.append(report_lag)
        if claim['closed_at'] is not None:
            event_ages.append(claim['closed_at'] - claim['cohort'])
        if claim['reopened_at'] is not None:
            event_ages.append(claim['reopened_at'] - claim['cohort'])
            if claim['closed_at'] is not None:
                reopen_delays.append(claim['reopened_at'] - claim['closed_at'])
        for transaction in claim['transactions']:
            event_ages.append(transaction['effective_month'] - claim['cohort'])
    developed_age = max(0, max(event_ages)) if event_ages else 0
    reopen_horizon = max(0, max(reopen_delays)) if reopen_delays else 0
    max_report_age = max(report_lags) if report_lags else 0
    report_tail = max(3, max_report_age + 1)

    return dict(
        cutoff=cutoff,
        group=group,
        shipments=shipments,
        keys=keys,
        policies=policies,
        states=states,
        by_batch=by_batch,
        transactions=transactions,
        skipped_cases=skipped_cases,
        skipped_transactions=skipped_transactions,
        developed_age=developed_age,
        report_tail=report_tail,
        max_report_age=max_report_age,
        reopen_horizon=reopen_horizon,
        regime=regime,
    )


def batch_metrics(context):
    metrics = {}
    for shipment in context['shipments']:
        key = (shipment['product'], shipment['cohort'])
        claims = context['by_batch'].get(key, [])
        age = context['cutoff'] - shipment['cohort']
        # 质保期结束后不再有新受保事故；已观测到的最晚发展年龄再留一个月余量，
        # 用于确认报案、结案、尾款和重开都已完整出现。越过该年龄视为“统计上
        # 成熟”，但若仍有未结案件，则仍要估计其剩余费用，不能把账上案件当作
        # 零费用；只有成熟且没有未结案件，才把准备金记为零。
        threshold = max(shipment['warranty_months'], context['developed_age'] + 1)
        mature = age >= threshold
        open_count = sum(1 for claim in claims if claim['state'] in {'open', 'open_reopened'})
        metrics[key] = dict(
            shipment=shipment,
            claims=claims,
            age=age,
            threshold=threshold,
            mature=mature,
            settled=mature and open_count == 0,
            reported=len(claims),
            open=open_count,
            open_first=sum(1 for claim in claims if claim['state'] == 'open'),
            open_reopened=sum(1 for claim in claims if claim['state'] == 'open_reopened'),
            closed=sum(1 for claim in claims if claim['state'] in {'closed', 'closed_reopened'}),
            reopened=sum(1 for claim in claims if claim['ever_reopened']),
            gross=sum(claim['gross'] for claim in claims),
            recovered=sum(claim['recovered'] for claim in claims),
            net=sum(claim['net'] for claim in claims),
            gross_pre=sum(claim['gross_pre'] for claim in claims),
            gross_post=sum(claim['gross_post'] for claim in claims),
            recovery_pre=sum(claim['recovery_pre'] for claim in claims),
            recovery_post=sum(claim['recovery_post'] for claim in claims),
        )
    return metrics


def _batch_rates(metrics, keys):
    rates = []
    for key in keys:
        units = metrics[key]['shipment']['units']
        if units:
            rates.append(metrics[key]['reported'] / units)
    return rates


def build_pool(context, metrics, keys):
    keys = list(keys)
    if not keys:
        return None
    units = sum(metrics[key]['shipment']['units'] for key in keys)
    claims = [claim for key in keys for claim in metrics[key]['claims']]
    if not units or not claims:
        return None
    frequency = len(claims) / units
    rates = _batch_rates(metrics, keys)
    if len(rates) > 1:
        se_frequency = stdev(rates) / math.sqrt(len(rates))
    else:
        se_frequency = math.sqrt(max(frequency, 0.0)) / units

    pre = [claim['gross_pre'] for claim in claims]
    late = [claim['gross_post'] for claim in claims
            if claim['state'] == 'closed' and not claim['ever_reopened']]
    reopened = [claim for claim in claims if claim['ever_reopened']]
    extra_gross = [claim['gross_post'] for claim in reopened]
    extra_recovery = [claim['recovery_post'] for claim in reopened]

    closed_claims = [claim for claim in claims if claim['closed_at'] is not None]
    horizon = context['reopen_horizon']
    curve = {}
    for lag in range(1, horizon + 1):
        denominator = [claim for claim in closed_claims if claim['closed_at'] <= context['cutoff'] - lag]
        numerator = [claim for claim in denominator
                     if claim['reopened_at'] is not None and claim['reopened_at'] <= claim['closed_at'] + lag]
        curve[lag] = (len(numerator) / len(denominator)) if denominator else None
    eligible = [claim for claim in closed_claims if claim['closed_at'] + horizon <= context['cutoff']]
    reopen_rate = (sum(1 for claim in eligible if claim['ever_reopened']) / len(eligible)) if eligible else 0.0
    se_reopen = math.sqrt(reopen_rate * (1 - reopen_rate) / len(eligible)) if eligible else 0.0

    # 结案后尾款也有自己的发展窗口；超过该窗口的已结案件不再重复预留尾款。
    late_lags = [transaction['effective_month'] - claim['closed_at']
                 for claim in claims if claim['closed_at'] is not None
                 for transaction in claim['transactions']
                 if transaction['amount'] > 0 and transaction['effective_month'] > claim['closed_at']]
    late_tail = max(late_lags) if late_lags else 0

    # 报案滞后曲线取参照池自身。政策改版若改变处理速度，参照池的曲线会随版本
    # 分开，而不是用全组合曲线替新政策假设旧节奏。
    report_lags = [claim['reported_month'] - claim['cohort'] for claim in claims]
    max_lag = max(report_lags) if report_lags else 0
    report_curve = {}
    for age in range(0, max_lag + 1):
        report_curve[age] = (sum(1 for lag in report_lags if lag <= age) / len(report_lags)
                             if report_lags else 0.0)

    return dict(
        keys=keys,
        batches=len(keys),
        units=units,
        claims=len(claims),
        frequency=frequency,
        se_frequency=se_frequency,
        severity_pre=_mean(pre),
        severity_sd=_stdev(pre),
        pre_values=pre,
        late_gross=_mean(late),
        late_samples=len(late),
        late_tail=late_tail,
        reopen_extra_gross=_mean(extra_gross),
        reopen_extra_recovery=_mean(extra_recovery),
        reopen_samples=len(extra_gross),
        recovery_mean=_mean([claim['recovered'] for claim in claims]),
        recovery_samples=len(claims),
        reopen_rate=reopen_rate,
        se_reopen=se_reopen,
        eligible_closed=len(eligible),
        closed_claims=len(closed_claims),
        reopened_claims=len(reopened),
        horizon=horizon,
        curve=curve,
        report_curve=report_curve,
        max_report_lag=max_lag,
        closed_lag=_mean([claim['closed_at'] - claim['reported_month'] for claim in closed_claims]),
        report_lag=_mean(report_lags),
    )


def apply_prior(pool, prior, strength=REOPEN_PRIOR_STRENGTH):
    """把同产品线其他政策版本的结案后重开率作为先验，缓解新政策零重开样本。"""
    if pool is None or prior is None:
        return pool
    copies = dict(pool)
    n = pool['eligible_closed']
    if n:
        blended = (pool['reopen_rate'] * n + prior['reopen_rate'] * strength) / (n + strength)
        se = math.sqrt(blended * (1 - blended) / (n + strength))
        copies['reopen_rate'] = blended
        copies['se_reopen'] = se
        copies['reopen_prior'] = prior['reopen_rate']
        copies['reopen_prior_strength'] = strength
    if not pool['reopen_samples'] and prior['reopen_samples']:
        copies['reopen_extra_gross'] = prior['reopen_extra_gross']
        copies['reopen_extra_recovery'] = prior['reopen_extra_recovery']
        copies['reopen_samples'] = prior['reopen_samples']
        copies['reopen_extra_source'] = 'product_prior'
    if not pool['late_samples'] and prior['late_samples']:
        copies['late_gross'] = prior['late_gross']
        copies['late_samples'] = prior['late_samples']
        copies['late_tail'] = prior['late_tail']
    if not pool['curve'] or all(value is None for value in pool['curve'].values()):
        copies['curve'] = prior['curve']
    if not pool['report_curve']:
        # 参照池自身没有已结案件时，用同产品线曲线补齐报案节奏；这保留政策
        # 版本的严重度与频率，而不是改用全组合的成本结构。
        copies['report_curve'] = prior['report_curve']
    return copies


def choose_reference(context, metrics, exact_pools, product, regime):
    same = exact_pools.get((product, regime))
    if same and same['batches'] >= MIN_POOL_BATCHES and same['claims'] >= MIN_POOL_CLAIMS:
        return same, 'same_policy', 'comparable', f'{product}/{regime}'
    product_keys = [key for key in metrics if key[0] == product and metrics[key]['mature']]
    if not product_keys:
        # 本产品线没有任何成熟批次：不能拿其他产品线的成本结构冒充它的参照。
        return None, 'none', 'no_reference', '-'
    product_pool = build_pool(context, metrics, product_keys)
    if product_pool and product_pool['claims'] >= MIN_POOL_CLAIMS:
        mixed_regimes = sorted({context['regime'](key[0], key[1]) for key in product_keys})
        # 只要参照池里没有与当前批次相同的政策版本，就是跨版本参照；不能因为池
        # 内部版本一致就误称“同政策但样本薄”。
        truncated = regime not in mixed_regimes or len(mixed_regimes) > 1
        comparability = 'policy_truncated' if truncated else 'thin_same_policy'
        return product_pool, 'product_all_policies', comparability, f'{product}/*'
    portfolio_keys = [key for key in metrics if metrics[key]['mature']]
    portfolio = build_pool(context, metrics, portfolio_keys)
    if portfolio and portfolio['claims'] >= MIN_POOL_CLAIMS:
        return portfolio, 'portfolio', 'portfolio_proxy', '*/*'
    return None, 'none', 'no_reference', '-'


def _scenario_parameters(pool, scenario):
    central = dict(
        frequency=pool['frequency'],
        severity=pool['severity_pre'],
        reopen=pool['reopen_rate'],
        late=pool['late_gross'],
        extra_gross=pool['reopen_extra_gross'],
        extra_recovery=pool['reopen_extra_recovery'],
    )
    if scenario == 'central':
        return central
    if scenario == 'favorable':
        return dict(
            frequency=max(0.0, pool['frequency'] - pool['se_frequency']),
            severity=_quantile(pool['pre_values'], 0.25),
            reopen=max(0.0, pool['reopen_rate'] - pool['se_reopen']),
            late=pool['late_gross'],
            extra_gross=pool['reopen_extra_gross'],
            extra_recovery=pool['reopen_extra_recovery'],
        )
    if scenario == 'prudent':
        return dict(
            frequency=pool['frequency'] + pool['se_frequency'],
            severity=_quantile(pool['pre_values'], 0.75),
            reopen=min(1.0, pool['reopen_rate'] + pool['se_reopen']),
            late=pool['late_gross'],
            extra_gross=pool['reopen_extra_gross'],
            extra_recovery=pool['reopen_extra_recovery'] * 0.5,
        )
    if scenario == 'conservative':
        return dict(
            frequency=pool['frequency'] + 2 * pool['se_frequency'],
            severity=_quantile(pool['pre_values'], 0.90),
            reopen=min(1.0, pool['reopen_rate'] + 2 * pool['se_reopen']),
            late=pool['late_gross'],
            extra_gross=pool['reopen_extra_gross'],
            extra_recovery=0.0,
        )
    raise ValueError('未知准备金情景')


def _future_reopen_probability(pool, closed_age, factor):
    horizon = pool['horizon']
    if horizon <= 0 or closed_age >= horizon:
        return 0.0
    at_horizon = pool['curve'].get(horizon) or 0.0
    at_age = pool['curve'].get(max(0, min(closed_age, horizon))) or 0.0
    if at_age >= 1:
        return 0.0
    conditional = max(0.0, (at_horizon - at_age) / (1 - at_age))
    return min(1.0, conditional * factor)


def _pool_completion(pool, age, complete):
    """按参照池已观测的报案滞后分布，估计截至该发展年龄已报案的比例。

    对尚未完成发展的批次，已报案比例只取到 ``1 - MIN_IBNR_TAIL``；否则当观测
    曲线恰好在此年龄达到 1.0 时，零报案批次会被误判为“永远不会再有报告”，
    再次把未发生的报告当成零费用。完整发展后的批次直接使用 1.0。
    """
    if complete:
        return 1.0
    curve = pool.get('report_curve') or {}
    if not curve:
        return 0.0
    observed = curve[max(0, min(age, max(curve)))]
    return min(observed, 1.0 - MIN_IBNR_TAIL)


def estimate_batch(context, metrics, shipment, pool, scenario):
    params = _scenario_parameters(pool, scenario)
    key = (shipment['product'], shipment['cohort'])
    item = metrics[key]
    units = shipment['units']
    reported = item['reported']
    age = item['age']
    completion = _pool_completion(pool, age, item['mature'])

    # 尚未报案案件用 Bornhuetter-Ferguson 口径：先按暴露与成熟参照频率估计最终
    # 案件数，再乘以尚未报案比例。已报案数不当作上界，未成熟批次不会因为“还没
    # 有报告”而得到零 IBNR；已完整报案的批次也不会被强行加出额外案件。
    ibnr_cases = max(0.0, params['frequency'] * units * (1.0 - completion))
    expected_ultimate_cases = reported + ibnr_cases

    expected_case_cost = (params['severity'] + params['late']
                          + params['reopen'] * params['extra_gross'])
    expected_case_recovery = params['reopen'] * params['extra_recovery']
    ibnr_gross = ibnr_cases * expected_case_cost
    ibnr_recovery = ibnr_cases * expected_case_recovery
    open_gross = open_recovery = 0.0
    reopen_gross = reopen_recovery = 0.0
    expected_reopen_cases = 0.0

    for claim in item['claims']:
        if claim['state'] == 'open':
            open_gross += max(0.0, params['severity'] + params['late'] - claim['gross_pre'])
            open_gross += params['reopen'] * params['extra_gross']
            open_recovery += max(0.0, params['reopen'] * params['extra_recovery'] - claim['recovery_pre'])
        elif claim['state'] == 'open_reopened':
            open_gross += max(0.0, params['extra_gross'] - claim['gross_post'])
            open_recovery += max(0.0, params['extra_recovery'] - claim['recovery_post'])
        elif claim['state'] == 'closed':
            reopen_factor = (params['reopen'] / pool['reopen_rate']) if pool['reopen_rate'] else 1.0
            closed_age = context['cutoff'] - claim['closed_at'] if claim['closed_at'] is not None else pool['horizon']
            probability = _future_reopen_probability(pool, closed_age, reopen_factor)
            expected_reopen_cases += probability
            # 结案后尾款只在参照池观测到的最长结案后窗口内预留；超出窗口的
            # 已结案件视为不再产生尾款，避免每个老批次都被重复加一笔。
            late = params['late'] if closed_age <= pool['late_tail'] else 0.0
            reopen_gross += late + probability * params['extra_gross']
            reopen_recovery += probability * params['extra_recovery']

    gross = ibnr_gross + open_gross + reopen_gross
    recovery = ibnr_recovery + open_recovery + reopen_recovery
    # 已入账追回不冲掉案件计数，也不重复预留：剩余费用只针对未来的期望支付与
    # 未来期望追回，已发生净额留在 paid_net。reserve_raw 只有在未来期望追回超过
    # 未来期望支付时才为负；账务准备金仍不取负值。
    raw = gross - recovery
    return dict(
        expected_cases=expected_ultimate_cases,
        ibnr_cases=ibnr_cases,
        completion=completion,
        expected_net_per_case=expected_case_cost - expected_case_recovery,
        expected_reopen_cases=expected_reopen_cases,
        ibnr_gross=ibnr_gross,
        open_gross=open_gross,
        reopen_gross=reopen_gross,
        gross=gross,
        recovery=recovery,
        raw=raw,
        reserve=max(0.0, raw),
        scenario=scenario,
        parameters=params,
    )


def _reference_note(comparability, regime):
    if comparability == 'comparable':
        return f'采用服务政策 {regime} 的成熟批次参照。'
    if comparability == 'thin_same_policy':
        return f'服务政策 {regime} 的成熟批次偏少，采用同产品线参照，未混合其他政策版本。'
    if comparability == 'policy_truncated':
        return (f'服务政策 {regime} 尚无足够成熟批次，参照池不含或混合了其他政策版本；'
                '发展节奏与最终成本不能完全分离，按 policy_truncated 标记。')
    if comparability == 'portfolio_proxy':
        return '产品线尚无足够成熟批次，采用全部产品线组合参照，按 portfolio_proxy 标记。'
    return '没有足够的成熟批次可作参照，准备金保持未知，不以零费用代替。'


def _policy_diagnostics(context, metrics):
    regime_rows = []
    products = sorted({shipment['product'] for shipment in context['shipments']})
    for product in products:
        versions = sorted({context['regime'](product, shipment['cohort'])
                           for shipment in context['shipments'] if shipment['product'] == product})
        for version in versions:
            keys = [key for key in metrics
                    if key[0] == product and context['regime'](key[0], key[1]) == version]
            claims = [claim for key in keys for claim in metrics[key]['claims']]
            mature_keys = [key for key in keys if metrics[key]['mature']]
            mature_claims = [claim for key in mature_keys for claim in metrics[key]['claims']]
            units = sum(metrics[key]['shipment']['units'] for key in keys)
            mature_units = sum(metrics[key]['shipment']['units'] for key in mature_keys)
            report_lags = [claim['reported_month'] - claim['cohort'] for claim in claims]
            close_lags = [claim['closed_at'] - claim['reported_month'] for claim in claims
                          if claim['closed_at'] is not None]
            reopen_delays = [claim['reopened_at'] - claim['closed_at'] for claim in claims
                             if claim['reopened_at'] is not None and claim['closed_at'] is not None]
            mature_frequency = (len(mature_claims) / mature_units) if mature_units else None
            mature_severity = _mean([claim['gross'] for claim in mature_claims]) if mature_claims else None
            mature_recovery = _mean([claim['recovered'] for claim in mature_claims]) if mature_claims else None
            cost_per_unit = ((mature_frequency * (mature_severity or 0)
                              - mature_frequency * (mature_recovery or 0))
                             if mature_units else None)
            regime_rows.append(dict(
                product=product,
                policy_version=version,
                visible_batches=len(keys),
                mature_batches=len(mature_keys),
                units=units,
                observed_claims=len(claims),
                observed_frequency=_r4((len(claims) / units) if units else None),
                mature_frequency=_r4(mature_frequency),
                mature_severity=_r2(mature_severity),
                mature_recovery=_r2(mature_recovery),
                cost_per_unit=_r2(cost_per_unit),
                mean_report_lag=_r4(_mean(report_lags)) if report_lags else None,
                mean_close_lag=_r4(_mean(close_lags)) if close_lags else None,
                mean_reopen_delay=_r4(_mean(reopen_delays)) if reopen_delays else None,
                reopened_claims=sum(1 for claim in claims if claim['ever_reopened']),
                maturity='mature' if mature_keys else 'immature',
            ))

    changes = []
    for product in products:
        ordered = [row for row in regime_rows if row['product'] == product]
        for previous, current in zip(ordered, ordered[1:]):
            if previous['maturity'] != 'mature' or current['maturity'] != 'mature':
                classification = 'indeterminate'
                note = f'{product} 的 {current["policy_version"]} 尚无足够成熟批次，无法分离节奏与最终成本。'
                tempo_ratio = cost_ratio = None
            else:
                # 处理节奏用报案滞后与结案滞后一起看；只看结案滞后会把“报案更晚、
                # 结案间隔不变”的政策变化误判为节奏不变。
                tempo_components = []
                for field in ('mean_report_lag', 'mean_close_lag'):
                    if previous[field] and current[field] is not None:
                        tempo_components.append(current[field] / previous[field])
                tempo_ratio = _mean(tempo_components, 1.0) if tempo_components else None
                cost_ratio = ((current['cost_per_unit'] / previous['cost_per_unit'])
                              if previous['cost_per_unit'] and current['cost_per_unit'] is not None else None)
                tempo_changed = tempo_ratio is not None and abs(math.log(max(tempo_ratio, 1e-9))) > TEMPO_TOLERANCE
                cost_changed = cost_ratio is not None and abs(math.log(max(cost_ratio, 1e-9))) > TEMPO_TOLERANCE
                if tempo_changed and not cost_changed:
                    classification, note = 'tempo', '处理速度变化为主，最终单位成本接近。'
                elif cost_changed and not tempo_changed:
                    classification, note = 'cost', '最终单位成本变化为主，处理速度接近。'
                elif tempo_changed and cost_changed:
                    classification, note = 'both', '处理速度与最终单位成本同时变化。'
                else:
                    classification, note = 'stable', '处理速度与最终单位成本都没有明显变化。'
            changes.append(dict(product=product, from_version=previous['policy_version'],
                                to_version=current['policy_version'], tempo_ratio=_r4(tempo_ratio),
                                cost_ratio=_r4(cost_ratio), classification=classification, note=note))
    return regime_rows, changes


def _aggregate_range(values, correlation):
    """把批次情景区间聚合到产品线：中心值相加，区间按相关假设计算联合半宽。"""
    central = sum(item['central'] for item in values)
    low_widths = [item['central'] - item['low'] for item in values]
    high_widths = [item['high'] - item['central'] for item in values]
    variance_low = sum(width ** 2 for width in low_widths)
    variance_high = sum(width ** 2 for width in high_widths)
    cross_low = sum(low_widths[i] * low_widths[j]
                    for i in range(len(low_widths)) for j in range(i + 1, len(low_widths)))
    cross_high = sum(high_widths[i] * high_widths[j]
                     for i in range(len(high_widths)) for j in range(i + 1, len(high_widths)))
    joint_low_width = math.sqrt(max(0.0, variance_low + 2 * correlation * cross_low))
    joint_high_width = math.sqrt(max(0.0, variance_high + 2 * correlation * cross_high))
    return dict(
        central=central,
        additive_low=max(0.0, central - sum(low_widths)),
        additive_high=central + sum(high_widths),
        joint_low=max(0.0, central - joint_low_width),
        joint_high=central + joint_high_width,
    )


def run(tables, options):
    cutoff, group = options['cutoff'], options.get('group', '')
    context = load_context(tables, cutoff, group)
    metrics = batch_metrics(context)

    # 参照池只取“成熟”批次：质保期已结束、且已越过已观测最晚发展年龄。成熟批次
    # 即使仍留有未结案件，也不影响它作为最终成本参照，其自身准备金另行估计。
    exact_keys = defaultdict(list)
    for key, item in metrics.items():
        if item['mature']:
            exact_keys[(key[0], context['regime'](key[0], key[1]))].append(key)
    build_pools = {name: build_pool(context, metrics, keys) for name, keys in exact_keys.items()}
    products = sorted({shipment['product'] for shipment in context['shipments']})
    product_prior = {}
    for product in products:
        keys = [key for key in metrics if key[0] == product and metrics[key]['mature']]
        product_prior[product] = build_pool(context, metrics, keys)

    rows = []
    for shipment in sorted(context['shipments'], key=lambda item: (item['product'], item['cohort'])):
        key = (shipment['product'], shipment['cohort'])
        item = metrics[key]
        regime = context['regime'](key[0], key[1])
        pool, reference, comparability, reference_pool = choose_reference(
            context, metrics, build_pools, key[0], regime)
        if pool is not None and comparability in {'comparable', 'thin_same_policy'}:
            pool = apply_prior(pool, product_prior.get(key[0]))
        row = dict(
            product=key[0], cohort=key[1], development_age=item['age'],
            units=shipment['units'], warranty_months=shipment['warranty_months'],
            maturity='mature' if item['mature'] else 'developing',
            policy_version=regime,
            reported=item['reported'], open_cases=item['open'],
            reopened_cases=item['reopened'], closed_cases=item['closed'],
            paid_gross=_r2(item['gross']), recovered=_r2(item['recovered']),
            paid_net=_r2(item['net']),
            paid_per_unit=_r4(item['net'] / shipment['units']) if shipment['units'] else None,
            reference=reference, reference_pool=reference_pool,
            comparability=comparability,
            reference_note=_reference_note(comparability, regime),
        )
        if item['settled']:
            row.update(
                status='settled',
                expected_cases=item['reported'], ibnr_cases=0.0,
                expected_reopen_cases=0.0,
                open_remaining_gross=0.0, ibnr_gross=0.0, reopen_gross=0.0,
                future_recovery=0.0, reserve_raw=0.0, reserve=0.0,
                reserve_favorable=0.0, reserve_prudent=0.0, reserve_conservative=0.0,
                ultimate_net=_r2(item['net']), ultimate_net_raw=_r2(item['net']),
                ultimate_per_unit=_r4(item['net'] / shipment['units']) if shipment['units'] else None,
                uncertainty_low=0.0, uncertainty_high=0.0,
            )
        elif pool is None:
            row.update(
                status='indeterminate',
                expected_cases=None, ibnr_cases=None, expected_reopen_cases=None,
                open_remaining_gross=None, ibnr_gross=None, reopen_gross=None,
                future_recovery=None, reserve_raw=None, reserve=None,
                reserve_favorable=None, reserve_prudent=None, reserve_conservative=None,
                ultimate_net=None, ultimate_net_raw=None, ultimate_per_unit=None,
                uncertainty_low=None, uncertainty_high=None,
            )
        else:
            estimates = {scenario: estimate_batch(context, metrics, shipment, pool, scenario)
                         for scenario in SCENARIOS}
            central = estimates['central']
            scenario_reserves = {scenario: estimates[scenario]['reserve'] for scenario in SCENARIOS}
            row.update(
                status='estimated',
                expected_cases=_r2(central['expected_cases']),
                ibnr_cases=_r2(central['ibnr_cases']),
                completion=_r4(central['completion']),
                expected_reopen_cases=_r2(central['expected_reopen_cases']),
                open_remaining_gross=_r2(central['open_gross']),
                ibnr_gross=_r2(central['ibnr_gross']),
                reopen_gross=_r2(central['reopen_gross']),
                future_recovery=_r2(central['recovery']),
                reserve_raw=_r2(central['raw']),
                reserve=_r2(central['reserve']),
                reserve_favorable=_r2(estimates['favorable']['reserve']),
                reserve_prudent=_r2(estimates['prudent']['reserve']),
                reserve_conservative=_r2(estimates['conservative']['reserve']),
                ultimate_net=_r2(item['net'] + central['reserve']),
                ultimate_net_raw=_r2(item['net'] + central['raw']),
                ultimate_per_unit=_r4((item['net'] + central['reserve']) / shipment['units']) if shipment['units'] else None,
                uncertainty_low=_r2(min(scenario_reserves.values())),
                uncertainty_high=_r2(max(scenario_reserves.values())),
            )
        rows.append(row)

    def scenario_field(scenario):
        return {'favorable': 'reserve_favorable', 'central': 'reserve',
                'prudent': 'reserve_prudent', 'conservative': 'reserve_conservative'}[scenario]

    scenarios = {}
    for scenario in SCENARIOS:
        field = scenario_field(scenario)
        by_line = {}
        for product in products:
            line_rows = [row for row in rows if row['product'] == product]
            paid = sum(row['paid_net'] for row in line_rows)
            reserve = sum(row[field] for row in line_rows if row['status'] == 'estimated')
            ultimate = sum((row['paid_net'] + row[field]) for row in line_rows if row['status'] == 'estimated')
            settled_paid = sum(row['paid_net'] for row in line_rows if row['status'] == 'settled')
            unestimated_paid = sum(row['paid_net'] for row in line_rows if row['status'] == 'indeterminate')
            by_line[product] = dict(
                paid_net=_r2(paid), reserve=_r2(reserve),
                ultimate_net=_r2(ultimate + settled_paid),
                unestimated_paid_net=_r2(unestimated_paid),
                has_unestimated=any(row['status'] == 'indeterminate' for row in line_rows),
            )
        scenarios[scenario] = dict(
            label={'favorable': '有利', 'central': '中心', 'prudent': '审慎', 'conservative': '保守'}[scenario],
            by_line=by_line,
            total=dict(
                paid_net=_r2(sum(item['paid_net'] for item in by_line.values())),
                reserve=_r2(sum(item['reserve'] for item in by_line.values())),
                ultimate_net=_r2(sum(item['ultimate_net'] for item in by_line.values())),
                unestimated_paid_net=_r2(sum(item['unestimated_paid_net'] for item in by_line.values())),
                has_unestimated=any(item['has_unestimated'] for item in by_line.values()),
            ),
        )

    line_ranges = {}
    for product in products:
        line_rows = [row for row in rows if row['product'] == product and row['status'] == 'estimated']
        line_ranges[product] = _aggregate_range(
            [dict(central=row['reserve'], low=row['uncertainty_low'], high=row['uncertainty_high'])
             for row in line_rows], WITHIN_LINE_CORRELATION)
        line_ranges[product]['has_unestimated'] = any(
            row['status'] == 'indeterminate' for row in rows if row['product'] == product)
        line_ranges[product]['estimated_batches'] = len(line_rows)
    total_range = _aggregate_range(
        [dict(central=line_ranges[product]['central'],
              low=line_ranges[product]['joint_low'],
              high=line_ranges[product]['joint_high'])
         for product in products], ACROSS_LINE_CORRELATION)
    total_range['additive_low'] = sum(line_ranges[product]['additive_low'] for product in products)
    total_range['additive_high'] = sum(line_ranges[product]['additive_high'] for product in products)
    total_range['has_unestimated'] = any(line_ranges[product]['has_unestimated'] for product in products)

    estimated = [row for row in rows if row['status'] == 'estimated']
    settled = [row for row in rows if row['status'] == 'settled']
    indeterminate = [row for row in rows if row['status'] == 'indeterminate']
    total_units = sum(row['units'] for row in rows)
    estimated_units = sum(row['units'] for row in estimated + settled)
    summary = {
        '出货暴露': total_units,
        '已知案件': sum(row['reported'] for row in rows),
        '净支付': _r2(sum(row['paid_net'] for row in rows)),
        '已估计准备金': _r2(sum(row['reserve'] for row in estimated)),
        '已估计最终净费用': _r2(sum(row['ultimate_net'] for row in estimated + settled)),
        '未估计批次净支付': _r2(sum(row['paid_net'] for row in indeterminate)),
        '估计覆盖暴露': estimated_units,
        '总暴露': total_units,
        '估计覆盖率': round(estimated_units / total_units, 4) if total_units else None,
    }

    notes = [
        '当前已付金额不是最终费用或准备金；最终净费用按批次剩余发展估计。',
        '准备金分解为已报案未结、尚未报案 IBNR、结案后重开和预计追回；重开按同一 claim_id 处理，不重复计算案件。',
        '追回以 signed_amount 负值入账；毛支付、追回和净额分列，reserve_raw 为有符号剩余费用，账簿准备金 reserve 不取负值。',
        '没有足够成熟参照的批次准备金保持未知（null），不以零费用代替；只有成熟且无未结案件的批次才把准备金记为零。',
        '批次年龄达到 max(质保月数, 已观测最晚发展年龄 + 1) 视为成熟；成熟批次仍可作参照，但存在未结案件时仍要估计。',
        f'联合风险范围把批次情景区间按相关假设聚合：同产品线相关 {WITHIN_LINE_CORRELATION}，'
        f'跨产品线相关 {ACROSS_LINE_CORRELATION}；additive 区间是完全相容的保守边界。',
    ]
    truncated = [row for row in rows if row['comparability'] == 'policy_truncated']
    if truncated:
        notes.append(f'{len(truncated)} 个批次的服务政策版本尚无足够成熟批次，参照池不含或混合了历史政策版本；'
                     '结果按 policy_truncated 标记，不能把政策处理速度变化当作最终成本变化。')
    proxies = [row for row in rows if row['comparability'] == 'portfolio_proxy']
    if proxies:
        notes.append(f'{len(proxies)} 个批次所在产品线没有足够成熟参照，采用组合参照，结果按 portfolio_proxy 标记。')
    if indeterminate:
        notes.append('{} 个批次缺乏成熟参照，准备金与最终净费用保持未知；其已付净额 {:.2f} 单独列示，'
                     '不并入已估计总额。'.format(len(indeterminate),
                                              sum(row['paid_net'] for row in indeterminate)))

    regimes, changes = _policy_diagnostics(context, metrics)
    provenance = dict(
        cutoff=cutoff,
        group=group,
        visible_shipments=len(context['shipments']),
        visible_claims=len(context['states']),
        visible_transactions=len(context['transactions']),
        visible_policies=[f'{row["product"]}/{row["version"]}'
                          for row in sorted(context['policies'],
                                            key=lambda item: (item['product'], item['start_month']))],
        skipped_case_rows=context['skipped_cases'],
        skipped_transactions=context['skipped_transactions'],
        developed_age=context['developed_age'],
        reopen_horizon=context['reopen_horizon'],
        report_tail=context['report_tail'],
        settlement_rule='批次年龄 >= max(质保月数, 已观测最晚发展年龄 + 1) 视为成熟；成熟且无未结案件才把准备金记为零',
        visibility_rule='known_month 与业务发生月都不晚于分析时点才可见',
    )

    return {
        'rows': rows,
        'summary': summary,
        'notes': notes,
        'scenarios': scenarios,
        'ranges': dict(
            lines={product: {key: (_r2(value) if isinstance(value, float) else value)
                             for key, value in line_ranges[product].items()}
                   for product in products},
            total={key: (_r2(value) if isinstance(value, float) else value)
                   for key, value in total_range.items()},
            assumptions=dict(within_line_correlation=WITHIN_LINE_CORRELATION,
                             across_line_correlation=ACROSS_LINE_CORRELATION,
                             method='批次情景区间聚合，同线/跨线相关；additive 为完全相容边界'),
        ),
        'diagnostics': dict(regimes=regimes, policy_changes=changes),
        'provenance': provenance,
        'coverage': dict(
            estimated_batches=len(estimated),
            settled_batches=len(settled),
            indeterminate_batches=len(indeterminate),
            estimated_units=estimated_units,
            total_units=total_units,
            estimated_ratio=(estimated_units / total_units) if total_units else None,
        ),
    }


def backtest(tables, options):
    """在历史关账时点重算，再用评估时点可见结果检验；估计不使用评估期账务。"""
    as_of = month_value(options['cutoff'])
    evaluate_at = month_value(options['evaluate'])
    group = options.get('group', '')
    if evaluate_at < as_of:
        raise ValueError('评估时点不能早于分析时点')

    as_of_context = load_context(tables, as_of, group)
    as_of_result = run(tables, {'cutoff': as_of, 'group': group})
    evaluate_context = load_context(tables, evaluate_at, group)
    evaluate_metrics = batch_metrics(evaluate_context)

    records = []
    for row in as_of_result['rows']:
        key = (row['product'], row['cohort'])
        observed = evaluate_metrics.get(key)
        if observed is None:
            continue
        observed_net = observed['net']
        observed_remaining = observed_net - row['paid_net']
        records.append(dict(
            product=row['product'], cohort=row['cohort'], maturity=row['maturity'],
            policy_version=row['policy_version'], comparability=row['comparability'],
            units=row['units'], reported_at_as_of=row['reported'],
            reported_observed=observed['reported'],
            paid_net_as_of=row['paid_net'], observed_net=_r2(observed_net),
            observed_remaining=_r2(observed_remaining),
            estimated_cases=row['expected_cases'],
            estimated_reserve=row['reserve'],
            estimated_ultimate=row['ultimate_net'],
            error_ultimate=None if row['ultimate_net'] is None else _r2(row['ultimate_net'] - observed_net),
            error_reserve=None if row['reserve'] is None else _r2(row['reserve'] - observed_remaining),
            status=row['status'],
        ))

    def aggregate(items):
        estimated = [row for row in items if row['error_ultimate'] is not None]
        indeterminate = [row for row in items if row['error_ultimate'] is None]
        absolute = [abs(row['error_ultimate']) for row in estimated]
        squared = [row['error_ultimate'] ** 2 for row in estimated]
        return dict(
            batches=len(items),
            estimated_batches=len(estimated),
            indeterminate_batches=len(indeterminate),
            paid_net_as_of=_r2(sum(row['paid_net_as_of'] for row in items)),
            estimated_reserve=_r2(sum(row['estimated_reserve'] or 0 for row in items)),
            estimated_ultimate=_r2(sum(row['estimated_ultimate'] or 0 for row in estimated)),
            observed_net=_r2(sum(row['observed_net'] for row in items)),
            observed_remaining=_r2(sum(row['observed_remaining'] for row in items)),
            bias_ultimate=_r2(sum(row['error_ultimate'] for row in estimated)) if estimated else None,
            bias_ultimate_ratio=(_r4(sum(row['error_ultimate'] for row in estimated)
                                     / sum(row['estimated_ultimate'] for row in estimated))
                                 if estimated and sum(row['estimated_ultimate'] for row in estimated) else None),
            mae_ultimate=_r2(sum(absolute) / len(absolute)) if absolute else None,
            rmse_ultimate=_r2(math.sqrt(sum(squared) / len(squared))) if squared else None,
            bias_reserve=_r2(sum(row['error_reserve'] for row in estimated)) if estimated else None,
            unestimated_observed_remaining=_r2(sum(row['observed_remaining'] for row in indeterminate)),
        )

    groups = {'total': aggregate(records), 'by_product': {}, 'by_maturity': {}, 'by_comparability': {}}
    for field, bucket in [('product', 'by_product'), ('maturity', 'by_maturity'),
                          ('comparability', 'by_comparability')]:
        for value in sorted({row[field] for row in records}):
            groups[bucket][value] = aggregate([row for row in records if row[field] == value])

    return dict(
        options=dict(cutoff=as_of, evaluate=evaluate_at, group=group),
        rows=records,
        summary=groups['total'],
        groups=groups,
        notes=[
            '回测的分析时点只读取当时可见的批次、案件和账务；评估时点单独读取，不用未来账务回填历史估计。',
            '估计值来自该分析时点实际采用的参照池与情景；未估计批次单独列出，不按零误差处理。',
            '比较 by_comparability 可观察政策截断参照是否带来系统性偏差；比较 by_maturity 可观察发展节奏变化。',
            'observed_remaining 是评估时点最终净费用减去分析时点已付净额，代表评估期内实际发生的剩余费用。',
        ],
        provenance=dict(
            as_of=as_of,
            evaluate_at=evaluate_at,
            as_of_visible=dict(shipments=len(as_of_context['shipments']),
                               claims=len(as_of_context['states']),
                               transactions=len(as_of_context['transactions'])),
            evaluate_visible=dict(shipments=len(evaluate_context['shipments']),
                                  claims=len(evaluate_context['states']),
                                  transactions=len(evaluate_context['transactions'])),
        ),
    )
