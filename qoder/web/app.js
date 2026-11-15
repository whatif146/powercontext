'use strict';

/**
 * 薄弱点雷达前端：纯静态、无构建，只与本站 /api/* 通信。
 * 后端有两种失败格式，都要原样摊到错误条上：
 *   FastAPI 校验/业务错误 -> {"detail": "..."} 或 {"detail": [{loc, msg}]}
 *   PowerContext 上游错误 -> {"ok": false, "server_url": "...", "error": {endpoint, status, code, message, details}}
 */

(function () {
  const DEFAULT_SUBJECTS = ['数据结构', '计算机组成原理', '概率论'];
  const SUBJECT_KEY = 'weakradar.subject';
  const SUBJECT_MAX = 40;
  const REVIEW_LOG_MAX = 5;

  const SCORE_LABELS = ['完全没印象', '几乎想不起', '提示后才想起', '吃力想起', '顺利想起', '轻松掌握'];

  const SAMPLE_NOTES = [
    '红黑树插入：先按二叉搜索树规则插入并着红色，再通过重新着色与旋转恢复性质',
    '左旋：让右子节点顶替父节点位置，父节点成为其左孩子，原来的左子树挂到父节点右侧',
    'B+ 树：非叶子节点只存索引，叶子节点用指针串联，所以范围查询只需顺序扫叶子层',
    '哈希冲突：链地址法把同义词挂在同一链表，开放定址法按探测序列找下一个空位',
    '拓扑排序：反复删除入度为 0 的顶点，结束后仍有剩余说明图中有环',
    'KMP 的 next[j]：模式串前 j 个字符组成的前缀与后缀的最长重叠长度',
  ].join('\n');

  const state = {
    subjects: DEFAULT_SUBJECTS.slice(),
    subject: '',
    scopeId: null,
    today: null,
    queue: [],
    flipped: null,
    reviewed: 0,
    reviewLog: [],
    rawLoaded: false,
    rawOpen: false,
  };

  const $ = (id) => document.getElementById(id);

  const dom = {
    error: $('error-bar'),
    errorText: $('error-text'),
    errorClose: $('error-close'),
    healthDot: $('health-dot'),
    healthText: $('health-text'),
    scopeInfo: $('scope-info'),
    chips: $('subject-chips'),
    subjectForm: $('subject-form'),
    customSubject: $('custom-subject'),
    refreshToday: $('btn-refresh-today'),
    stats: $('today-stats'),
    context: $('today-context'),
    reviewLog: $('review-log'),
    cards: $('card-list'),
    empty: $('today-empty'),
    notesText: $('notes-text'),
    importBtn: $('btn-import'),
    sampleBtn: $('btn-sample'),
    notesResult: $('notes-result'),
    recallQuery: $('recall-query'),
    recallLimit: $('recall-limit'),
    recallBtn: $('btn-recall'),
    recallResult: $('recall-result'),
    rawPanel: $('raw-panel'),
    rawMeta: $('raw-meta'),
    rawList: $('raw-list'),
    rawToggle: $('btn-raw-toggle'),
    rawRefresh: $('btn-raw-refresh'),
  };

  // ------------------------------------------------------------------ 通用工具

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function stringify(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (err) {
      return String(value);
    }
  }

  function readStored(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function writeStored(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      /* 隐私模式下 localStorage 会抛错，不影响主流程 */
    }
  }

  function normalizeSubject(raw) {
    return String(raw || '').replace(/[\r\n\t|]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, SUBJECT_MAX);
  }

  // ------------------------------------------------------------------ 错误条

  function showError(message, kind) {
    dom.errorText.textContent = message;
    dom.error.classList.toggle('is-warn', kind === 'warn');
    dom.error.hidden = false;
  }

  function clearError() {
    dom.error.hidden = true;
    dom.errorText.textContent = '';
  }

  function validationLine(item) {
    if (!item || typeof item !== 'object') return String(item);
    const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== 'body').join('.') : '';
    const msg = item.msg || item.type || '字段不合法';
    return where ? `${where}: ${msg}` : String(msg);
  }

  function describeError(path, status, statusText, data, text) {
    const head = `${path} · HTTP ${status}${statusText ? ' ' + statusText : ''}`;

    if (data && typeof data === 'object') {
      const upstream = data.error;
      if (upstream && typeof upstream === 'object') {
        const facts = [];
        if (upstream.endpoint) facts.push(`调用点 ${upstream.endpoint}`);
        if (upstream.code) facts.push(`code=${upstream.code}`);
        if (upstream.status) facts.push(`上游 HTTP ${upstream.status}`);
        let message = String(upstream.message || '上游未给出错误说明');
        if (upstream.details !== undefined && upstream.details !== null) {
          message += ` | details=${stringify(upstream.details).replace(/\s+/g, ' ')}`;
        }
        if (data.server_url) message += ` | PowerContext=${data.server_url}`;
        return `${head}（${facts.join(' / ')}）：${message}`;
      }
      if (typeof upstream === 'string') return `${head}：${upstream}`;
      if (typeof data.detail === 'string') return `${head}：${data.detail}`;
      if (Array.isArray(data.detail)) {
        return `${head}：请求体校验失败 -> ${data.detail.map(validationLine).join('；')}`;
      }
      if (typeof data.message === 'string') return `${head}：${data.message}`;
    }

    const snippet = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 240);
    return snippet ? `${head}：${snippet}` : head;
  }

  /** 返回 {ok, status, data}；失败信息已经写进错误条，调用方只需决定是否继续。 */
  async function request(path, options) {
    const opts = options || {};
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      const reason = err && err.message ? err.message : String(err);
      showError(`${path} 请求没能发出：${reason}。请确认后端已在 127.0.0.1:8080 启动。`);
      return { ok: false, status: 0, data: null };
    }

    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = null;
      }
    }

    if (!response.ok) {
      showError(describeError(path, response.status, response.statusText, data, text));
      return { ok: false, status: response.status, data: data };
    }

    if (!data || typeof data !== 'object') {
      showError(`${path} 返回的不是 JSON 对象（HTTP ${response.status}）：${text.slice(0, 240)}`);
      return { ok: false, status: response.status, data: null };
    }

    if (data.ok === false) {
      showError(describeError(path, response.status, response.statusText, data, text));
      return { ok: false, status: response.status, data: data };
    }

    clearError();
    return { ok: true, status: response.status, data: data };
  }

  function busy(button, on, busyText) {
    if (!button) return;
    if (on) {
      button.dataset.label = button.dataset.label || button.textContent;
      button.textContent = busyText || '处理中…';
      button.disabled = true;
    } else {
      if (button.dataset.label) button.textContent = button.dataset.label;
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ 健康状态 / scope

  async function checkHealth() {
    const result = await request('/api/health');
    if (!result.ok) {
      state.scopeId = null;
      dom.healthDot.className = 'dot dot-bad';
      dom.healthText.textContent = '后端/上游异常';
      dom.scopeInfo.textContent = '';
      return;
    }
    const info = result.data.powercontext || {};
    if (info.ready) {
      dom.healthDot.className = 'dot dot-ok';
      dom.healthText.textContent = 'PowerContext 已就绪';
    } else {
      dom.healthDot.className = 'dot dot-bad';
      dom.healthText.textContent = 'PowerContext 未连接';
    }
    dom.healthDot.title = `上游地址 ${info.server_url || '未知'}${info.error ? '\n' + info.error : ''}`;
    if (info.scope_id) {
      state.scopeId = info.scope_id;
      renderScopeInfo();
    }
  }

  function renderScopeInfo() {
    dom.scopeInfo.textContent = state.scopeId ? `scope ${state.scopeId}` : '';
    dom.scopeInfo.title = state.scopeId || '';
  }

  async function ensureScope() {
    const result = await request('/api/scope', { method: 'POST', body: { subject: state.subject } });
    if (!result.ok) return false;
    state.scopeId = result.data.scope_id || null;
    renderScopeInfo();
    return true;
  }

  // ------------------------------------------------------------------ 今日复习

  function statChip(label, value, title) {
    const chip = el('span', 'stat');
    chip.appendChild(document.createTextNode(label));
    chip.appendChild(el('b', null, value));
    if (title) chip.title = title;
    return chip;
  }

  function renderStats() {
    const data = state.today || {};
    const stats = clear(dom.stats);
    stats.appendChild(statChip('日期', data.date || '—'));
    stats.appendChild(statChip('卡片总数', data.total === undefined ? '—' : data.total));
    stats.appendChild(statChip('今日到期', data.due_count === undefined ? '—' : data.due_count));
    stats.appendChild(statChip('每日上限', data.max_cards_per_day === undefined ? '—' : data.max_cards_per_day));
    stats.appendChild(statChip('待复习', state.queue.length));
    stats.appendChild(statChip('本次已自评', state.reviewed));
    if (data.unparsed && data.unparsed.length) {
      const samples = data.unparsed.slice(0, 3).join('\n');
      stats.appendChild(statChip('无法解析', data.unparsed.length, `这些 review_card 文本不合 SPEC 格式，已跳过：\n${samples}`));
    }
  }

  function renderContext() {
    const data = state.today || {};
    clear(dom.context);
    if (!data.context) {
      dom.context.hidden = true;
      return;
    }
    const title = el('span', 'ctx-title', `今日上下文（context/prepare · ${data.context_status || 'unknown'}）`);
    dom.context.appendChild(title);
    const chips = el('div', 'ctx-chips');
    (state.queue || []).forEach((card) => {
      chips.appendChild(tag(`${card.topic || '未命名'} · ${card.due || '—'}`, 'tag-due'));
    });
    dom.context.appendChild(chips);
    const toggle = el('button', 'btn btn-ghost btn-small', '查看装配原文');
    toggle.type = 'button';
    const raw = el('pre', 'ctx-raw');
    raw.textContent = data.context;
    raw.hidden = true;
    toggle.addEventListener('click', () => {
      raw.hidden = !raw.hidden;
      toggle.textContent = raw.hidden ? '查看装配原文' : '收起装配原文';
    });
    dom.context.appendChild(toggle);
    dom.context.appendChild(raw);
    dom.context.hidden = false;
  }

  function renderReviewLog() {
    const list = clear(dom.reviewLog);
    if (!state.reviewLog.length) {
      dom.reviewLog.hidden = true;
      return;
    }
    dom.reviewLog.hidden = false;
    state.reviewLog.forEach((line) => list.appendChild(el('p', 'log-line', line)));
  }

  function tag(text, className) {
    return el('span', 'tag ' + (className || ''), text);
  }

  function cardElement(card) {
    const article = el('div', 'card');
    article.tabIndex = 0;
    article.setAttribute('role', 'button');
    article.setAttribute('aria-label', `知识点 ${card.topic || ''}，翻面自评`);

    const inner = el('div', 'card-inner');

    // 正面
    const front = el('div', 'card-face card-front');
    const frontTags = el('div', 'card-tags');
    frontTags.appendChild(tag(card.subject || state.subject, 'tag-subject'));
    frontTags.appendChild(tag(card.due ? `到期 ${card.due}` : '到期未知', 'tag-due'));
    if (!card.reps) frontTags.appendChild(tag('新卡', 'tag-new'));
    front.appendChild(frontTags);
    front.appendChild(el('h3', 'card-topic', card.topic || '（无标题）'));

    const meta = el('div', 'card-tags');
    meta.appendChild(tag(`间隔 ${card.interval === undefined ? '?' : card.interval} 天`));
    meta.appendChild(tag(`重复 ${card.reps === undefined ? '?' : card.reps} 次`));
    meta.appendChild(tag(`难度 ${card.ease === undefined ? '?' : card.ease}`));
    meta.appendChild(tag(`上次 ${card.last === undefined ? '?' : card.last} 分`));
    front.appendChild(meta);

    front.appendChild(el('p', 'flip-hint', '点击翻面看原句 →'));
    front.addEventListener('click', () => flip(article, card.entry_id));

    // 背面
    const back = el('div', 'card-face card-back');
    const backHead = el('div', 'row-actions');
    const backTitle = el('span', 'flip-hint', '回忆原句后给自己打分（翻面后按 0–5 也可）');
    backHead.appendChild(backTitle);
    const unflip = el('button', 'btn btn-ghost btn-small', '← 回到正面');
    unflip.type = 'button';
    unflip.addEventListener('click', (event) => {
      event.stopPropagation();
      flip(article, null);
    });
    backHead.appendChild(unflip);
    back.appendChild(backHead);

    back.appendChild(el('h3', 'card-topic', card.topic || '（无标题）'));
    back.appendChild(el('p', 'card-note', card.note || '（这张卡没有原句摘要）'));
    back.appendChild(el('p', 'card-memline', `已存入 PowerContext · ${String(card.entry_id || '').slice(0, 12)}…`));
    back.appendChild(scoreRow(card, article));

    inner.appendChild(front);
    inner.appendChild(back);
    article.appendChild(inner);
    article.dataset.entryId = String(card.entry_id || '');
    return article;
  }

  function scoreRow(card, article) {
    const row = el('div', 'scores');
    for (let score = 0; score <= 5; score += 1) {
      const level = score <= 2 ? 's-low' : score === 3 ? 's-mid' : 's-high';
      const button = el('button', `score-btn ${level}`);
      button.type = 'button';
      button.dataset.score = String(score);
      button.appendChild(el('b', null, String(score)));
      button.appendChild(document.createTextNode(SCORE_LABELS[score]));
      button.title = `${score} 分 · ${SCORE_LABELS[score]}`;
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        review(card, score, article);
      });
      row.appendChild(button);
    }
    return row;
  }

  function setCardBusy(article, on) {
    if (!article) return;
    article.querySelectorAll('.score-btn').forEach((button) => {
      button.disabled = on;
    });
  }

  function flip(article, entryId) {
    document.querySelectorAll('.card.is-flipped').forEach((other) => {
      if (other !== article) other.classList.remove('is-flipped');
    });
    if (entryId === null) {
      article.classList.remove('is-flipped');
      if (state.flipped === String(article.dataset.entryId)) state.flipped = null;
      return;
    }
    article.classList.add('is-flipped');
    state.flipped = String(entryId);
  }

  function renderCards() {
    const list = clear(dom.cards);
    state.queue.forEach((card) => list.appendChild(cardElement(card)));
    dom.empty.hidden = state.queue.length > 0;
    if (!state.queue.length) {
      const total = (state.today && state.today.total) || 0;
      dom.empty.textContent = total
        ? `今日到期 ${state.today.due_count} 张，已全部完成（本次已自评 ${state.reviewed} 张）。明天再来，记忆仍在 PowerContext 里。`
        : '还没有知识点卡片：在右侧「粘贴笔记导入」里贴一段笔记，系统会切成卡片并立刻进入今日队列。';
    }
  }

  async function loadToday() {
    if (!state.subject) return;
    busy(dom.refreshToday, true, '刷新中…');
    const result = await request('/api/today?subject=' + encodeURIComponent(state.subject));
    busy(dom.refreshToday, false);
    if (!result.ok) {
      state.today = null;
      state.queue = [];
      renderStats();
      renderContext();
      renderCards();
      return;
    }
    state.today = result.data;
    state.queue = Array.isArray(result.data.cards) ? result.data.cards : [];
    state.flipped = null;
    renderStats();
    renderContext();
    renderCards();
  }

  async function review(card, score, article) {
    if (card._reviewing) return;
    if (!card.entry_id || !card.entry_version_id) {
      showError(`卡片「${card.topic}」缺少 entry_id / entry_version_id，无法写回记忆。请点「刷新」重新拉取今日队列。`);
      return;
    }

    card._reviewing = true;
    setCardBusy(article, true);
    const result = await request('/api/review', {
      method: 'POST',
      body: {
        subject: state.subject,
        entry_id: card.entry_id,
        entry_version_id: card.entry_version_id,
        score: score,
      },
    });
    card._reviewing = false;
    setCardBusy(article, false);

    if (!result.ok) {
      if (result.status === 409) {
        showError(
          `卡片「${card.topic}」的记忆版本已变（页面里的 entry_version_id 过期），已自动重新拉取今日队列，请按最新状态重评。`,
          'warn',
        );
        await loadToday();
      }
      return;
    }

    const next = result.data.card || {};
    state.reviewed += 1;
    state.reviewLog.unshift(
      `已记录 ${score} 分 · ${card.topic || ''} → 下次 ${next.due || '?'}（间隔 ${next.interval === undefined ? '?' : next.interval} 天，难度 ${next.ease === undefined ? '?' : next.ease}）`,
    );
    state.reviewLog = state.reviewLog.slice(0, REVIEW_LOG_MAX);

    state.queue = state.queue.filter((item) => item.entry_id !== card.entry_id);
    state.flipped = null;
    if (state.today) {
      state.today.due_count = Math.max(0, (state.today.due_count || 0) - 1);
    }
    renderStats();
    renderReviewLog();
    renderCards();
  }

  // ------------------------------------------------------------------ 笔记导入

  async function importNotes() {
    const text = dom.notesText.value;
    if (!normalizeSubject(state.subject)) {
      showError('请先选择一个科目再导入笔记。', 'warn');
      return;
    }
    if (!text.trim()) {
      showError('笔记内容为空：请粘贴至少一行知识点。', 'warn');
      return;
    }

    busy(dom.importBtn, true, '写入记忆中…');
    const result = await request('/api/notes', { method: 'POST', body: { subject: state.subject, text: text } });
    busy(dom.importBtn, false);
    if (!result.ok) return;

    const data = result.data;
    const box = clear(dom.notesResult);
    const summary = el('p', 'result-summary');
    summary.appendChild(el('b', null, `新入库 ${data.created}`));
    summary.appendChild(document.createTextNode(` / 重复 ${data.duplicates} / 候选 ${data.candidate} 条（scope ${data.scope_id || state.scopeId || '—'}）`));
    box.appendChild(summary);

    const topics = (data.cards || []).slice(0, 8).map((card) => card.topic);
    if (topics.length) {
      box.appendChild(el('p', 'hit-mono', '新卡片：' + topics.join('、') + ((data.cards || []).length > 8 ? ' …' : '')));
    }
    if ((data.existing || []).length) {
      box.appendChild(el('p', 'result-summary', `其中 ${data.existing.length} 条内容与已有卡片完全相同，已复用原卡片、调度状态未变。`));
    }
    if ((data.failed || []).length) {
      box.appendChild(el('p', 'result-summary', `${data.failed.length} 条写入失败，详见错误条。`));
      showError(`POST /api/notes 部分失败（${data.failed.length} 条）：\n${data.failed.slice(0, 5).join('\n')}`);
    }
    dom.notesResult.hidden = false;

    await Promise.all([loadToday(), state.rawLoaded ? loadRaw() : Promise.resolve()]);
  }

  // ------------------------------------------------------------------ 记忆召回

  function kindBadge(text) {
    if (String(text).indexOf('card|') === 0) return el('span', 'badge badge-card', 'review_card');
    if (String(text).indexOf('log|') === 0) return el('span', 'badge badge-log', 'review_log');
    if (String(text).indexOf('pref|') === 0) return el('span', 'badge badge-pref', 'preference');
    return el('span', 'badge', '其他');
  }

  async function runRecall() {
    const query = dom.recallQuery.value.trim();
    if (!query) {
      showError('请输入要召回的关键词，例如「旋转」或「到期」。', 'warn');
      return;
    }
    let limit = parseInt(dom.recallLimit.value, 10);
    if (!Number.isFinite(limit)) limit = 10;
    limit = Math.min(50, Math.max(1, limit));
    dom.recallLimit.value = String(limit);

    busy(dom.recallBtn, true, '召回中…');
    const result = await request('/api/recall', { method: 'POST', body: { subject: state.subject, query: query, limit: limit } });
    busy(dom.recallBtn, false);
    if (!result.ok) return;

    const data = result.data;
    const box = clear(dom.recallResult);
    const summary = el('p', 'result-summary');
    summary.appendChild(el('b', null, `命中 ${data.count}`));
    summary.appendChild(document.createTextNode(` 条 · mode=${data.mode || '—'} · query=「${data.query || query}」`));
    box.appendChild(summary);

    const hits = Array.isArray(data.hits) ? data.hits : [];
    if (!hits.length) {
      box.appendChild(el('p', 'result-summary', 'memory/search 没有命中：这些词在 PowerContext 该 scope 里不存在。换个关键词，或先导入笔记。'));
    }
    hits.forEach((hit) => {
      const item = el('div', 'hit');
      const head = el('div', 'hit-head');
      head.appendChild(kindBadge(hit.text));
      if (hit.score !== undefined && hit.score !== null) {
        head.appendChild(el('span', 'badge', `score ${Number(hit.score).toFixed(4)}`));
      }
      if (hit.matched_by) head.appendChild(el('span', 'badge', `matched_by=${stringify(hit.matched_by).replace(/\s+/g, '')}`));
      if (hit.entry_id) head.appendChild(el('span', 'badge', `entry ${String(hit.entry_id).slice(0, 12)}…`));
      item.appendChild(head);

      if (hit.card) {
        const line = el('p', 'hit-text');
        line.appendChild(el('b', null, hit.card.topic || ''));
        line.appendChild(document.createTextNode(hit.card.note ? ` —— ${hit.card.note}` : ''));
        const meta = el('div', 'hit-head');
        meta.appendChild(el('span', 'badge', `due=${hit.card.due}`));
        meta.appendChild(el('span', 'badge', `reps=${hit.card.reps}`));
        meta.appendChild(el('span', 'badge', `ease=${hit.card.ease}`));
        meta.appendChild(el('span', 'badge', `last=${hit.card.last}`));
        item.appendChild(line);
        item.appendChild(meta);
        item.appendChild(el('p', 'hit-mono', hit.text));
      } else {
        item.appendChild(el('p', 'hit-mono', hit.text || ''));
      }
      box.appendChild(item);
    });

    dom.recallResult.hidden = false;
  }

  // ------------------------------------------------------------------ 记忆原文

  async function loadRaw() {
    busy(dom.rawRefresh, true, '拉取中…');
    const result = await request('/api/memory/raw?subject=' + encodeURIComponent(state.subject));
    busy(dom.rawRefresh, false);
    if (!result.ok) return;

    const data = result.data;
    state.rawLoaded = true;
    dom.rawMeta.textContent = `共 ${data.count} 条 entries · scope ${data.scope_id} · 上游 ${data.server_url}`;

    const list = clear(dom.rawList);
    const entries = Array.isArray(data.entries) ? data.entries : [];
    if (!entries.length) {
      list.appendChild(el('p', 'result-summary', '这个 scope 里还没有任何 memory entry。'));
    }
    entries.forEach((entry) => {
      const item = el('div', 'hit');
      const head = el('div', 'hit-head');
      head.appendChild(el('span', 'badge', entry.kind || '未知 kind'));
      Object.keys(entry).forEach((key) => {
        if (key === 'text' || key === 'kind' || key === 'citation') return;
        const value = entry[key];
        if (value === null || value === undefined || typeof value === 'object') return;
        head.appendChild(el('span', 'badge', `${key}=${value}`));
      });
      const citation = entry.citation && typeof entry.citation === 'object' ? entry.citation : {};
      if (citation.entry_id) head.appendChild(el('span', 'badge', `entry_id=${citation.entry_id}`));
      if (citation.entry_version_id) head.appendChild(el('span', 'badge', `entry_version_id=${citation.entry_version_id}`));
      item.appendChild(head);
      item.appendChild(el('p', 'hit-mono', entry.text || ''));

      const details = el('details', 'raw-json');
      details.appendChild(el('summary', null, '查看该 entry 的原始 JSON'));
      details.appendChild(el('pre', null, stringify(entry)));
      item.appendChild(details);
      list.appendChild(item);
    });

    setRawOpen(true);
  }

  function setRawOpen(open) {
    state.rawOpen = open;
    dom.rawPanel.hidden = !open;
    dom.rawToggle.textContent = open ? '收起' : '展开';
  }

  // ------------------------------------------------------------------ 科目切换

  function renderChips() {
    const box = clear(dom.chips);
    state.subjects.forEach((subject) => {
      const chip = el('button', 'chip' + (subject === state.subject ? ' is-active' : ''), subject);
      chip.type = 'button';
      chip.addEventListener('click', () => selectSubject(subject));
      box.appendChild(chip);
    });
  }

  async function selectSubject(subject) {
    const clean = normalizeSubject(subject);
    if (!clean) {
      showError('科目名不能为空。', 'warn');
      return;
    }
    if (state.subjects.indexOf(clean) === -1) state.subjects.push(clean);

    state.subject = clean;
    state.scopeId = null;
    state.today = null;
    state.queue = [];
    state.flipped = null;
    state.reviewed = 0;
    state.reviewLog = [];
    writeStored(SUBJECT_KEY, clean);

    renderChips();
    renderStats();
    renderReviewLog();
    renderCards();
    clear(dom.context);
    dom.context.hidden = true;
    dom.notesResult.hidden = true;
    dom.recallResult.hidden = true;
    clear(dom.rawList);
    dom.rawMeta.textContent = `科目：${clean}`;
    renderScopeInfo();

    if (!(await ensureScope())) return;
    await loadToday();
    if (state.rawLoaded) await loadRaw();
  }

  // ------------------------------------------------------------------ 事件绑定 / 启动

  function bind() {
    dom.errorClose.addEventListener('click', clearError);

    dom.subjectForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const value = normalizeSubject(dom.customSubject.value);
      if (!value) {
        showError('请输入科目名后再切换。', 'warn');
        return;
      }
      dom.customSubject.value = '';
      selectSubject(value);
    });

    dom.refreshToday.addEventListener('click', () => loadToday());
    dom.importBtn.addEventListener('click', () => importNotes());
    dom.sampleBtn.addEventListener('click', () => {
      dom.notesText.value = SAMPLE_NOTES;
      dom.notesText.focus();
    });
    dom.recallBtn.addEventListener('click', () => runRecall());
    dom.recallQuery.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') runRecall();
    });
    dom.rawRefresh.addEventListener('click', () => loadRaw());
    dom.rawToggle.addEventListener('click', () => {
      if (!state.rawOpen) {
        loadRaw();
        return;
      }
      setRawOpen(false);
    });

    document.addEventListener('keydown', (event) => {
      if (state.flipped === null || !state.queue.length) return;
      const target = event.target;
      const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
      if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
      if (!/^[0-5]$/.test(event.key)) return;
      const score = Number(event.key);
      const card = state.queue.find((item) => String(item.entry_id) === state.flipped);
      if (!card) return;
      event.preventDefault();
      const article = document.querySelector(`.card[data-entry-id="${CSS.escape(state.flipped)}"]`);
      review(card, score, article);
    });

    window.addEventListener('focus', () => {
      if (document.visibilityState === 'visible') checkHealth();
    });
  }

  async function init() {
    bind();
    const stored = normalizeSubject(readStored(SUBJECT_KEY) || '');
    await selectSubject(stored || DEFAULT_SUBJECTS[0]);
    await checkHealth();
  }

  init();
})();
