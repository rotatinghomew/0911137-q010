# 本地接口

- `GET /api/catalog`：资料名称、可选业务范围、业务时点范围、表计数与资料指纹。
- `GET /api/view`：批次最终净费用与剩余准备金分析，可选 `cutoff` 整数和 `group` 业务范围；未知筛选项或越界时点返回 400。
- `GET /api/backtest`：历史关账时点 `cutoff`、评估时点 `evaluate` 与可选 `group`；`evaluate < cutoff` 返回 400。输出该关账时点的估计、评估时点实际结果、误差与分组检验。
- `GET /api/source?table=shipments`：按实际资料表名称查询原始记录；不存在的表返回 404。
- `POST /api/analyses`：JSON 对象包含 `title`、`options`，可选 `kind`（`view` 或 `backtest`，默认 `view`）。重新计算并保存，成功返回 201。`options` 与该类分析的筛选结构相同。
- `GET /api/analyses`：只返回版本目录及资料指纹。
- `GET /api/analyses/{id}`：返回保存的完整结果，不重新计算。

## 视图结构

`/api/view` 的 `result` 包含：

- `rows`：批次行。批次级字段包括暴露、发展年龄、成熟状态、服务政策版本、案件计数、毛支付/追回/净额，以及准备金分解 `ibnr_gross`、`open_remaining_gross`、`reopen_gross`、`future_recovery`、`reserve_raw`、`reserve`、最终净费用与四个情景准备金。
- `summary`：当前范围的合计；未估计批次单独列在 `未估计批次净支付`。
- `scenarios`：`favorable`、`central`、`prudent`、`conservative` 四套准备金方案的按产品线与总额。
- `ranges`：批次到产品线的 `additive` 与 `joint` 区间、相关假设与是否覆盖全部暴露。
- `diagnostics`：政策版本分层与相邻版本的节奏/成本变化分类。
- `provenance`：该关账时点可见的记录计数、剔除的未来行数、已观测发展年龄、重开窗口、成熟规则与可见性规则。
- `coverage`：已估计、已完整、未估计批次数与暴露覆盖率。
- `notes`：业务口径提示。

`/api/backtest` 的 `result` 包含 `rows`（逐批估计与实际结果）、`summary`（总体偏差、MAE、RMSE、未估计剩余费用）、`groups`（`by_product`、`by_maturity`、`by_comparability`）、`notes` 与 `provenance`。

API 的 `rows` 是领域行，`summary` 是当前汇总，`notes` 是业务口径提示，`options` 是实际采用的筛选值。无记录使用空列表，不能把无观测偷偷表示为有效的零值；没有成熟参照的批次准备金为 `null`。错误 JSON 的 `error` 可直接在页面反馈。

这是本地单用户应用，不包含认证与外网部署设置。默认只监听回环地址，容器中可显式监听所有接口，但宿主机端口建议只绑定回环地址。
