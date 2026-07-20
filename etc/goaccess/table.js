/* Client side sorting and pagination for the custom panel pages.
 *
 * Opt in by rendering <div class="card sortable"> around a table (or around a cross tab card).
 * Everything here is presentation only: the server still decides which rows exist and what the
 * default order is, and this file only reorders and hides what is already in the document.
 *
 * Two container shapes are supported and normalised to one {headers, rows} model:
 *   - a real <table>: headers are thead th, rows are tbody tr, cells are td
 *   - a cross tab card: headers are .xhead span, rows are details.x, cells are summary > span
 * Everything past collect() is shared between them.
 *
 * Loaded by URL as /table.js, like nav.js and custom.css. A filesystem path would 404.
 */
(function () {
  var MAXKEYS = 3;
  var SIZES = [25, 50, 100, 250, 0];   // 0 means All
  var DEFSIZE = 100;
  var UP = '▲', DOWN = '▼';
  var SUP = ['¹', '²', '³'];

  // The furniture below is created by this file and exists nowhere in the served HTML, so its CSS
  // ships here too rather than being pasted into the four generators' separate <style> blocks. Four
  // copies would drift; one cannot. Nothing here is styled before the script runs, so injecting at
  // init costs no flash of unstyled content.
  var CSS = [
    'th.sortable,.xhead span.sortable{cursor:pointer;user-select:none}',
    'th.sortable:hover,.xhead span.sortable:hover{color:rgba(255,255,255,.85)}',
    'th.sorted,.xhead span.sorted{color:#cfe0ff}',
    '.sortmark{margin-left:.25rem;font-size:.72em;color:#7aa2f7}',
    '.sortbar{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:0 0 .7rem;font-size:.8rem}',
    '.sortlbl{color:rgba(255,255,255,.5)}',
    '.chip{display:inline-flex;align-items:center;border-radius:999px;overflow:hidden;background:linear-gradient(90deg,#016eda,#d900c0)}',
    '.chip button{background:none;border:none;color:#fff;font:600 12px Roboto,system-ui,sans-serif;cursor:pointer;padding:3px 4px}',
    '.chipname{padding-left:10px}',
    '.chipx{padding-right:9px;opacity:.75;font-size:13px}',
    '.chipx:hover{opacity:1}',
    '.chipclear,.pbtn,.psize{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);color:rgba(255,255,255,.75);border-radius:8px;font:600 12px Roboto,system-ui,sans-serif;padding:3px 9px;cursor:pointer}',
    '.chipclear:hover,.pbtn:not(:disabled):hover{background:rgba(111,76,255,.22);color:#fff}',
    '.sorthint{color:rgba(255,255,255,.32);font-size:.74rem;margin-left:.2rem}',
    '.pager{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.8rem 0 0;padding-top:.7rem;border-top:1px solid rgba(255,255,255,.09);font-size:.8rem}',
    '.pinfo{color:rgba(255,255,255,.5);margin-right:auto}',
    '.pnav{display:inline-flex;flex-wrap:wrap;gap:.25rem;align-items:center}',
    '.pbtn{min-width:28px;text-align:center}',
    '.pbtn:disabled{opacity:.3;cursor:default}',
    '.pbtn.pcur{background:linear-gradient(90deg,#016eda,#d900c0);border-color:transparent;color:#fff}',
    '.pgap{color:rgba(255,255,255,.35);padding:0 .1rem}',
    '.psize{padding:3px 6px}',
    '.psize option{background:#0d0e21;color:#e8e8f0}'
  ].join('\n');

  function injectCSS() {
    if (document.getElementById('hails-table-css')) return;
    var s = document.createElement('style');
    s.id = 'hails-table-css';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  function num(s) {
    // Percentages, thousands separators and stray whitespace are the only decoration the renderers
    // put on an otherwise bare number. Anything else falls back to a string compare.
    var t = String(s).replace(/[,%\s]/g, '');
    if (t === '') return null;
    var v = Number(t);
    return isFinite(v) ? v : null;
  }

  function collect(card) {
    // The cross tab shape has to be tested FIRST. Each of its details blocks contains a child table
    // of target urls, so a querySelector('table') on the card matches that nested child and would
    // silently sort one expanded block's children instead of the blocks themselves.
    var xhead = card.querySelector('.xhead');
    if (xhead) {
      var blocks = Array.prototype.slice.call(card.querySelectorAll(':scope > details.x'));
      return {
        headers: Array.prototype.slice.call(xhead.children),
        rows: blocks.map(function (d) {
          var s = d.querySelector('summary');
          return { el: d, cells: Array.prototype.slice.call(s.children) };
        }),
        pinned: [], parent: card, anchor: card, xtab: true
      };
    }
    var tbl = card.querySelector('table');
    if (tbl && tbl.tHead && tbl.tBodies.length) {
      var body = tbl.tBodies[0];
      var rows = [], tot = [];
      Array.prototype.forEach.call(body.rows, function (r) {
        // The "Total (shown)" row is a summary of the rows above it, not one of them. It must never
        // sort into the middle of the table and never be paged away.
        (r.classList.contains('tot') ? tot : rows).push({
          el: r, cells: Array.prototype.slice.call(r.cells)
        });
      });
      return {
        headers: Array.prototype.slice.call(tbl.tHead.rows[0].cells),
        rows: rows, pinned: tot, parent: body, anchor: tbl
      };
    }
    return null;
  }

  function keysFor(t, col) {
    // Read every cell in the column once and decide the column's type from what came back. data-s
    // wins where a renderer had to state the sort value because the display text hides it (byte
    // sizes read "1.2 MB", trend deltas read "▲ +12%").
    var vals = t.rows.map(function (r) {
      var c = r.cells[col];
      if (!c) return { n: null, s: '' };
      var ds = c.getAttribute('data-s');
      if (ds !== null) {
        var dn = Number(ds);
        return isFinite(dn) ? { n: dn, s: ds } : { n: null, s: ds };
      }
      var txt = (c.textContent || '').trim();
      return { n: num(txt), s: txt };
    });
    var numeric = vals.length > 0 && vals.every(function (v) { return v.n !== null; });
    return { numeric: numeric, vals: vals };
  }

  // Where reordered rows go back in. A table's rows own their tbody outright, so appending is safe
  // and only the pinned total has to be kept below them. A cross tab's blocks are siblings of the
  // header and the pager inside the card, so appending would push every block past the pager and
  // leave the pagination controls sitting above the rows they control.
  function insertPoint(t) {
    if (t.pinned.length) return t.pinned[0].el;
    if (t.xtab) return t.pager || null;
    return null;
  }

  function apply(t) {
    var chain = t.chain;
    if (chain.length) {
      chain.forEach(function (k) {
        if (!t.cache[k.col]) t.cache[k.col] = keysFor(t, k.col);
      });
      var idx = t.rows.map(function (r, i) { return i; });
      // Array.prototype.sort is stable in every browser we care about, so equal rows keep the
      // server's ranking as the implicit last tiebreak.
      idx.sort(function (a, b) {
        for (var i = 0; i < chain.length; i++) {
          var k = chain[i], c = t.cache[k.col], d;
          if (c.numeric) d = c.vals[a].n - c.vals[b].n;
          else d = c.vals[a].s.localeCompare(c.vals[b].s, undefined, { numeric: true });
          if (d) return k.dir < 0 ? -d : d;
        }
        return 0;
      });
      var frag = document.createDocumentFragment();
      idx.forEach(function (i) { frag.appendChild(t.rows[i].el); });
      t.parent.insertBefore(frag, insertPoint(t));
      t.order = idx.map(function (i) { return t.rows[i]; });
    } else {
      t.order = t.rows.slice();
    }
    markHeaders(t);
    renderChips(t);
    paint(t);
  }

  function markHeaders(t) {
    t.headers.forEach(function (h, i) {
      var old = h.querySelector('.sortmark');
      if (old) old.remove();
      var pos = -1;
      for (var j = 0; j < t.chain.length; j++) if (t.chain[j].col === i) pos = j;
      h.classList.toggle('sorted', pos >= 0);
      if (pos < 0) return;
      var m = document.createElement('span');
      m.className = 'sortmark';
      m.textContent = (t.chain[pos].dir < 0 ? DOWN : UP) + (t.chain.length > 1 ? SUP[pos] : '');
      h.appendChild(m);
    });
  }

  function renderChips(t) {
    var bar = t.bar;
    bar.textContent = '';
    if (!t.chain.length) { bar.hidden = true; return; }
    bar.hidden = false;
    var lbl = document.createElement('span');
    lbl.className = 'sortlbl';
    lbl.textContent = 'Sorted by';
    bar.appendChild(lbl);
    t.chain.forEach(function (k, i) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      var name = document.createElement('button');
      name.type = 'button';
      name.className = 'chipname';
      name.textContent = headText(t.headers[k.col]) + ' ' + (k.dir < 0 ? DOWN : UP);
      name.title = 'Reverse this key';
      name.onclick = function () { k.dir = -k.dir; t.page = 1; apply(t); save(t); };
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'chipx';
      x.textContent = '×';
      x.title = 'Remove this key';
      x.onclick = function () {
        t.chain.splice(i, 1); t.page = 1; apply(t); save(t);
      };
      chip.appendChild(name);
      chip.appendChild(x);
      bar.appendChild(chip);
    });
    var clr = document.createElement('button');
    clr.type = 'button';
    clr.className = 'chipclear';
    clr.textContent = 'clear';
    clr.onclick = function () { t.chain = []; t.page = 1; restore(t); save(t); };
    bar.appendChild(clr);
    var hint = document.createElement('span');
    hint.className = 'sorthint';
    hint.textContent = 'shift click a column to add a key';
    bar.appendChild(hint);
  }

  function restore(t) {
    // Clearing the sort has to put the server's original ranking back, so the initial DOM order is
    // captured once at setup and replayed here rather than being re-derived from any column.
    var frag = document.createDocumentFragment();
    t.rows.forEach(function (r) { frag.appendChild(r.el); });
    t.parent.insertBefore(frag, insertPoint(t));
    t.order = t.rows.slice();
    markHeaders(t);
    renderChips(t);
    paint(t);
  }

  function headText(h) {
    return (h.textContent || '').replace(UP, '').replace(DOWN, '')
      .replace(/[¹²³]/g, '').trim();
  }

  function paint(t) {
    var n = t.order.length;
    var size = t.size || n;
    var pages = size ? Math.max(1, Math.ceil(n / size)) : 1;
    if (t.page > pages) t.page = pages;
    var from = (t.page - 1) * size, to = from + size;
    t.order.forEach(function (r, i) { r.el.hidden = (i < from || i >= to); });
    renderPager(t, n, pages, from, Math.min(to, n));
  }

  function renderPager(t, n, pages, from, to) {
    var p = t.pager;
    p.textContent = '';
    // A table that fits on one page at the default size gets no furniture at all, so the short
    // panels and the overview cards look exactly as they did.
    if (n <= DEFSIZE && pages <= 1) { p.hidden = true; return; }
    p.hidden = false;

    var info = document.createElement('span');
    info.className = 'pinfo';
    info.textContent = n ? ('Showing ' + (from + 1) + ' to ' + to + ' of ' + n) : 'No rows';
    p.appendChild(info);

    if (pages > 1) {
      var nav = document.createElement('span');
      nav.className = 'pnav';
      nav.appendChild(pbtn(t, '‹', t.page - 1, t.page <= 1));
      pageNumbers(pages, t.page).forEach(function (x) {
        if (x === null) {
          var gap = document.createElement('span');
          gap.className = 'pgap';
          gap.textContent = '…';
          nav.appendChild(gap);
        } else {
          var b = pbtn(t, String(x), x, false);
          if (x === t.page) b.classList.add('pcur');
          nav.appendChild(b);
        }
      });
      nav.appendChild(pbtn(t, '›', t.page + 1, t.page >= pages));
      p.appendChild(nav);
    }

    var sel = document.createElement('select');
    sel.className = 'psize';
    sel.title = 'Rows per page';
    SIZES.forEach(function (s) {
      var o = document.createElement('option');
      o.value = String(s);
      o.textContent = s ? (s + ' / page') : 'All';
      if (s === t.size) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = function () { t.size = Number(sel.value); t.page = 1; paint(t); };
    p.appendChild(sel);
  }

  function pbtn(t, label, target, disabled) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'pbtn';
    b.textContent = label;
    b.disabled = !!disabled;
    b.onclick = function () {
      t.page = target;
      paint(t);
      if (t.anchor.scrollIntoView) t.anchor.scrollIntoView({ block: 'nearest' });
    };
    return b;
  }

  function pageNumbers(pages, cur) {
    // First, last, and a window around the current page; gaps become an ellipsis.
    var out = [], want = {};
    want[1] = want[pages] = 1;
    for (var i = cur - 1; i <= cur + 1; i++) if (i >= 1 && i <= pages) want[i] = 1;
    var prev = 0;
    Object.keys(want).map(Number).sort(function (a, b) { return a - b; }).forEach(function (i) {
      if (prev && i > prev + 1) out.push(null);
      out.push(i);
      prev = i;
    });
    return out;
  }

  /* ---- persistence -------------------------------------------------------------------------- */
  function skey(t) {
    var file = location.pathname.split('/').pop() || 'index.html';
    // Keyed on the table's index WITHIN ITS VIEW, not within the document: page() emits the same
    // table three times, once per window, and a document wide index would give the weekly copy a
    // different key than the daily one.
    return 'hailsSort:' + file + ':' + t.idx;
  }

  function save(t) {
    try {
      if (t.chain.length) localStorage.setItem(skey(t), JSON.stringify(t.chain));
      else localStorage.removeItem(skey(t));
    } catch (e) {}
    // One choice covers every window, so the sibling tables at the same index follow along. The
    // windows are not guaranteed to share a shape: Bandwidth's Total view is a by domain table with
    // one column fewer than the period tables beside it, so a key is only carried over to a sibling
    // that actually has that column, rather than pointing it at a cell that does not exist.
    (t.siblings || []).forEach(function (s) {
      if (s === t) return;
      s.chain = t.chain.filter(function (k) { return k.col < s.headers.length; })
                       .map(function (k) { return { col: k.col, dir: k.dir }; });
      s.page = 1;
      if (s.chain.length) apply(s); else restore(s);
    });
  }

  function load(t) {
    try {
      var raw = localStorage.getItem(skey(t));
      if (!raw) return [];
      var v = JSON.parse(raw);
      if (!Array.isArray(v)) return [];
      return v.filter(function (k) {
        return k && typeof k.col === 'number' && k.col >= 0 && k.col < t.headers.length;
      }).slice(0, MAXKEYS).map(function (k) { return { col: k.col, dir: k.dir < 0 ? -1 : 1 }; });
    } catch (e) { return []; }
  }

  /* ---- setup -------------------------------------------------------------------------------- */
  function click(t, col, shift) {
    var pos = -1;
    for (var i = 0; i < t.chain.length; i++) if (t.chain[i].col === col) pos = i;
    if (shift) {
      if (pos >= 0) t.chain[pos].dir = -t.chain[pos].dir;
      else {
        t.chain.push({ col: col, dir: 1 });
        if (t.chain.length > MAXKEYS) t.chain.shift();
      }
    } else if (pos === 0 && t.chain.length === 1) {
      t.chain[0].dir = -t.chain[0].dir;
    } else {
      t.chain = [{ col: col, dir: 1 }];
    }
    t.page = 1;
    apply(t);
    save(t);
  }

  function setup(card, idx) {
    var t = collect(card);
    if (!t || !t.rows.length) return null;
    t.card = card;
    t.idx = idx;
    t.chain = [];
    t.cache = {};
    t.page = 1;
    t.size = DEFSIZE;
    t.order = t.rows.slice();

    t.bar = document.createElement('div');
    t.bar.className = 'sortbar';
    t.bar.hidden = true;
    card.insertBefore(t.bar, card.firstChild);

    t.pager = document.createElement('div');
    t.pager.className = 'pager';
    t.pager.hidden = true;
    card.appendChild(t.pager);

    t.headers.forEach(function (h, i) {
      h.classList.add('sortable');
      h.tabIndex = 0;
      h.title = 'Sort by ' + headText(h) + ' (shift click to add a key)';
      h.addEventListener('click', function (e) { click(t, i, e.shiftKey); });
      h.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); click(t, i, e.shiftKey); }
      });
    });
    return t;
  }

  function init() {
    // Index per view so the three window copies of a table share one key; cards outside a .view
    // (there are none today) fall into their own bucket.
    var counters = {}, groups = {};
    var all = [];
    injectCSS();
    Array.prototype.forEach.call(document.querySelectorAll('.card.sortable'), function (card) {
      var view = card.closest('.view');
      var vid = view ? view.id : '_';
      var idx = counters[vid] = (counters[vid] === undefined ? 0 : counters[vid] + 1);
      var t = setup(card, idx);
      if (!t) return;
      (groups[idx] = groups[idx] || []).push(t);
      all.push(t);
    });
    all.forEach(function (t) { t.siblings = groups[t.idx]; });
    all.forEach(function (t) {
      t.chain = load(t);
      if (t.chain.length) apply(t); else paint(t);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
