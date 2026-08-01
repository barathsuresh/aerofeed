/* aerofeed frontend — vanilla JS, no build step.
 *
 * Connects to the local WebSocket server, upserts a Leaflet marker per aircraft
 * keyed by icao24, and reaps markers that stop being updated.
 *
 * Server protocol (see local/local_ws_server.py):
 *   -> {type:"subscribe",  bounds:[lamin,lomin,lamax,lomax]}
 *   <- {type:"subscribed", cells:[{key,bbox}], truncated, max_cells, ...}
 *   <- {type:"aircraft",   region_cell, state:{icao24, callsign, latitude, ...}}
 */

'use strict';

// Where to find the WebSocket endpoint. Read at runtime from config.json,
// which Terraform generates with the deployed API's wss:// URL — so the same
// bundle works against a local server and against AWS, and a redeployed API
// changes one generated object rather than this source file.
const CONFIG_URL = 'config.json';

// Fallback when config.json is absent: the local pipeline, served by
// `python -m http.server` next to run_local.py.
const LOCAL_WS_PORT = 8765;

// Resolved during boot, before the first connect().
let wsUrl = null;

// Fallback subscription point, used when the page URL carries no lat/lon.
// Matches config.py's default so frontend and backend agree out of the box.
const DEFAULT_LAT = 40.7;
const DEFAULT_LON = -74.0;

// A marker is dropped once this stale. OpenSky refreshes every 5-10s and the
// poller every 15s, so 90s is several missed updates — long enough not to
// flicker aircraft that simply did not move (the delta filter suppresses
// those), short enough that departures leave the map promptly.
const STALE_MS = 90_000;
const SWEEP_MS = 5_000;

// Reconnect backoff. The dev server restarts often; retry without hammering it.
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 15_000;

// Settle time after the map stops moving before re-subscribing. Long enough
// that a drag across several cells only resubscribes once at the end.
const MOVE_DEBOUNCE_MS = 400;

// Markers outside the viewport are kept in `tracked` but taken out of the DOM.
// The margin means a small pan reveals aircraft already placed rather than
// popping them in at the edge; 0.5 is half a viewport of slack on each side.
const CULL_MARGIN = 0.5;

// IP geolocation. No key, CORS-enabled, HTTPS. Startup never blocks on it —
// the timeout falls through to the configured default.
const GEOIP_URL = 'https://ipwho.is/';
const GEOIP_TIMEOUT_MS = 3_000;

// How long a tab may sit hidden before we drop the connection. The server
// cannot tell a backgrounded tab from an attentive one, so an abandoned tab
// would keep the poller — and the upstream API bill — running indefinitely.
// The grace period stops a quick alt-tab from churning the connection.
const HIDDEN_GRACE_MS = 60_000;

// icao24 -> {marker, lastSeen}. The single source of truth for what is on screen.
const tracked = new Map();

let map;
let reconnectDelay = RECONNECT_MIN_MS;
let socket = null;
let hiddenTimer = null;
let moveTimer = null;
let cellOutlines = [];
let firstSubscription = true;
let bootPoint = { lat: DEFAULT_LAT, lon: DEFAULT_LON };
// Set by anything that changes the count; drained once per animation frame.
let countDirty = false;
// Padded viewport, recomputed only when the map moves. upsert() consults it
// for every aircraft, ~70 times a second, so it must not allocate per call.
let cullBounds = null;
// Distinguishes "we closed this on purpose" from "the link dropped", so a
// deliberate pause does not trigger the reconnect backoff.
let paused = false;

const el = {
  count: document.getElementById('count'),
  dot: document.getElementById('dot'),
  statusText: document.getElementById('status-text'),
  cell: document.getElementById('cell'),
};

/* --- subscription point ---------------------------------------------------- */

/** Read lat/lon from the page URL, falling back to the defaults.
 *
 * Stands in for IP geolocation: localhost has none, so the coordinates are
 * passed through to the server, which snaps them to a region cell.
 */
function urlPoint() {
  const params = new URLSearchParams(location.search);
  const lat = Number(params.get('lat'));
  const lon = Number(params.get('lon'));
  // Number('') is 0, so check the param was actually present before trusting it.
  const hasLat = params.get('lat') !== null && Number.isFinite(lat);
  const hasLon = params.get('lon') !== null && Number.isFinite(lon);
  return hasLat && hasLon ? { lat, lon, source: 'url' } : null;
}

/** Approximate the viewer's location from their IP.
 *
 * Deliberately not navigator.geolocation: cells are 5 degrees — roughly 550km
 * — so city-level accuracy already rounds to the same cell as a GPS fix, and
 * the native API costs a permission prompt for precision that is discarded.
 *
 * The request goes to a third party and therefore discloses the viewer's IP
 * to them. That is the trade for not asking permission; `?lat=&lon=` skips
 * the lookup entirely for anyone who would rather it did not happen.
 */
async function geoipPoint() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), GEOIP_TIMEOUT_MS);
  try {
    const response = await fetch(GEOIP_URL, { signal: controller.signal });
    if (!response.ok) return null;
    const data = await response.json();
    const lat = Number(data.latitude);
    const lon = Number(data.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    return { lat, lon, source: data.city || 'your IP' };
  } catch {
    // Offline, blocked by an ad blocker, rate limited, timed out. All of them
    // mean the same thing here: fall back, never block startup on it.
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** URL override, then GeoIP, then the configured default. */
async function subscriptionPoint() {
  return urlPoint()
      || await geoipPoint()
      || { lat: DEFAULT_LAT, lon: DEFAULT_LON, source: 'default' };
}

/* --- map ------------------------------------------------------------------- */

function initMap(point) {
  map = L.map('map', { zoomControl: true }).setView([point.lat, point.lon], 7);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  // moveend covers pan and zoom both, and fires once the gesture settles.
  map.on('moveend', scheduleSubscription);
  // Culling runs on the same event but undebounced: the markers that just came
  // into view should appear with the pan, not 400ms after it.
  map.on('moveend', syncVisibility);
}

/** Frame the map on the cell the server actually assigned us.
 *
 * The requested point and the cell are not the same thing — snapping means the
 * point can sit near an edge, so the outline shows what is really covered.
 * Aircraft only exist inside it; that is the difference between "the sky is
 * empty here" and "nothing is subscribed here".
 */
function showCells(message, fit) {
  // Older single-cell shape stays supported so a stale tab keeps working.
  const cells = Array.isArray(message.cells) && message.cells.length
    ? message.cells
    : [{ key: message.region_cell, bbox: message.bbox }];

  el.cell.textContent = cells.length === 1
    ? `cell ${cells[0].key}`
    : `${cells.length} cells`;
  if (message.truncated) {
    // The difference between "no aircraft there" and "nobody asked about
    // there". Without saying so, a capped viewport looks like a dead feed.
    el.cell.textContent += ` (capped at ${message.max_cells} — zoom in for full coverage)`;
    el.cell.classList.add('capped');
  } else {
    el.cell.classList.remove('capped');
  }

  // Redrawn, not appended — otherwise every resubscribe litters the map with
  // the outline of each cell ever visited.
  for (const outline of cellOutlines) map.removeLayer(outline);
  cellOutlines = [];

  const covered = [];
  for (const cell of cells) {
    if (!Array.isArray(cell.bbox) || cell.bbox.length !== 4) continue;
    const [lamin, lomin, lamax, lomax] = cell.bbox;
    const bounds = [[lamin, lomin], [lamax, lomax]];
    covered.push(bounds);
    cellOutlines.push(L.rectangle(bounds, {
      color: '#58a6ff', weight: 1, opacity: 0.35, fill: false, dashArray: '5,6',
    }).addTo(map));
  }

  // Only on the first subscription. Refitting after a pan would yank the map
  // out from under the hand that just moved it.
  if (fit && covered.length) {
    map.fitBounds(covered.reduce((acc, b) => acc.extend(b), L.latLngBounds(covered[0])),
                  { padding: [20, 20] });
  }
}

/* --- following the map ----------------------------------------------------- */

/** Tell the server where we are looking now.
 *
 * The server holds one cell per connection and the poller only fetches cells
 * with subscribers, so without this a pan into next-door sky shows nothing —
 * the aircraft are real, nobody ever asked for them.
 *
 * Sent on every settled move, including moves within the current cell: the
 * client cannot know where the 5-degree boundaries are, and the server drops
 * a same-cell request cheaply.
 */
function sendSubscription() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  // Bounds, not the centre: a zoomed-out viewport spans several cells, and
  // subscribing to only the middle one is exactly why zooming out used to
  // show a handful of aircraft floating in an otherwise empty map.
  const b = map.getBounds();
  socket.send(JSON.stringify({
    type: 'subscribe',
    bounds: [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()],
  }));
}

/** Debounced: a drag fires moveend once, but a flick-zoom can fire several. */
function scheduleSubscription() {
  clearTimeout(moveTimer);
  moveTimer = setTimeout(sendSubscription, MOVE_DEBOUNCE_MS);
}

/* --- markers --------------------------------------------------------------- */

// Mode A codes that mean an emergency whatever the ADS-B emergency field says.
// Mirrors core/models.py EMERGENCY_SQUAWKS.
const EMERGENCY_SQUAWKS = new Set(['7500', '7600', '7700']);

function isEmergency(state) {
  return (
    (state.emergency && state.emergency !== 'none') ||
    EMERGENCY_SQUAWKS.has(state.squawk)
  );
}

/** Plane glyph rotated to the reported heading.
 *
 * Classes stack, most important last, so emergency wins the fill it needs.
 */
function planeIcon(state) {
  const rotation = Number.isFinite(state.true_track) ? state.true_track : 0;
  const classes = ['plane', state.on_ground ? 'ground' : 'air'];
  if (state.military) classes.push('military');
  if (state.position_source === 'last_known') classes.push('stale');
  if (state.position_source === 'estimated') classes.push('estimated');
  if (isEmergency(state)) classes.push('emergency');

  return L.divIcon({
    className: '',            // suppress Leaflet's default styling
    iconSize: [22, 22],
    iconAnchor: [11, 11],
    html:
      `<div class="${classes.join(' ')}" style="transform: rotate(${rotation}deg)">` +
      '<svg viewBox="0 0 512 512"><path d="M256 16c-13 0-24 34-24 76v82L32 300v52l200-46v92l-52 38v34l76-18 76 18v-34l-52-38v-92l200 46v-52L280 174V92c0-42-11-76-24-76z"/></svg>' +
      '</div>',
  });
}

function fmt(value, digits, unit) {
  return Number.isFinite(value) ? `${value.toFixed(digits)}${unit}` : '—';
}

/** Escape text that came off the wire before it goes near innerHTML.
 *
 * Registration, type and description are third-party database strings, not
 * ours. Interpolating them raw would put someone else's data in charge of our
 * DOM.
 */
function esc(text) {
  if (text === null || text === undefined) return '';
  return String(text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function row(label, value) {
  return value === '' || value === null || value === undefined
    ? ''
    : `<tr><td>${label}</td><td>${value}</td></tr>`;
}

/** How old this position is, in words, or '' when it is current. */
function positionNote(state) {
  if (state.position_source === 'estimated') {
    return '<div class="warn">estimated position — no fix from aircraft</div>';
  }
  if (state.position_source === 'last_known') {
    const age = state.time_position
      ? Math.round(Date.now() / 1000 - state.time_position)
      : null;
    const when = age === null ? 'stale' : `${Math.floor(age / 60)}m ${age % 60}s old`;
    return `<div class="warn">last known position — ${when}</div>`;
  }
  return '';
}

function popupHtml(state) {
  // baro is the ATC-relevant altitude; geo is the fallback when baro is absent.
  const altitude = state.baro_altitude ?? state.geo_altitude;

  const badges =
    (state.military ? '<span class="badge mil">mil</span>' : '') +
    (state.position_source === 'last_known' ? '<span class="badge warn">stale</span>' : '') +
    (state.position_source === 'estimated' ? '<span class="badge warn">est</span>' : '') +
    (isEmergency(state) ? `<span class="badge stop">${esc(state.emergency !== 'none' && state.emergency ? state.emergency : state.squawk)}</span>` : '');

  return (
    `<b>${esc(state.callsign || state.registration || state.icao24)}</b>${badges}` +
    (state.description ? `<div class="sub">${esc(state.description)}</div>` : '') +
    positionNote(state) +
    '<table>' +
    row('icao24', esc(state.icao24)) +
    row('registration', esc(state.registration)) +
    row('type', esc(state.aircraft_type)) +
    row('altitude', fmt(altitude, 0, ' m')) +
    row('velocity', fmt(state.velocity, 0, ' m/s')) +
    row('heading', fmt(state.true_track, 0, '°')) +
    row('vert rate', fmt(state.vertical_rate, 1, ' m/s')) +
    row('squawk', esc(state.squawk)) +
    row('on ground', state.on_ground ? 'yes' : 'no') +
    row('source', esc(state.data_source)) +
    '</table>'
  );
}

/** The set of classes an aircraft's glyph needs, as a comparable string.
 *
 * Rotation is excluded on purpose: it changes almost every update and is
 * applied directly to the existing element, which costs nothing. Only a change
 * in this key requires rebuilding the icon.
 */
function iconKey(state) {
  return [
    state.on_ground ? 'ground' : 'air',
    state.military ? 'mil' : '',
    state.position_source || '',
    isEmergency(state) ? 'emg' : '',
  ].join('|');
}

/** Point the existing glyph without touching the DOM tree.
 *
 * setIcon() destroys and recreates the marker's element. At ~70 updates a
 * second across a thousand-plus aircraft that is the difference between a
 * smooth map and an unusable one, and all it usually achieves is a new
 * rotation — which is one style write on the element already there.
 */
function applyRotation(marker, state) {
  const element = marker.getElement();          // undefined while culled
  const glyph = element && element.firstElementChild;
  if (!glyph) return;
  glyph.style.transform = `rotate(${Number.isFinite(state.true_track) ? state.true_track : 0}deg)`;
}

/** Create or move the marker for one aircraft. */
function upsert(state) {
  // Still nullable, but now only when every source failed: the client already
  // falls back to the last known fix and then to a receiver-derived estimate,
  // so reaching here means there is genuinely nowhere to draw it.
  if (state.latitude === null || state.longitude === null) return;

  const position = [state.latitude, state.longitude];
  const existing = tracked.get(state.icao24);
  const key = iconKey(state);

  if (existing) {
    existing.marker.setLatLng(position);
    // The state object backs the popup, which reads it lazily on open.
    existing.state = state;
    existing.lastSeen = Date.now();
    if (existing.key !== key) {
      // Genuinely different glyph — took off, landed, went stale, declared an
      // emergency. Rare enough that a rebuild is fine.
      existing.marker.setIcon(planeIcon(state));
      existing.key = key;
    }
    // Aircraft cross the viewport edge under their own power, not only when
    // the map moves, so visibility is re-checked on every update too.
    showIfVisible(existing, currentBounds());
    applyRotation(existing.marker, state);
  } else {
    const marker = L.marker(position, {
      icon: planeIcon(state),
      title: state.callsign || state.registration || state.icao24,
    });
    // Function form: Leaflet calls this when the popup opens, so the HTML for
    // a thousand closed popups is never built. Reads the latest state via the
    // entry, not the state captured at creation.
    const entry = { marker, lastSeen: Date.now(), state, key, onMap: false };
    marker.bindPopup(() => popupHtml(entry.state));
    tracked.set(state.icao24, entry);
    showIfVisible(entry, currentBounds());
  }
  countDirty = true;
}

/* --- viewport culling ------------------------------------------------------ */

/** Viewport plus a margin, so a small pan does not pop markers in at the edge. */
function currentBounds() {
  if (cullBounds === null) cullBounds = map.getBounds().pad(CULL_MARGIN);
  return cullBounds;
}

/** Add or remove one marker from the map according to the viewport.
 *
 * Leaflet repositions every marker it holds on every pan frame, whether or not
 * it is on screen. With a thousand-plus aircraft that cost is the lag; markers
 * off screen are kept in `tracked` but out of the DOM entirely.
 */
function showIfVisible(entry, bounds) {
  const visible = bounds.contains(entry.marker.getLatLng());
  if (visible === entry.onMap) return;
  if (visible) {
    entry.marker.addTo(map);
    applyRotation(entry.marker, entry.state);
  } else {
    map.removeLayer(entry.marker);
  }
  entry.onMap = visible;
}

/** Re-evaluate every marker against the current viewport. */
function syncVisibility() {
  cullBounds = null;                    // the map moved; the cached box is stale
  const bounds = currentBounds();
  for (const entry of tracked.values()) showIfVisible(entry, bounds);
  countDirty = true;
}

/** Drop markers that have stopped being updated.
 *
 * Client-side because the server never sends a "gone" event: an aircraft that
 * leaves the box simply stops appearing in polls.
 */
function sweepStale() {
  const cutoff = Date.now() - STALE_MS;
  let removed = 0;
  for (const [icao24, entry] of tracked) {
    if (entry.lastSeen < cutoff) {
      if (entry.onMap) map.removeLayer(entry.marker);
      tracked.delete(icao24);
      removed += 1;
    }
  }
  if (removed) countDirty = true;
}

/** Repaint the counter at most once a frame.
 *
 * upsert() used to write it per aircraft, so a poll over dense airspace was a
 * thousand-plus layout-invalidating DOM writes for a number that only a human
 * reads. A flag plus one write per frame is the same information.
 */
function renderCount() {
  if (!countDirty) return;
  countDirty = false;
  const shown = tracked.size ? countOnMap() : 0;
  el.count.innerHTML = shown === tracked.size
    ? `${tracked.size} <span>aircraft tracked</span>`
    : `${shown} <span>shown of ${tracked.size} tracked</span>`;
}

function countOnMap() {
  let n = 0;
  for (const entry of tracked.values()) if (entry.onMap) n += 1;
  return n;
}

/* --- connection ------------------------------------------------------------ */

function setStatus(text, cssClass) {
  el.statusText.textContent = text;
  el.dot.className = cssClass || '';
}

/** Load the deployed endpoint, falling back to the local dev server.
 *
 * A missing or unparseable config.json is the normal local case, not an error:
 * run_local.py serves the frontend as plain files with no config generated.
 */
async function loadConfig() {
  try {
    const response = await fetch(CONFIG_URL, { cache: 'no-store' });
    if (response.ok) {
      const config = await response.json();
      if (config.wsUrl) {
        console.info('aerofeed: endpoint from config.json', config.apiVersion || '');
        return config.wsUrl;
      }
    }
  } catch {
    // Fall through — running locally.
  }
  return `ws://${location.hostname || 'localhost'}:${LOCAL_WS_PORT}`;
}

function connect(point) {
  const url = `${wsUrl}/?lat=${encodeURIComponent(point.lat)}` +
              `&lon=${encodeURIComponent(point.lon)}`;
  setStatus('connecting…', '');

  socket = new WebSocket(url);

  socket.addEventListener('open', () => {
    setStatus('live', 'live');
    reconnectDelay = RECONNECT_MIN_MS;   // reset backoff after a good connect
    // The URL carries the boot point, but the user may have panned since —
    // after a reconnect that would silently resubscribe them to where they
    // started rather than where they are looking.
    sendSubscription();
  });

  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      console.warn('unparseable message', event.data);
      return;
    }
    if (message.type === 'subscribed') {
      // Fit only the first cell of a session; later ones come from the user
      // moving the map themselves and must not fight them for control of it.
      showCells(message, firstSubscription);
      firstSubscription = false;
    } else if (message.type === 'aircraft') {
      upsert(message.state);
    }
  });

  socket.addEventListener('close', () => {
    // A pause is our own doing — sit quietly until the tab is visible again.
    if (paused) {
      setStatus('paused — tab hidden', '');
      return;
    }
    // Markers are deliberately left on the map: they age out via sweepStale
    // rather than blanking the screen on a brief reconnect.
    setStatus(`disconnected — retrying in ${Math.round(reconnectDelay / 1000)}s`, 'down');
    setTimeout(() => connect(point), reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  });

  // 'error' always precedes 'close', so reconnect is handled there only.
  socket.addEventListener('error', () => setStatus('connection error', 'down'));
}

/* --- visibility ------------------------------------------------------------ */

/** Drop the connection so the backend stops polling for a tab nobody is watching.
 *
 * Closing is the entire mechanism: the server's disconnect path already
 * deregisters the connection, and once the last one goes the scheduler stops
 * calling OpenSky. No client->server protocol needed.
 */
function pause() {
  hiddenTimer = null;
  if (!socket || socket.readyState > WebSocket.OPEN) return;  // already closing/closed
  paused = true;
  socket.close(1000, 'tab hidden');
}

/** Cancel a pending pause, and reconnect if we already paused. */
function resume() {
  clearTimeout(hiddenTimer);
  hiddenTimer = null;
  if (!paused) return;              // within the grace period; link still live
  paused = false;
  reconnectDelay = RECONNECT_MIN_MS;
  connect(bootPoint);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // setTimeout in a hidden tab is throttled, which only ever delays the
    // pause — harmless, and it still fires.
    if (!paused && hiddenTimer === null) hiddenTimer = setTimeout(pause, HIDDEN_GRACE_MS);
  } else {
    resume();
  }
});

/* --- boot ------------------------------------------------------------------ */

(async function boot() {
  // Endpoint first: connect() needs it, and both lookups are independent so
  // they overlap rather than adding their latencies together.
  const [endpoint, point] = await Promise.all([loadConfig(), subscriptionPoint()]);
  wsUrl = endpoint;
  bootPoint = point;
  el.statusText.textContent = `locating via ${point.source}…`;
  initMap(point);
  connect(point);
  setInterval(sweepStale, SWEEP_MS);
  // One coalesced counter repaint per frame, instead of one per aircraft.
  (function tick() { renderCount(); requestAnimationFrame(tick); })();
})();
