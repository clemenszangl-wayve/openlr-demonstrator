'use strict';

const FRC_COLORS = ['#c0392b', '#e67e22', '#d4ac0d', '#27ae60', '#16a085', '#2980b9', '#7f8c8d', '#95a5a6'];
const LEVEL_COLORS = {elevated: '#f39c12', ground: '#5d6d7e', tunnel: '#8073ac'};
const COLORS = {valhalla: '#111', original: '#2f80ed', decoded: '#d81b60', osmDecoded: '#00897b', transferred: '#ff9800',
  wrong: '#ff1100'};
const TILE_NAMES = {streets: 'Mapbox Streets', satellite: 'Mapbox Satellite Streets', light: 'Mapbox Light'};
const OSM_TILES = {type: 'raster', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize: 256, maxzoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'};

const state = {
  config: null,
  points: [],
  route: null,
  decoded: null,
  markers: [],
  lrpMarkers: {left: [], right: []},
  hidden: {left: new Set(), right: new Set(['osm-decoded', 'transferred'])},
  syncing: false,
  autoTimer: null,
  routeSeq: 0,
  decodeSeq: 0,
};

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const pct = (x) => (x == null ? '-' : `${Math.round(100 * x)}%`);
const num = (x, d = 2) => (x == null ? '-' : Number(x).toFixed(d));
const metres = (x, d = 1) => (x == null ? '-' : `${Number(x).toFixed(d)} m`);
const km = (m) => (m == null ? '-' : m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);
const levelName = (lv) => (lv < 0 ? 'tunnel' : lv === 0 ? 'ground' : 'elevated');
const frcText = (frc) => (frc == null ? 'any road' : `FRC${frc} ${state.config.frc_labels[frc]}`);
const bar = (x) => (x == null ? '' : `<span class="metric-bar"><i style="width:${Math.max(0, Math.min(1, x)) * 100}%"></i></span>`);
const chips = (runs) => `<span class="chips">${runs.map(([c, m]) => `<span class="${esc(c)}">${esc(c)} ${km(m)}</span>`).join('')}</span>`;

function status(side, text, kind = '') {
  const el = $(`status-${side}`);
  el.textContent = text;
  el.className = `msg ${kind}`;
}

async function api(path, body) {
  const options = body === undefined ? undefined
    : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// ------------------------------------------------------------------ points <-> text
function formatPoints(points) {
  return points.map((p) => `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}${p.frc == null ? '' : `, FRC${p.frc}`}`
    + (p.heading == null ? '' : `, ${Math.round(p.heading)}°`)).join('\n');
}

function parsePoints(text) {
  const points = [];
  const errors = [];
  const entries = text.split('\n').flatMap((l) => (l.includes(';') ? l.split(';') : [l]));
  entries.forEach((raw) => {
    let line = raw.replace(/#.*/, '').trim();
    if (!line) return;
    const frcMatch = line.match(/frc\s*[:=]?\s*([0-7])\b/i);
    if (frcMatch) line = line.replace(frcMatch[0], ' ');
    const headingMatch = line.match(/(\d{1,3}(?:\.\d+)?)\s*°|\bh(?:eading)?\s*[:=]?\s*(\d{1,3}(?:\.\d+)?)/i);
    if (headingMatch) line = line.replace(headingMatch[0], ' ');
    const nums = line.split(/[\s,]+/).filter(Boolean).map(Number);
    if (nums.length < 2 || nums.slice(0, 2).some(Number.isNaN)) {
      errors.push(`cannot read "${raw.trim()}"`);
      return;
    }
    let [a, b] = nums;
    if (Math.abs(a) > 90) [a, b] = [b, a]; // lon, lat order
    let frc = frcMatch ? Number(frcMatch[1]) : null;
    if (frc == null && nums.length >= 3 && Number.isInteger(nums[2]) && nums[2] >= 0 && nums[2] <= 7) frc = nums[2];
    const heading = headingMatch ? Number(headingMatch[1] ?? headingMatch[2]) % 360 : null;
    points.push({lat: a, lon: b, frc, heading});
  });
  return {points, errors};
}

function updateHash() {
  const p = state.points.map((q) => `${q.lat.toFixed(6)},${q.lon.toFixed(6)}${q.frc == null ? '' : `,${q.frc}`}`
    + (q.heading == null ? '' : `,h${Math.round(q.heading)}`)).join(';');
  const params = new URLSearchParams();
  if (p) params.set('p', p);
  params.set('d', $('spacing').value);
  history.replaceState(null, '', `#${params.toString().replace(/%2C/g, ',').replace(/%3B/g, ';')}`);
}

function pointsChanged({fromText = false} = {}) {
  if (!fromText) $('points').value = formatPoints(state.points);
  updateHash();
  renderMarkers();
  scheduleAuto();
}

function scheduleAuto(delay = 700) {
  clearTimeout(state.autoTimer);
  if ($('auto').checked && state.points.length >= 2) state.autoTimer = setTimeout(route, delay);
}

// ------------------------------------------------------------------ maps
function overlayLayers(defs) {
  return defs.map(([id, source, type, paint, extra = {}]) => ({id, source, type, paint, ...extra}));
}

const LEFT_LAYERS = overlayLayers([
  ['osm-route-casing', 'osm-route', 'line', {'line-color': '#fff', 'line-width': 11, 'line-opacity': 0.9},
    {layout: {'line-cap': 'round', 'line-join': 'round'}}],
  ['osm-route', 'osm-route', 'line', {
    'line-color': ['match', ['get', 'lvl'], 'elevated', LEVEL_COLORS.elevated, 'tunnel', LEVEL_COLORS.tunnel, LEVEL_COLORS.ground],
    'line-width': 7, 'line-opacity': 0.9}, {layout: {'line-cap': 'round', 'line-join': 'round'}}],
  ['valhalla', 'valhalla', 'line', {'line-color': COLORS.valhalla, 'line-width': 2, 'line-dasharray': [2, 2]}],
]);

const RIGHT_LAYERS = overlayLayers([
  ['original', 'original', 'line', {'line-color': COLORS.original, 'line-width': 13, 'line-opacity': 0.35},
    {layout: {'line-cap': 'round', 'line-join': 'round'}}],
  ['osm-decoded', 'osm-decoded', 'line', {'line-color': COLORS.osmDecoded, 'line-width': 3, 'line-dasharray': [1, 1.5]}],
  ['transferred', 'transferred', 'line', {'line-color': COLORS.transferred, 'line-width': 3}],
  ['wrong', 'wrong', 'line', {'line-color': COLORS.wrong, 'line-width': 12, 'line-opacity': 0.45, 'line-blur': 2},
    {layout: {'line-cap': 'round', 'line-join': 'round'}}],
  ['decoded', 'decoded', 'line', {'line-color': COLORS.decoded, 'line-width': 4.5},
    {filter: ['!', ['get', 'tunnel']], layout: {'line-cap': 'round', 'line-join': 'round'}}],
  ['decoded-tunnel', 'decoded', 'line', {'line-color': COLORS.decoded, 'line-width': 4.5, 'line-dasharray': [1.2, 1]},
    {filter: ['get', 'tunnel']}],
  ['candidates', 'candidates', 'circle', {
    'circle-radius': ['case', ['get', 'chosen'], 6, 4.5],
    'circle-color': ['case', ['get', 'chosen'], COLORS.decoded, '#fff'],
    'circle-stroke-color': ['case', ['get', 'chosen'], '#fff', '#555'], 'circle-stroke-width': 2}],
]);

const LEGENDS = {
  left: [
    ['osm-route', `<span class="swatch" style="background:${LEVEL_COLORS.elevated}"></span>Route on OSM:&nbsp;
      <span style="color:${LEVEL_COLORS.elevated}">elevated</span>&nbsp;/&nbsp;<span style="color:${LEVEL_COLORS.ground}">ground</span>&nbsp;/&nbsp;<span
      style="color:${LEVEL_COLORS.tunnel}">tunnel</span>`, ['osm-route', 'osm-route-casing']],
    ['valhalla', '<span class="swatch" style="background:repeating-linear-gradient(90deg,#111 0 4px,transparent 4px 8px)"></span>Valhalla route shape', ['valhalla']],
    ['lrps', '<span class="dot" style="background:#1a9850"></span>OpenLR LRPs (arrow = bearing)', []],
  ],
  right: [
    ['original', `<span class="swatch" style="background:${COLORS.original};opacity:.5;height:8px"></span>Original route (OSM)`, ['original']],
    ['decoded', `<span class="swatch" style="background:${COLORS.decoded}"></span>Decoded on Mapbox (dashed: tunnel)`, ['decoded', 'decoded-tunnel']],
    ['wrong', `<span class="swatch" style="background:${COLORS.wrong};opacity:.5;height:8px"></span>Different road than the original`, ['wrong']],
    ['lrps', '<span class="dot" style="background:#0b6e4f"></span>LRPs from the OpenLR code', []],
    ['candidates', `<span class="dot" style="background:${COLORS.decoded}"></span>Mapbox candidates (filled = chosen)`, ['candidates']],
    ['osm-decoded', `<span class="swatch" style="background:${COLORS.osmDecoded}"></span>Same code decoded on OSM`, ['osm-decoded']],
    ['transferred', `<span class="swatch" style="background:${COLORS.transferred}"></span>Mapbox route matched back onto OSM`, ['transferred']],
  ],
};

function emptyStyle(sources, layers) {
  return {version: 8, sources, layers};
}

function rightBasemaps() {
  const styles = state.config.tile_styles || [];
  if (!styles.length) return {osm: {...OSM_TILES}};
  return Object.fromEntries(styles.map((s) => [s, {
    type: 'raster', tiles: [`/tiles/mapbox/${s}/{z}/{x}/{y}.png`], tileSize: 256, maxzoom: 22,
    attribution: '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> &copy; OpenStreetMap',
  }]));
}

let leftMap;
let rightMap;
const hoverPopups = new Map();
let popup = null;

function makeMap(container, sources, baseLayers, overlay, bounds) {
  const geo = Object.fromEntries([...new Set(overlay.map((l) => l.source))].map((s) => [s, {
    type: 'geojson', data: {type: 'FeatureCollection', features: []}}]));
  const map = new maplibregl.Map({
    container, style: emptyStyle({...sources, ...geo}, [
      {id: 'background', type: 'background', paint: {'background-color': '#f2f2f0'}}, ...baseLayers, ...overlay]),
    bounds, fitBoundsOptions: {padding: 20}, attributionControl: {compact: true}, dragRotate: false, pitchWithRotate: false,
    preserveDrawingBuffer: true,
  });
  map.touchZoomRotate.disableRotation();
  map.on('load', () => { map.ready = true; });
  map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
  map.addControl(new maplibregl.ScaleControl(), 'bottom-right');
  const hover = new maplibregl.Popup({closeButton: false, closeOnClick: false, className: 'hover-popup', offset: 8});
  hoverPopups.set(map, hover);
  overlay.filter((l) => l.type === 'line').forEach((l) => {
    map.on('mousemove', l.id, (e) => {
      const f = e.features && e.features[0];
      if (!f || !f.properties.label) return;
      map.getCanvas().style.cursor = 'pointer';
      hover.setLngLat(e.lngLat).setHTML(esc(f.properties.label)).addTo(map);
    });
    map.on('mouseleave', l.id, () => {
      map.getCanvas().style.cursor = '';
      hover.remove();
    });
  });
  return map;
}

function whenReady(map, fn) {
  if (map.ready) fn();
  else map.once('load', fn);
}

function setData(map, source, features) {
  whenReady(map, () => map.getSource(source).setData({type: 'FeatureCollection', features}));
}

const line = (coords, props) => ({type: 'Feature', geometry: {type: 'LineString', coordinates: coords}, properties: props});
const point = (lon, lat, props) => ({type: 'Feature', geometry: {type: 'Point', coordinates: [lon, lat]}, properties: props});

function syncMaps(a, b) {
  a.on('move', () => {
    if (!$('sync').checked || state.syncing) return;
    state.syncing = true;
    b.jumpTo({center: a.getCenter(), zoom: a.getZoom()});
    state.syncing = false;
  });
}

function closePopup() {
  if (popup) popup.remove();
  popup = null;
}

function openPopup(map, lngLat, html) {
  closePopup();
  popup = new maplibregl.Popup({closeOnClick: false, maxWidth: '380px'}).setLngLat(lngLat).setHTML(html).addTo(map);
  return popup.getElement();
}

function renderLegend(side) {
  const map = side === 'left' ? leftMap : rightMap;
  const el = $(`legend-${side}`);
  el.innerHTML = LEGENDS[side].map(([key, html]) =>
    `<label><input type="checkbox" data-key="${key}" ${state.hidden[side].has(key) ? '' : 'checked'}>${html}</label>`).join('');
  el.querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
    const key = cb.dataset.key;
    if (cb.checked) state.hidden[side].delete(key);
    else state.hidden[side].add(key);
    applyVisibility(side, map);
  }));
  applyVisibility(side, map);
}

function applyVisibility(side, map) {
  const apply = () => LEGENDS[side].forEach(([key, , layers]) => {
    const visible = !state.hidden[side].has(key);
    layers.forEach((id) => map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none'));
    if (key === 'lrps') state.lrpMarkers[side].forEach((m) => { m.getElement().style.display = visible ? '' : 'none'; });
  });
  whenReady(map, apply);
}

// ------------------------------------------------------------------ route points on the left map
function renderMarkers() {
  state.markers.forEach((m) => m.remove());
  state.markers = state.points.map((p, i) => {
    const el = document.createElement('div');
    el.className = 'pt';
    const color = p.frc == null ? '#555' : FRC_COLORS[p.frc];
    el.style.borderColor = color;
    el.innerHTML = `<b style="background:${color}">${i + 1}</b>${p.frc == null ? 'any' : `FRC${p.frc}`}`
      + (p.heading == null ? '' : `<i class="heading" style="transform:rotate(${p.heading}deg)">&uarr;</i>`);
    el.title = `Point ${i + 1}: ${frcText(p.frc)}${p.heading == null ? '' : `, heading ${Math.round(p.heading)}°`}`
      + ' - drag to move, click to edit';
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      askRoad(i, false);
    });
    const marker = new maplibregl.Marker({element: el, draggable: true}).setLngLat([p.lon, p.lat]).addTo(leftMap);
    marker.on('dragend', () => {
      const ll = marker.getLngLat();
      p.lat = ll.lat;
      p.lon = ll.lng;
      pointsChanged();
    });
    return marker;
  });
}

const sameHeading = (a, b) => (a == null || b == null ? a == b : Math.abs(((a - b + 540) % 360) - 180) < 30);

async function roadChoices(p) {
  const {roads} = await api(`/api/nearby?lat=${p.lat}&lon=${p.lon}`);
  const groups = [];
  roads.forEach((r) => {
    const g = groups.find((c) => c.frc === r.frc && sameHeading(c.heading, r.heading));
    if (g) g.roads.push(r);
    else groups.push({frc: r.frc, heading: r.heading, roads: [r]});
  });
  return groups;
}

async function askRoad(i, isNew) {
  const p = state.points[i];
  let choices = [];
  try {
    choices = await roadChoices(p);
  } catch (e) {
    status('left', e.message, 'error');
  }
  if (isNew && choices.length <= 1) {
    if (choices.length === 1) [p.frc, p.heading] = [choices[0].frc, choices[0].heading];
    else status('left', `Point ${i + 1}: no OSM road within 40 m`, 'warn');
    pointsChanged();
    return;
  }
  const buttons = choices.map((c, k) => {
    const names = [...new Set(c.roads.map((r) => `${r.name} - level ${r.level} (${levelName(r.level)})`))].slice(0, 2);
    const current = c.frc === p.frc && sameHeading(c.heading, p.heading);
    const dir = c.heading == null ? '' : ` <i class="heading" style="transform:rotate(${c.heading}deg)">&uarr;</i> ${c.heading}&deg;`;
    return `<button class="choice" data-k="${k}"><b>${esc(frcText(c.frc))}${dir}${current ? ' &#10003;' : ''}</b>
      <small>${names.map(esc).join('<br>')} &middot; ${c.roads[0].distance} m</small></button>`;
  });
  buttons.push(`<button class="choice" data-k=""><b>Any road${p.frc == null ? ' &#10003;' : ''}</b>
    <small>Valhalla snaps to the nearest road</small></button>`);
  const el = openPopup(leftMap, [p.lon, p.lat], `<div class="popup"><h4>Point ${i + 1}: which road do you mean?</h4>
    ${choices.length ? '' : '<p class="hint">No OSM road within 40 m.</p>'}${buttons.join('')}
    <div class="row" style="margin-top:6px"><span class="spacer"></span>
    <button class="small danger" data-delete>Delete point</button></div></div>`);
  el.querySelectorAll('.choice').forEach((b) => b.addEventListener('click', () => {
    const c = b.dataset.k === '' ? {frc: null, heading: null} : choices[Number(b.dataset.k)];
    [p.frc, p.heading] = [c.frc, c.heading];
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

// ------------------------------------------------------------------ LRP markers (bearing arrows)
function lrpMarkers(side, map, lrps, onClick) {
  state.lrpMarkers[side].forEach((m) => m.remove());
  state.lrpMarkers[side] = lrps.map((p, i) => {
    const el = document.createElement('div');
    el.className = `lrp${side === 'right' ? ' received' : ''}`;
    el.innerHTML = `<span class="arrow" style="transform:rotate(${p.bear}deg)"></span><b>${i + 1}</b>`;
    el.title = `LRP ${i + 1}: ${p.frc} / ${p.fow}, bearing ${p.bear}°`;
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      onClick(p, i);
    });
    return new maplibregl.Marker({element: el}).setLngLat([p.lon, p.lat]).addTo(map);
  });
  applyVisibility(side, map);
}

function lrpRows(p) {
  return [
    ['position', `${num(p.lat, 6)}, ${num(p.lon, 6)}`],
    ['FRC / FOW', `${p.frc} / ${p.fow}`],
    ['bearing', `${p.bear}&deg;`],
    ['LFRCNP', p.lfrcnp ?? '- (last LRP)'],
    ['DNP', p.dnp == null ? '-' : `${p.dnp} m`],
  ];
}

const table = (rows) => `<table>${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('')}</table>`;

// ------------------------------------------------------------------ left: route + encode
async function route() {
  if (state.points.length < 2) {
    status('left', 'Click the map to add at least two route points.');
    return;
  }
  const seq = ++state.routeSeq;
  ++state.decodeSeq;
  clearRight();
  status('right', '');
  $('decode').disabled = false;
  status('left', 'Routing with Valhalla, placing the route on OSM, encoding ...', 'busy');
  $('run').disabled = true;
  try {
    const r = await api('/api/route', {points: state.points, disambiguate: $('disambiguate').checked,
      max_lrp_distance: Number($('spacing').value)});
    if (seq !== state.routeSeq) return;
    state.route = r;
    renderLeft();
    $('code').value = r.openlr.binary;
    status('left', '');
    decode();
  } catch (e) {
    if (seq === state.routeSeq) status('left', e.message, 'error');
  } finally {
    if (seq === state.routeSeq) $('run').disabled = false;
  }
}

function renderLeft() {
  const r = state.route;
  setData(leftMap, 'valhalla', [line(r.valhalla.shape, {label: `Valhalla route: ${km(r.valhalla.length)}`})]);
  setData(leftMap, 'osm-route', r.osm.paths.map((p) => line(p.path, {lvl: levelName(p.level), label: p.label})));
  lrpMarkers('left', leftMap, r.openlr.lrps, (p, i) => openPopup(leftMap, [p.lon, p.lat],
    `<div class="popup"><h4>Encoded LRP ${i + 1}</h4>${table([...lrpRows(p),
      ['node', p.valid ? 'valid (Rule 4)' : 'invalid - no alternative'], ['OSM line', esc(p.line)]])}</div>`));
  const agree = r.osm.way_agreement;
  $('route-info').innerHTML = `<table class="table">
    <tr><th>Valhalla route</th><td>${km(r.valhalla.length)} ${chips(r.valhalla.classes)}
      ${r.valhalla.cached ? '<span class="hint">(cached)</span>' : ''}</td></tr>
    <tr><th>On the OSM network</th><td>${km(r.osm.length)}, ${r.osm.paths.length} lines;
      <span class="${agree > 0.98 ? 'ok' : 'warn'}">${pct(agree)}</span> of the Valhalla route on the same OSM ways</td></tr>
  </table>`;
  renderCode(r.openlr);
}

function renderCode(o) {
  $('code-info').textContent = `${o.binary.length} characters, ${o.bytes} bytes, ${o.lrps.length} LRPs`
    + (o.p_off || o.n_off ? `, offsets ${Math.round(o.p_off)} / ${Math.round(o.n_off)} m` : '');
  $('lrp-table').innerHTML = `<table class="table"><tr><th>LRP</th><th>FRC</th><th>FOW</th><th class="num">bearing</th>
    <th>LFRCNP</th><th class="num">DNP</th><th>OSM line</th></tr>${o.lrps.map((p) => `<tr>
    <td>${p.index}</td><td>${p.frc}</td><td>${p.fow}</td><td class="num">${p.bear}&deg;</td><td>${p.lfrcnp ?? '-'}</td>
    <td class="num">${p.dnp == null ? '-' : `${p.dnp} m`}</td><td class="roads">${esc(p.line)}${p.valid ? '' : ' <span class="warn">(invalid node)</span>'}</td>
    </tr>`).join('')}</table>`;
  $('encoder-log').querySelector('ol').innerHTML = o.steps.map((s) => `<li>${esc(s)}</li>`).join('');
}

// ------------------------------------------------------------------ right: decode on Mapbox
async function decode() {
  const binary = $('code').value.trim();
  if (!binary) return;
  const routeId = state.route && state.route.openlr.binary === binary ? state.route.route_id : null;
  const seq = ++state.decodeSeq;
  clearRight();
  status('right', 'Decoding on Mapbox: Tilequery candidates, then one Directions request per LRP pair ...', 'busy');
  $('decode').disabled = true;
  try {
    const r = await api('/api/decode', {binary, route_id: routeId});
    if (seq !== state.decodeSeq) return;
    state.decoded = r;
    renderRight();
    status('right', '');
    if (!routeId) fitTo(r.reference.points.map((p) => [p.lon, p.lat]));
  } catch (e) {
    if (seq === state.decodeSeq) status('right', e.message, 'error');
  } finally {
    if (seq === state.decodeSeq) $('decode').disabled = false;
  }
}

function clearRight() {
  state.decoded = null;
  [...new Set(RIGHT_LAYERS.map((l) => l.source))].forEach((s) => setData(rightMap, s, []));
  lrpMarkers('right', rightMap, [], () => {});
  ['verdict', 'accuracy', 'similarity'].forEach((id) => { $(id).innerHTML = ''; });
  $('verdict').className = '';
  $('reference-card').hidden = true;
  $('decoding-card').hidden = true;
}

function renderRight() {
  const r = state.decoded;
  const mb = r.mapbox;
  setData(rightMap, 'original', (r.original ? r.original.paths : []).map((p) => line(p.path, {label: `Original: ${p.label}`})));
  setData(rightMap, 'osm-decoded', r.osm_decoded.paths.map((p) => line(p.path, {label: `Decoded on OSM: ${p.label}`})));
  setData(rightMap, 'transferred', r.transferred.map((p) => line(p.path, {label: `Mapbox route on OSM: ${p.label}`})));
  setData(rightMap, 'wrong', r.differences.wrong.map((p) => line(p.path, {label: `Not in the original: ${p.label}`})));
  setData(rightMap, 'decoded', mb.segments.map((s) => line(s.path, {tunnel: s.tunnel, label: `Mapbox: ${s.label}`})));
  setData(rightMap, 'candidates', mb.candidates.flatMap((cands, i) => cands.map((c, k) =>
    point(c.lon, c.lat, {lrp: i, k, chosen: c.chosen}))));
  lrpMarkers('right', rightMap, r.reference.points, (p, i) => showReceivedLrp(p, i));
  renderVerdict(r);
  renderMetrics(r);
  renderReference(r);
  renderDecoding(r);
  const style = TILE_NAMES[$('basemap-right').value] || 'OpenStreetMap';
  $('right-foot').innerHTML = `Basemap: ${esc(style)} tiles, proxied by the local server (the token stays there).
    The decoder uses the <code>${esc(mb.profile)}</code> routing profile; basemap and routing graph can differ slightly.`;
}

function showReceivedLrp(p, i) {
  const cands = state.decoded.mapbox.candidates[i] || [];
  const rows = cands.map((c) => `<tr class="${c.chosen ? 'chosen' : ''}"><td>${c.chosen ? '&#10003;' : ''}</td>
    <td>${esc(c.label)}</td><td>FRC${c.frc}</td><td class="num">${c.distance} m</td><td class="num">${num(c.score, 2)}</td></tr>`).join('');
  openPopup(rightMap, [p.lon, p.lat], `<div class="popup"><h4>LRP ${i + 1} as received</h4>${table(lrpRows(p))}
    <h4>Mapbox candidates</h4><table class="table">${rows || '<tr><td>none</td></tr>'}</table></div>`);
}

function renderVerdict(r) {
  const el = $('verdict');
  const mb = r.mapbox;
  const s = r.similarity || {};
  const vs = r.original ? 'the original route' : 'the same code decoded on OSM (no original route: pasted code)';
  if (!mb.segments.length || !r.accuracy) {
    el.className = 'bad';
    el.innerHTML = `<b>&#10007; Not decoded on Mapbox</b> &ndash; ${esc(mb.error || 'no route found')}`;
    return;
  }
  const a = r.accuracy;
  const same = a.correct && r.similar;
  el.className = same ? 'ok' : 'bad';
  const head = same ? '&#10003; Same route on Mapbox' : a.correct ? '&#9888; Same roads, but the geometry differs'
    : '&#10007; Mapbox decoded a different route';
  el.innerHTML = `<b>${head}</b> &ndash; compared with ${vs}.<br>
    Accuracy ${pct(a.precision)} precision / ${pct(a.recall)} recall / ${pct(a.level_accuracy)} on the right level;
    similarity F1 ${num(s.buffer && s.buffer['10'].f1)} within 10 m, Fr&eacute;chet ${metres(s.frechet)},
    road classes agree on ${pct(s.frc_agreement)}.
    ${differenceText(r.differences)}
    ${mb.checked ? '' : '<br><span class="warn">No LRP pair passed all OpenLR checks - best-effort route shown.</span>'}`;
}

function differenceText(d) {
  const roads = (list) => list.sort((a, b) => b[2] - a[2]).slice(0, 3)
    .map(([name, level, m]) => `${esc(name)} (level ${level > 0 ? '+' : ''}${level}, ${levelName(level)}) ${km(m)}`).join(', ');
  if (!d.wrong_roads.length && !d.missed_roads.length) return '';
  return `<br>Mapbox used <b>${roads(d.wrong_roads) || '-'}</b> instead of <b>${roads(d.missed_roads) || '-'}</b>.`;
}

function renderMetrics(r) {
  const a = r.accuracy;
  const s = r.similarity || {};
  $('accuracy').innerHTML = !a ? '' : `<h2>Accuracy</h2>
    <p class="hint">Mapbox route matched back onto OSM, compared line by line with ${r.original ? 'the original' : 'the OSM decode'}.</p>
    <table class="table">
      <tr><td>Precision <small class="hint">(decoded length on the route)</small></td><td class="num">${pct(a.precision)}${bar(a.precision)}</td></tr>
      <tr><td>Recall <small class="hint">(route length found)</small></td><td class="num">${pct(a.recall)}${bar(a.recall)}</td></tr>
      <tr><td>Level accuracy <small class="hint">(right deck)</small></td><td class="num">${pct(a.level_accuracy)}${bar(a.level_accuracy)}</td></tr>
      <tr><td>Correct <small class="hint">(P, R &ge; 95%)</small></td><td class="num ${a.correct ? 'ok' : 'bad'}">${a.correct ? 'yes' : 'no'}</td></tr>
    </table>
    <h4>Decoded road classes (Mapbox)</h4>${chips(r.mapbox.classes)}`;
  if (!s.buffer) {
    $('similarity').innerHTML = '';
    return;
  }
  const buf = ['5', '10', '20'].map((t) => `<tr><td>within ${t} m</td><td class="num">${num(s.buffer[t].precision)}</td>
    <td class="num">${num(s.buffer[t].recall)}</td><td class="num"><b>${num(s.buffer[t].f1)}</b></td></tr>`).join('');
  $('similarity').innerHTML = `<h2>Similarity</h2>
    <p class="hint">Geometry of the Mapbox route vs. ${r.original ? 'the original' : 'the OSM decode'}, plus road attributes.</p>
    <table class="table">
      <tr><th>Buffer overlap</th><th class="num">precision</th><th class="num">recall</th><th class="num">F1</th></tr>${buf}
    </table>
    <table class="table" style="margin-top:6px">
      <tr><td>Length original / decoded</td><td class="num">${km(s.length_original)} / ${km(s.length_decoded)} (&times;${num(s.length_ratio, 3)})</td></tr>
      <tr><td>Deviation mean / median / p95</td><td class="num">${metres(s.deviation_mean)} / ${metres(s.deviation_median)} / ${metres(s.deviation_p95)}</td></tr>
      <tr><td>Hausdorff / Fr&eacute;chet distance</td><td class="num">${metres(s.hausdorff)} / ${metres(s.frechet)}</td></tr>
      <tr><td>Start / end offset</td><td class="num">${metres(s.start_offset)} / ${metres(s.end_offset)}</td></tr>
      <tr><td>Road class (FRC) agreement</td><td class="num">${pct(s.frc_agreement)}${bar(s.frc_agreement)}</td></tr>
      <tr><td>Tunnel / not tunnel agreement</td><td class="num">${pct(s.tunnel_agreement)}${bar(s.tunnel_agreement)}</td></tr>
    </table>`;
}

function renderReference(r) {
  const ref = r.reference;
  const el = $('reference-card');
  el.hidden = false;
  el.innerHTML = `<h3>OpenLR code as received</h3>
    <p class="hint mono">${esc(r.binary)}</p>
    <table class="table"><tr><th>LRP</th><th>lat, lon</th><th>FRC</th><th>FOW</th><th class="num">bearing</th><th>LFRCNP</th>
    <th class="num">DNP</th></tr>${ref.points.map((p) => `<tr><td>${p.index}</td><td class="mono">${num(p.lat, 5)}, ${num(p.lon, 5)}</td>
    <td>${p.frc}</td><td>${p.fow}</td><td class="num">${p.bear}&deg;</td><td>${p.lfrcnp ?? '-'}</td>
    <td class="num">${p.dnp == null ? '-' : `${p.dnp} m`}</td></tr>`).join('')}</table>
    <p class="hint">Offsets: positive ${pct(ref.poffs)} of the first leg, negative ${pct(ref.noffs)} of the last leg
      (on Mapbox: ${metres(r.mapbox.p_off, 0)} / ${metres(r.mapbox.n_off, 0)}).
      ${r.osm_decoded.error ? `Decoding on OSM failed: ${esc(r.osm_decoded.error)}` : `Decoded on OSM: ${km(r.osm_decoded.length)}.`}</p>`;
}

function renderDecoding(r) {
  const mb = r.mapbox;
  const el = $('decoding-card');
  el.hidden = false;
  const cands = mb.candidates.map((cs, i) => cs.map((c, k) => `<tr class="${c.chosen ? 'chosen' : ''}">
    <td>${k === 0 ? `LRP ${i + 1}` : ''}</td><td>${c.chosen ? '&#10003;' : ''}</td><td>${esc(c.label)}</td><td>FRC${c.frc}</td>
    <td class="num">${c.distance} m</td><td class="num">${num(c.score, 2)}</td></tr>`).join('')
    || `<tr><td>LRP ${i + 1}</td><td></td><td colspan="4" class="bad">no drivable road within the search radius</td></tr>`).join('');
  const legs = mb.attempts.map((a) => `<tr class="${a.status === 'ok' ? 'chosen' : 'failed'}">
    <td>${a.from} &rarr; ${a.to}</td><td>${esc(a.start)} &rarr; ${esc(a.end)}</td>
    <td class="num">${km(a.length)} / ${km(a.dnp)}</td><td class="${a.status === 'ok' ? 'ok' : 'bad'}">${esc(a.status)}</td></tr>`).join('');
  const t = r.seconds;
  el.innerHTML = `<h3>How the code was decoded on Mapbox</h3>
    <p class="hint">Candidates: Mapbox Tilequery (road layer) around each LRP, rated by distance, FRC and FOW.
      Each LRP pair: a Directions request from candidate to candidate (start bearing &plusmn;40&deg;), accepted if the route
      starts on the candidate's road class, its length matches DNP and no road below LFRCNP is used.</p>
    <h4>Candidates per LRP</h4>
    <table class="table"><tr><th></th><th></th><th>road</th><th>FRC</th><th class="num">distance</th><th class="num">score</th></tr>${cands}</table>
    <h4>Route attempts</h4>
    <table class="table"><tr><th>LRPs</th><th>from &rarr; to</th><th class="num">length / DNP</th><th>check</th></tr>
    ${legs || '<tr><td colspan="4">none</td></tr>'}</table>
    <p class="hint">Mapbox API requests for this decode: <b>${mb.requests}</b> (the rest came from the local cache);
      ${mb.attempts.length} route attempts. Time: OSM decode ${Math.round(1000 * t.osm_decode)} ms, Mapbox decode
      ${Math.round(1000 * t.mapbox_decode)} ms.</p>`;
}

function fitTo(coords) {
  if (!coords.length) return;
  const b = coords.reduce((acc, c) => acc.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
  leftMap.fitBounds(b, {padding: 60, maxZoom: 16, duration: 0});
  if (!$('sync').checked) rightMap.fitBounds(b, {padding: 60, maxZoom: 16, duration: 0});
}

// ------------------------------------------------------------------ wiring
function loadPreset(key) {
  const preset = state.config.presets.find((p) => p.key === key);
  if (!preset) return;
  state.points = preset.route_points.map((p) => ({...p}));
  pointsChanged();
  fitTo(state.points.map((p) => [p.lon, p.lat]));
  clearTimeout(state.autoTimer);
  route();
}

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const d = params.get('d');
  if (d && [...$('spacing').options].some((o) => o.value === d)) $('spacing').value = d;
  const p = params.get('p');
  if (!p) return false;
  const {points} = parsePoints(p.replace(/;/g, '\n'));
  if (points.length < 2) return false;
  state.points = points;
  return true;
}

async function init() {
  state.config = await api('/api/config');
  const c = state.config;
  const [lonMin, latMin, lonMax, latMax] = c.bbox;
  const bounds = [[lonMin, latMin], [lonMax, latMax]];
  $('profile').textContent = c.mapbox ? c.mapbox.profile : 'not configured';
  const spacing = String(c.max_lrp_distance.default);
  if (![...$('spacing').options].some((o) => o.value === spacing)) $('spacing').add(new Option(`${spacing} m`, spacing));
  $('spacing').value = spacing;
  $('spacing').options[[...$('spacing').options].findIndex((o) => o.value === spacing)].text += ' - recommended';

  leftMap = makeMap('map-left', {osm: OSM_TILES}, [{id: 'osm', type: 'raster', source: 'osm'}], LEFT_LAYERS, bounds);
  const basemaps = rightBasemaps();
  const names = Object.keys(basemaps);
  rightMap = makeMap('map-right', basemaps, names.map((id, i) => ({id: `base-${id}`, type: 'raster', source: id,
    layout: {visibility: i === 0 ? 'visible' : 'none'}})), RIGHT_LAYERS, bounds);
  $('basemap-right').innerHTML = names.map((n) => `<option value="${n}">${TILE_NAMES[n] || 'OpenStreetMap'}</option>`).join('');
  $('basemap-right').addEventListener('change', () => {
    names.forEach((n) => rightMap.setLayoutProperty(`base-${n}`, 'visibility', n === $('basemap-right').value ? 'visible' : 'none'));
    if (state.decoded) renderRight();
  });
  syncMaps(leftMap, rightMap);
  syncMaps(rightMap, leftMap);
  renderLegend('left');
  renderLegend('right');
  rightMap.on('click', 'candidates', (e) => {
    const f = e.features[0];
    const cand = state.decoded.mapbox.candidates[f.properties.lrp][f.properties.k];
    openPopup(rightMap, e.lngLat, `<div class="popup"><h4>Mapbox candidate for LRP ${f.properties.lrp + 1}</h4>${table([
      ['road', esc(cand.label)], ['class / FRC', `${esc(cand.class)} / FRC${cand.frc}`], ['distance', `${cand.distance} m`],
      ['score', num(cand.score, 3)], ['chosen', cand.chosen ? 'yes' : 'no']])}</div>`);
  });
  rightMap.on('mouseenter', 'candidates', () => { rightMap.getCanvas().style.cursor = 'pointer'; });
  rightMap.on('mouseleave', 'candidates', () => { rightMap.getCanvas().style.cursor = ''; });

  if (!c.valhalla) status('left', 'Valhalla is not configured (VALHALLA_URL) - paste an OpenLR code instead.', 'warn');
  if (!c.mapbox) status('right', 'Mapbox is not configured: set MAPBOX_ACCESS_TOKEN in .env and restart.', 'error');

  $('preset').innerHTML += c.presets.map((p) => `<option value="${p.key}">${esc(p.title)}</option>`).join('');
  $('preset').addEventListener('change', () => loadPreset($('preset').value));
  $('points').addEventListener('input', () => {
    const {points, errors} = parsePoints($('points').value);
    $('points-msg').textContent = errors.join('; ');
    $('points-msg').className = `msg ${errors.length ? 'error' : ''}`;
    state.points = points;
    pointsChanged({fromText: true});
  });
  $('reverse').addEventListener('click', () => {
    state.points.reverse();
    pointsChanged();
  });
  $('clear').addEventListener('click', () => {
    state.points = [];
    pointsChanged();
  });
  $('spacing').addEventListener('change', () => {
    updateHash();
    scheduleAuto(0);
  });
  $('disambiguate').addEventListener('change', () => scheduleAuto(0));
  $('run').addEventListener('click', () => {
    clearTimeout(state.autoTimer);
    route();
  });
  $('decode').addEventListener('click', decode);
  $('code').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      decode();
    }
  });
  $('copy').addEventListener('click', () => navigator.clipboard && navigator.clipboard.writeText($('code').value.trim()));
  leftMap.on('click', (e) => {
    const target = e.originalEvent.target;
    if (target.closest && target.closest('.pt, .lrp, .maplibregl-popup')) return;
    if (popup) return closePopup();
    state.points.push({lat: e.lngLat.lat, lon: e.lngLat.lng, frc: null});
    askRoad(state.points.length - 1, true);
  });

  if (readHash()) {
    pointsChanged();
    fitTo(state.points.map((p) => [p.lon, p.lat]));
    clearTimeout(state.autoTimer);
    route();
  } else if (c.presets.some((p) => p.key === 'shuto3-exit')) {
    $('preset').value = 'shuto3-exit';
    loadPreset('shuto3-exit');
  }
}

init().catch((e) => status('left', `Cannot start: ${e.message}`, 'error'));
