import { api, chip, escapeHtml } from '../api.js';

function reflowChip(state) {
  const ok = state.chip === '回炉已亮';
  return `<span class="chip ${ok ? 'ok' : 'bad'}">${escapeHtml(state.chip)}</span>`;
}

export async function renderBackfill(el) {
  const [jobs, gapRows, state, reflows] = await Promise.all([
    api('/api/jobs'),
    api('/api/rows?status=' + encodeURIComponent('待补采')),
    api('/api/dead_letter/state'),
    api('/api/dead_letter/reflows'),
  ]);
  const summary = jobs.summary || {};
  const settled = state.chip === '回炉已亮';
  const mismatch = state.dead_letter_count - state.covered_count;

  el.innerHTML = `
    <div class="card">
      <h2>${reflowChip(state)} 缺口补采队列</h2>
      <p class="muted">对待补采/缺口访视行排队补采；执行成功会把载荷补齐、质量改合格并关闭关联质控发现。</p>
      <div class="grid">
        <div class="stat"><div class="n">${summary['排队'] || 0}</div><div class="l">排队</div></div>
        <div class="stat"><div class="n">${summary['执行中'] || 0}</div><div class="l">执行中</div></div>
        <div class="stat"><div class="n">${summary['成功'] || 0}</div><div class="l">成功</div></div>
        <div class="stat"><div class="n">${summary['失败'] || 0}</div><div class="l">失败（死信）</div></div>
      </div>
      <div class="row">
        <select id="statusFilter">
          <option value="">全部任务</option>
          <option>排队</option>
          <option>执行中</option>
          <option>成功</option>
          <option>失败</option>
        </select>
        <button id="reload" class="ghost">刷新</button>
      </div>
      <div id="msg" class="muted"></div>
    </div>
    <div class="card">
      <h2>死信回炉 · 甲路（物化包 / cutover_book）</h2>
      <div class="grid">
        <div class="stat"><div class="n">${state.dead_letter_count}</div><div class="l">死信条数</div></div>
        <div class="stat"><div class="n">${state.covered_count}</div><div class="l">已入包</div></div>
        <div class="stat"><div class="n">${mismatch}</div><div class="l">缺行</div></div>
        <div class="stat"><div class="n">${state.landing_count}</div><div class="l">落点行</div></div>
      </div>
      <p class="muted">
        徽标 ${escapeHtml(state.chip)} · 读轨 ${escapeHtml(state.active_read)} ·
        拒回刷 ${state.reject_refresh ? '是' : '否'} ·
        指纹 ${state.cutover_fp ? escapeHtml(state.cutover_fp.slice(0, 12)) + '…' : '空'} ·
        闸门 ${state.gate_open ? '放行' : '已挡'}
      </p>
      ${settled && mismatch === 0 ? '' : `<p class="warn-line">死信条数与落点行不一致时补采排队被挡；请落定回炉后再排队。</p>`}
      <div class="row">
        <button id="settle">落定回炉（写落点行）</button>
        <button id="reloadReflow" class="ghost">刷新回炉</button>
      </div>
      <div id="reflowMsg" class="muted"></div>
    </div>
    <div class="card">
      <h2>待补采访视行（可一键排队）</h2>
      <table>
        <thead><tr><th>行ID</th><th>批次</th><th>中心</th><th>受试者访视</th><th>质量</th><th>操作</th></tr></thead>
        <tbody>
          ${(gapRows.items || []).map((r) => `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.batch_code)}</td>
              <td>${escapeHtml(r.site_name || '')}</td>
              <td>${escapeHtml(r.subject_key)} / ${escapeHtml(r.visit_code)}</td>
              <td>${chip(r.quality)}</td>
              <td><button data-enq="${r.id}">加入补采队列</button></td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">当前没有待补采行</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>补采任务</h2>
      <table>
        <thead>
          <tr>
            <th>任务号</th><th>行</th><th>中心批次</th><th>状态</th>
            <th>尝试</th><th>消息</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="jobBody">
          ${(jobs.items || []).map(jobRow).join('') || '<tr><td colspan="7" class="muted">暂无任务</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>死信回炉名录</h2>
      <table>
        <thead>
          <tr><th>ID</th><th>状态</th><th>死信条数</th><th>落点行</th><th>指纹</th><th>备注</th><th>时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          ${(reflows.items || []).map((r) => `
            <tr>
              <td>${r.id}</td>
              <td>${chip(r.status === 'settled' ? '已落定' : '失败')}${r.error ? `<div class="muted">${escapeHtml(r.error)}</div>` : ''}</td>
              <td>${r.dead_count}</td>
              <td>${r.row_count}</td>
              <td class="muted">${r.fingerprint ? escapeHtml(r.fingerprint.slice(0, 12)) + '…' : '-'}</td>
              <td class="muted">${escapeHtml(r.note || '')}</td>
              <td class="muted">${escapeHtml(r.settled_at || r.created_at || '')}</td>
              <td><button data-reflow="${r.id}" class="ghost">详情</button></td>
            </tr>`).join('') || '<tr><td colspan="8" class="muted">尚未落定回炉</td></tr>'}
        </tbody>
      </table>
    </div>`;

  function jobRow(j) {
    return `
      <tr>
        <td>${escapeHtml(j.job_code)}</td>
        <td>#${j.row_id} ${escapeHtml(j.subject_key)}/${escapeHtml(j.visit_code)}</td>
        <td>${escapeHtml(j.site_name || '')}<div class="muted">${escapeHtml(j.batch_code || '')}</div></td>
        <td>${chip(j.status)}</td>
        <td>${j.attempt}</td>
        <td class="muted">${escapeHtml(j.message || '')}</td>
        <td class="row">
          ${j.status === '排队' || j.status === '失败' ? `<button data-run="${j.id}">执行成功</button>` : ''}
          ${j.status === '排队' ? `<button data-fail="${j.id}" class="danger">模拟失败</button>` : ''}
          ${j.status === '失败' ? `<button data-retry="${j.id}" class="warn">重试排队</button>` : ''}
          <button data-view="${j.id}" class="ghost">详情</button>
        </td>
      </tr>`;
  }

  const msg = el.querySelector('#msg');
  const reflowMsg = el.querySelector('#reflowMsg');
  const jobBody = el.querySelector('#jobBody');

  async function reloadJobs() {
    const status = el.querySelector('#statusFilter').value;
    const url = status ? `/api/jobs?status=${encodeURIComponent(status)}` : '/api/jobs';
    const data = await api(url);
    jobBody.innerHTML = (data.items || []).map(jobRow).join('') || '<tr><td colspan="7" class="muted">暂无任务</td></tr>';
    bindJobs();
  }

  function bindJobs() {
    el.querySelectorAll('[data-run]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/jobs/${btn.dataset.run}/run`, {
            method: 'POST',
            body: JSON.stringify({ succeed: true }),
          });
          msg.className = 'muted';
          msg.textContent = `任务 ${btn.dataset.run} 执行成功`;
          await renderBackfill(el);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-fail]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/jobs/${btn.dataset.fail}/run`, {
            method: 'POST',
            body: JSON.stringify({ succeed: false }),
          });
          await renderBackfill(el);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-retry]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/jobs/${btn.dataset.retry}/retry`, { method: 'POST', body: '{}' });
          await renderBackfill(el);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-view]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const j = await api(`/api/jobs/${btn.dataset.view}`);
          msg.className = 'muted';
          msg.textContent = JSON.stringify(j, null, 2);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
  }

  el.querySelectorAll('[data-enq]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api('/api/jobs', {
          method: 'POST',
          body: JSON.stringify({ row_id: Number(btn.dataset.enq) }),
        });
        await renderBackfill(el);
      } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
    };
  });

  el.querySelectorAll('[data-reflow]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const d = await api(`/api/dead_letter/reflow/${btn.dataset.reflow}`);
        reflowMsg.className = 'muted';
        reflowMsg.innerHTML = `<strong>回炉 #${d.id} · ${escapeHtml(d.status)} · ${escapeHtml(d.fate)}</strong>
          <div>死信 ${d.dead_count} · 落点行 ${d.row_count_actual || d.row_count} · 指纹 ${escapeHtml(d.fingerprint || '-')}</div>
          <div>${(d.rows || []).map((r) => escapeHtml(`${r.job_code} → #${r.row_id} ${r.subject_key}/${r.visit_code} @ ${r.batch_code}`)).join('<br>') || '<em>无落点行</em>'}</div>`;
      } catch (e) { reflowMsg.textContent = e.message; reflowMsg.className = 'err'; }
    };
  });

  el.querySelector('#settle').onclick = async () => {
    try {
      const r = await api('/api/dead_letter/commit', {
        method: 'POST',
        body: JSON.stringify({ note: '缺口补采页落定' }),
      });
      reflowMsg.className = 'muted';
      reflowMsg.textContent = `回炉 #${r.id} 已落定：死信 ${r.dead_count} → 落点行 ${r.row_count}`;
      await renderBackfill(el);
    } catch (e) { reflowMsg.textContent = e.message; reflowMsg.className = 'err'; }
  };
  el.querySelector('#reloadReflow').onclick = () => renderBackfill(el);
  el.querySelector('#reload').onclick = () => reloadJobs().catch((e) => { msg.textContent = e.message; msg.className = 'err'; });
  el.querySelector('#statusFilter').onchange = () => reloadJobs();
  bindJobs();
}
