/* HR Platform — Shared UI Script */
/* 1. Immediate theme apply (before render) */
/* 2. Theme persistence + toggle injection */
/* 3. Shared interactions: accordion, tabs, modals, clock, toasts */

(function () {
  /* Prevent flash-of-wrong-theme — runs synchronously before DOM paint */
  if (localStorage.getItem('app-theme') === 'light') {
    document.documentElement.classList.add('light-mode');
  }
})();

document.addEventListener('DOMContentLoaded', function () {
  'use strict';

  // ── Theme sync + toggle ─────────────────────────────────────────────────
  const body = document.body;
  const isLight = body.classList.contains('theme-light') ||
                  document.documentElement.classList.contains('light-mode');

  if (isLight) {
    body.classList.add('theme-light');
    body.classList.remove('theme-dark');
    document.documentElement.classList.add('light-mode');
    localStorage.setItem('app-theme', 'light');
  } else {
    body.classList.add('theme-dark');
    body.classList.remove('theme-light');
    document.documentElement.classList.remove('light-mode');
    localStorage.setItem('app-theme', 'dark');
  }

  if (!document.getElementById('theme-toggle')) {
    const btn = document.createElement('button');
    btn.id = 'theme-toggle';
    btn.setAttribute('aria-label', 'Toggle light/dark theme');
    btn.setAttribute('title', 'Toggle theme');
    btn.textContent = isLight ? '☾' : '☀';
    btn.addEventListener('click', function () {
      const nowLight = document.documentElement.classList.toggle('light-mode');
      body.classList.toggle('theme-light', nowLight);
      body.classList.toggle('theme-dark', !nowLight);
      localStorage.setItem('app-theme', nowLight ? 'light' : 'dark');
      btn.textContent = nowLight ? '☾' : '☀';
    });
    document.body.appendChild(btn);
  }

  // ── Live clock ──────────────────────────────────────────────────────────
  function updateClock() {
    const el = document.getElementById('live-clock');
    if (!el) return;
    const now = new Date();
    const d = now.toLocaleDateString('en-AU', { day: '2-digit', month: 'short', year: 'numeric' });
    const t = now.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false });
    el.querySelector('.clock-text').textContent = `${d}  ${t}`;
  }
  setInterval(updateClock, 1000);
  updateClock();

  // ── Accordion ───────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    const trigger = e.target.closest('.accordion-trigger');
    if (!trigger) return;
    const item = trigger.closest('.accordion-item');
    if (!item) return;
    const isOpen = item.classList.contains('open');
    item.parentElement.querySelectorAll('.accordion-item').forEach(s => s.classList.remove('open'));
    if (!isOpen) item.classList.add('open');
  });

  // ── Tabs ────────────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('.app-tab[data-tab]');
    if (!btn) return;
    const nav = btn.closest('.app-tabs-bar');
    const panelGroup = nav ? nav.nextElementSibling : null;
    const target = btn.dataset.tab;
    if (nav) nav.querySelectorAll('.app-tab').forEach(b => {
      b.classList.remove('active');
      b.setAttribute('aria-selected', 'false');
    });
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    if (!panelGroup) return;
    panelGroup.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const panel = panelGroup.querySelector(`[data-panel="${target}"]`);
    if (panel) panel.classList.add('active');
  });

  // ── Modals ──────────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    const openBtn = e.target.closest('[data-modal-open]');
    if (openBtn) {
      const modal = document.getElementById(openBtn.dataset.modalOpen);
      if (modal) { modal.classList.add('active'); modal.querySelector('[autofocus]')?.focus(); }
      return;
    }
    const closeBtn = e.target.closest('.modal-close, [data-modal-close]');
    if (closeBtn) {
      closeBtn.closest('.modal-overlay')?.classList.remove('active');
      return;
    }
    if (e.target.classList.contains('modal-overlay')) {
      e.target.classList.remove('active');
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal-overlay.active').forEach(m => m.classList.remove('active'));
    }
  });

  // ── Toast auto-dismiss ──────────────────────────────────────────────────
  document.querySelectorAll('.app-alert[data-autodismiss]').forEach(function (el) {
    setTimeout(function () {
      el.style.transition = 'opacity .4s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 400);
    }, 4500);
  });

  // ── KPI bar animation ───────────────────────────────────────────────────
  document.querySelectorAll('.kpi-fill[data-pct]').forEach(function (bar) {
    bar.style.width = '0%';
    requestAnimationFrame(() => {
      setTimeout(() => { bar.style.width = Math.min(parseFloat(bar.dataset.pct) || 0, 100) + '%'; }, 120);
    });
  });

  // ── Confirm dangerous forms ─────────────────────────────────────────────
  document.addEventListener('submit', function (e) {
    if (e.target.dataset.confirm && !confirm(e.target.dataset.confirm)) {
      e.preventDefault();
    }
  });

  // ── Task status auto-submit ─────────────────────────────────────────────
  document.querySelectorAll('.task-status-select').forEach(function (sel) {
    sel.addEventListener('change', () => sel.closest('form').submit());
  });

  // ── Live stats fetch ────────────────────────────────────────────────────
  async function fetchStats() {
    try {
      const res = await fetch('/api/stats');
      if (!res.ok) return;
      const data = await res.json();
      Object.entries(data).forEach(([key, val]) => {
        document.querySelectorAll(`[data-stat="${key}"]`).forEach(el => { el.textContent = val; });
      });
    } catch {}
  }
  if (document.querySelector('[data-stat]')) {
    fetchStats();
    setInterval(fetchStats, 30000);
  }

  // ── Table search filter ─────────────────────────────────────────────────
  const searchInput = document.getElementById('table-search');
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      const q = this.value.toLowerCase();
      document.querySelectorAll('[data-searchable]').forEach(row => {
        row.style.display = !q || row.textContent.toLowerCase().includes(q) ? '' : 'none';
      });
    });
  }

  // ── Sidebar active page highlight ───────────────────────────────────────
  const path = '/' + (window.location.pathname.split('/')[1] || '');
  document.querySelectorAll('.nav-item[data-page]').forEach(link => {
    if (link.dataset.page && path.startsWith(link.dataset.page)) {
      link.classList.add('active');
      link.setAttribute('aria-current', 'page');
    }
  });

  // ── Task filter dropdowns ───────────────────────────────────────────────
  function applyTaskFilters() {
    const statusF   = document.getElementById('filter-status');
    const priorityF = document.getElementById('filter-priority');
    const searchF   = document.getElementById('table-search');
    const sv = statusF?.value || '';
    const pv = priorityF?.value || '';
    const qv = (searchF?.value || '').toLowerCase();
    document.querySelectorAll('[data-status]').forEach(row => {
      const ms = !sv || row.dataset.status === sv;
      const mp = !pv || row.dataset.priority === pv;
      const mq = !qv || row.textContent.toLowerCase().includes(qv);
      row.style.display = (ms && mp && mq) ? '' : 'none';
    });
  }
  ['filter-status', 'filter-priority'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', applyTaskFilters);
  });

  // ── News Ticker ─────────────────────────────────────────────────────────
  (async function initTicker() {
    const inner = document.getElementById('ticker-inner');
    if (!inner) return;
    try {
      const res = await fetch('/api/news/top');
      if (!res.ok) return;
      const items = await res.json();
      if (!items.length) return;
      const html = items.map(it =>
        `<span class="ticker-item"><a href="${it.link}" target="_blank" rel="noopener noreferrer">${it.title}</a>${it.source ? ` <span class="ticker-sep">— ${it.source}</span>` : ''}</span>`
      ).join('<span class="ticker-sep" style="padding:0 8px">·</span>');
      inner.innerHTML = html + '<span style="padding:0 40px"></span>' + html;
    } catch {}
  })();

  // ── News page tab loader ─────────────────────────────────────────────────
  async function loadNewsPanel(category) {
    const panel = document.getElementById('news-panel-' + category);
    if (!panel || panel.dataset.loaded) return;
    panel.dataset.loaded = '1';
    panel.innerHTML = '<div style="padding:32px;text-align:center;color:var(--text-muted);font-size:13px">Loading headlines…</div>';
    if (category === 'archive') {
      await loadArchivePanel(panel);
      return;
    }
    try {
      const res = await fetch('/api/news/' + category);
      const items = await res.json();
      if (!items.length) {
        panel.innerHTML = '<div class="app-card app-card-padded" style="color:var(--text-muted);font-size:13px">No headlines found.</div>';
        return;
      }
      panel.innerHTML = '<div class="app-card">' + items.map((it, idx) => newsCardHTML(it, category, idx)).join('') + '</div>';
    } catch {
      panel.innerHTML = '<div class="app-alert danger">Failed to load headlines. Check network.</div>';
    }
  }

  function newsCardHTML(it, category, idx) {
    const titleColor = idx % 2 === 0 ? 'var(--text-primary)' : 'var(--accent)';
    return `<div class="news-card" id="nc-${category}-${idx}" data-link="${it.link}">
      <div class="news-card-actions" style="justify-content:space-between">
        <a class="news-card-title" href="${it.link}" target="_blank" rel="noopener noreferrer" style="color:${titleColor}">${it.title}</a>
        <button class="btn-save-news" onclick="saveNewsItem(this,'${category}',${idx})" title="Save to Archive">
          <svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
          Save
        </button>
      </div>
      <div class="news-card-meta">
        ${(it.categories && it.categories.length) ? it.categories.map(c => `<span class="app-badge muted" style="font-size:9px;padding:2px 6px">${c}</span>`).join('') : ''}
        ${it.source ? `<span>${it.source}</span><span>·</span>` : ''}
        <span>${it.pub_date ? new Date(it.pub_date).toLocaleString('en-AU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : ''}</span>
      </div>
    </div>`;
  }

  async function loadArchivePanel(panel) {
    try {
      const res = await fetch('/api/news/archive');
      const items = await res.json();
      if (!items.length) {
        panel.innerHTML = '<div class="app-card app-card-padded" style="color:var(--text-muted);font-size:13px">No saved articles yet. Use the Save button on any headline.</div>';
        return;
      }
      panel.innerHTML = '<div class="app-card">' + items.map(it => archiveCardHTML(it)).join('') + '</div>';
    } catch {
      panel.innerHTML = '<div class="app-alert danger">Failed to load archive.</div>';
    }
  }

  function archiveCardHTML(it) {
    const savedAt = it.saved_at ? new Date(it.saved_at).toLocaleString('en-AU',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
    return `<div class="news-card" id="arc-${it.id}">
      <div class="news-card-actions" style="justify-content:space-between">
        <a class="news-card-title" href="${it.link}" target="_blank" rel="noopener noreferrer">${it.title}</a>
        <button class="btn-save-news" style="border-color:var(--danger);color:var(--danger)" onclick="deleteArchiveItem('${it.id}')" title="Remove from Archive">
          <svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>
          Remove
        </button>
      </div>
      <div class="news-card-meta">
        ${it.source ? `<span>${it.source}</span><span>·</span>` : ''}
        ${it.category ? `<span class="app-badge muted" style="font-size:9px">${it.category}</span><span>·</span>` : ''}
        <span>Saved ${savedAt}</span>
        ${it.saved_by ? `<span>by ${it.saved_by}</span>` : ''}
      </div>
    </div>`;
  }

  window.saveNewsItem = async function(btn, category, idx) {
    const card = btn.closest('.news-card');
    const title = card.querySelector('.news-card-title').textContent;
    const link = card.querySelector('.news-card-title').href;
    const meta = card.querySelector('.news-card-meta');
    const source = meta?.querySelector('span')?.textContent || '';
    const pub_date = meta?.querySelectorAll('span')[2]?.textContent || '';
    btn.disabled = true;
    try {
      const res = await fetch('/api/news/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title, link, source, pub_date, category})
      });
      const data = await res.json();
      if (data.ok) {
        btn.classList.add('saved');
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg> Saved';
        // Invalidate archive cache
        const ap = document.getElementById('news-panel-archive');
        if (ap) delete ap.dataset.loaded;
      }
    } catch { btn.disabled = false; }
  };

  window.deleteArchiveItem = async function(id) {
    if (!confirm('Remove this article from your archive?')) return;
    const card = document.getElementById('arc-' + id);
    try {
      const res = await fetch('/api/news/archive/' + id + '/delete', {method:'POST'});
      const data = await res.json();
      if (data.ok && card) card.remove();
    } catch {}
  };

  // Load first news panel on page load if on news page
  const firstNewsPanel = document.querySelector('[data-panel].active[id^="news-panel-"]');
  if (firstNewsPanel) loadNewsPanel(firstNewsPanel.id.replace('news-panel-', ''));

  // Wire up news tabs to load on click
  document.querySelectorAll('.app-tab[data-news-tab]').forEach(btn => {
    btn.addEventListener('click', () => loadNewsPanel(btn.dataset.newsTab));
  });
});
