'use strict';

const API = '/api';

// ──────────────────────────────────────────────
// 상태
// ──────────────────────────────────────────────
const state = {
  page: 1,
  totalCount: 0,
  pageCount: 20,
  lastQuery: {},
  selected: new Set(),       // 선택된 rcp_no
  embedPollers: new Map(),   // rcp_no → intervalId
};

// ──────────────────────────────────────────────
// API 헬퍼
// ──────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ──────────────────────────────────────────────
// 탭 전환
// ──────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
    if (btn.dataset.tab === 'chat') refreshEmbeddedList();
  });
});

// ──────────────────────────────────────────────
// 상태 표시
// ──────────────────────────────────────────────
async function checkHealth() {
  const dot  = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  try {
    const h = await apiFetch('/health');
    dot.className = 'status-dot ok';
    const issues = [];
    if (!h.dart_key_set)   issues.push('DART키 미설정');
    if (!h.gemini_key_set) issues.push('Gemini키 미설정');
    text.textContent = issues.length ? issues.join(' · ') : '정상 연결';
    if (issues.length) dot.className = 'status-dot err';
  } catch {
    dot.className = 'status-dot err';
    text.textContent = '서버 연결 실패';
  }
}
checkHealth();

// ──────────────────────────────────────────────
// 기업 자동완성
// ──────────────────────────────────────────────
const inputCorpName = document.getElementById('inputCorpName');
const suggestList   = document.getElementById('suggestList');
let suggestTimer;

inputCorpName.addEventListener('input', () => {
  clearTimeout(suggestTimer);
  const q = inputCorpName.value.trim();
  if (q.length < 1) { suggestList.classList.remove('open'); return; }
  suggestTimer = setTimeout(() => fetchSuggestions(q), 250);
});

inputCorpName.addEventListener('keydown', e => {
  if (e.key === 'Enter') { suggestList.classList.remove('open'); search(); }
});

document.addEventListener('click', e => {
  if (!e.target.closest('.autocomplete-wrap')) suggestList.classList.remove('open');
});

async function fetchSuggestions(q) {
  try {
    const companies = await apiFetch(`/dart/companies/search?q=${encodeURIComponent(q)}`);
    if (!companies.length) { suggestList.classList.remove('open'); return; }
    suggestList.innerHTML = companies.map(c => `
      <li class="suggest-item" data-code="${c.corp_code}" data-name="${c.corp_name}">
        ${c.corp_name}
        <span class="suggest-code">${c.stock_code || c.corp_code}</span>
      </li>`).join('');
    suggestList.classList.add('open');
    suggestList.querySelectorAll('.suggest-item').forEach(li => {
      li.addEventListener('click', () => {
        inputCorpName.value = li.dataset.name;
        inputCorpName.dataset.corpCode = li.dataset.code;
        suggestList.classList.remove('open');
      });
    });
  } catch { /* ignore */ }
}

// ──────────────────────────────────────────────
// 검색
// ──────────────────────────────────────────────
document.getElementById('btnSearch').addEventListener('click', () => search());

function search(page = 1) {
  state.page = page;
  state.selected.clear();
  toggleEmbedSelectedBtn();

  const corpCode = inputCorpName.dataset.corpCode || '';
  const corpName = corpCode ? '' : inputCorpName.value.trim();
  state.lastQuery = {
    corp_code:   corpCode,
    corp_name:   corpName,
    bgn_de:      document.getElementById('inputBgnDe').value.replace(/-/g, ''),
    end_de:      document.getElementById('inputEndDe').value.replace(/-/g, ''),
    pblntf_ty:   document.getElementById('inputPblntfTy').value,
    page_no:     page,
    page_count:  state.pageCount,
  };

  const params = new URLSearchParams();
  Object.entries(state.lastQuery).forEach(([k, v]) => { if (v) params.append(k, v); });

  const list = document.getElementById('disclosureList');
  list.innerHTML = `<div class="disclosure-table-wrap"><table class="disclosure-table"><tbody>
    <tr class="loader-row"><td colspan="7"><span class="spinner"></span> 검색 중…</td></tr>
  </tbody></table></div>`;
  document.getElementById('resultsHeader').style.display = 'none';

  apiFetch(`/dart/disclosures?${params}`)
    .then(data => renderDisclosures(data))
    .catch(e => {
      list.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${e.message}</p></div>`;
      toast(e.message, 'error');
    });
}

function renderDisclosures(data) {
  state.totalCount = data.total_count || 0;
  const items = data.items || [];

  document.getElementById('resultCount').textContent = `${state.totalCount.toLocaleString()}건`;
  const hdr = document.getElementById('resultsHeader');
  hdr.style.display = 'flex';

  const list = document.getElementById('disclosureList');

  if (!items.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">🔍</div><p>검색 결과가 없습니다.</p></div>`;
    document.getElementById('pagination').innerHTML = '';
    return;
  }

  const rows = items.map(item => {
    const rcp = item.rcp_no || item.rcept_no || '';
    const badgeInfo = embedBadge(item.is_embedded);
    const dartUrl = `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${rcp}`;
    const embedBtn = item.is_embedded === 2 ? '' :
      `<button class="btn-embed" data-rcp="${rcp}"
        ${item.is_embedded === 1 ? 'disabled' : ''}
       >${item.is_embedded === 1 ? '처리중' : '임베딩'}</button>`;
    return `
      <tr>
        <td class="col-check"><input type="checkbox" class="row-check" data-rcp="${rcp}" /></td>
        <td class="col-date">${formatDate(item.rcept_dt)}</td>
        <td class="col-corp">${escHtml(item.corp_name || '')}</td>
        <td class="col-report" title="${escHtml(item.report_nm || '')}">${escHtml(truncate(item.report_nm || '', 48))}</td>
        <td class="col-filer">${escHtml(item.flr_nm || '')}</td>
        <td class="col-status" id="status-${rcp}">
          <span class="badge ${badgeInfo.cls}">${badgeInfo.text}</span>
          ${embedBtn}
        </td>
        <td class="col-link"><a href="${dartUrl}" target="_blank" class="icon-link" title="DART 원문">🔗</a></td>
      </tr>`;
  }).join('');

  list.innerHTML = `
    <div class="disclosure-table-wrap">
      <table class="disclosure-table">
        <thead>
          <tr>
            <th class="col-check"><input type="checkbox" id="checkAll" /></th>
            <th class="col-date">접수일</th>
            <th class="col-corp">기업명</th>
            <th class="col-report">보고서명</th>
            <th class="col-filer">공시제출인</th>
            <th class="col-status">임베딩</th>
            <th class="col-link">원문</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  // 전체 선택
  document.getElementById('checkAll').addEventListener('change', e => {
    document.querySelectorAll('.row-check').forEach(cb => {
      cb.checked = e.target.checked;
      const rcp = cb.dataset.rcp;
      e.target.checked ? state.selected.add(rcp) : state.selected.delete(rcp);
    });
    toggleEmbedSelectedBtn();
  });

  // 개별 선택
  document.querySelectorAll('.row-check').forEach(cb => {
    cb.addEventListener('change', e => {
      const rcp = cb.dataset.rcp;
      e.target.checked ? state.selected.add(rcp) : state.selected.delete(rcp);
      toggleEmbedSelectedBtn();
    });
  });

  // 임베딩 버튼
  document.querySelectorAll('.btn-embed').forEach(btn => {
    btn.addEventListener('click', () => embedOne(btn.dataset.rcp));
  });

  // 처리중인 항목 폴링
  items.filter(i => i.is_embedded === 1).forEach(i => startPoller(i.rcp_no || i.rcept_no));

  renderPagination();
}

// ──────────────────────────────────────────────
// 임베딩
// ──────────────────────────────────────────────
async function embedOne(rcp_no) {
  try {
    await apiFetch(`/dart/disclosures/${rcp_no}/embed`, { method: 'POST' });
    updateStatusCell(rcp_no, 1);
    startPoller(rcp_no);
    toast(`임베딩 시작: ${rcp_no}`, 'info');
  } catch (e) {
    toast(e.message, 'error');
  }
}

document.getElementById('btnEmbedSelected').addEventListener('click', async () => {
  const rcpNos = [...state.selected];
  for (const rcp of rcpNos) await embedOne(rcp);
  state.selected.clear();
  toggleEmbedSelectedBtn();
  document.querySelectorAll('.row-check').forEach(c => c.checked = false);
  document.getElementById('checkAll') && (document.getElementById('checkAll').checked = false);
});

function startPoller(rcp_no) {
  if (state.embedPollers.has(rcp_no)) return;
  const id = setInterval(async () => {
    try {
      const s = await apiFetch(`/dart/disclosures/${rcp_no}/status`);
      updateStatusCell(rcp_no, s.is_embedded);
      if (s.is_embedded === 2) {
        toast(`임베딩 완료: ${rcp_no}`, 'success');
        clearInterval(id); state.embedPollers.delete(rcp_no);
      } else if (s.is_embedded === 3) {
        toast(`임베딩 실패: ${rcp_no}`, 'error');
        clearInterval(id); state.embedPollers.delete(rcp_no);
      }
    } catch { clearInterval(id); state.embedPollers.delete(rcp_no); }
  }, 3000);
  state.embedPollers.set(rcp_no, id);
}

function updateStatusCell(rcp_no, statusCode) {
  const cell = document.getElementById(`status-${rcp_no}`);
  if (!cell) return;
  const { cls, text } = embedBadge(statusCode);
  const embedBtn = statusCode === 2 ? '' :
    `<button class="btn-embed" data-rcp="${rcp_no}"
      ${statusCode === 1 ? 'disabled' : ''}
     >${statusCode === 1 ? '처리중' : '임베딩'}</button>`;
  cell.innerHTML = `<span class="badge ${cls}">${text}</span>${embedBtn}`;
  if (statusCode !== 2) {
    const btn = cell.querySelector('.btn-embed');
    btn && btn.addEventListener('click', () => embedOne(rcp_no));
  }
}

function toggleEmbedSelectedBtn() {
  const btn = document.getElementById('btnEmbedSelected');
  btn.style.display = state.selected.size > 0 ? 'inline-flex' : 'none';
  btn.textContent = `✨ ${state.selected.size}건 임베딩`;
}

// ──────────────────────────────────────────────
// 기업 DB 동기화
// ──────────────────────────────────────────────
document.getElementById('btnSyncCompanies').addEventListener('click', async () => {
  try {
    const r = await apiFetch('/dart/companies/sync', { method: 'POST' });
    toast(r.message, 'info');
  } catch (e) { toast(e.message, 'error'); }
});

// ──────────────────────────────────────────────
// 페이지네이션
// ──────────────────────────────────────────────
function renderPagination() {
  const total = Math.ceil(state.totalCount / state.pageCount);
  const cur   = state.page;
  const pg    = document.getElementById('pagination');
  if (total <= 1) { pg.innerHTML = ''; return; }

  const pages = [];
  const delta = 2;
  for (let i = 1; i <= total; i++) {
    if (i === 1 || i === total || (i >= cur - delta && i <= cur + delta)) pages.push(i);
    else if (pages[pages.length - 1] !== '…') pages.push('…');
  }

  pg.innerHTML = pages.map(p =>
    p === '…'
      ? `<span class="page-btn" style="border:none;cursor:default">…</span>`
      : `<button class="page-btn ${p === cur ? 'active' : ''}" data-page="${p}">${p}</button>`
  ).join('');

  pg.querySelectorAll('[data-page]').forEach(btn => {
    btn.addEventListener('click', () => search(Number(btn.dataset.page)));
  });
}

// ──────────────────────────────────────────────
// 채팅
// ──────────────────────────────────────────────
const chatMessages = document.getElementById('chatMessages');
const chatInput    = document.getElementById('chatInput');
const btnSend      = document.getElementById('btnSend');

btnSend.addEventListener('click', sendChat);
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});

function setQuestion(q) {
  chatInput.value = q;
  document.querySelector('[data-tab="chat"]').click();
  chatInput.focus();
}

async function sendChat() {
  const question = chatInput.value.trim();
  if (!question) return;
  const corpName = document.getElementById('chatCorpName').value.trim();

  appendMsg('user', question);
  chatInput.value = '';
  btnSend.disabled = true;

  const typingId = appendTyping();
  try {
    const data = await apiFetch('/chat', {
      method: 'POST',
      body: JSON.stringify({ question, corp_name: corpName || null }),
    });
    removeTyping(typingId);
    appendMsg('ai', data.answer, data.sources);
  } catch (e) {
    removeTyping(typingId);
    appendMsg('ai', `오류가 발생했습니다: ${e.message}`);
    toast(e.message, 'error');
  } finally {
    btnSend.disabled = false;
  }
}

function appendMsg(role, text, sources = []) {
  // 환영 화면 제거
  const welcome = chatMessages.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const sourcesHtml = sources.length
    ? `<div class="sources">${sources.map(s => {
        const url = `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${s.rcp_no}`;
        return `<a href="${url}" target="_blank" class="source-chip">${escHtml(s.corp_name)} · ${escHtml(truncate(s.report_nm, 25))}</a>`;
      }).join('')}</div>`
    : '';

  const avatar = role === 'user' ? '👤' : '🤖';
  const html = `
    <div class="msg ${role}">
      <div class="msg-avatar">${avatar}</div>
      <div class="msg-body">
        <div class="msg-bubble"><pre>${escHtml(text)}</pre></div>
        ${sourcesHtml}
      </div>
    </div>`;
  chatMessages.insertAdjacentHTML('beforeend', html);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function appendTyping() {
  const id = `typing-${Date.now()}`;
  chatMessages.insertAdjacentHTML('beforeend', `
    <div class="msg ai" id="${id}">
      <div class="msg-avatar">🤖</div>
      <div class="msg-body">
        <div class="msg-bubble">
          <div class="typing-dots"><span></span><span></span><span></span></div>
        </div>
      </div>
    </div>`);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return id;
}

function removeTyping(id) {
  document.getElementById(id)?.remove();
}

// 임베딩된 공시 목록 새로고침
async function refreshEmbeddedList() {
  const el = document.getElementById('embeddedList');
  try {
    const list = await apiFetch('/dart/embedded');
    if (!list.length) {
      el.innerHTML = '<p class="text-muted small">없음</p>';
      return;
    }
    el.innerHTML = list.map(d => `
      <div class="embedded-item">
        <div class="corp">${escHtml(d.corp_name)}</div>
        <div class="rpt">${escHtml(truncate(d.report_nm, 30))}</div>
      </div>`).join('');
  } catch {
    el.innerHTML = '<p class="text-muted small">불러오기 실패</p>';
  }
}

document.getElementById('btnRefreshEmbedded').addEventListener('click', refreshEmbeddedList);

// ──────────────────────────────────────────────
// 토스트
// ──────────────────────────────────────────────
function toast(msg, type = 'info', ms = 3500) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.getElementById('toastContainer').appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ──────────────────────────────────────────────
// 유틸
// ──────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function truncate(s, n) {
  return s && s.length > n ? s.slice(0, n) + '…' : (s || '');
}

function formatDate(d) {
  if (!d || d.length !== 8) return d || '';
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`;
}

function embedBadge(code) {
  const m = {
    0: { cls: 'badge-idle', text: '미처리' },
    1: { cls: 'badge-proc', text: '처리중' },
    2: { cls: 'badge-done', text: '완료' },
    3: { cls: 'badge-fail', text: '실패' },
  };
  return m[code] || m[0];
}

// 기본 날짜 설정 (최근 30일)
(function setDefaultDates() {
  const end   = new Date();
  const start = new Date(); start.setDate(start.getDate() - 30);
  const fmt   = d => d.toISOString().slice(0, 10);
  document.getElementById('inputBgnDe').value = fmt(start);
  document.getElementById('inputEndDe').value = fmt(end);
})();
