'use strict';

const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
const FRC_COLORS = ['#c0392b', '#e67e22', '#d4ac0d', '#27ae60', '#16a085', '#2980b9', '#7f8c8d', '#95a5a6'];
const LEVEL_COLORS = {elevated: '#f39c12', ground: '#9e9e9e', tunnel: '#8073ac'};
const WIDTHS = {openlr: 8, hmm: 6, mapbox: 5, nearest: 4};
const PROFILE_OFFSETS = {openlr: 0, hmm: -0.12, nearest: 0.12, mapbox: -0.24};

const state = {
  config: null,
  points: [],
  result: null,
  mode3d: false,
  markers: [],
  hidden: new Set(['mapbox-raw']),
  autoTimer: null,
};

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const pct = (x) => `${Math.round(100 * x)}%`;
const rgb = (hex, a = 255) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)).concat(a);
const levelName = (lv) => (lv < 0 ? 'tunnel' : lv === 0 ? 'ground' : 'elevated');
const levelColor = (lv) => LEVEL_COLORS[levelName(lv)];
const levelText = (lv) => `level ${lv > 0 ? '+' : ''}${lv} (${levelName(lv)})`;
const frcText = (frc) => (frc == null ? 'any road' : `FRC${frc} ${state.config.frc_labels[frc]}`);

function status(text, kind = '') {
  const el = $('status');
  el.textContent = text;
  el.className = `msg ${kind}`;
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// ------------------------------------------------------------------ points <-> text
function formatPoints(points) {
  return points.map((p) => `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}${p.frc == null ? '' : `, FRC${p.frc}`}`).join('\n');
}

function parsePoints(text) {
  const points = [];
  const errors = [];
  const entries = text.split('\n').flatMap((l) => (l.includes(';') ? l.split(';') : [l]));
  entries.forEach((raw) => {
    const line = raw.replace(/#.*/, '').trim();
    if (!line) return;
    const frcMatch = line.match(/frc\s*[:=]?\s*([0-7])\b/i);
    const rest = frcMatch ? line.replace(frcMatch[0], ' ') : line;
    const nums = rest.split(/[\s,]+/).filter(Boolean).map(Number);
    if (nums.length < 2 || nums.slice(0, 2).some(Number.isNaN)) {
      errors.push(`cannot read "${raw.trim()}"`);
      return;
    }
    let [a, b] = nums;
    if (Math.abs(a) > 90) [a, b] = [b, a]; // lon, lat order
    let frc = frcMatch ? Number(frcMatch[1]) : null;
    if (frc == null && nums.length >= 3 && Number.isInteger(nums[2]) && nums[2] >= 0 && nums[2] <= 7) frc = nums[2];
    points.push({lat: a, lon: b, frc});
  });
  return {points, errors};
}

function pointsChanged({fromText = false} = {}) {
  if (!fromText) $('points').value = formatPoints(state.points);
  const hash = state.points.map((p) => `${p.lat.toFixed(6)},${p.lon.toFixed(6)}${p.frc == null ? '' : `,${p.frc}`}`).join(';');
  history.replaceState(null, '', hash ? `#p=${hash}` : location.pathname);
  renderMarkers();
  scheduleAuto();
}

function scheduleAuto(delay = 700) {
  clearTimeout(state.autoTimer);
  if ($('auto').checked && state.points.length >= 2) state.autoTimer = setTimeout(run, delay);
}

// ------------------------------------------------------------------ map
function baseStyle() {
  const raster = (url, maxzoom, attribution) => ({type: 'raster', tiles: [url], tileSize: 256, maxzoom, attribution});
  return {
    version: 8,
    sources: {
      gray: raster(`${ESRI}/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`, 16,
        'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors'),
      imagery: raster(`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, 19, 'Tiles &copy; Esri &mdash; Maxar, Earthstar Geographics'),
      osm: raster('https://tile.openstreetmap.org/{z}/{x}/{y}.png', 19, '&copy; OpenStreetMap contributors'),
    },
    layers: [
      {id: 'background', type: 'background', paint: {'background-color': '#f2f2f0'}},
      ...['gray', 'imagery', 'osm'].map((id) => ({id, type: 'raster', source: id, layout: {visibility: id === 'gray' ? 'visible' : 'none'}})),
    ],
  };
}

const map = new maplibregl.Map({
  container: 'map', style: baseStyle(), center: [139.69, 35.652], zoom: 14, maxPitch: 80,
  attributionControl: {compact: true}, preserveDrawingBuffer: true,
});
map.addControl(new maplibregl.NavigationControl({visualizePitch: true}), 'top-right');
map.addControl(new maplibregl.ScaleControl(), 'bottom-right');
const overlay = new deck.MapboxOverlay({
  interleaved: true, layers: [],
  getTooltip: ({object}) => (object && object.label ? {text: object.label} : null),
});
map.addControl(overlay);
let popup = null;

function closePopup() {
  if (popup) popup.remove();
  popup = null;
}

function openPopup(lngLat, html) {
  closePopup();
  popup = new maplibregl.Popup({closeOnClick: false, maxWidth: '380px'}).setLngLat(lngLat).setHTML(html).addTo(map);
  return popup.getElement();
}

function renderMarkers() {
  state.markers.forEach((m) => m.remove());
  state.markers = state.points.map((p, i) => {
    const el = document.createElement('div');
    el.className = 'pt';
    const color = p.frc == null ? '#555' : FRC_COLORS[p.frc];
    el.style.borderColor = color;
    el.innerHTML = `<b style="background:${color}">${i + 1}</b>${p.frc == null ? 'any' : `FRC${p.frc}`}`;
    el.title = `Point ${i + 1}: ${frcText(p.frc)} - drag to move, click to edit`;
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      editPoint(i);
    });
    const marker = new maplibregl.Marker({element: el, draggable: true}).setLngLat([p.lon, p.lat]).addTo(map);
    marker.on('dragend', () => {
      const ll = marker.getLngLat();
      p.lat = ll.lat;
      p.lon = ll.lng;
      pointsChanged();
    });
    return marker;
  });
}

async function roadChoices(p) {
  const {roads} = await api(`/api/nearby?lat=${p.lat}&lon=${p.lon}`);
  const byFrc = new Map();
  roads.forEach((r) => {
    if (!byFrc.has(r.frc)) byFrc.set(r.frc, []);
    byFrc.get(r.frc).push(r);
  });
  return [...byFrc.entries()].map(([frc, rs]) => ({frc, roads: rs}));
}

function choiceHtml(choices, current) {
  const buttons = choices.map((c) => {
    const names = [...new Set(c.roads.map((r) => `${r.name} - ${levelText(r.level)}`))].slice(0, 2);
    const mark = c.frc === current ? ' &#10003;' : '';
    return `<button class="choice" data-frc="${c.frc}"><b>${esc(frcText(c.frc))}${mark}</b>
      <small>${names.map(esc).join('<br>')} &middot; ${c.roads[0].distance} m</small></button>`;
  });
  buttons.push(`<button class="choice" data-frc=""><b>Any road${current == null ? ' &#10003;' : ''}</b>
    <small>snap to the nearest road in the direction of travel</small></button>`);
  return buttons.join('');
}

async function askRoad(i, isNew) {
  const p = state.points[i];
  let choices = [];
  try {
    choices = await roadChoices(p);
  } catch (e) {
    status(e.message, 'error');
  }
  if (isNew && choices.length === 1) {
    p.frc = choices[0].frc;
    pointsChanged();
    return;
  }
  if (isNew && choices.length === 0) {
    status(`Point ${i + 1}: no road within 40 m`, 'warn');
    pointsChanged();
    return;
  }
  const el = openPopup([p.lon, p.lat], `<div class="popup"><h4>Point ${i + 1}: which road do you mean?</h4>
    ${choices.length ? '' : '<p class="hint">No road within 40 m.</p>'}${choiceHtml(choices, p.frc)}
    <div class="row" style="margin-top:6px"><span class="spacer"></span>
    <button class="small danger" data-delete>Delete point</button></div></div>`);
  el.querySelectorAll('.choice').forEach((b) => b.addEventListener('click', () => {
    p.frc = b.dataset.frc === '' ? null : Number(b.dataset.frc);
    closePopup();
    pointsChanged();
  }));
  el.querySelector('[data-delete]').addEventListener('click', () => {
    state.points.splice(i, 1);
    closePopup();
    pointsChanged();
  });
  if (isNew) pointsChanged();
}

const editPoint = (i) => askRoad(i, false);

map.on('click', (e) => {
  const target = e.originalEvent.target;
  if (target.closest && target.closest('.pt, .maplibregl-popup')) return;
  const picked = overlay.pickObject({x: e.point.x, y: e.point.y, radius: 6, layerIds: ['lrps']});
  if (picked) return showLrp(picked.object);
  if (popup) return closePopup();
  state.points.push({lat: e.lngLat.lat, lon: e.lngLat.lng, frc: null});
  askRoad(state.points.length - 1, true);
});

function showLrp(lrp) {
  const rows = [
    ['FRC / FOW', `${lrp.frc} / ${lrp.fow}`],
    ['BEAR', `${lrp.bear}&deg; (sector ${lrp.bear_sector}&deg;)`],
    ['LFRCNP', lrp.lfrcnp ?? '-'],
    ['DNP', lrp.dnp == null ? '-' : `${lrp.dnp} m (binary ${lrp.dnp_binary} m)`],
    ['node', lrp.valid ? 'valid (Rule 4)' : 'invalid - no alternative'],
    ['encoder line', esc(lrp.line)],
  ].map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('');
  const cands = lrp.candidates.map((c) => `<tr><td>${c.score.toFixed(3)}</td><td>${esc(c.label)}</td><td>${c.attrs}</td></tr>`).join('');
  openPopup([lrp.lon, lrp.lat], `<div class="popup"><h4>OpenLR LRP ${lrp.index}</h4><table>${rows}</table>
    <h4 style="margin-top:8px">Decoder candidates on OSM</h4><table>${cands || '<tr><td>none</td></tr>'}</table></div>`);
}

// ------------------------------------------------------------------ deck.gl layers
function z(path) {
  return state.mode3d ? path : path.map((p) => [p[0], p[1], 0]);
}

function buildLayers() {
  const r = state.result;
  if (!r) return [];
  const show = (id) => !state.hidden.has(id);
  const dash = deck.PathStyleExtension ? [new deck.PathStyleExtension({dash: true})] : [];
  const trigger = {getPath: state.mode3d};
  const layers = [];
  if (show('network')) {
    layers.push(new deck.PathLayer({
      id: 'network', data: r.network, getPath: (d) => z(d.path), getColor: (d) => rgb(levelColor(d.level), 210),
      getWidth: 2.5, widthUnits: 'pixels', pickable: true, updateTriggers: trigger,
    }));
  }
  if (show('truth')) {
    layers.push(new deck.PathLayer({
      id: 'truth', data: r.truth.paths.map((p) => ({...p, label: `Ground truth: ${p.label}`})),
      getPath: (d) => z(d.path), getColor: rgb(state.config.colors.truth, 70), getWidth: 16, widthUnits: 'pixels',
      capRounded: true, jointRounded: true, pickable: true, updateTriggers: trigger,
    }));
  }
  if (show('polyline')) {
    layers.push(new deck.PathLayer({
      id: 'polyline', data: [{path: r.polyline, label: 'Linestring sent to the matchers (map A coordinates, no height)'}],
      getPath: (d) => d.path, getColor: rgb(state.config.colors.polyline), getWidth: 2, widthUnits: 'pixels',
      pickable: true, extensions: dash, getDashArray: [4, 3], dashJustified: true,
    }));
  }
  r.methods.filter((m) => show(m.key)).forEach((m) => {
    layers.push(new deck.PathLayer({
      id: `m-${m.key}`, data: m.paths.map((p) => ({...p, label: `${m.short}: ${p.label}`})),
      getPath: (d) => z(d.path), getColor: rgb(m.color, 235), getWidth: WIDTHS[m.key] || 4, widthUnits: 'pixels',
      capRounded: true, jointRounded: true, pickable: true, updateTriggers: trigger,
    }));
  });
  const mb = r.methods.find((m) => m.key === 'mapbox' && m.mapbox);
  if (mb && show('mapbox-raw')) {
    layers.push(new deck.PathLayer({
      id: 'mapbox-raw', data: mb.mapbox.raw, getPath: (d) => d.path, getColor: rgb(mb.color), getWidth: 2,
      widthUnits: 'pixels', pickable: true, extensions: dash, getDashArray: [2, 3],
    }));
  }
  if (show('lrps')) {
    const lift = (l) => [l.lon, l.lat, state.mode3d ? l.z + 14 : 0];
    if (state.mode3d) {
      layers.push(new deck.LineLayer({
        id: 'lrp-stems', data: r.openlr.lrps, getSourcePosition: (l) => [l.lon, l.lat, l.z], getTargetPosition: lift,
        getColor: [0, 0, 0, 200], getWidth: 2, updateTriggers: {getTargetPosition: state.mode3d},
      }));
    }
    layers.push(new deck.ScatterplotLayer({
      id: 'lrps', data: r.openlr.lrps.map((l) => ({...l, label: `LRP ${l.index}: ${l.frc} / ${l.fow} (click for details)`})),
      getPosition: lift, getRadius: 8, radiusUnits: 'pixels', getFillColor: rgb('#1a9850'), getLineColor: [0, 0, 0, 255],
      stroked: true, lineWidthMinPixels: 2, pickable: true, billboard: true, updateTriggers: {getPosition: state.mode3d},
    }));
    layers.push(new deck.TextLayer({
      id: 'lrp-labels', data: r.openlr.lrps, getPosition: lift, getText: (l) => `LRP ${l.index}`, getSize: 12,
      getPixelOffset: [0, -18], getColor: [0, 0, 0, 255], fontWeight: 700, outlineWidth: 3, outlineColor: [255, 255, 255, 255],
      fontSettings: {sdf: true}, billboard: true, updateTriggers: {getPosition: state.mode3d},
    }));
  }
  return layers;
}

function refreshLayers() {
  overlay.setProps({layers: buildLayers()});
}

function fitResult() {
  const coords = state.result.truth.paths.flatMap((p) => p.path);
  if (!coords.length) return;
  if (state.mode3d) return view3d();
  const lons = coords.map((c) => c[0]);
  const lats = coords.map((c) => c[1]);
  const legend = $('legend').hidden ? 0 : $('legend').offsetWidth;
  map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
    {padding: {top: 70, bottom: 50, right: 70, left: legend + 50}, pitch: 0, bearing: 0, duration: 600});
}

function view3d() {
  // Look at the middle of the route from the side, close enough to see the levels apart.
  const c = state.result.truth.paths.flatMap((p) => p.path);
  const mid = Math.floor(c.length / 2);
  const a = c[Math.max(0, mid - 5)];
  const b = c[Math.min(c.length - 1, mid + 5)];
  const heading = Math.atan2((b[0] - a[0]) * Math.cos(b[1] * Math.PI / 180), b[1] - a[1]) * 180 / Math.PI;
  map.easeTo({center: [c[mid][0], c[mid][1]], zoom: 16, pitch: 62, bearing: heading - 70, duration: 900});
}

// ------------------------------------------------------------------ legend
function renderLegend() {
  const r = state.result;
  const items = [
    ['network', `OSM roads: <span class="levels"><span style="color:${LEVEL_COLORS.elevated}">elevated</span>
      <span style="color:#777">ground</span> <span style="color:${LEVEL_COLORS.tunnel}">tunnel</span></span>`],
    ['truth', `<span class="swatch" style="background:${state.config.colors.truth};opacity:.35;height:8px"></span>Ground truth`],
    ['polyline', `<span class="swatch" style="background:${state.config.colors.polyline}"></span>Transmitted linestring (map A)`],
    ...r.methods.map((m) => [m.key, `<span class="swatch" style="background:${m.color}"></span>${esc(m.short)}`
      + `${m.key === 'mapbox' ? ' (on OSM)' : ''}`]),
    ...(r.methods.some((m) => m.key === 'mapbox' && m.mapbox) ? [['mapbox-raw',
      '<span class="swatch" style="background:repeating-linear-gradient(90deg,#e7298a 0 3px,transparent 3px 6px)"></span>Mapbox raw geometry']] : []),
    ['lrps', '<span class="swatch" style="background:#1a9850;width:10px;height:10px;border-radius:50%"></span>OpenLR LRPs'],
  ];
  const el = $('legend');
  el.innerHTML = items.map(([id, html]) =>
    `<label><input type="checkbox" data-layer="${id}" ${state.hidden.has(id) ? '' : 'checked'}> ${html}</label>`).join('');
  el.hidden = false;
  el.querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
    if (cb.checked) state.hidden.delete(cb.dataset.layer);
    else state.hidden.add(cb.dataset.layer);
    refreshLayers();
  }));
}

// ------------------------------------------------------------------ results panel
function verdict(m) {
  if (!m.paths.length) return '<span class="verdict bad">&#10007; no result</span>';
  return m.correct ? '<span class="verdict ok">&#10003; correct</span>' : '<span class="verdict bad">&#10007; wrong</span>';
}

function renderResult() {
  const r = state.result;
  $('results').hidden = false;
  const levels = r.truth.levels.map((lv) => (lv > 0 ? `+${lv}` : `${lv}`)).join(', ');
  $('summary').innerHTML = `Route ${(r.truth.length / 1000).toFixed(2)} km on OSM levels ${levels}. Map A: ${esc(r.difference)}.
    A method is <b>correct</b> if precision and recall are both &ge; 90 %.`;
  const rows = r.methods.map((m) => `<tr>
      <td><span class="swatch" style="background:${m.color}"></span>${esc(m.short)}
        <div class="roads">${esc(m.roads.slice(0, 4).join(' → '))}${m.roads.length > 4 ? ' …' : ''}</div>
        ${m.error ? `<div class="msg error">${esc(m.error)}</div>` : ''}</td>
      <td>${verdict(m)}</td><td class="num">${pct(m.precision)}</td><td class="num">${pct(m.recall)}</td>
      <td class="num">${pct(m.level_accuracy)}</td><td class="num">${m.ms}</td></tr>`).join('');
  $('result-table').innerHTML = `<tr><th>method</th><th></th><th>prec.</th><th>recall</th><th>right level</th><th>ms</th></tr>${rows}`;
  $('profile').innerHTML = profileSvg(r);
  renderRouteInfo(r);
  renderOpenlr(r);
  renderMapbox(r);
}

function renderRouteInfo(r) {
  const items = r.route.points.map((p, i) => `<li><b>${p.frc == null ? `any &rarr; FRC${p.road_frc}` : `FRC${p.frc}`}</b>
    ${esc(p.road)} &middot; ${levelText(p.level)} &middot; ${p.distance} m</li>`).join('');
  const notes = r.route.notes.map((n) => `<div class="msg warn">${esc(n)}</div>`).join('');
  $('route-info').innerHTML = `<h3>Route points snapped to OSM</h3><ol>${items}</ol>${notes}`;
}

function renderOpenlr(r) {
  const o = r.openlr;
  const rows = o.lrps.map((l) => `<tr><td>${l.index}</td><td>${l.frc}</td><td>${l.fow}</td><td class="num">${l.bear}&deg;</td>
    <td>${l.lfrcnp ?? '-'}</td><td class="num">${l.dnp == null ? '-' : `${l.dnp} m`}</td></tr>`).join('');
  const linestringChars = r.polyline.map((c) => `${c[0].toFixed(5)},${c[1].toFixed(5)}`).join(';').length;
  $('openlr-card').innerHTML = `<h3>OpenLR location reference</h3>
    <div class="row"><code class="code" id="olr-code">${esc(o.binary)}</code><button class="small" id="copy">Copy</button></div>
    <p class="hint">${o.lrps.length} LRPs, ${o.bytes} bytes &ndash; the linestring needs ${r.polyline.length} coordinates
      (${linestringChars} characters). Offsets +${o.p_off} m / &minus;${o.n_off} m. Click an LRP on the map for the decoder candidates.</p>
    <table class="table"><tr><th>#</th><th>FRC</th><th>FOW</th><th>BEAR</th><th>LFRCNP</th><th>DNP</th></tr>${rows}</table>
    <details><summary>Encoder log (whitepaper 11.1)</summary><ul>${o.steps.map((s) => `<li>${esc(s)}</li>`).join('')}</ul></details>`;
  $('copy').addEventListener('click', () => navigator.clipboard.writeText(o.binary));
}

function renderMapbox(r) {
  const m = r.methods.find((x) => x.key === 'mapbox');
  const card = $('mapbox-card');
  card.hidden = !m;
  if (!m) return;
  if (!m.mapbox || m.error) {
    card.innerHTML = `<h3>Mapbox</h3><div class="msg error">${esc(m.error || 'no answer')}</div>`;
    return;
  }
  const mb = m.mapbox;
  const chips = mb.classes.map(([c, len]) => `<span class="${esc(c.split(' ')[0].replace('_link', ''))}">${esc(c)} ${len} m</span>`).join('');
  card.innerHTML = `<h3>What Mapbox answered</h3>
    <p class="hint">Profile <code>${esc(mb.profile)}</code> &middot; ${mb.matched}/${mb.sent} points matched &middot;
      confidence ${mb.confidence.map((c) => c.toFixed(2)).join(', ')}${mb.cached ? ' &middot; cached' : ''}</p>
    <div class="chips">${chips}</div>
    <p class="hint">Mapbox returns geometry and a road class per segment on its own map, not OSM IDs. For scoring,
      the geometry is matched onto OSM roads of the class Mapbox reported (motorway vs. trunk separates the stacked roads).
      Toggle &ldquo;Mapbox raw geometry&rdquo; in the legend to see the original answer.</p>`;
}

function profileSvg(r) {
  const W = 370;
  const H = 150;
  const pad = {l: 64, r: 6, t: 6, b: 22};
  const d = r.profile.distance;
  if (d.length < 2) return '';
  const all = [...r.profile.truth, ...Object.values(r.profile.methods).flat()].filter((v) => v != null);
  const lo = Math.min(0, ...all);
  const hi = Math.max(0, ...all);
  const x = (v) => pad.l + (v / d[d.length - 1]) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (hi - v + 0.5) / (hi - lo + 1) * (H - pad.t - pad.b);
  const line = (vals, off) => {
    let path = '';
    let pen = false;
    vals.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      path += `${pen ? 'L' : 'M'}${x(d[i]).toFixed(1)},${y(v + off).toFixed(1)}`;
      pen = true;
    });
    return path;
  };
  let svg = `<svg width="${W}" height="${H}" font-size="10" font-family="sans-serif">`;
  for (let lv = lo; lv <= hi; lv++) {
    svg += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(lv)}" y2="${y(lv)}" stroke="#eee"/>`
      + `<text x="${pad.l - 4}" y="${y(lv) + 3}" text-anchor="end" fill="#666">${lv > 0 ? '+' : ''}${lv} ${levelName(lv)}</text>`;
  }
  const km = d[d.length - 1] / 1000;
  const step = km > 4 ? 1 : 0.5;
  for (let k = 0; k <= km + 1e-9; k += step) {
    svg += `<text x="${x(k * 1000)}" y="${H - 6}" text-anchor="middle" fill="#666">${k} km</text>`;
  }
  svg += `<path d="${line(r.profile.truth, 0)}" stroke="#222" stroke-opacity=".18" stroke-width="9" fill="none"/>`;
  r.methods.forEach((m) => {
    const vals = r.profile.methods[m.key];
    if (vals) svg += `<path d="${line(vals, PROFILE_OFFSETS[m.key] || 0)}" stroke="${m.color}" stroke-width="2" fill="none"/>`;
  });
  return `${svg}</svg>`;
}

// ------------------------------------------------------------------ run
function requestBody() {
  return {
    points: state.points,
    difference: Object.fromEntries(['shift', 'bearing', 'warp', 'simplify'].map((k) => [k, Number($(k).value)])),
    methods: [...document.querySelectorAll('#methods input:checked')].map((cb) => cb.value),
    disambiguate: $('disambiguate').checked,
    mapbox_radius: $('mapbox-radius').value,
  };
}

async function run() {
  clearTimeout(state.autoTimer);
  if (state.points.length < 2) {
    status('Add at least two route points.', 'warn');
    return;
  }
  $('run').disabled = true;
  status('Encoding, decoding and matching ...');
  try {
    const first = !state.result;
    state.result = await api('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(requestBody())});
    renderResult();
    renderLegend();
    refreshLayers();
    if (first || state.fitNext) fitResult();
    state.fitNext = false;
    status('');
  } catch (e) {
    status(e.message, 'error');
  } finally {
    $('run').disabled = false;
  }
}

// ------------------------------------------------------------------ setup
function setupControls(cfg) {
  const preset = $('preset');
  cfg.presets.forEach((p) => preset.add(new Option(p.title, p.key)));
  preset.addEventListener('change', () => {
    const p = cfg.presets.find((x) => x.key === preset.value);
    if (!p) return;
    state.points = p.points.map((q) => ({...q}));
    state.fitNext = true;
    pointsChanged();
    run();
  });

  $('frc-table').innerHTML = Object.entries(cfg.frc_labels).map(([frc, label]) =>
    `<tr><td><span class="swatch" style="background:${FRC_COLORS[frc]};width:10px;height:10px;border-radius:50%"></span>FRC${frc}</td><td>${label}</td></tr>`).join('');

  let textTimer = null;
  $('points').addEventListener('input', () => {
    clearTimeout(textTimer);
    textTimer = setTimeout(() => {
      const {points, errors} = parsePoints($('points').value);
      $('points-msg').textContent = errors.join('; ');
      $('points-msg').className = `msg ${errors.length ? 'error' : ''}`;
      state.points = points;
      pointsChanged({fromText: true});
    }, 500);
  });
  $('clear').addEventListener('click', () => {
    state.points = [];
    pointsChanged();
  });
  $('reverse').addEventListener('click', () => {
    state.points.reverse();
    pointsChanged();
  });

  for (const key of ['shift', 'bearing', 'warp', 'simplify']) {
    const input = $(key);
    input.value = cfg.difference[key];
    const show = () => { $(`${key}-v`).textContent = input.value; };
    show();
    input.addEventListener('input', show);
    input.addEventListener('change', () => scheduleAuto(100));
  }

  $('methods').innerHTML = cfg.methods.map((m) => `<label class="check ${m.available ? '' : 'disabled'}"
      title="${m.available ? '' : 'Set MAPBOX_ACCESS_TOKEN in .env and restart the server'}">
      <input type="checkbox" value="${m.key}" ${m.available ? 'checked' : 'disabled'}>
      <span class="swatch" style="background:${m.color}"></span><span>${esc(m.name)}${m.key === 'mapbox'
        ? `<small>${cfg.mapbox ? `profile ${esc(cfg.mapbox.profile)}` : 'not configured: set MAPBOX_ACCESS_TOKEN in .env'}</small>` : ''}</span></label>`).join('');
  $('methods').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => scheduleAuto(100)));
  $('disambiguate').addEventListener('change', () => scheduleAuto(100));
  if (cfg.mapbox) {
    $('mapbox-radius').value = cfg.mapbox.radius;
    $('mapbox-radius').addEventListener('change', () => scheduleAuto(100));
  } else {
    $('mapbox-radius-row').hidden = true;
  }

  $('run').addEventListener('click', run);
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') run();
  });
  $('toggle-3d').addEventListener('click', () => {
    state.mode3d = !state.mode3d;
    $('toggle-3d').classList.toggle('active', state.mode3d);
    if (state.mode3d && state.result) view3d();
    else map.easeTo({pitch: state.mode3d ? 60 : 0, bearing: 0, duration: 700});
    refreshLayers();
  });
  $('basemap').addEventListener('change', () => {
    for (const id of ['gray', 'imagery', 'osm']) map.setLayoutProperty(id, 'visibility', id === $('basemap').value ? 'visible' : 'none');
  });
}

function drawExtent(bbox) {
  const [x0, y0, x1, y1] = bbox;
  map.addSource('extent', {type: 'geojson', data: {type: 'Feature', geometry: {type: 'LineString',
    coordinates: [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]}}});
  map.addLayer({id: 'extent', type: 'line', source: 'extent', paint: {'line-color': '#1f6feb', 'line-width': 1.5, 'line-dasharray': [4, 3]}});
}

async function init() {
  const cfg = await api('/api/config');
  state.config = cfg;
  setupControls(cfg);
  map.on('load', () => drawExtent(cfg.bbox));
  map.fitBounds([[cfg.bbox[0], cfg.bbox[1]], [cfg.bbox[2], cfg.bbox[3]]], {padding: 20, duration: 0});
  const hash = new URLSearchParams(location.hash.slice(1)).get('p');
  if (hash) {
    state.points = parsePoints(hash).points;
    state.fitNext = true;
    pointsChanged();
    run();
  } else {
    $('preset').value = cfg.presets[0].key;
    $('preset').dispatchEvent(new Event('change'));
  }
}

init().catch((e) => status(`Cannot reach the server: ${e.message}`, 'error'));
