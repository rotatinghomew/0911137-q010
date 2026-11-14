# 质保费用发展台

用于查阅质保费用发展的业务记录、批次最终净费用与剩余准备金、保存的分析版本及历史回测。现有功能包括业务范围筛选、关账时点查阅、准备金分解与情景、产品线相容总额与联合风险范围、原始资料展开以及不可变分析结果回看。统计口径见 `docs/data-contract.md`，HTTP 结构见 `docs/http-api.md`。

## 口径摘要

- 只有关账时点当时可见的批次、案件和账务进入分析；尚未发生的报告不当作零费用。
- 一个 `claim_id` 只算一个案件，案件重开不重复计入暴露或案件数。
- 准备金分解为已报案未结、尚未报案（IBNR）、结案后重开和未来追回；毛支付、追回、净额分列。
- 参照池按服务政策版本分层；新政策或新产品缺少成熟参照时保持未知，不用零或他产品线冒充。
- 同时给出四套准备金情景、产品线 `additive`/`joint` 区间与政策节奏/成本诊断，便于比较准备金方案。
- 回测在历史关账时点重算，再用评估时点结果检验，不把未来账务带回历史验证。

## 运行

需要 Python >=3.11,<3.12，不依赖特定补丁版本。应用运行只使用标准库，浏览器 JavaScript 无构建步骤。以下命令在项目根目录执行：

```sh
python3 -m claims_development
```

浏览器打开本机 8080 端口即可操作。资料默认来自 `fixtures/data.json`；分析结果默认保存在 `.state/analyses.sqlite3`。可用 `--data` 指定同口径资料，`--state` 指定状态文件，`--port` 调整监听端口。页面可切换关账时点、查看准备金情景与联合区间，或输入历史关账时点和评估时点运行回测，并把任一分析保存为不可变版本。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试涵盖领域口径、准备金分解、政策分层与回测边界、真实 HTTP 往返、错误筛选、跨进程存储及不可变保存。页面直接消费与测试相同的 API，不使用预录结果。

浏览器验证工具与产品运行依赖分开锁定；准备一次后可重复执行筛选、保存、刷新回看和资料查阅：

```sh
python3 -m pip install -r requirements-test.txt
python3 -m playwright install --with-deps chromium
python3 tests/browser_smoke.py
```

前两条是首次测试环境安装，完成后无需重复执行。基础容器只运行产品和标准库测试；浏览器测试在安装了 `requirements-test.txt` 与 Chromium 的开发环境运行。

也可使用无第三方 JavaScript 依赖的 npm 工作区入口：`npm ci --ignore-scripts` 后，`npm start -- --host 127.0.0.1 --port 8080` 启动同一个 Python HTTP 服务，`npm run test:browser` 执行上述浏览器回归。npm 仅负责命令编排，不是产品运行依赖；不要把 `web/` 单独作为静态站点运行，页面需要同源 API。

## 容器

```sh
docker build -t claims-development:local .
docker run --rm -p 127.0.0.1:8080:8080 claims-development:local
```

基础镜像以内容摘要固定，Python 打包依赖固定在 `requirements-build.txt`。容器构建完成后可断网运行；若要保留容器外分析版本，可将宿主目录挂载到 `/app/.state`。

## 快速业务核对

```sh
python3 -m claims_development --demo
```

此入口启动真实本地 HTTP 服务，计算当前摘要，保存后重新读取再退出。输出依赖当前资料，不预先指定统计结论。初次阅读可先在页面调整时点或业务范围，再保存和回看分析；测试和该核对入口可在九十秒内完成，不包含镜像构建时间。
