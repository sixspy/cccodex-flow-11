/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = {
  user: '', lab: '', items: [], selected: null,
  view: 'flows',
  merges: [], merge: null,
  notice: '', noticeTone: '',
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || '请求失败');
  return j;
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    state.items = (await api('/api/flows')).items;
    state.merges = (await api('/api/merges')).items;
    render();
  } catch (e) {
    location.href = '/login';
  }
}

function say(msg, tone) { state.notice = msg; state.noticeTone = tone || 'ok'; }

async function switchView(view) {
  state.view = view;
  state.notice = '';
  if (view === 'merges') state.merges = (await api('/api/merges')).items;
  render();
}

async function select(id) {
  state.selected = await api('/api/flows/' + id);
  render();
}

async function selectMerge(id) {
  try {
    state.merge = await api('/api/merges/' + id);
    state.notice = '';
  } catch (e) { say(e.message, 'err'); }
  state.merges = (await api('/api/merges')).items;
  render();
}

async function doExtract() {
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/extract', { method: 'POST' });
    state.items = (await api('/api/flows')).items;
    state.merges = (await api('/api/merges')).items;
    say('抽取完成');
  } catch (e) { say(e.message, 'err'); }
  render();
}

async function doLand() {
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/land', { method: 'POST' });
    state.items = (await api('/api/flows')).items;
    say('落库完成');
  } catch (e) { say(e.message, 'err'); }
  render();
}

async function mergeAction(action) {
  const id = state.merge.id;
  const body = {};
  if (action === 'merge') {
    const sel = document.getElementById('disposition');
    body.disposition = sel ? sel.value : '';
  }
  try {
    state.merge = await api('/api/merges/' + id + '/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    say({
      merge: '合并完成，冲突已按钉死策略裁定',
      segment: '抽段完成',
      land: '合并结果已入库',
    }[action] || '完成');
  } catch (e) {
    state.merge = await api('/api/merges/' + id).catch(() => state.merge);
    say(e.message, 'err');
  }
  state.merges = (await api('/api/merges')).items;
  render();
}

async function changePolicy() {
  const sel = document.getElementById('policy');
  try {
    state.merge = await api('/api/merges/' + state.merge.id + '/policy', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conflict_policy: sel.value }),
    });
    say('冲突策略已钉死');
  } catch (e) {
    state.merge = await api('/api/merges/' + state.merge.id).catch(() => state.merge);
    say(e.message, 'err');
  }
  state.merges = (await api('/api/merges')).items;
  render();
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

function flowStatusChip(status) {
  return `<span class="chip">${esc(status)}</span>`;
}

function flowRows() {
  return state.items.map((it) => `
    <button type="button" class="row ${state.selected && state.selected.id === it.id ? 'on' : ''}" data-id="${it.id}">
      <strong>${esc(it.title)}</strong>
      <span class="mono">${esc(it.code)}</span>
      <span class="chip">${esc(it.status)}</span>
    </button>`).join('');
}

function flowDetail() {
  const s = state.selected;
  if (!s) return '<p class="hint">从左侧选择一条编排流</p>';
  return `
    <section class="detail">
      <h2>${esc(s.title)}</h2>
      <p class="mono">${esc(s.source_uri)}</p>
      <p>状态 ${flowStatusChip(s.status)} · 已抽取 ${s.extracted} · 已落库 ${s.landed}</p>
      <div class="actions">
        <button type="button" id="btn-ex">执行抽取</button>
        <button type="button" id="btn-land" class="ghost">执行落库</button>
      </div>
    </section>`;
}

function mergeRows() {
  return state.merges.map((m) => `
    <button type="button" class="row ${state.merge && state.merge.id === m.id ? 'on' : ''}" data-mid="${m.id}">
      <strong>${esc(m.title)}</strong>
      <span class="mono">主份 ${m.primary_count} · 次份 ${m.secondary_count}</span>
      <span class="mono">冲突 ${m.conflict_count} 行 · ${esc(m.conflict_policy)}</span>
      <span class="chip">${esc(m.status)}</span>
    </button>`).join('');
}

function diffTable(conflicts) {
  if (!conflicts.length) return '<p class="hint">无抵触行</p>';
  return `<table class="grid"><thead><tr>
      <th>合并键</th><th>字段</th><th>主份</th><th>次份</th><th>裁定</th><th>结果</th>
    </tr></thead><tbody>
    ${conflicts.map((c) => c.diff.map((d, i) => `
      <tr class="${c.outcome === '冲突匣' ? 'bad' : 'keep'}">
        ${i === 0 ? `<td rowspan="${c.diff.length}" class="mono">${esc(c.key)}</td>` : ''}
        <td class="mono">${esc(d.field)}</td>
        <td class="mono">${esc(d.primary)}</td>
        <td class="mono">${esc(d.secondary)}</td>
        <td>${esc(c.disposition)}</td>
        <td>${esc(c.outcome)}<div class="note">${esc(c.note)}</div></td>
      </tr>`).join('')).join('')}
    </tbody></table>`;
}

function mergeDetail() {
  const m = state.merge;
  if (!m) return '<p class="hint">从左侧选择一条双源合并</p>';
  const locked = m.strategy.locked_today;
  const policyOptions = m.strategy.choices.map((p) =>
    `<option value="${esc(p)}" ${p === m.conflict_policy ? 'selected' : ''}>${esc(p)}</option>`
  ).join('');
  const dispositionOptions = m.strategy.choices.map((p) =>
    `<option value="${esc(p)}" ${p === m.conflict_policy ? 'selected' : ''}>${esc(p)}</option>`
  ).join('');
  const loss = m.loss ? `
    <div class="alert">合并争用败绩：先手已夺取合并（${esc(m.loss.by)} · ${esc(m.loss.at)}），本次未执行。</div>` : '';
  const segments = m.segments.length ? `
    <h3>抽段 ${m.segment_count} 段</h3>
    <table class="grid"><thead><tr><th>段号</th><th>键起</th><th>键止</th><th>行数</th></tr></thead><tbody>
      ${m.segments.map((s) => `<tr><td>${s.seg_no}</td><td class="mono">${esc(s.key_from)}</td>
        <td class="mono">${esc(s.key_to)}</td><td>${s.row_count}</td></tr>`).join('')}
    </tbody></table>` : '';
  return `
    <section class="detail">
      <h2>${esc(m.title)}</h2>
      <p>状态 ${flowStatusChip(m.status)} · 策略 <span class="chip">${esc(m.conflict_policy)}</span>
         ${locked ? '<span class="chip lock">当日钉死</span>' : '<span class="chip">可更换</span>'}</p>
      ${loss}
      <div class="cols">
        <div class="pane primary">
          <h3>主份编排</h3>
          <p>${esc(m.primary ? m.primary.title : '缺失')} ${m.primary ? flowStatusChip(m.primary.status) : ''}</p>
          <p class="mono">抽取 ${m.primary ? m.primary.extracted : 0} 行</p>
          <p class="mono">源 ${esc(m.primary ? m.primary.source_uri : '')}</p>
        </div>
        <div class="pane secondary">
          <h3>次份编排</h3>
          <p>${esc(m.secondary ? m.secondary.title : '缺失')} ${m.secondary ? flowStatusChip(m.secondary.status) : ''}</p>
          <p class="mono">抽取 ${m.secondary ? m.secondary.extracted : 0} 行</p>
          <p class="mono">源 ${esc(m.secondary ? m.secondary.source_uri : '')}</p>
        </div>
      </div>
      <div class="pane">
        <h3>合并键</h3>
        <p class="mono">${esc(m.key_rule || '（空缺）')}</p>
        <p class="mono">主份 ${m.counts.primary} · 次份 ${m.counts.secondary} · 冲突 ${m.counts.conflicts} 行
          · 并入 ${m.counts.merged} 行</p>
        <p class="hint">合并键拼法：${esc(m.key_fields.join(' + '))}，段内以 | 连接；键任一段空缺即不入合并。</p>
        ${m.key_preview.length ? `<p class="mono">键预看 ${m.key_preview.map((k) =>
          `${esc(k.key)}[${k.primary ? '主' : ''}${k.secondary ? '次' : ''}${k.conflict ? '·抵触' : ''}]`).join(' ')}</p>` : ''}
      </div>
      <div class="pane">
        <h3>冲突裁定预看</h3>
        <p>${m.ready
          ? '<span class="chip ok">双份齐件，可合并</span>'
          : `<span class="chip bad">${esc(m.ready_reason)}</span>`}</p>
        ${diffTable(m.conflicts)}
        <h4>冲突匣（${m.conflict_box.length} 行整条败绩）</h4>
        ${m.conflict_box.length
          ? `<table class="grid"><thead><tr><th>合并键</th><th>主份</th><th>次份</th></tr></thead><tbody>
             ${m.conflict_box.map((c) => `<tr class="bad"><td class="mono">${esc(c.key)}</td>
               <td class="mono">${esc(JSON.stringify(c.primary_payload))}</td>
               <td class="mono">${esc(JSON.stringify(c.secondary_payload))}</td></tr>`).join('')}
             </tbody></table>`
          : '<p class="hint">冲突匣为空</p>'}
      </div>
      <div class="pane">
        <h3>冲突策略</h3>
        <p>当前钉死：<span class="chip">${esc(m.conflict_policy)}</span> · 钉死日期 ${esc(m.strategy.pinned_on)}</p>
        <p class="hint">${locked
          ? '当日策略已钉死，不得更换；点合并时处置须与此一致，否则返回冲突策略漂了。'
          : '当日未钉死，可在此裁定策略；一旦裁定当天不得更换。'}</p>
        <div class="actions">
          <select id="policy" ${locked ? 'disabled' : ''}>${policyOptions}</select>
          <button type="button" id="btn-policy" class="ghost" ${locked ? 'disabled' : ''}>钉死策略</button>
        </div>
      </div>
      <div class="actions">
        <select id="disposition">${dispositionOptions}</select>
        <button type="button" id="btn-merge">执行合并</button>
        <button type="button" id="btn-segment" class="ghost">抽取段</button>
        <button type="button" id="btn-mland" class="ghost">合并结果入库</button>
      </div>
      <p class="hint">合并前须双份各自抽取成功；合并没收尾不准入库；抽段须在合并收尾之后。</p>
      ${segments}
    </section>`;
}

function render() {
  const el = document.getElementById('app');
  const nav = `
    <nav class="tabs">
      <button type="button" class="tab ${state.view === 'flows' ? 'on' : ''}" data-view="flows">编排流</button>
      <button type="button" class="tab ${state.view === 'merges' ? 'on' : ''}" data-view="merges">双源合并</button>
    </nav>`;
  const body = state.view === 'flows'
    ? `<div class="layout"><aside>${flowRows()}</aside><main>${flowDetail()}</main></div>`
    : `<div class="layout"><aside>${mergeRows()}</aside><main>${mergeDetail()}</main></div>`;
  el.innerHTML = `
    <header class="bar"><div class="brand">${esc(state.lab)}</div>
      <div>${esc(state.user)} · <button type="button" id="out" class="link">退出</button></div></header>
    ${nav}
    ${state.notice ? `<div class="alert ${state.noticeTone}">${esc(state.notice)}</div>` : ''}
    ${body}`;
  el.querySelectorAll('.row[data-id]').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  el.querySelectorAll('.row[data-mid]').forEach((b) => { b.onclick = () => selectMerge(+b.dataset.mid); });
  el.querySelectorAll('.tab').forEach((b) => { b.onclick = () => switchView(b.dataset.view); });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLand;
  const bp = el.querySelector('#btn-policy'); if (bp && !bp.disabled) bp.onclick = changePolicy;
  const bm = el.querySelector('#btn-merge'); if (bm) bm.onclick = () => mergeAction('merge');
  const bs = el.querySelector('#btn-segment'); if (bs) bs.onclick = () => mergeAction('segment');
  const bml = el.querySelector('#btn-mland'); if (bml) bml.onclick = () => mergeAction('land');
}

boot();
