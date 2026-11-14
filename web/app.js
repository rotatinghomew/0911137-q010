const statusNode = document.querySelector('#status');
const filters = document.querySelector('#filters');
const backtestForm = document.querySelector('#backtest-form');
let current = null;
let requestGeneration = 0;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败');
  return data;
}

function text(tag, value) {
  const node = document.createElement(tag);
  node.textContent = typeof value === 'object' ? JSON.stringify(value) : String(value ?? '未知');
  return node;
}

function table(container, rows) {
  container.replaceChildren();
  if (!rows.length) {
    container.append(text('p', '当前范围没有记录'));
    return;
  }
  const columns = Array.from(new Set(rows.flatMap(row => Object.keys(row))));
  const grid = document.createElement('table');
  const head = document.createElement('tr');
  for (const column of columns) head.append(text('th', column));
  const thead = document.createElement('thead');
  thead.append(head);
  grid.append(thead);
  const body = document.createElement('tbody');
  for (const row of rows) {
    const line = document.createElement('tr');
    for (const column of columns) line.append(text('td', row[column]));
    body.append(line);
  }
  grid.append(body);
  container.append(grid);
}

function block(container, rows) {
  container.replaceChildren();
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      container.append(text('dt', key), text('dd', typeof value === 'object' ? JSON.stringify(value) : value));
    }
  }
}

function scenarioTable(container, scenarios) {
  const rows = Object.entries(scenarios).map(([name, value]) => ({
    情景: value.label, 已付净额: value.total.paid_net, 准备金: value.total.reserve,
    最终净费用: value.total.ultimate_net, 未估计批次已付净额: value.total.unestimated_paid_net,
  }));
  table(container, rows);
}

function rangeTable(container, ranges) {
  const rows = [
    ...Object.entries(ranges.lines).map(([product, value]) => ({
      范围: product, 批次中心准备金: value.central,
      完全相容区间: `${value.additive_low} ~ ${value.additive_high}`,
      联合区间: `${value.joint_low} ~ ${value.joint_high}`,
      有未估计批次: value.has_unestimated,
    })),
    {
      范围: '合计', 批次中心准备金: ranges.total.central,
      完全相容区间: `${ranges.total.additive_low} ~ ${ranges.total.additive_high}`,
      联合区间: `${ranges.total.joint_low} ~ ${ranges.total.joint_high}`,
      有未估计批次: ranges.total.has_unestimated,
    },
  ];
  table(container, rows);
}

function diagnosticsTable(container, diagnostics) {
  const rows = [
    ...diagnostics.regimes.map(row => ({类型: '政策分层', ...row})),
    ...diagnostics.policy_changes.map(row => ({类型: '版本变化', ...row})),
  ];
  table(container, rows);
}

function provenanceText(provenance) {
  return [
    `关账时点 ${provenance.cutoff}`,
    `可见：出货 ${provenance.visible_shipments}、案件 ${provenance.visible_claims}、流水 ${provenance.visible_transactions}`,
    `剔除：未来案件行 ${provenance.skipped_case_rows}、未来流水 ${provenance.skipped_transactions}`,
    `服务政策：${provenance.visible_policies.join('、') || '无'}`,
    `已观测最晚发展年龄 ${provenance.developed_age}，结案后重开窗口 ${provenance.reopen_horizon}，报案尾部 ${provenance.report_tail}`,
    `可见性：${provenance.visibility_rule}；成熟规则：${provenance.settlement_rule}`,
  ].join('\n');
}

function render(value, savedTitle = '') {
  current = {value, kind: 'view'};
  const summary = document.querySelector('#summary');
  summary.replaceChildren();
  for (const [key, amount] of Object.entries(value.result.summary)) {
    summary.append(text('dt', key), text('dd', amount));
  }
  scenarioTable(document.querySelector('#scenarios'), value.result.scenarios);
  rangeTable(document.querySelector('#ranges'), value.result.ranges);
  table(document.querySelector('#results'), value.result.rows);
  diagnosticsTable(document.querySelector('#diagnostics'), value.result.diagnostics);
  document.querySelector('#notes').replaceChildren(...value.result.notes.map(note => text('li', note)));
  document.querySelector('#provenance').textContent = provenanceText(value.result.provenance);
  document.querySelector('#digest').textContent = `资料指纹：${value.digest}`;
  statusNode.textContent = savedTitle ? `正在查看保存的分析：${savedTitle}` : '分析已更新';
  filters.elements.cutoff.value = value.options.cutoff;
  filters.elements.group.value = value.options.group;
  backtestForm.elements.cutoff.value = value.options.cutoff;
  backtestForm.elements.cutoff.max = value.options.cutoff;
  backtestForm.elements.group.value = value.options.group;
}

function backtestGroups(container, groups) {
  const rows = [];
  for (const [bucket, label] of [['by_product', '产品线'], ['by_maturity', '成熟度'], ['by_comparability', '参照可比性']]) {
    for (const [value, summary] of Object.entries(groups[bucket])) {
      rows.push({
        分组: label, 取值: value, 批次数: summary.batches,
        已估计批次: summary.estimated_batches, 未估计批次: summary.indeterminate_batches,
        估计准备金: summary.estimated_reserve, 实际剩余费用: summary.observed_remaining,
        最终净费用偏差: summary.bias_ultimate, 偏差率: summary.bias_ultimate_ratio,
        平均绝对误差: summary.mae_ultimate, 均方根误差: summary.rmse_ultimate,
      });
    }
  }
  table(container, rows);
}

function renderBacktest(value) {
  current = {value, kind: 'backtest'};
  const summary = document.querySelector('#backtest-summary');
  summary.replaceChildren();
  for (const [key, amount] of Object.entries(value.result.summary)) {
    summary.append(text('dt', key), text('dd', amount));
  }
  backtestGroups(document.querySelector('#backtest-groups'), value.result.groups);
  table(document.querySelector('#backtest-results'), value.result.rows);
  document.querySelector('#backtest-notes').replaceChildren(...value.result.notes.map(note => text('li', note)));
  statusNode.textContent = `回测已更新：${value.options.cutoff} → ${value.options.evaluate}`;
}

async function refresh() {
  const generation = ++requestGeneration;
  const query = new URLSearchParams(new FormData(filters));
  statusNode.textContent = '正在分析';
  try {
    const result = await api(`/api/view?${query}`);
    if (generation === requestGeneration) render(result);
  } catch (error) {
    if (generation === requestGeneration) {
      current = null;
      statusNode.textContent = error.message;
    }
  }
}

async function history() {
  const entries = await api('/api/analyses');
  const list = document.querySelector('#history');
  list.replaceChildren();
  for (const entry of entries) {
    const item = document.createElement('li');
    const button = text('button', entry.title);
    button.type = 'button';
    button.addEventListener('click', async () => {
      const generation = ++requestGeneration;
      try {
        const saved = await api(`/api/analyses/${entry.id}`);
        if (generation !== requestGeneration) return;
        if (saved.options.evaluate !== undefined) renderBacktest(saved);
        else render(saved, saved.title);
      } catch (error) {
        statusNode.textContent = error.message;
      }
    });
    item.append(button, text('small', entry.created_at));
    list.append(item);
  }
}

filters.addEventListener('submit', event => {
  event.preventDefault();
  refresh();
});

backtestForm.addEventListener('submit', async event => {
  event.preventDefault();
  const generation = ++requestGeneration;
  const query = new URLSearchParams(new FormData(backtestForm));
  statusNode.textContent = '正在回测';
  try {
    const result = await api(`/api/backtest?${query}`);
    if (generation === requestGeneration) renderBacktest(result);
  } catch (error) {
    if (generation === requestGeneration) statusNode.textContent = error.message;
  }
});

document.querySelector('#save').addEventListener('submit', async event => {
  event.preventDefault();
  if (!current) return;
  try {
    const saved = await api('/api/analyses', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: event.target.elements.title.value, options: current.value.options, kind: current.kind}),
    });
    await history();
    statusNode.textContent = `已保存分析：${saved.title}`;
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

document.querySelector('#source').addEventListener('click', async () => {
  try {
    const data = await api(`/api/source?table=${encodeURIComponent(document.querySelector('#tables').value)}`);
    table(document.querySelector('#raw'), data.rows);
  } catch (error) {
    statusNode.textContent = error.message;
  }
});

async function start() {
  const catalog = await api('/api/catalog');
  document.querySelector('#description').textContent = catalog.description;
  for (const input of [filters.elements.cutoff, backtestForm.elements.cutoff, backtestForm.elements.evaluate]) {
    input.min = catalog.min_cutoff;
    input.max = catalog.max_cutoff;
  }
  filters.elements.cutoff.value = catalog.default_cutoff;
  backtestForm.elements.cutoff.value = catalog.default_cutoff;
  backtestForm.elements.evaluate.value = catalog.max_cutoff;
  const groups = [['', '全部'], ...catalog.groups.map(group => [group, group])];
  for (const element of [filters.elements.group, backtestForm.elements.group]) {
    for (const [key, label] of groups) {
      const option = text('option', label);
      option.value = key;
      element.append(option);
    }
  }
  for (const name of Object.keys(catalog.tables)) {
    const option = text('option', `${name}（${catalog.tables[name]}）`);
    option.value = name;
    document.querySelector('#tables').append(option);
  }
  await refresh();
  await history();
}

start().catch(error => { statusNode.textContent = error.message; });
