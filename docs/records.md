# 记录样式

以下为当前业务文件中的实际记录，字段口径见 `data-contract.md`；字段标识保持业务接口写法。`known_month` 表示该行对分析可见的月份，`status_at` / `effective_month` 是业务发生月份；两者都不会被对方替代。

### `shipments`

字段：`cohort`、`known_month`、`product`、`units`、`warranty_months`。

```json
{
  "product": "标准型",
  "cohort": 1,
  "units": 170,
  "warranty_months": 12,
  "known_month": 1
}
```

### `cases`

字段：`claim_id`、`cohort`、`known_month`、`product`、`reported_month`、`status`、`status_at`。

```json
{
  "claim_id": "c000",
  "product": "加强型",
  "cohort": 1,
  "reported_month": 6,
  "status": "open",
  "status_at": 6,
  "known_month": 6
}
```

### `transactions`

字段：`claim_id`、`effective_month`、`known_month`、`signed_amount`、`transaction`。

```json
{
  "transaction": "c000-p",
  "claim_id": "c000",
  "signed_amount": 833.83,
  "effective_month": 7,
  "known_month": 8
}
```

### `policies`

字段：`end_month`、`product`、`start_month`、`version`。

```json
{
  "product": "标准型",
  "version": "v1",
  "start_month": 1,
  "end_month": 15
}
```
