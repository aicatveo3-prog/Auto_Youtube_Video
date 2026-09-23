'use strict';

const PAGE = 200;   // rows drawn per step

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const el = (t, cls, text) => {
  const n = document.createElement(t);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const S = {
  channel: null,      // loaded channel blob
  key: null,
  videos: [],
  picked: new Set(),
  results: {},        // vid -> {state, detail} from the running extract job
  ctab: 'all',        // all | done | todo | members | clean
  limit: 0,           // how many rows are currently drawn
  poll: null,
  reader: null,       // loaded transcript
  clean: null,        // loaded 정리본
  article: null,      // loaded 읽을거리 slug
  articleDoc: null,   // loaded 읽을거리 full doc (for 전체 복사)
};

/* ── helpers ────────────────────────────────────────────── */
function fmtDur(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}`
           : `${m}:${String(x).padStart(2, '0')}`;
}
function fmtNum(v) {
  if (v == null) return '—';
  if (v >= 1e8) return (v / 1e8).toFixed(1).replace(/\.0$/, '') + '억';
  if (v >= 1e4) return (v / 1e4).toFixed(1).replace(/\.0$/, '') + '만';
  return v.toLocaleString('ko-KR');
}
const commas = (n) => (n == null ? '—' : n.toLocaleString('ko-KR'));
// yt-dlp stores upload_date as "YYYYMMDD"; show it as "YYYY.MM.DD".
function fmtDate(d) {
  if (!d || !/^\d{8}$/.test(d)) return '';
  return `${d.slice(0, 4)}.${d.slice(4, 6)}.${d.slice(6, 8)}`;
}

let toastTimer;
function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 2600);
}
/*
 * STATIC mode: the build injects window.YT_STATIC on the GitHub Pages copy.
 * There is no server there, so every read is served from pre-built JSON under
 * data/, and every write is a no-op (the UI that triggers writes is hidden).
 */
const STATIC = !!window.YT_STATIC;

async function api(path, opts) {
  if (STATIC) return staticApi(path, opts);
  const r = await fetch(path, opts);
  let b = {};
  try { b = await r.json(); } catch { /* no body */ }
  if (!r.ok) throw new Error(b.error || `${r.status} ${r.statusText}`);
  return b;
}
const jpost = (p, d) => api(p, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(d),
});

/* ── static back-end (GitHub Pages) ─────────────────────── */
let _searchIndex = null;   // data/search.json, loaded once on first search

async function getJSON(rel) {
  const r = await fetch(rel);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

async function staticApi(path, opts) {
  // Writes cannot happen on a static host. The buttons are hidden, but guard
  // anyway so a stray call fails quietly instead of throwing into the UI.
  if (opts && opts.method && opts.method !== 'GET') {
    return { ok: false, static: true };
  }
  const [route, query] = path.split('?');
  if (route === '/api/library') return getJSON('data/library.json');
  if (route === '/api/job') return { status: 'idle', kind: 'idle' };
  if (route.startsWith('/api/channel/')) {
    return getJSON(`data/ch/${route.slice('/api/channel/'.length)}.json`);
  }
  if (route.startsWith('/api/video/')) {
    return getJSON(`data/v/${route.slice('/api/video/'.length)}.json`);
  }
  if (route.startsWith('/api/clean/')) {
    const vid = route.slice('/api/clean/'.length);
    try {
      const v = await getJSON(`data/v/${vid}.json`);
      return v.clean || { exists: false };
    } catch { return { exists: false }; }
  }
  if (route === '/api/archived') {
    try { return { prompts: await getJSON('data/archived.json') }; }
    catch { return { prompts: [] }; }
  }
  if (route === '/api/search') {
    const q = decodeURIComponent((query || '').replace(/^q=/, ''));
    return staticSearch(q);
  }
  if (route === '/api/channels') {
    const lib = await getJSON('data/library.json');
    return { channels: lib.channels };
  }
  if (route === '/api/prompts') {
    try { return { prompts: await getJSON('data/prompts.json') }; }
    catch { return { prompts: [] }; }
  }
  if (route.startsWith('/api/prompt/')) {
    const key = decodeURIComponent(route.slice('/api/prompt/'.length));
    try {
      const list = await getJSON('data/prompts.json');
      return list.find((p) => p.key === key) || { error: 'not found' };
    } catch { return { error: 'not found' }; }
  }
  if (route === '/api/articles') {
    try { return { articles: await getJSON('data/articles.json') }; }
    catch { return { articles: [] }; }
  }
  if (route.startsWith('/api/article/')) {
    const slug = route.slice('/api/article/'.length);
    try { return await getJSON(`data/art/${slug}.json`); }
    catch { return { error: 'not found' }; }
  }
  return {};
}

/* Full-text search over data/search.json, replicating the server's snippets. */
const SNIPPET_PAD = 70;
async function staticSearch(q) {
  q = (q || '').trim();
  if (q.length < 2) throw new Error('검색어를 두 글자 이상 입력하세요.');
  if (!_searchIndex) _searchIndex = await getJSON('data/search.json');

  const needle = q.toLowerCase();
  const results = [];
  let totalHits = 0;
  for (const doc of _searchIndex) {
    const flat = doc.text || '';
    const low = flat.toLowerCase();
    if (!low.includes(needle)) continue;
    const spots = [];
    let from = 0, i;
    while ((i = low.indexOf(needle, from)) !== -1) { spots.push(i); from = i + needle.length; }
    totalHits += spots.length;
    const snippets = spots.slice(0, 4).map((pos) => {
      const a = Math.max(0, pos - SNIPPET_PAD);
      const b = Math.min(flat.length, pos + q.length + SNIPPET_PAD);
      return {
        before: (a > 0 ? '...' : '') + flat.slice(a, pos),
        match: flat.slice(pos, pos + q.length),
        after: flat.slice(pos + q.length, b) + (b < flat.length ? '...' : ''),
      };
    });
    results.push({
      id: doc.id, title: doc.title, channel: doc.channel,
      channel_key: doc.key, hits: spots.length,
      more: Math.max(0, spots.length - 4), snippets,
    });
  }
  results.sort((x, y) => y.hits - x.hits);
  return { query: q, videos: results.length, hits: totalHits,
           searched: _searchIndex.length, results };
}

/* ── visual helpers ─────────────────────────────────────── */

/*
 * Monogram stands in for a channel avatar, which we do not collect. The hue
 * comes from the channel id, so a channel keeps the same colour forever and
 * two channels rarely collide.
 */
function monogram(name, seed) {
  let h = 0;
  for (const ch of (seed || name || '?')) h = (h * 31 + ch.codePointAt(0)) % 360;
  const node = el('div', 'mono', (name || '?').trim().charAt(0) || '?');
  node.style.background =
    `linear-gradient(140deg, hsl(${h} 52% 42%), hsl(${(h + 34) % 360} 54% 30%))`;
  node.setAttribute('aria-hidden', 'true');
  return node;
}

/*
 * Channel face: the real avatar when we have one, monogram otherwise. The
 * monogram also covers the case where the avatar URL has expired, since
 * YouTube's image host rotates them.
 */
function channelFace(c, size) {
  const fallback = () => {
    const m = monogram(c.channel, c.key || c.channel_id);
    if (size) { m.style.width = m.style.height = `${size}px`; }
    return m;
  };
  if (!c.avatar) return fallback();

  const img = el('img', 'avatar');
  img.src = c.avatar;
  img.alt = '';
  img.loading = 'lazy';
  img.decoding = 'async';
  if (size) { img.width = img.height = size; img.style.width = img.style.height = `${size}px`; }
  img.addEventListener('error', () => img.replaceWith(fallback()), { once: true });
  return img;
}

const SVG = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {
  const n = document.createElementNS(SVG, tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, String(v)));
  return n;
}

/* Donut showing extracted / total for a channel. */
function ring(pct) {
  const box = el('div', 'ring');
  const r = 18, c = 2 * Math.PI * r;
  const svg = svgEl('svg', { width: 44, height: 44, viewBox: '0 0 44 44' });
  const common = { cx: 22, cy: 22, r, fill: 'none', 'stroke-width': 4 };
  svg.append(svgEl('circle', { ...common, class: 'trk' }));
  svg.append(svgEl('circle', {
    ...common, class: 'val', 'stroke-linecap': 'round',
    'stroke-dasharray': c.toFixed(1),
    'stroke-dashoffset': (c * (1 - pct / 100)).toFixed(1),
  }));
  box.append(svg, el('b', null, `${pct}%`));
  box.setAttribute('role', 'img');
  box.setAttribute('aria-label', `추출 진행률 ${pct}%`);
  return box;
}

/*
 * Row actions.
 *
 * These were icon-only and revealed on hover, which made the primary action
 * unreadable: a page glyph looks like "copy", and an action you cannot see is
 * not an affordance. The reading action now carries its label at all times.
 */
function icon(kind) {
  const svg = svgEl('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
  if (kind === 'read') {
    // Open book, distinct from any document/copy glyph.
    svg.append(svgEl('path', {
      d: 'M12 7c-1.7-1.4-4.3-1.9-7-1.4v12c2.7-.5 5.3 0 7 1.4 1.7-1.4 4.3-1.9 7-1.4v-12c-2.7-.5-5.3 0-7 1.4z',
      fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    }));
    svg.append(svgEl('path', {
      d: 'M12 7v12', fill: 'none', stroke: 'currentColor',
      'stroke-width': 1.7, 'stroke-linecap': 'round',
    }));
  } else {
    // Player frame with a filled play head: reads as "video" at small sizes.
    svg.append(svgEl('rect', {
      x: 2.5, y: 5, width: 19, height: 14, rx: 4,
      fill: 'none', stroke: 'currentColor', 'stroke-width': 1.7,
    }));
    svg.append(svgEl('path', { d: 'M10.5 9.2l5.2 2.8-5.2 2.8z', fill: 'currentColor' }));
  }
  return svg;
}

function actionBtn(kind, label, href, newTab) {
  const a = el('a', `rowbtn ${kind}`);
  a.href = href;
  a.title = label;
  if (newTab) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
  a.append(icon(kind), el('span', null, label));
  return a;
}

function skeleton(host, rows, cls) {
  host.textContent = '';
  const box = el('div', 'skel');
  for (let i = 0; i < rows; i++) box.append(el('div', `skel-row ${cls || ''}`));
  host.append(box);
}

/* Highlight every occurrence of needle inside text, without using innerHTML. */
function highlight(container, text, needle) {
  container.textContent = '';
  if (!needle) { container.textContent = text; return 0; }
  const low = text.toLowerCase(), n = needle.toLowerCase();
  let i = 0, from = 0, count = 0;
  while ((i = low.indexOf(n, from)) !== -1) {
    if (i > from) container.append(text.slice(from, i));
    const mk = el('mark', null, text.slice(i, i + needle.length));
    mk.dataset.hit = String(count);
    container.append(mk);
    from = i + needle.length;
    count++;
  }
  container.append(text.slice(from));
  return count;
}

/* ── router ─────────────────────────────────────────────── */
const VIEWS = ['library', 'add', 'channel', 'video', 'search', 'archived', 'article'];

/*
 * Scroll memory.
 *
 * This is a hash-routed single page: every view shares one scrolling document,
 * so leaving a long channel list to read a video and coming back used to snap
 * to the top. We remember scrollY per hash when the hash changes, then restore
 * it once the destination view has rendered. A hash we have never parked at
 * (a fresh forward navigation) simply lands at the top.
 */
const scrollMem = new Map();
let curHash = null;

function restoreScroll(h) {
  const y = scrollMem.get(h) || 0;
  // Two frames: one for the view swap, one for the freshly appended rows to lay
  // out, so the target offset actually exists before we jump to it.
  requestAnimationFrame(() =>
    requestAnimationFrame(() => window.scrollTo(0, y)));
}

function show(name) {
  VIEWS.forEach((v) => { $('#v-' + v).hidden = v !== name; });
  $$('[data-nav]').forEach((a) =>
    a.classList.toggle('on', a.dataset.nav === name));
}

async function route() {
  const h = location.hash.replace(/^#/, '') || '/';
  const [, head, arg] = h.match(/^\/([^/?]*)\/?([^?]*)/) || [, '', ''];
  let ok = true;
  try {
    if (head === '' ) { show('library'); await loadLibrary(); }
    else if (head === 'add') { show('add'); }
    else if (head === 'channel' && arg) { show('channel'); await loadChannel(arg); }
    else if (head === 'video' && arg) { show('video'); await loadReader(arg); }
    else if (head === 'search') {
      show('search');
      const q = new URLSearchParams(h.split('?')[1] || '').get('q') || '';
      await runSearch(q);
    }
    else if (head === 'archived') { show('archived'); await loadArchived(); }
    else if (head === 'article' && arg) { show('article'); await loadArticle(arg); }
    else { ok = false; location.hash = '#/'; }
  } catch (e) {
    ok = false;
    toast(e.message, true);
  }
  // Only track and restore for a view that actually rendered. Restoring after
  // the awaits above means the content is in the DOM and tall enough to reach
  // the saved offset.
  if (ok) { curHash = h; restoreScroll(h); }
}

/* ── library ────────────────────────────────────────────── */
async function loadLibrary() {
  const grid = $('#chan-grid');
  if (!grid.children.length) skeleton(grid, 6, 'skel-card');

  const d = await api('/api/library');
  const t = d.totals;

  const box = $('#totals');
  box.textContent = '';
  [['채널', t.channels], ['목록 영상', t.listed],
   ['추출', t.extracted], ['정리본', t.cleaned ?? 0],
   ['모은 단어', t.words]]
    .forEach(([k, v]) => {
      const c = el('div', 'stat');
      c.append(el('div', 'sv', commas(v)), el('div', 'sk', k));
      box.append(c);
    });

  grid.textContent = '';
  d.channels.forEach((c) => {
    const a = el('a', 'ccard');
    a.href = `#/channel/${encodeURIComponent(c.key)}`;
    a.append(channelFace(c));

    const body = el('div', 'cbody');
    body.append(el('div', 'cname', c.channel));
    const bits = [`${commas(c.extracted)} / ${commas(c.total)}`];
    if (c.cleaned) bits.push(`정리본 ${c.cleaned}`);
    if (c.words) bits.push(`${commas(c.words)}단어`);
    body.append(el('div', 'cstat', bits.join(' · ')));
    a.append(body);

    a.append(ring(c.total ? Math.round((c.extracted / c.total) * 100) : 0));
    grid.append(a);
  });
  $('#chan-none').hidden = d.channels.length > 0;

  $('#loose-wrap').hidden = d.loose.length === 0;
  $('#loose-n').textContent = d.loose.length ? `${d.loose.length}개` : '';
  const ll = $('#loose-list');
  ll.textContent = '';
  d.loose.forEach((v) => ll.append(looseRow(v)));
}

function looseRow(v) {
  const card = el('div', 'card');
  card.append(el('div', 'pick'));
  const img = el('img');
  img.src = v.thumb; img.alt = ''; img.loading = 'lazy';
  const mid = el('div');
  const a = el('a', 'title link-plain', v.title);
  a.href = `#/video/${v.id}`;
  mid.append(a);
  mid.append(el('div', 'meta',
    `${v.channel || '채널 미확인'} · ${commas(v.words)}단어 · ${fmtDur(v.duration)}`));
  const right = el('div', 'right');
  right.append(el('span', 'badge local', '추출됨'));
  if (v.clean) right.append(el('span', 'badge clean', '정리본'));
  if (v.article) right.append(el('span', 'badge article', '읽을거리'));
  const acts = el('div', 'acts');
  acts.append(actionBtn('read', '읽기', `#/video/${v.id}`));
  acts.append(actionBtn('yt', '유튜브',
    `https://www.youtube.com/watch?v=${v.id}`, true));
  right.append(acts);
  card.append(img, mid, right);
  return card;
}

/* ── channel detail ─────────────────────────────────────── */
async function loadChannel(key) {
  if (S.key !== key) skeleton($('#list'), 8);
  const blob = await api(`/api/channel/${encodeURIComponent(key)}`);
  S.key = key;
  S.channel = blob;
  S.results = {};                        // clear before isDone() is consulted
  S.videos = blob.videos || [];
  S.picked = new Set(
    S.videos.filter((v) => v.checked && !isDone(v)).map((v) => v.id));

  const face = $('#cd-face');
  face.textContent = '';
  face.append(channelFace({ ...blob, key }, 52));

  $('#cd-name').textContent = blob.channel || key;
  const bits = [];
  if (blob.followers) bits.push(`구독자 ${fmtNum(blob.followers)}`);
  bits.push(`영상 ${S.videos.length}개`);
  const done = S.videos.filter((v) => v.local).length;
  bits.push(`추출 ${done}개`);
  if (blob.listed_at) bits.push(`${blob.listed_at} 기준`);
  $('#cd-info').textContent = bits.join(' · ');

  $$('.tabbtn').forEach((b) => b.classList.toggle('on', b.dataset.ct === S.ctab));
  S.limit = PAGE;
  renderChannel();
  await renderChannelArticles(key);
  await resumeJob();
}

/*
 * 읽을거리(기획글) that belong to this channel, shown as a small section above
 * the video list. Clicking one opens the article page.
 */
async function renderChannelArticles(key) {
  const wrap = $('#cd-articles');
  const list = $('#cd-articles-list');
  list.textContent = '';
  let arts = [];
  try { arts = (await api('/api/articles')).articles || []; } catch { arts = []; }
  const mine = arts.filter((a) => a.channel_key === key);
  wrap.hidden = mine.length === 0;
  $('#cd-articles-n').textContent = mine.length || '';
  // Collapse once there are enough articles to bury the video list; a channel
  // with one or two stays open so the section is not easy to miss.
  wrap.open = mine.length > 0 && mine.length <= 2;
  mine.forEach((a) => {
    const card = el('a', 'art-card');
    card.href = `#/article/${encodeURIComponent(a.slug)}`;
    card.append(el('span', 'art-ico', '📄'));
    const body = el('div', 'art-card-body');
    body.append(el('div', 'art-card-title', a.title));
    const sub = [];
    if (a.subtitle) sub.push(a.subtitle);
    if (a.related?.length) sub.push(`연관 영상 ${a.related.length}개`);
    if (sub.length) body.append(el('div', 'art-card-sub muted small', sub.join(' · ')));
    card.append(body);
    list.append(card);
  });
}

/*
 * Re-attach to a run that is already in flight.
 *
 * Extraction lives on the server, so a browser reload or a trip to another
 * screen leaves it going. Without this the page would look idle while hundreds
 * of videos were still being fetched.
 */
async function resumeJob() {
  if (STATIC || S.poll) return;
  let job;
  try { job = await api('/api/job'); } catch { return; }
  if (job.status !== 'running' || job.kind !== 'extract') return;
  $('#run-prog').hidden = false;
  $('#log-box').hidden = false;
  $('#btn-cancel').hidden = false;
  $('#btn-run').disabled = true;
  poll(extractDone, extractTick);
}

/* Members-only videos can never be extracted, so they are not "미추출". */
const isMembers = (v) => v.access === 'members' && !isDone(v);

function ctabCounts() {
  const done = S.videos.filter(isDone).length;
  const members = S.videos.filter(isMembers).length;
  return {
    all: S.videos.length,
    done,
    todo: S.videos.length - done - members,
    members,
    clean: S.videos.filter((v) => v.clean).length,
  };
}

function visible() {
  const q = $('#q').value.trim().toLowerCase();
  const kind = $('#ftab').value;
  let rows = S.videos.filter((v) => {
    if (S.ctab === 'done' && !isDone(v)) return false;
    if (S.ctab === 'todo' && (isDone(v) || isMembers(v))) return false;
    if (S.ctab === 'members' && !isMembers(v)) return false;
    if (S.ctab === 'clean' && !v.clean) return false;
    if (q && !v.title.toLowerCase().includes(q)) return false;
    if (kind !== 'all' && v.tab !== kind) return false;
    return true;
  });
  const by = $('#sort').value;
  const n = (x) => (x == null ? -1 : x);
  if (by === 'oldest') rows = rows.slice().reverse();
  else if (by === 'views') rows = rows.slice().sort((a, b) => n(b.views) - n(a.views));
  else if (by === 'longest') rows = rows.slice().sort((a, b) => n(b.duration) - n(a.duration));
  else if (by === 'shortest') rows = rows.slice().sort((a, b) => n(a.duration) - n(b.duration));
  else if (by === 'title') rows = rows.slice().sort((a, b) => a.title.localeCompare(b.title, 'ko'));
  return rows;
}

function renderChannel() {
  const counts = ctabCounts();
  $$('.tabbtn').forEach((b) => {
    b.querySelector('span').textContent = counts[b.dataset.ct];
  });
  // Selecting and extracting only make sense where something is unextracted.
  const picking = S.ctab !== 'done' && S.ctab !== 'clean';
  $('#pickbar').hidden = !picking;
  $('#runbar').hidden = !picking;

  const rows = visible();
  const list = $('#list');
  list.textContent = '';
  $('#empty').hidden = rows.length > 0;
  if (!rows.length) {
    $('#empty').textContent = S.ctab === 'clean'
      ? '이 채널에는 아직 정리본이 없습니다. 영상을 열어 Kiro에 요청하면 만들어집니다.'
      : '조건에 맞는 영상이 없습니다.';
  }

  /*
   * Draw a window, not the whole channel. A full listing can run to thousands
   * of videos (침착맨 has 6,659) and this function re-runs on every keystroke in
   * the filter box and on every progress tick, so rendering all of them made
   * typing stutter.
   */
  const shown = rows.slice(0, S.limit);
  const frag = document.createDocumentFragment();
  shown.forEach((v) => frag.append(videoRow(v)));
  list.append(frag);

  if (rows.length > shown.length) {
    const more = el('button', 'ghost showmore');
    const left = rows.length - shown.length;
    more.textContent = `${commas(Math.min(left, PAGE))}개 더 보기 (남은 ${commas(left)}개)`;
    more.addEventListener('click', () => { S.limit += PAGE; renderChannel(); });
    list.append(more);
  }
  updateCount();
}

/*
 * Single source of truth for "this video already has a transcript".
 *
 * v.local only refreshes when the channel is reloaded, which during a long run
 * does not happen until the job ends. Consulting the live job results as well
 * means a video stops being selectable the moment it finishes, not minutes or
 * hours later.
 */
function isDone(v) {
  if (v.local) return true;
  const r = S.results[v.id];
  return !!r && (r.state === 'ok' || r.state === 'cached');
}

function videoRow(v) {
  const res = S.results[v.id];
  const done = isDone(v);
  const locked = isMembers(v);
  const card = el('div', 'card'
    + (S.picked.has(v.id) ? ' sel' : '')
    + (locked ? ' locked' : ''));

  const pick = el('div', 'pick');
  if (!done && !locked && !STATIC) {
    const cb = el('input');
    cb.type = 'checkbox';
    cb.id = `cb-${v.id}`;
    cb.checked = S.picked.has(v.id);
    cb.addEventListener('change', () => {
      if (cb.checked) S.picked.add(v.id); else S.picked.delete(v.id);
      card.classList.toggle('sel', cb.checked);
      updateCount();
      saveSelection();
    });
    pick.append(cb);
  } else if (locked) {
    // A disabled box explains "not available" better than a blank cell does.
    const cb = el('input');
    cb.type = 'checkbox';
    cb.disabled = true;
    cb.title = '멤버십 전용 영상이라 자막을 가져올 수 없습니다';
    pick.append(cb);
  }
  card.append(pick);

  const img = el('img');
  img.src = v.thumb; img.alt = ''; img.loading = 'lazy';
  card.append(img);

  const mid = el('div');
  if (done) {
    const a = el('a', 'title link-plain', v.title);
    a.href = `#/video/${v.id}`;
    mid.append(a);
  } else {
    const lab = el('label', 'title', v.title);
    lab.htmlFor = `cb-${v.id}`;
    mid.append(lab);
  }
  const metaBits = [fmtDur(v.duration), `조회 ${fmtNum(v.views)}`];
  const vdate = fmtDate(v.local_date);
  if (vdate) metaBits.push(vdate);
  mid.append(el('div', 'meta', metaBits.join(' · ')));
  card.append(mid);

  const right = el('div', 'right');
  if (v.tab !== 'videos') {
    right.append(el('span', 'badge ' + (v.tab === 'shorts' ? 'shorts' : 'live'),
      v.tab === 'shorts' ? '쇼츠' : '라이브'));
  }
  if (res) {
    const label = { ok: '추출 완료', cached: '이미 있음',
                    no_captions: '자막 없음', error: '실패' }[res.state] || res.state;
    const b = el('span', 'badge ' + res.state, label);
    b.title = res.detail || '';
    right.append(b);
  } else if (v.local) {
    right.append(el('span', 'badge local',
      v.local_words ? `${commas(v.local_words)}단어` : '추출됨'));
  }
  if (v.clean) right.append(el('span', 'badge clean', '정리본'));
  if (v.article) right.append(el('span', 'badge article', '읽을거리'));

  if (locked) {
    const b = el('span', 'badge members', '멤버십 전용');
    b.title = '구독 멤버십 회원에게만 공개된 영상입니다. 자막을 가져올 수 없습니다.';
    right.append(b);
  } else if (!done && v.fail === 'no_captions') {
    const b = el('span', 'badge nocap', '자막 없음');
    b.title = '이 영상에는 자막이 없습니다. 나중에 생길 수 있어 다시 시도할 수 있습니다.';
    right.append(b);
    const again = el('button', 'link');
    again.type = 'button';
    again.textContent = '다시 시도';
    if (STATIC) again.hidden = true;
    again.addEventListener('click', async () => {
      try {
        await jpost(`/api/retry/${v.id}`, {});
        v.fail = '';
        renderChannel();
        toast('다시 시도할 수 있습니다. 체크해서 추출하세요.');
      } catch (e) { toast(e.message, true); }
    });
    right.append(again);
  } else if (!done && v.fail === 'error') {
    const b = el('span', 'badge error', '실패');
    b.title = '추출 중 오류가 났습니다. 다시 시도해보세요.';
    right.append(b);
  }

  const acts = el('div', 'acts');
  if (done) acts.append(actionBtn('read', '읽기', `#/video/${v.id}`));
  acts.append(actionBtn('yt', '유튜브',
    `https://www.youtube.com/watch?v=${v.id}`, true));
  right.append(acts);
  card.append(right);
  return card;
}

function updateCount() {
  const n = visible().length;
  const drawn = Math.min(n, S.limit);
  const shown = drawn < n ? `${commas(drawn)} / ${commas(n)}개 표시`
                          : `${commas(n)}개 표시`;
  $('#count').textContent = `${commas(S.picked.size)}개 선택 · ${shown}`;
  $('#btn-run').disabled = S.picked.size === 0;
}

let saveTimer;
function saveSelection() {
  if (STATIC || !S.key) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    jpost('/api/select', { key: S.key, ids: [...S.picked] }).catch(() => {});
  }, 600);
}

/* ── reader ─────────────────────────────────────────────── */
async function loadReader(vid) {
  if (S.reader?.id !== vid) {
    $('#rd-body').textContent = '';
    skeleton($('#rd-body'), 5);
  }
  const d = await api(`/api/video/${encodeURIComponent(vid)}`);
  S.reader = d;
  $('#rd-title').textContent = d.title;
  const bits = [d.channel, `${commas(d.words)}단어`, fmtDur(d.duration)];
  const up = fmtDate(d.upload_date);
  if (up) bits.push(`업로드 ${up}`);
  if (d.caption_kind) bits.push(d.caption_kind);
  bits.push(d.path);
  $('#rd-meta').textContent = bits.filter(Boolean).join(' · ');
  $('#rd-yt').href = `https://www.youtube.com/watch?v=${d.id}`;
  $('#rd-back').href = d.channel_key ? `#/channel/${d.channel_key}` : '#/';
  $('#rd-back').textContent = d.channel_key ? `← ${d.channel}` : '← 라이브러리';

  // Linked 읽을거리(아티클): a labelled strip of chips above the reading
  // controls. Scales to any number — chips wrap to the next line.
  const readsWrap = $('#rd-reads');
  const readsList = $('#rd-reads-list');
  readsList.textContent = '';
  const arts = d.articles || [];
  readsWrap.hidden = arts.length === 0;
  if (arts.length) {
    $('#rd-reads-head').textContent =
      arts.length > 1 ? `📄 관련 읽을거리 ${arts.length}` : '📄 관련 읽을거리';
    arts.forEach((a) => {
      const chip = el('a', 'read-chip');
      chip.href = `#/article/${encodeURIComponent(a.slug)}`;
      chip.append(el('span', 'read-chip-ico', '📄'));
      chip.append(el('span', 'read-chip-title', a.title));
      chip.append(el('span', 'read-chip-arrow', '›'));
      readsList.append(chip);
    });
  }

  renderShots(d.frames || []);

  $('#rd-find').value = '';
  $('#rd-hits').textContent = '';
  $('#rd-orig').checked = false;
  paintReader('');
  await loadClean(d.id);          // sets S.clean, then applyMode uses it
}

/* Captured stills: a labelled thumbnail grid. The online AI picks the moments
 * (shots.json), the local machine grabs the frames (ytframes.py); here we just
 * show whatever was captured. Click a thumb to see it full-size. */
function renderShots(frames) {
  const wrap = $('#rd-shots');
  const grid = $('#rd-shots-grid');
  grid.textContent = '';
  wrap.hidden = !frames.length;
  if (!frames.length) return;
  $('#rd-shots-head').textContent =
    frames.length > 1 ? `🖼️ 캡쳐한 화면 ${frames.length}` : '🖼️ 캡쳐한 화면';
  frames.forEach((f, i) => {
    const fig = el('figure', 'shot');
    const img = el('img');
    img.src = f.url; img.alt = f.label || f.t; img.loading = 'lazy';
    fig.append(img);
    const cap = el('figcaption', 'shot-cap');
    if (f.t) cap.append(el('span', 'shot-t', f.t));
    if (f.label) cap.append(el('span', 'shot-label', f.label));
    fig.append(cap);
    fig.addEventListener('click', () => openLightbox(frames, i));
    grid.append(fig);
  });
}

// Lightbox holds the whole frame list so arrow keys / buttons can page through.
let _lbFrames = [];
let _lbIdx = 0;

function openLightbox(frames, idx) {
  _lbFrames = frames || [];
  _lbIdx = idx || 0;
  paintLightbox();
  $('#lightbox').hidden = false;
}
function paintLightbox() {
  const n = _lbFrames.length;
  if (!n) return;
  const f = _lbFrames[_lbIdx];
  $('#lightbox-img').src = f.url;
  const bits = [`${_lbIdx + 1} / ${n}`, f.t, f.label].filter(Boolean);
  $('#lightbox-cap').textContent = bits.join('  ·  ');
  const one = n <= 1;
  $('#lb-prev').hidden = one;
  $('#lb-next').hidden = one;
  // Clamp, not wrap: grey out the arrow once you reach an end.
  $('#lb-prev').disabled = _lbIdx === 0;
  $('#lb-next').disabled = _lbIdx === n - 1;
}
function lbStep(delta) {
  const n = _lbFrames.length;
  const next = _lbIdx + delta;
  if (next < 0 || next >= n) return;   // stop at both ends, no wrap
  _lbIdx = next;
  paintLightbox();
}
function closeLightbox() {
  $('#lightbox').hidden = true;
  $('#lightbox-img').src = '';
}

/*
 * Decide what the reader shows.
 *
 * A 정리본 is what you actually came to read, so it owns the column and 원본 is
 * a checkbox away. Without one there is nothing to prefer, so 원본 takes the
 * column and the "how to make one" hint sits above it instead of beside it.
 */
function readerMode() {
  if (!S.clean?.exists) return 'orig';
  return $('#rd-orig').checked ? 'both' : 'clean';
}

function applyMode() {
  const mode = readerMode();
  const hasClean = !!S.clean?.exists;

  $('#rd-split-wrap').dataset.mode = mode;
  $('#rd-orig-wrap').hidden = !hasClean;      // nothing to toggle without one
  $('#rd-orig-pane').hidden = mode === 'clean';
  $('#rd-clean-pane').hidden = false;

  // Keep find pointed at whatever is on screen.
  const target = hasClean ? '정리본' : '원본';
  $('#rd-find').placeholder = `${target}에서 찾기`;
  const q = $('#rd-find').value.trim();
  if (q) runFind(q); else $('#rd-hits').textContent = '';
}

function paintReader(needle) {
  const body = $('#rd-body');
  body.textContent = '';
  const paras = (S.reader?.text || '').split(/\n{2,}/);
  let hits = 0;
  paras.forEach((p) => {
    const t = p.replace(/\n/g, ' ').trim();
    if (!t) return;
    const node = el('p');
    hits += highlight(node, t, needle);
    body.append(node);
  });
  return hits;
}

/*
 * Wrap matches inside already-rendered markup. The 정리본 is a node tree by the
 * time we search it, so we cannot rebuild it from a string the way the plain
 * transcript is rebuilt.
 */
function markWithin(root, needle) {
  if (!needle) return 0;
  const n = needle.toLowerCase();
  let hits = 0;
  const walk = (node) => {
    for (const child of [...node.childNodes]) {
      if (child.nodeType === Node.TEXT_NODE) {
        const text = child.nodeValue;
        const low = text.toLowerCase();
        if (!low.includes(n)) continue;
        const frag = document.createDocumentFragment();
        let from = 0, i;
        while ((i = low.indexOf(n, from)) !== -1) {
          if (i > from) frag.append(text.slice(from, i));
          frag.append(el('mark', null, text.slice(i, i + needle.length)));
          from = i + needle.length;
          hits++;
        }
        frag.append(text.slice(from));
        child.replaceWith(frag);
      } else if (child.nodeType === Node.ELEMENT_NODE && child.tagName !== 'MARK') {
        walk(child);
      }
    }
  };
  walk(root);
  return hits;
}

function runFind(needle) {
  const onClean = !!S.clean?.exists;
  let hits;
  if (onClean) {
    const body = $('#cl-body');
    renderMarkdown(body, S.clean.text);      // reset, then mark
    hits = markWithin(body, needle);
  } else {
    hits = paintReader(needle);
  }
  const host = onClean ? $('#cl-body') : $('#rd-body');
  $('#rd-hits').textContent = needle
    ? (hits ? `${hits}건 일치` : '일치 없음') : '';
  if (needle && hits) {
    host.querySelector('mark')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
}

async function writeClipboard(text, okMsg) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg || `복사했습니다 · ${commas(text.length)}자`);
  } catch {
    // Clipboard API needs a secure context; fall back to a hidden selection.
    const ta = el('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    toast(ok ? (okMsg || '복사했습니다') : '복사에 실패했습니다.', !ok);
  }
}

/* ── markdown ───────────────────────────────────────────── */
/*
 * Renders the subset the cleanup prompt actually emits: ## / ### headings,
 * **bold**, `code`, > quotes, - and 1. lists, and pipe tables. Built with DOM
 * nodes rather than innerHTML so model output can never inject markup.
 */
function inline(parent, text) {
  const re = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) {
      parent.append(el('strong', null, tok.slice(2, -2)));
    } else if (tok.startsWith('*')) {
      parent.append(el('em', null, tok.slice(1, -1)));
    } else if (tok.startsWith('`')) {
      parent.append(el('code', null, tok.slice(1, -1)));
    } else if (tok.startsWith('[')) {
      // [label](url)
      const lb = tok.indexOf('](');
      const label = tok.slice(1, lb);
      const url = tok.slice(lb + 2, -1);
      const a = el('a', null, label);
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      parent.append(a);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

function renderMarkdown(container, md) {
  container.textContent = '';
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  let i = 0;

  const isRow = (s) => /^\s*\|.*\|\s*$/.test(s);
  const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // Fenced code block ``` ... ``` — kept verbatim in a <pre> so ASCII
    // diagrams (whitespace alignment, line breaks) survive intact.
    if (/^\s*```/.test(line)) {
      i++;
      const buf = [];
      while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
      if (i < lines.length) i++;                 // skip the closing fence
      const pre = el('pre', 'codeblock');
      pre.textContent = buf.join('\n');
      container.append(pre);
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const node = el('h' + Math.min(h[1].length + 1, 5));
      inline(node, h[2]);
      container.append(node);
      i++;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { container.append(el('hr')); i++; continue; }

    if (line.startsWith('>')) {
      const q = el('blockquote');
      while (i < lines.length && lines[i].startsWith('>')) {
        const p = el('p');
        inline(p, lines[i].replace(/^>\s?/, ''));
        q.append(p);
        i++;
      }
      container.append(q);
      continue;
    }

    // table: header row, separator, then body rows
    if (isRow(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const table = el('table');
      const thead = el('thead'), hr = el('tr');
      cells(line).forEach((c) => { const th = el('th'); inline(th, c); hr.append(th); });
      thead.append(hr);
      table.append(thead);
      i += 2;
      const tbody = el('tbody');
      while (i < lines.length && isRow(lines[i])) {
        const tr = el('tr');
        cells(lines[i]).forEach((c) => { const td = el('td'); inline(td, c); tr.append(td); });
        tbody.append(tr);
        i++;
      }
      table.append(tbody);
      container.append(table);
      continue;
    }

    const bullet = /^\s*[-*+]\s+/, numbered = /^\s*\d+[.)]\s+/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const list = el(ordered ? 'ol' : 'ul');
      while (i < lines.length &&
             (bullet.test(lines[i]) || numbered.test(lines[i]))) {
        const li = el('li');
        inline(li, lines[i].replace(ordered ? numbered : bullet, ''));
        list.append(li);
        i++;
      }
      container.append(list);
      continue;
    }

    // paragraph: consume until a blank line or the start of another block
    const buf = [];
    while (i < lines.length && lines[i].trim() &&
           !/^(#{1,4}\s|>|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[i]) &&
           !isRow(lines[i])) {
      buf.push(lines[i].trim());
      i++;
    }
    if (buf.length) {
      const p = el('p');
      inline(p, buf.join(' '));
      container.append(p);
    }
  }
}

/* ── 정리본 ─────────────────────────────────────────────── */
async function loadClean(vid) {
  const pane = $('#rd-clean-pane');
  const body = $('#cl-body'), empty = $('#cl-empty');
  $('#cl-pastebox').hidden = true;
  $('#cl-text').value = '';

  let d;
  try { d = await api(`/api/clean/${encodeURIComponent(vid)}`); }
  catch { d = { exists: false }; }
  S.clean = d;
  await fillPrompts();

  if (d.exists) {
    empty.hidden = true;
    body.hidden = false;
    renderMarkdown(body, d.text);
    $('#cl-meta').textContent =
      `${commas(d.chars)}자 · ${d.prompt || 'cleanup'} · ${d.generated || ''}`;
    $('#cl-copy').hidden = false;
    $('#cl-drop').hidden = false;
  } else {
    empty.hidden = false;
    body.hidden = true;
    body.textContent = '';
    $('#cl-meta').textContent = '';
    $('#cl-copy').hidden = true;
    $('#cl-drop').hidden = true;
    $('#cl-ask').textContent = `"${vid} 정리본 만들어줘"`;
    await fillPrompts();
  }
  pane.hidden = false;
  applyMode();
}

async function fillPrompts() {
  const sel = $('#cl-prompt');
  if (sel.options.length) return;
  try {
    const { prompts } = await api('/api/prompts');
    prompts.forEach((p) => {
      const o = el('option', null, p.name);
      o.value = p.key;
      sel.append(o);
    });
  } catch { /* prompt list is optional */ }
}

async function copyPromptAndText() {
  const name = $('#cl-prompt').value || 'cleanup';
  try {
    const p = await api(`/api/prompt/${encodeURIComponent(name)}`);
    const payload = `${p.text}\n\n---\n\n# ${S.reader.title}\n채널: ${S.reader.channel}\n\n${S.reader.text}`;
    await writeClipboard(payload, `프롬프트+본문 복사 · ${commas(payload.length)}자`);
  } catch (e) { toast(e.message, true); }
}

async function saveClean() {
  const text = $('#cl-text').value.trim();
  if (text.length < 50) { toast('내용이 너무 짧습니다.', true); return; }
  try {
    await jpost(`/api/clean/${encodeURIComponent(S.reader.id)}`,
                { text, prompt: $('#cl-prompt').value || 'cleanup' });
    toast('정리본을 저장했습니다.');
    await loadClean(S.reader.id);
  } catch (e) { toast(e.message, true); }
}

async function dropClean() {
  if (!confirm('이 정리본을 삭제할까요? 원본 스크립트는 그대로 남습니다.')) return;
  try {
    await api(`/api/clean/${encodeURIComponent(S.reader.id)}`, { method: 'DELETE' });
    toast('삭제했습니다.');
    await loadClean(S.reader.id);
  } catch (e) { toast(e.message, true); }
}

/* ── search ─────────────────────────────────────────────── */
async function runSearch(q) {
  $('#gq').value = q;
  $('#sr-head').textContent = q ? `"${q}" 검색 결과` : '검색';
  const list = $('#sr-list');
  list.textContent = '';
  $('#sr-none').hidden = true;
  if (!q) { $('#sr-info').textContent = ''; return; }

  $('#sr-info').textContent = '검색 중...';
  let d;
  try {
    d = await api('/api/search?q=' + encodeURIComponent(q));
  } catch (e) {
    $('#sr-info').textContent = '';
    $('#sr-none').hidden = false;
    $('#sr-none').textContent = e.message;
    return;
  }
  $('#sr-info').textContent =
    `스크립트 ${d.searched}개 중 ${d.videos}개에서 ${commas(d.hits)}건 발견`;
  if (!d.results.length) {
    $('#sr-none').hidden = false;
    $('#sr-none').textContent = '일치하는 내용이 없습니다.';
    return;
  }
  d.results.forEach((r) => {
    const box = el('div', 'hit');
    const head = el('div', 'hithead');
    const a = el('a', 'link-plain strong', r.title);
    a.href = `#/video/${r.id}`;
    head.append(a);
    head.append(el('span', 'muted small',
      `${r.channel || ''} · ${r.hits}건`));
    box.append(head);

    r.snippets.forEach((sn) => {
      const p = el('p', 'snip');
      p.append(sn.before);
      p.append(el('mark', null, sn.match));
      p.append(sn.after);
      box.append(p);
    });
    if (r.more) box.append(el('p', 'muted small', `... 이 영상에 ${r.more}건 더`));
    list.append(box);
  });
}

/* ── channel listing job ────────────────────────────────── */
// Pull an 11-char video id out of a bare id or any common YouTube URL shape.
// Returns null for channel handles/URLs, which fall through to channel listing.
function parseVideoId(t) {
  t = (t || '').trim();
  if (/^[A-Za-z0-9_-]{11}$/.test(t)) return t;
  const pats = [
    /[?&]v=([A-Za-z0-9_-]{11})/, /youtu\.be\/([A-Za-z0-9_-]{11})/,
    /\/shorts\/([A-Za-z0-9_-]{11})/, /\/embed\/([A-Za-z0-9_-]{11})/,
    /\/live\/([A-Za-z0-9_-]{11})/,
  ];
  for (const p of pats) { const m = t.match(p); if (m) return m[1]; }
  return null;
}

// Switch between the 채널 추가 / 영상 개별 추가 tabs on the add page.
function showAddTab(which) {
  const isVid = which === 'video';
  $('#tab-add-ch').classList.toggle('on', !isVid);
  $('#tab-add-vid').classList.toggle('on', isVid);
  $('#add-ch-panel').hidden = isVid;
  $('#add-vid-panel').hidden = !isVid;
}

// Extract a single video directly (no channel listing). Reuses /api/extract.
async function doExtractOne(vid) {
  $('#btn-vadd').disabled = true;
  $('#vadd-prog').hidden = false;
  $('#vadd-bar').style.width = '15%';
  $('#vadd-text').textContent = '영상 자막을 가져오는 중...';
  try {
    await jpost('/api/extract', { ids: [vid], lang: 'auto' });
    poll(async (job) => {
      $('#btn-vadd').disabled = false;
      $('#vadd-bar').style.width = '100%';
      const r = (job.results || {})[vid] || {};
      $('#vadd-text').textContent = job.message || '';
      if (r.state === 'ok' || r.state === 'cached') {
        toast('영상 추출 완료');
        location.hash = `#/video/${encodeURIComponent(vid)}`;
      } else if (r.state === 'no_captions') {
        toast('이 영상에는 자막이 없습니다.', true);
      } else {
        toast(job.message || r.detail || '추출에 실패했습니다.', true);
      }
    }, (job) => {
      $('#vadd-text').textContent = `영상 자막을 가져오는 중... ${job.elapsed}초`;
    });
  } catch (e) {
    $('#btn-vadd').disabled = false;
    $('#vadd-prog').hidden = true;
    toast(e.message, true);
  }
}

async function doVideoAdd() {
  const raw = $('#vid-target').value.trim();
  if (!raw) { toast('영상 URL 또는 ID를 입력하세요.', true); return; }
  const vid = parseVideoId(raw);
  if (!vid) { toast('영상 URL 또는 11자리 영상 ID를 정확히 입력하세요.', true); return; }
  doExtractOne(vid);
}

async function doLoad() {
  const target = $('#target').value.trim();
  if (!target) { toast('채널을 입력하세요.', true); return; }
  // Forgiving: if a video URL/ID was pasted into the channel box, jump to the
  // 영상 개별 추가 tab and extract it there instead of failing as a channel.
  const vid = parseVideoId(target);
  if (vid) {
    showAddTab('video');
    $('#vid-target').value = target;
    doExtractOne(vid);
    return;
  }
  const tabs = $$('.tab:checked').map((c) => c.value);
  if (!tabs.length) { toast('탭을 하나 이상 선택하세요.', true); return; }

  const payload = { target, tabs, refresh: $('#refresh').checked,
                    meta_lang: $('#meta-lang').value };
  const lim = $('#limit').value.trim();
  if (lim) payload.limit = Number(lim);

  $('#btn-load').disabled = true;
  $('#load-prog').hidden = false;
  $('#load-bar').style.width = '15%';
  $('#load-text').textContent = '목록을 가져오는 중...';
  try {
    await jpost('/api/list', payload);
    poll(async (job) => {
      $('#btn-load').disabled = false;
      $('#load-bar').style.width = '100%';
      $('#load-text').textContent = job.message || '';
      if (job.status === 'error') { toast(job.message || '실패', true); return; }
      toast(job.message || '완료');
      const { channels } = await api('/api/channels');
      const needle = target.replace(/^@/, '').toLowerCase();
      const hit = channels.find((c) =>
        (c.handle || '').replace(/^@/, '').toLowerCase() === needle ||
        (c.channel || '').toLowerCase() === needle) ||
        channels.slice().sort((a, b) =>
          (b.listed_at || '').localeCompare(a.listed_at || ''))[0];
      if (hit) location.hash = `#/channel/${encodeURIComponent(hit.key)}`;
    }, (job) => {
      $('#load-text').textContent = `목록을 가져오는 중... ${job.elapsed}초`;
    });
  } catch (e) {
    $('#btn-load').disabled = false;
    $('#load-prog').hidden = true;
    toast(e.message, true);
  }
}

/* ── extraction job ─────────────────────────────────────── */
function extractTick(job) {
  S.results = job.results || {};
  // Drop finished videos from the selection as they land, so the count and the
  // saved state never claim an already-extracted video is still queued.
  let dropped = 0;
  S.videos.forEach((v) => {
    if (S.picked.has(v.id) && isDone(v)) { S.picked.delete(v.id); dropped++; }
  });
  if (dropped) saveSelection();

  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  $('#run-bar').style.width = pct + '%';
  $('#run-text').textContent =
    `${job.done} / ${job.total} 완료 · ${job.elapsed}초` +
    (job.current ? ` · 진행 중 ${job.current}` : '');
  renderChannel();
}

async function extractDone(job) {
  $('#btn-cancel').hidden = true;
  $('#btn-run').disabled = false;
  if (job.status === 'done') toast(`추출 완료 · ${job.message}`);
  else if (job.status === 'cancelled') toast(job.message || '중단했습니다.');
  else if (job.status === 'error') toast(job.message || '실패', true);
  if (!S.key) return;
  const blob = await api(`/api/channel/${encodeURIComponent(S.key)}`);
  S.videos = blob.videos || [];
  S.picked = new Set([...S.picked].filter(
    (id) => !S.videos.find((v) => v.id === id && isDone(v))));
  renderChannel();
  saveSelection();
}

async function doRun() {
  const ids = [...S.picked].filter((id) => {
    const v = S.videos.find((x) => x.id === id);
    return v && !isDone(v) && !isMembers(v);
  });
  if (!ids.length) { toast('추출할 영상이 없습니다.', true); return; }
  $('#btn-run').disabled = true;
  $('#run-prog').hidden = false;
  $('#log-box').hidden = false;
  try {
    await jpost('/api/extract', { ids, lang: $('#lang').value });
    $('#btn-cancel').hidden = false;
    poll(extractDone, extractTick);
  } catch (e) {
    $('#btn-run').disabled = false;
    toast(e.message, true);
  }
}

function poll(onDone, onTick) {
  clearInterval(S.poll);
  S.poll = setInterval(async () => {
    let job;
    try { job = await api('/api/job'); } catch { return; }
    if (job.log?.length) $('#log').textContent = job.log.join('\n');
    if (job.status === 'running') { onTick?.(job); return; }
    clearInterval(S.poll);
    S.poll = null;
    await onDone(job);
  }, 700);
}

/* ── 정리본 프롬프트 모달 ───────────────────────────────── */
let _prompts = null;

async function loadPrompts() {
  if (_prompts) return _prompts;
  try { _prompts = (await api('/api/prompts')).prompts || []; }
  catch { _prompts = []; }
  return _prompts;
}

/* Lists every active 정리본 프롬프트 (cleanup-*) in the modal dropdown. */
async function openPromptModal() {
  const all = await loadPrompts();
  const list = all.filter((p) => p.key.startsWith('cleanup'));

  const sel = $('#pm-select');
  sel.textContent = '';
  if (!list.length) {
    const o = el('option', null, '사용 가능한 프롬프트가 없습니다');
    o.value = ''; sel.append(o);
  } else {
    list.forEach((p) => {
      const o = el('option', null, p.name);
      o.value = p.key; sel.append(o);
    });
    // Opened from a channel page: default to that channel's own prompt
    // (prompts are named cleanup-<채널명>), falling back to the generic one.
    const chName = S.channel && S.channel.channel;
    if (chName) {
      const pref = list.find((p) => p.key === `cleanup-${chName}`)
        || list.find((p) => p.key.startsWith('cleanup-')
             && (p.key.slice('cleanup-'.length) === chName
                 || (p.name && p.name.includes(chName))));
      if (pref) sel.value = pref.key;
    }
  }

  $('#pm-title').textContent = '정리본 프롬프트';
  renderPromptText();
  $('#prompt-modal').hidden = false;
  document.body.classList.add('modal-open');   // lock the page behind the modal
}

function renderPromptText() {
  const p = (_prompts || []).find((x) => x.key === $('#pm-select').value);
  $('#pm-desc').textContent = p ? (p.description || '') : '';
  $('#pm-text').textContent = p ? p.text : '표시할 프롬프트가 없습니다.';
}

function closePromptModal() {
  $('#prompt-modal').hidden = true;
  document.body.classList.remove('modal-open');
}

/* ── 폐기된 목록 ─────────────────────────────────────────── */
/*
 * Retired items kept for reference. The 분석본 feature was removed, but its
 * authoring prompt is parked here so nothing is lost. Prompt text is shown
 * raw (it is meant to be copied and reused, not read as prose).
 */
async function loadArchived() {
  const host = $('#arch-list');
  host.textContent = '';
  let list;
  try { list = (await api('/api/archived')).prompts || []; }
  catch { list = []; }

  $('#arch-none').hidden = list.length > 0;
  list.forEach((p) => {
    const card = el('div', 'arch-card');

    const head = el('div', 'arch-head');
    head.append(el('h3', 'arch-title', p.name));
    head.append(el('span', 'spacer'));
    if (p.chars) head.append(el('span', 'muted small', `${commas(p.chars)}자`));
    const copy = el('button', 'ghost');
    copy.type = 'button';
    copy.textContent = '복사';
    copy.addEventListener('click', () =>
      writeClipboard(p.text, `프롬프트 복사 · ${commas(p.text.length)}자`));
    head.append(copy);
    card.append(head);

    if (p.description) card.append(el('p', 'muted small arch-desc', p.description));

    const pre = el('pre', 'reader arch-text');
    pre.textContent = p.text;
    card.append(pre);

    host.append(card);
  });
}

/* ── 읽을거리(아티클) ────────────────────────────────────── */
/*
 * A standalone long-form article. It carries a prominent "연관된 영상" card so
 * the link back to the source video is always clear, completing the two-way
 * connection (video → article button, article → related video card).
 */
async function loadArticle(slug) {
  const body = $('#ar-body');
  if (S.article !== slug) { body.textContent = ''; skeleton(body, 6); }
  S.article = slug;

  const d = await api(`/api/article/${encodeURIComponent(slug)}`);
  if (d.error) { toast('글을 찾을 수 없습니다.', true); location.hash = '#/'; return; }
  S.articleDoc = d;                 // kept so 전체 복사 can read the full text

  $('#ar-title').textContent = d.title;
  $('#ar-sub').textContent = d.subtitle || '';
  $('#ar-sub').hidden = !d.subtitle;

  const back = $('#ar-back');
  if (d.channel_key) {
    back.href = `#/channel/${encodeURIComponent(d.channel_key)}`;
    back.textContent = `← ${d.channel || '채널'}`;
  } else {
    back.href = '#/';
    back.textContent = '← 라이브러리';
  }

  // 연관된 영상 — the link back to the source video(s).
  const relWrap = $('#ar-related');
  const relList = $('#ar-related-list');
  relList.textContent = '';
  const vids = d.related_videos || [];
  relWrap.hidden = vids.length === 0;
  vids.forEach((v) => {
    const a = el('a', 'relvid');
    a.href = `#/video/${v.id}`;
    const img = el('img', 'relvid-thumb');
    img.src = v.thumb; img.alt = ''; img.loading = 'lazy';
    const info = el('div', 'relvid-info');
    info.append(el('div', 'relvid-title', v.title));
    const sub = [v.channel, '이 글과 연관된 영상'].filter(Boolean).join(' · ');
    info.append(el('div', 'relvid-sub muted small', sub));
    a.append(img, info);
    relList.append(a);
  });

  renderMarkdown(body, d.text);
}

/* ── wiring ─────────────────────────────────────────────── */
// Park the current view's scroll position before the hash flips, so pressing
// back later drops you where you left off instead of at the top.
window.addEventListener('hashchange', () => {
  if (curHash !== null) scrollMem.set(curHash, window.scrollY);
  route();
});

$('#cd-prompts').addEventListener('click', () => openPromptModal());
$('#pm-select').addEventListener('change', renderPromptText);
$('#pm-close').addEventListener('click', closePromptModal);
$('#pm-copy').addEventListener('click', () => {
  const p = (_prompts || []).find((x) => x.key === $('#pm-select').value);
  if (p) writeClipboard(p.text, `프롬프트 복사 · ${commas(p.text.length)}자`);
});
$('#prompt-modal').addEventListener('click', (e) => {
  if (e.target === $('#prompt-modal')) closePromptModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#prompt-modal').hidden) closePromptModal();
  if (!$('#lightbox').hidden) {
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowLeft') { e.preventDefault(); lbStep(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); lbStep(1); }
  }
});

// Backdrop click closes; controls and the image itself must not bubble to it.
$('#lightbox').addEventListener('click', closeLightbox);
$('#lb-close').addEventListener('click', (e) => { e.stopPropagation(); closeLightbox(); });
$('#lb-prev').addEventListener('click', (e) => { e.stopPropagation(); lbStep(-1); });
$('#lb-next').addEventListener('click', (e) => { e.stopPropagation(); lbStep(1); });
// Clicking the image advances to the next frame rather than closing.
$('#lightbox-img').addEventListener('click', (e) => { e.stopPropagation(); lbStep(1); });

$('#gsearch').addEventListener('submit', (e) => {
  e.preventDefault();
  const q = $('#gq').value.trim();
  if (q.length < 2) { toast('검색어를 두 글자 이상 입력하세요.', true); return; }
  location.hash = `#/search?q=${encodeURIComponent(q)}`;
});

$('#btn-load').addEventListener('click', doLoad);
$('#target').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLoad(); });
$('#tab-add-ch').addEventListener('click', () => showAddTab('channel'));
$('#tab-add-vid').addEventListener('click', () => showAddTab('video'));
$('#btn-vadd').addEventListener('click', doVideoAdd);
$('#vid-target').addEventListener('keydown', (e) => { if (e.key === 'Enter') doVideoAdd(); });
$('#btn-run').addEventListener('click', doRun);
$('#btn-cancel').addEventListener('click', () => jpost('/api/cancel', {}).catch(() => {}));

$$('.tabbtn').forEach((b) => b.addEventListener('click', () => {
  S.ctab = b.dataset.ct;
  $$('.tabbtn').forEach((x) => x.classList.toggle('on', x === b));
  S.limit = PAGE;                 // a new filter starts from the top
  renderChannel();
}));
['#q', '#sort', '#ftab'].forEach((s) =>
  $(s).addEventListener('input', () => { S.limit = PAGE; renderChannel(); }));

/* Acts on every row the filter matches, not just the drawn window. */
$('#sel-all').addEventListener('click', () => {
  visible().forEach((v) => {
    if (!isDone(v) && !isMembers(v)) S.picked.add(v.id);
  });
  renderChannel(); saveSelection();
});
$('#sel-none').addEventListener('click', () => {
  S.picked.clear(); renderChannel(); saveSelection();
});

$('#rd-copy').addEventListener('click', () => writeClipboard(S.reader?.text));
$('#ar-copy').addEventListener('click', () => {
  const d = S.articleDoc;
  if (!d) return;
  const head = [d.title, d.subtitle].filter(Boolean).join('\n');
  const text = `${head}\n\n${d.text || ''}`;
  writeClipboard(text, `글 전체 복사 · ${commas(text.length)}자`);
});
$('#cl-copy').addEventListener('click', () => writeClipboard(S.clean?.text));
$('#cl-copyprompt').addEventListener('click', copyPromptAndText);
$('#cl-drop').addEventListener('click', dropClean);
$('#cl-save').addEventListener('click', saveClean);
$('#cl-paste').addEventListener('click', () => {
  $('#cl-pastebox').hidden = false;
  $('#cl-text').focus();
});
$('#cl-cancel').addEventListener('click', () => { $('#cl-pastebox').hidden = true; });
$('#rd-orig').addEventListener('change', applyMode);
let findTimer;
$('#rd-find').addEventListener('input', () => {
  clearTimeout(findTimer);
  findTimer = setTimeout(() => runFind($('#rd-find').value.trim()), 180);
});

/*
 * The back link and .panehead are sticky and pin themselves relative to the
 * nav, whose height changes when it wraps on narrow screens. Measuring it
 * keeps both offsets exact instead of trusting a hard-coded number.
 */
function syncNavHeight() {
  const nav = $('.nav');
  if (!nav) return;
  const h = Math.round(nav.getBoundingClientRect().height);
  if (h) document.documentElement.style.setProperty('--navh', `${h}px`);
}
syncNavHeight();
{
  const nav = $('.nav');
  if (nav && window.ResizeObserver) new ResizeObserver(syncNavHeight).observe(nav);
  else window.addEventListener('resize', syncNavHeight);
}

// On GitHub Pages there is no server: mark the body so CSS can hide every
// collect/extract/author control, and skip job polling entirely.
if (STATIC) document.body.classList.add('static');

route();
