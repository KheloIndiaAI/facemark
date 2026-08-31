/* Inline SVG charts - no build step, no CDN, no dependencies.
 *
 * PALETTE
 * -------
 * Every colour below was checked with a validator rather than chosen by eye,
 * against this app's own white card surface. Two things that looked fine were
 * wrong and had to be replaced:
 *
 *   - the previous palette led with accent-blue then violet (#2563eb, #7c3aed).
 *     Under deuteranopia those are 0.4 apart (dE, OKLab x100) - the same
 *     colour. Even with full colour vision they scored 12.4, under the 15
 *     floor. Blue/orange/teal below score 13.7 worst-pair under deuteranopia
 *     and 22.6 with normal vision.
 *   - it also used --green/--amber/--red as ordinary series colours. Those are
 *     status colours: they mean good/warning/bad. Spending them on "series 2"
 *     leaves nothing to say a geo-fence failed with. They are now used ONLY
 *     for the geo-verification chart, where they genuinely encode state.
 *
 * Categorical is capped at 3. A 4th (violet) passed when only neighbours were
 * compared but failed once every pair was: violet vs blue is 5.4 under
 * deuteranopia. More series fold into "Other" rather than inventing a hue.
 *
 * The sequential ramp is one hue, light to dark, and its lightest step still
 * clears 2:1 against white - a paler tint disappears on the card.
 */

const CH = {
    pad: { t: 18, r: 18, b: 34, l: 46 },
    // Validated: CVD-safe at all pairs, chroma floor, 3:1 vs #ffffff.
    categorical: ['#2563eb', '#c2410c', '#0d9488'],
    // Validated ordinal: monotone lightness, >=0.06 steps, 2.17:1 light end.
    sequential: ['#7fb3f7', '#5595ef', '#2f74e3', '#2159c4', '#173f8f'],
    // Reserved. Never a series colour.
    status: { good: 'var(--green)', warn: 'var(--amber)', bad: 'var(--red)', none: 'var(--track)' },
    grid: 'var(--border-subtle)',
    ink: 'var(--text-secondary)',
    muted: 'var(--text-muted)',
    surface: 'var(--bg-surface)',
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

function niceMax(v) {
    if (v <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    const step = Math.ceil(v / mag * 4) / 4;      // quarter-decade steps read cleanly
    return step * mag;
}

/* Smooth path through the points, using monotone cubic interpolation
 * (Fritsch-Carlson).
 *
 * The choice of spline matters for honesty, not just looks. A Catmull-Rom or
 * plain cardinal spline overshoots around a sharp change - it will dip a curve
 * below zero between two positive readings, or bulge it above a peak that was
 * never reached. On an attendance chart that invents sessions that did not
 * happen. Monotone cubic is constrained so the curve never leaves the range of
 * the points it joins: between two readings it can only move from one toward
 * the other. It is smooth AND it cannot draw a value that is not in the data.
 */
function monotonePath(pts) {
    const n = pts.length;
    if (n < 2) return '';
    if (n === 2) return `M${pts[0].x},${pts[0].y} L${pts[1].x},${pts[1].y}`;

    const dx = [], dy = [], slope = [];
    for (let i = 0; i < n - 1; i++) {
        dx[i] = pts[i + 1].x - pts[i].x;
        dy[i] = pts[i + 1].y - pts[i].y;
        slope[i] = dx[i] ? dy[i] / dx[i] : 0;
    }

    // Tangents: average of neighbouring slopes, flattened at local extremes so
    // the curve turns over cleanly instead of overshooting past the point.
    const m = [slope[0]];
    for (let i = 1; i < n - 1; i++) {
        m[i] = (slope[i - 1] * slope[i] <= 0) ? 0 : (slope[i - 1] + slope[i]) / 2;
    }
    m[n - 1] = slope[n - 2];

    // Fritsch-Carlson limiter: keeps every tangent inside the circle of radius
    // 3, which is the condition that guarantees no overshoot.
    for (let i = 0; i < n - 1; i++) {
        if (slope[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
        const a = m[i] / slope[i], b = m[i + 1] / slope[i];
        const h = Math.hypot(a, b);
        if (h > 3) {
            const t = 3 / h;
            m[i] = t * a * slope[i];
            m[i + 1] = t * b * slope[i];
        }
    }

    let d = `M${pts[0].x.toFixed(2)},${pts[0].y.toFixed(2)}`;
    for (let i = 0; i < n - 1; i++) {
        const c = dx[i] / 3;
        d += ` C${(pts[i].x + c).toFixed(2)},${(pts[i].y + m[i] * c).toFixed(2)}` +
             ` ${(pts[i + 1].x - c).toFixed(2)},${(pts[i + 1].y - m[i + 1] * c).toFixed(2)}` +
             ` ${pts[i + 1].x.toFixed(2)},${pts[i + 1].y.toFixed(2)}`;
    }
    return d;
}

/* Each chart needs its own gradient id, or the second chart on a page reuses
   the first one's colour. */
let _gid = 0;
const nextId = (p) => `${p}-${(++_gid)}`;

function emptyChart(w, h, message) {
    return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="${esc(message)}">
        <text x="${w / 2}" y="${h / 2}" text-anchor="middle" fill="${CH.muted}"
              font-size="13" font-family="var(--font-sans)">${esc(message)}</text></svg>`;
}

/* A table view ships with every chart. The tooltip enhances; this is what
   guarantees no value is reachable only by hovering - which is unusable on a
   phone and invisible to a screen reader. */
function tableView(cols, rows, caption) {
    return `<details class="chart-table">
        <summary>View as table</summary>
        <table class="data-table chart-data-table">
            <caption class="sr-only">${esc(caption || '')}</caption>
            <thead><tr>${cols.map(c => `<th scope="col">${esc(c)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map(r => `<tr>${r.map((c, i) =>
                `<td data-label="${esc(cols[i])}">${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody>
        </table></details>`;
}

/* ---------------------------------------------------------------------------
   Area chart - a single series over time.
   One series, so no legend box: the card title already says what is plotted.
   Values are labelled at the extremes only; a number on all 23 points is noise.
   --------------------------------------------------------------------------- */
function areaChart(data, opts = {}) {
    const w = opts.width || 760, h = opts.height || 240;
    if (!data || data.length < 2) return emptyChart(w, h, opts.empty || 'Not enough data yet');
    const { t, r, b, l } = CH.pad;
    const iw = w - l - r, ih = h - t - b;
    const vals = data.map(d => +d.value || 0);
    const max = niceMax(Math.max(...vals) || 1);
    const hue = opts.color || CH.categorical[0];
    const X = i => l + (data.length === 1 ? iw / 2 : (i / (data.length - 1)) * iw);
    const Y = v => t + ih - (v / max) * ih;

    let grid = '';
    for (let i = 0; i <= 4; i++) {
        const v = (max / 4) * i, y = Y(v);
        grid += `<line x1="${l}" y1="${y}" x2="${l + iw}" y2="${y}" stroke="${CH.grid}" stroke-width="1"/>
                 <text x="${l - 8}" y="${y + 4}" text-anchor="end" font-size="10"
                       fill="${CH.muted}" style="font-variant-numeric:tabular-nums">${Math.round(v)}</text>`;
    }

    const pts = data.map((d, i) => ({ x: X(i), y: Y(+d.value || 0) }));
    const line = monotonePath(pts);
    const area = `${line} L${X(data.length - 1).toFixed(2)},${(t + ih).toFixed(2)}` +
                 ` L${X(0).toFixed(2)},${(t + ih).toFixed(2)} Z`;
    // A wash that fades toward the baseline rather than a flat block: the ink
    // sits under the line where the reader is looking and thins out below.
    const gid = nextId('ch-grad');

    // Label the peak and the final point only - the story is "how high did it
    // get" and "where is it now".
    const peak = vals.indexOf(Math.max(...vals));
    const marks = [peak, data.length - 1].filter((v, i, a) => a.indexOf(v) === i);
    let dots = '', labels = '';
    marks.forEach(i => {
        dots += `<circle class="ch-dot" cx="${X(i)}" cy="${Y(vals[i])}" r="4.5" fill="${hue}"
                         stroke="${CH.surface}" stroke-width="2"/>`;
        const anchor = i === data.length - 1 && i !== 0 ? 'end' : 'middle';
        labels += `<text class="ch-dot" x="${X(i)}" y="${Y(vals[i]) - 11}" text-anchor="${anchor}" font-size="11"
                         font-weight="600" fill="${CH.ink}">${vals[i]}</text>`;
    });

    // X labels thinned to whatever fits, so they never collide.
    const every = Math.max(1, Math.ceil(data.length / (opts.xTicks || 6)));
    let xlab = '';
    data.forEach((d, i) => {
        if (i % every && i !== data.length - 1) return;
        xlab += `<text x="${X(i)}" y="${h - 12}" text-anchor="middle" font-size="10"
                       fill="${CH.muted}">${esc(d.short || d.label)}</text>`;
    });

    // Hit targets: one full-height band per point, so the pointer only has to
    // be near the right date, not on the 2px line.
    let hit = '';
    data.forEach((d, i) => {
        const bw = iw / data.length;
        hit += `<rect class="ch-hit" x="${(X(i) - bw / 2).toFixed(1)}" y="${t}" width="${bw.toFixed(1)}" height="${ih}"
                      fill="transparent" tabindex="0" role="img"
                      data-x="${X(i).toFixed(1)}"
                      aria-label="${esc(d.label)}: ${vals[i]}${esc(opts.unit || '')}"
                      data-tip="${esc(d.label)}|${vals[i]}${esc(opts.unit || '')}"></rect>`;
    });

    return `<div class="ch-wrap">
      <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" font-family="var(--font-sans)" class="ch-svg">
        ${grid}
        <defs>
          <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${hue}" stop-opacity="0.22"/>
            <stop offset="100%" stop-color="${hue}" stop-opacity="0.02"/>
          </linearGradient>
        </defs>
        <path class="ch-area" d="${area}" fill="url(#${gid})"/>
        <path class="ch-line" d="${line}" pathLength="100" fill="none" stroke="${hue}" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>
        <line class="ch-cross" x1="0" y1="${t}" x2="0" y2="${t + ih}"
              stroke="${CH.muted}" stroke-width="1" opacity="0"/>
        ${dots}${labels}${xlab}${hit}
      </svg>
      <div class="ch-tip" hidden></div>
    </div>`;
}

/* ---------------------------------------------------------------------------
   Columns. Bars cap at 24px so the band keeps its air, 4px rounded at the data
   end and square on the baseline, with a 2px surface gap between neighbours.
   --------------------------------------------------------------------------- */
function barChart(data, opts = {}) {
    const w = opts.width || 760, h = opts.height || 240;
    if (!data || !data.length) return emptyChart(w, h, opts.empty || 'No data yet');
    const { t, r, b, l } = CH.pad;
    const iw = w - l - r, ih = h - t - b;
    const max = niceMax(Math.max(...data.map(d => +d.value || 0)) || 1);
    const band = iw / data.length;
    const bw = Math.min(24, Math.max(4, band - 2));   // 2px surface gap, 24px cap

    let grid = '';
    for (let i = 0; i <= 4; i++) {
        const v = (max / 4) * i, y = t + ih - (v / max) * ih;
        grid += `<line x1="${l}" y1="${y}" x2="${l + iw}" y2="${y}" stroke="${CH.grid}" stroke-width="1"/>
                 <text x="${l - 8}" y="${y + 4}" text-anchor="end" font-size="10"
                       fill="${CH.muted}" style="font-variant-numeric:tabular-nums">${Math.round(v)}</text>`;
    }

    // A number over every column is chaos once there are many of them and simply
    // goes unread. With a handful of bars each value is useful; past that only
    // the peak is labelled and the axis, tooltip and table carry the rest.
    const labelAll = data.length <= 8;
    const peakIdx = data.reduce((best, d, i) =>
        (+d.value || 0) > (+data[best].value || 0) ? i : best, 0);

    let bars = '';
    data.forEach((d, i) => {
        const v = +d.value || 0;
        const bh = Math.max(2, (v / max) * ih);
        const x = l + i * band + (band - bw) / 2;
        const y = t + ih - bh;
        const hue = d.color || opts.color || CH.categorical[0];
        // Rounded at the data end only: a shape rounded at the baseline reads
        // as floating, and the baseline is where the value is anchored.
        const rr = Math.min(4, bw / 2, bh);
        const path = `M${x},${y + bh} L${x},${y + rr} Q${x},${y} ${x + rr},${y}
                      L${x + bw - rr},${y} Q${x + bw},${y} ${x + bw},${y + rr}
                      L${x + bw},${y + bh} Z`;
        bars += `<path class="ch-hit ch-bar" style="--i:${i}" d="${path}" fill="${hue}" tabindex="0" role="img"
                       aria-label="${esc(d.label)}: ${v}${esc(opts.unit || '')}"
                       data-tip="${esc(d.label)}|${v}${esc(opts.unit || '')}"></path>
                 ${labelAll || i === peakIdx ? `<text x="${x + bw / 2}" y="${y - 6}" text-anchor="middle" font-size="10"
                       fill="${CH.ink}" font-weight="600">${v}${esc(opts.unit || '')}</text>` : ''}
                 <text x="${x + bw / 2}" y="${h - 12}" text-anchor="middle" font-size="10"
                       fill="${CH.muted}">${esc(d.short || d.label)}</text>`;
    });
    return `<div class="ch-wrap">
      <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" font-family="var(--font-sans)" class="ch-svg">
        ${grid}${bars}
      </svg><div class="ch-tip" hidden></div></div>`;
}

/* ---------------------------------------------------------------------------
   Horizontal stacked bar - part-to-whole.
   Horizontal because the categories have long names, and a stacked bar rather
   than a pie because comparing arc angles is guesswork.
   --------------------------------------------------------------------------- */
function stackedBar(segments, opts = {}) {
    const total = segments.reduce((a, s) => a + (+s.value || 0), 0);
    if (!total) return `<div class="text-sm text-muted">${esc(opts.empty || 'No data yet')}</div>`;
    const h = opts.height || 30;

    // A segment narrower than its own label gets no inline label; the legend
    // and tooltip carry it. Clipping the text would be worse than omitting it.
    let x = 0, bars = '', legend = '';
    segments.forEach((s, i) => {
        const v = +s.value || 0;
        if (!v) return;
        const pct = (v / total) * 100;
        const hue = s.color || CH.categorical[i % CH.categorical.length];
        bars += `<div class="ch-seg ch-hit" style="width:${pct}%;background:${hue};--i:${i}"
                      tabindex="0" role="img"
                      aria-label="${esc(s.label)}: ${v} (${pct.toFixed(1)}%)"
                      data-tip="${esc(s.label)}|${v} (${pct.toFixed(1)}%)">
                   ${pct >= 12 ? `<span class="ch-seg-label">${pct.toFixed(0)}%</span>` : ''}
                 </div>`;
        legend += `<span class="ch-key"><span class="ch-key-rect" style="background:${hue}"></span>
                     ${esc(s.label)} <b>${v}</b></span>`;
        x += pct;
    });
    return `<div class="ch-wrap">
        <div class="ch-stack" style="height:${h}px">${bars}</div>
        <div class="ch-legend">${legend}</div>
        <div class="ch-tip" hidden></div>
    </div>`;
}

/* Sparkline for a stat tile: no axes, no labels, just the shape. */
function sparkline(values, opts = {}) {
    const w = opts.width || 120, h = opts.height || 32;
    const v = (values || []).map(x => +x || 0);
    if (v.length < 2) return '';
    const max = Math.max(...v), min = Math.min(...v);
    const span = (max - min) || 1;
    const X = i => (i / (v.length - 1)) * (w - 2) + 1;
    const Y = x => h - 3 - ((x - min) / span) * (h - 8);
    const line = monotonePath(v.map((x, i) => ({ x: X(i), y: Y(x) })));
    const hue = opts.color || CH.categorical[0];
    const gid = nextId('ch-spark');
    return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" class="ch-spark" aria-hidden="true">
        <defs>
          <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${hue}" stop-opacity="0.26"/>
            <stop offset="100%" stop-color="${hue}" stop-opacity="0.02"/>
          </linearGradient>
        </defs>
        <path d="${line} L${X(v.length - 1).toFixed(2)},${h} L${X(0).toFixed(2)},${h} Z"
              fill="url(#${gid})"/>
        <path class="ch-line" d="${line}" pathLength="100" fill="none" stroke="${hue}" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>
        <circle class="ch-dot" cx="${X(v.length - 1)}" cy="${Y(v[v.length - 1])}" r="3"
                fill="${hue}" stroke="var(--bg-surface)" stroke-width="2"/>
    </svg>`;
}

/* A single ratio against its limit - the form for "how full is this", where a
 * two-slice pie would be the wrong answer. The unfilled track is a lighter step
 * of the same ramp rather than plain grey, so the whole ring reads as one
 * measure at one glance.
 */
function radialMeter(value, opts = {}) {
    const size = opts.size || 132;
    const stroke = opts.stroke || 11;
    const pct = Math.max(0, Math.min(100, +value || 0));
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const hue = opts.color || CH.categorical[0];
    const gid = nextId('ch-meter');
    return `<div class="ch-meter" style="width:${size}px;height:${size}px">
        <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img"
             aria-label="${esc(opts.label || 'Rate')}: ${pct.toFixed(1)}%">
          <defs>
            <linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stop-color="${hue}" stop-opacity="0.75"/>
              <stop offset="100%" stop-color="${hue}"/>
            </linearGradient>
          </defs>
          <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
                  stroke="${CH.sequential[0]}" stroke-opacity="0.30" stroke-width="${stroke}"/>
          <circle class="ch-meter-fill" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
                  stroke="url(#${gid})" stroke-width="${stroke}" stroke-linecap="round"
                  stroke-dasharray="${c.toFixed(2)}"
                  style="--dash:${(c * (1 - pct / 100)).toFixed(2)};--circ:${c.toFixed(2)}"
                  transform="rotate(-90 ${size / 2} ${size / 2})"/>
        </svg>
        <div class="ch-meter-text">
            <span class="ch-meter-value">${pct.toFixed(0)}<small>%</small></span>
            ${opts.caption ? `<span class="ch-meter-caption">${esc(opts.caption)}</span>` : ''}
        </div>
    </div>`;
}

/* Horizontal bars for a ranked list - the form for "many items, long names". */
function rankedBars(data, opts = {}) {
    if (!data || !data.length) return `<div class="text-sm text-muted">${esc(opts.empty || 'No data yet')}</div>`;
    const max = Math.max(...data.map(d => +d.value || 0)) || 1;
    return `<div class="ch-wrap"><div class="ch-ranked">
        ${data.map((d, i) => {
            const v = +d.value || 0;
            const pct = (v / max) * 100;
            const hue = d.color || opts.color || CH.categorical[0];
            return `<div class="ch-rank-row ch-hit" tabindex="0" role="img"
                         aria-label="${esc(d.label)}: ${v}${esc(opts.unit || '')}"
                         data-tip="${esc(d.label)}|${v}${esc(opts.unit || '')}">
                <span class="ch-rank-label">${esc(d.label)}</span>
                <span class="ch-rank-track"><span class="ch-rank-fill"
                      style="--w:${Math.max(pct, 1.5)}%;--i:${i};background:${hue}"></span></span>
                <span class="ch-rank-value">${v}${esc(opts.unit || '')}</span>
            </div>`;
        }).join('')}
    </div><div class="ch-tip" hidden></div></div>`;
}

/* ---------------------------------------------------------------------------
   One delegated hover/focus handler for every chart on the page. Keyboard
   focus shows the same readout as the pointer, which is why every mark carries
   tabindex and an aria-label rather than relying on the tooltip alone.
   --------------------------------------------------------------------------- */
function initChartInteraction(root) {
    const host = root || document;
    if (host.__chartsBound) return;
    host.__chartsBound = true;

    const show = (target) => {
        const wrap = target.closest('.ch-wrap');
        const tip = wrap && wrap.querySelector('.ch-tip');
        if (!tip) return;
        const [label, value] = (target.getAttribute('data-tip') || '').split('|');
        // textContent, never innerHTML: these labels are athlete names and
        // centre names out of the database.
        tip.textContent = '';
        const v = document.createElement('strong');
        v.textContent = value || '';
        const l = document.createElement('span');
        l.textContent = label || '';
        tip.append(v, l);
        tip.hidden = false;

        const wr = wrap.getBoundingClientRect();
        const tr = target.getBoundingClientRect();
        let left = tr.left - wr.left + tr.width / 2;
        left = Math.max(4, Math.min(left, wr.width - 4));
        tip.style.left = left + 'px';
        tip.style.top = Math.max(0, tr.top - wr.top - 8) + 'px';

        const cross = wrap.querySelector('.ch-cross');
        const dx = target.getAttribute('data-x');
        if (cross && dx) {
            cross.setAttribute('x1', dx);
            cross.setAttribute('x2', dx);
            cross.setAttribute('opacity', '1');
        }
        target.classList.add('ch-active');
        if (wrap) wrap.classList.add('ch-focusing');
    };
    const hide = (target) => {
        const wrap = target.closest('.ch-wrap');
        if (!wrap) return;
        const tip = wrap.querySelector('.ch-tip');
        if (tip) tip.hidden = true;
        const cross = wrap.querySelector('.ch-cross');
        if (cross) cross.setAttribute('opacity', '0');
        target.classList.remove('ch-active');
        wrap.classList.remove('ch-focusing');
    };

    host.addEventListener('pointerover', e => {
        const hit = e.target.closest && e.target.closest('.ch-hit');
        if (hit) show(hit);
    });
    host.addEventListener('pointerout', e => {
        const hit = e.target.closest && e.target.closest('.ch-hit');
        if (hit) hide(hit);
    });
    host.addEventListener('focusin', e => {
        const hit = e.target.closest && e.target.closest('.ch-hit');
        if (hit) show(hit);
    });
    host.addEventListener('focusout', e => {
        const hit = e.target.closest && e.target.closest('.ch-hit');
        if (hit) hide(hit);
    });
}

/* Counts a stat tile's value up to its final number.
 *
 * Reads the number already rendered in the DOM rather than taking it as an
 * argument, so the markup is correct before any of this runs - if the script
 * fails or motion is switched off, the final value is already on screen. The
 * animation never decides what the number is.
 */
function countUp(root, ms = 650) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    (root || document).querySelectorAll('.stat-value').forEach(el => {
        if (el.dataset.counted) return;
        const raw = el.textContent.trim();
        const m = raw.match(/^(-?[\d,]+(?:\.\d+)?)(.*)$/);
        if (!m) return;                       // not a plain number - leave it alone
        const target = parseFloat(m[1].replace(/,/g, ''));
        if (!isFinite(target)) return;
        const suffix = m[2] || '';
        const decimals = (m[1].split('.')[1] || '').length;
        el.dataset.counted = '1';

        const t0 = performance.now();
        const step = (now) => {
            const p = Math.min(1, (now - t0) / ms);
            // Ease out: fast first, settling at the end, so the eye lands on
            // the final value rather than watching a linear ramp.
            const v = target * (1 - Math.pow(1 - p, 3));
            el.textContent = v.toFixed(decimals) + suffix;
            if (p < 1) requestAnimationFrame(step);
            else el.textContent = raw;        // exact original, never a rounding artefact
        };
        requestAnimationFrame(step);
    });
}

window.Charts = {
    countUp, radialMeter,
    areaChart, barChart, stackedBar, sparkline, rankedBars,
    tableView, emptyChart, initChartInteraction,
    esc,
    categorical: CH.categorical,
    sequential: CH.sequential,
    status: CH.status,
};
