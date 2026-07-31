/* aerofeed frontend — vanilla JS, no build step.
 *
 * Connects to the local WebSocket server, upserts a Leaflet marker per aircraft
 * keyed by icao24, and reaps markers that stop being updated.
 *
 * Server protocol (see local/local_ws_server.py):
 *   {type:"subscribed", region_cell, bbox:[lamin,lomin,lamax,lomax]}
 *   {type:"aircraft",   region_cell, state:{icao24, callsign, latitude, ...}}
 */

'use strict';

const WS_PORT = 8765;

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
function subscriptionPoint() {
  const params = new URLSearchParams(location.search);
  const lat = Number(params.get('lat'));
  const lon = Number(params.get('lon'));
  // Number('') is 0, so check the param was actually present before trusting it.
  const hasLat = params.get('lat') !== null && Number.isFinite(lat);
  const hasLon = params.get('lon') !== null && Number.isFinite(lon);
  return hasLat && hasLon ? { lat, lon } : { lat: DEFAULT_LAT, lon: DEFAULT_LON };
}

/* --- map ------------------------------------------------------------------- */

function initMap(point) {
  map = L.map('map', { zoomControl: true }).setView([point.lat, point.lon], 7);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
}

/** Frame the map on the cell the server actually assigned us.
 *
 * The requested point and the cell are not the same thing — snapping means the
 * point can sit near an edge, so fitting the box shows what is really covered.
 */
function showCell(regionCell, bbox) {
  el.cell.textContent = `cell ${regionCell}`;
  if (!Array.isArray(bbox) || bbox.length !== 4) return;

  const [lamin, lomin, lamax, lomax] = bbox;
  const bounds = [[lamin, lomin], [lamax, lomax]];
  L.rectangle(bounds, {
    color: '#58a6ff', weight: 1, opacity: 0.5, fill: false, dashArray: '5,6',
  }).addTo(map);
  map.fitBounds(bounds, { padding: [20, 20] });
}

/* --- markers --------------------------------------------------------------- */

/** Plane glyph rotated to the reported heading. */
function planeIcon(heading, onGround) {
  const rotation = Number.isFinite(heading) ? heading : 0;
  return L.divIcon({
    className: '',            // suppress Leaflet's default styling
    iconSize: [22, 22],
    iconAnchor: [11, 11],
    html:
      `<div class="plane ${onGround ? 'ground' : 'air'}" style="transform: rotate(${rotation}deg)">` +
      '<svg viewBox="0 0 512 512"><path d="M256 16c-13 0-24 34-24 76v82L32 300v52l200-46v92l-52 38v34l76-18 76 18v-34l-52-38v-92l200 46v-52L280 174V92c0-42-11-76-24-76z"/></svg>' +
      '</div>',
  });
}

function fmt(value, digits, unit) {
  return Number.isFinite(value) ? `${value.toFixed(digits)}${unit}` : '—';
}

function popupHtml(state) {
  // baro is the ATC-relevant altitude; geo is the fallback when baro is absent.
  const altitude = state.baro_altitude ?? state.geo_altitude;
  return (
    `<b>${state.callsign || state.icao24}</b>` +
    '<table>' +
    `<tr><td>icao24</td><td>${state.icao24}</td></tr>` +
    `<tr><td>altitude</td><td>${fmt(altitude, 0, ' m')}</td></tr>` +
    `<tr><td>velocity</td><td>${fmt(state.velocity, 0, ' m/s')}</td></tr>` +
    `<tr><td>heading</td><td>${fmt(state.true_track, 0, '°')}</td></tr>` +
    `<tr><td>vert rate</td><td>${fmt(state.vertical_rate, 1, ' m/s')}</td></tr>` +
    `<tr><td>on ground</td><td>${state.on_ground ? 'yes' : 'no'}</td></tr>` +
    '</table>'
  );
}

/** Create or move the marker for one aircraft. */
function upsert(state) {
  // Position is nullable in OpenSky and genuinely arrives null — an aircraft
  // heard by a receiver but without a position fix. Nothing to place on a map.
  if (state.latitude === null || state.longitude === null) return;

  const position = [state.latitude, state.longitude];
  const existing = tracked.get(state.icao24);

  if (existing) {
    existing.marker.setLatLng(position);
    existing.marker.setIcon(planeIcon(state.true_track, state.on_ground));
    existing.marker.setPopupContent(popupHtml(state));
    existing.lastSeen = Date.now();
  } else {
    const marker = L.marker(position, {
      icon: planeIcon(state.true_track, state.on_ground),
      title: state.callsign || state.icao24,
    }).bindPopup(popupHtml(state)).addTo(map);
    tracked.set(state.icao24, { marker, lastSeen: Date.now() });
  }
  renderCount();
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
      map.removeLayer(entry.marker);
      tracked.delete(icao24);
      removed += 1;
    }
  }
  if (removed) renderCount();
}

function renderCount() {
  el.count.innerHTML = `${tracked.size} <span>aircraft tracked</span>`;
}

/* --- connection ------------------------------------------------------------ */

function setStatus(text, cssClass) {
  el.statusText.textContent = text;
  el.dot.className = cssClass || '';
}

function connect(point) {
  const url = `ws://${location.hostname || 'localhost'}:${WS_PORT}/` +
              `?lat=${encodeURIComponent(point.lat)}&lon=${encodeURIComponent(point.lon)}`;
  setStatus('connecting…', '');

  socket = new WebSocket(url);

  socket.addEventListener('open', () => {
    setStatus('live', 'live');
    reconnectDelay = RECONNECT_MIN_MS;   // reset backoff after a good connect
  });

  socket.addEventListener('message', (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      console.warn('unparseable message', event.data);
      return;
    }
    if (message.type === 'subscribed') showCell(message.region_cell, message.bbox);
    else if (message.type === 'aircraft') upsert(message.state);
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
  connect(point);
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

const point = subscriptionPoint();
initMap(point);
connect(point);
setInterval(sweepStale, SWEEP_MS);
