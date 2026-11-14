# 资料口径

`shipments` 的 `units` 是质保暴露，`warranty_months` 是质保长度。`cases` 一个 `claim_id` 可多次 `status=open/reopened/closed`，`reported_month` 是首次报案月。`transactions` 的 `signed_amount` 支付为正、追回为负，`known_month` 表示入账可见月，不以业务归属月替代。`policies` 说明各产品线的服务版本有效月。

## 关账时点与可见性

分析时点（`cutoff`）表示一次关账。只有 `known_month <= cutoff` 且业务发生月 `<= cutoff` 的记录才进入该时点的视图：

- `shipments`：`cohort` 与 `known_month` 都不晚于关账时点。
- `cases`：`reported_month`、`status_at`、`known_month` 都不晚于关账时点。
- `transactions`：`effective_month` 与 `known_month` 都不晚于关账时点。

`known_month` 只决定信息何时可见，不替代业务月。一篇晚报的案件不因为记录晚到就按晚报月份计算发展年龄；晚到账务也不会用业务归属月“回到过去”提前计入。回测不使用未来才可见的账务或案件行。

## 案件与发展状态

- 一个 `claim_id` 只算一个案件。`reopened` 是同一案件的状态变化，不增加案件数，也不重复增加批次暴露。
- 案件状态按已可见行重放为 `open`、`closed`、`open_reopened`、`closed_reopened`。`closed_at` 取第一次结案，`reopened_at` 取第一次重开。
- 批次“成熟”要求发展年龄达到 `max(质保月数, 已观测最晚发展年龄 + 1)`。成熟只表示参照池可以把它当作完整发展的样本；若该批次仍有未结案件，准备金仍要估计，不能当作零。
- 只有既成熟又无未结案件的批次，才把准备金记为零。

## 最终净费用与准备金

每批最终净费用 = 已付净额 + 剩余准备金。剩余准备金按以下分解估计，并随账务情景变化：

| 组成 | 说明 |
| --- | --- |
| `ibnr_gross` | 尚未报案案件（IBNR）的未来毛支付。频率来自成熟参照池，未报案比例来自已观测报案滞后曲线。 |
| `open_remaining_gross` | 已报案未结案件的剩余毛支付。 |
| `reopen_gross` | 结案后重开带来的未来毛支付，含结案后尾款。 |
| `future_recovery` | 上述三类未来支付对应的预计追回。 |
| `reserve_raw` | `毛利支付 - 预计追回`，可以为负。 |
| `reserve` | 账务准备金，取 `max(0, reserve_raw)`，不出现负值。 |

尚未发生的报告不能当作零费用：未报案案件使用 Bornhuetter-Ferguson 口径，先按暴露 × 参照频率估计最终案件数，再乘尚未报案比例。已报案数不作为上界；观测曲线在某个年龄接近完全时，仍保留 `MIN_IBNR_TAIL` 的最小未报案厚度，避免把“恰好还没报”误判为“永远不会再报”。

追回按 `signed_amount` 负值处理。追回不冲掉案件计数，也不会因为历史追回很多就把未来 IBNR 置零；历史净额与未来预计追回分列。异常大的历史追回会压低 `paid_net` 和最终净费用，但不会制造负的准备金。

## 参照池与新产品

参照池只取成熟批次。优先级：

1. 同产品线、同服务政策版本，至少 `MIN_POOL_BATCHES` 个批次且 `MIN_POOL_CLAIMS` 个案件 → `comparable`。
2. 同产品线全部政策版本 → `policy_truncated`（若混合了版本）或 `thin_same_policy`。
3. 全部产品线组合 → `portfolio_proxy`。
4. 都没有 → `no_reference`，准备金与最终净费用为 `null`，不以零代替；该批已付净额单独列在 `未估计批次净支付`。

新产品没有成熟参照时保持 `indeterminate`。不允许用其他产品线的成本结构冒充。

## 服务政策与节奏

`policies` 的版本按批次 `cohort` 所在月份生效（`start_month <= cohort < end_month`，`end_month` 为空表示持续有效）。参照池优先同政策版本；当新政策尚无成熟批次时，退回产品线合并参照并标记 `policy_truncated`，提示节奏与最终成本不能完全分离。

诊断表同时给出：

- 各政策版本的成熟批次数、案件频率、每案毛支付/追回、单位成本。
- 相邻版本的报案滞后、结案滞后、重开延迟。
- `tempo_ratio`（处理节奏变化）与 `cost_ratio`（最终单位成本变化）。任一侧没有成熟样本时为 `indeterminate`。只有节奏变化、单位成本稳定时分类为 `tempo`；只有成本变化时分类为 `cost`；两者都变或都不变分别标记 `both`、`stable`。

重开率在样本很少时用同产品线其他政策版本作先验平滑，避免新政策零重开样本把准备金系统性压低。

## 情景与联合风险范围

每个估计批次给出四个情景：`favorable`、`central`、`prudent`、`conservative`。情景同时调整频率、每案严重度分位、重开率和追回率，用于比较准备金方案。

- 批次区间为 `[min(四情景准备金), max(四情景准备金)]`。
- 产品线把各批次中心值逐级相加；`additive` 区间是各批次区间直接相加的完全相容上界。
- `joint` 区间按相关假设聚合：同产品线相关 `WITHIN_LINE_CORRELATION`，跨产品线相关 `ACROSS_LINE_CORRELATION`。相关越高，联合区间越接近完全相容边界；相关越低越接近独立相加。
- 未估计批次不进入区间，`has_unestimated` 标记提示区间不覆盖全部暴露。

## 历史回测

`/api/backtest` 在历史关账时点 `cutoff` 重算估计，再用评估时点 `evaluate` 可见结果检验：

- 分析时点只读取当时可见的批次、案件和账务；评估时点单独读取。
- `observed_remaining = 评估时点最终净费用 - 分析时点已付净额`，代表评估期内实际发生的剩余费用。
- `error_ultimate = 分析时点估计最终净费用 - 评估时点最终净费用`；`error_reserve` 用 `observed_remaining` 对应。
- 未估计批次不按零误差处理，单列 `unestimated_observed_remaining`。
- 分组输出 `by_product`、`by_maturity`、`by_comparability`，可观察政策截断参照是否带来系统性偏差，以及发展节奏变化在不同成熟度上的表现。

## 现有资料

- `shipments`：44 条本地记录。
- `cases`：314 条本地记录。
- `transactions`：164 条本地记录。
- `policies`：4 条本地记录。

所有日期、月份、周或小时均用各表中的整数业务时标，页面的分析时点使用同一单位。资料中的 known 字段表示该行信息何时对分析可见，不是数据载入时间。字段名按原始业务接口保留。

这些记录是项目随附的虚构业务数据，不包含真实员工、客户、交易或设备。`fixtures/data.json` 的表结构可通过资料查阅页面检查；服务不从外网补数。

## 分析版本

当前分析从同一份文件一次读取所有表，并把原始字节的 SHA-256 与筛选条件一起返回。保存分析时重新基于指定条件计算，保存的是结果值及该次资料指纹，而不是指向未来可变数据的引用。已有版本只读；同名保存产生另一条版本。历史结果保留原有字段语义，不因最新源文件被替换而重算。

没有记录使用空列表或 `null`，不能把无观测表示为有效的零值。历史版本保存后不重算。
