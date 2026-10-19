import asyncio
import gzip
import json
import os
from datetime import date, timedelta

import pandas as pd
import yaml
from fastapi.responses import Response
from nicegui import app as _ng_app, core as _ng_core, ui, Client

from data import (
    CACHE_FILE,
    ERRORS_FILE,
    DEFAULT_FOLDER,
    EXCLUDED_SPORTS,
    GPS_ROUNDING,
    load_and_process_all_data,
)

with open("config.yaml") as f:
    _cfg = yaml.safe_load(f)

APP_TITLE    = _cfg['ui']['title']
DEFAULT_LAT  = _cfg['ui']['default_lat']
DEFAULT_LON  = _cfg['ui']['default_lon']
DEFAULT_ZOOM = _cfg['ui']['default_zoom']
APP_PORT     = _cfg['ui'].get('port', 8080)
MIN_DATE     = date.fromisoformat(_cfg['ui'].get('min_date', '2020-01-01'))

TILE_OPTIONS = {
    "dark":      "Dark (CartoDB)",
    "osm":       "Bright (OpenStreetMap)",
    "satellite": "Satellite (ESRI)",
    "topo":      "Relief (OpenTopoMap)",
}

MODE_OPTIONS = {
    "heatmap":     "Heatmap",
    "heatmap_log": "Heatmap (log scale)",
    "scatter":     "Itineraries",
}

DRAWER_W    = 310
SLIDER_BASE = date(2020, 1, 1)

_data_cache:   dict  = {}
_map_payload:  bytes = gzip.compress(b'{"data":[],"mode":"heatmap","tile":"dark"}', compresslevel=1)


@_ng_app.get('/mapdata')
def _mapdata_endpoint():
    return Response(
        content=_map_payload,
        media_type='application/json',
        headers={'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _days(d: date) -> int:
    return max(0, (d - SLIDER_BASE).days)


def _date(v: int) -> date:
    return SLIDER_BASE + timedelta(days=int(v))


def _fmt(d: date) -> str:
    return d.strftime('%d/%m/%Y')


def _filter_df(df: pd.DataFrame, sports: list[str], d_from: date, d_to: date) -> pd.DataFrame:
    mask = (
        df['sport'].isin(sports)
        & (df['time'].dt.date >= d_from)
        & (df['time'].dt.date <= d_to)
    )
    return df[mask]


def _to_display_data(df: pd.DataFrame) -> list[list]:
    if df.empty:
        return []
    agg = df.groupby(['lat', 'lon']).size().reset_index(name='w')
    return agg[['lat', 'lon', 'w']].values.tolist()


def _to_path_data(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    paths = []
    for file_name, group in df.groupby('file_name'):
        sorted_group = group.sort_values('time')
        coords = sorted_group[['lon', 'lat']].values.tolist()
        if len(coords) > 1:
            paths.append({'path': coords, 'name': file_name})
    return paths


# ---------------------------------------------------------------------------
# deck.gl JavaScript
# ---------------------------------------------------------------------------

_MAP_JS = """
(function () {
  // Create the map container and append it directly to <body>,
  // OUTSIDE NiceGUI's Vue-managed DOM tree so reactive updates never destroy it.
  if (!document.getElementById('map-container')) {
    const d = document.createElement('div');
    d.id = 'map-container';
    document.body.appendChild(d);
  }

  const { DeckGL, TileLayer, BitmapLayer, HeatmapLayer, PathLayer } = deck;

  const TILES = {
    dark:      'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    osm:       'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    topo:      'https://tile.opentopomap.org/{z}/{x}/{y}.png',
    satellite: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  };

  const HEAT_COLORS = [
    [1,   152, 189, 180],
    [73,  227, 206, 190],
    [216, 254, 181, 200],
    [254, 237, 177, 210],
    [254, 173,  84, 220],
    [209,  55,  78, 230],
  ];

  function makeTileLayer(key) {
    return new TileLayer({
      id: 'base',
      data: TILES[key] || TILES.dark,
      tileSize: 256,
      renderSubLayers: props => {
        const { bbox: { west, south, east, north } } = props.tile;
        return new BitmapLayer(props, {
          data: null, image: props.data, bounds: [west, south, east, north],
        });
      },
    });
  }

  function makeHeatmap(data, logScale) {
    return new HeatmapLayer({
      id: 'heatmap',
      data,
      getPosition: d => [d[1], d[0]],
      getWeight:   d => logScale ? Math.log1p(d[2] || 1) : (d[2] || 1),
      radiusPixels: 30,
      intensity:    logScale ? 3 : 1,
      threshold:    0.03,
      aggregation: 'SUM',
      colorRange: HEAT_COLORS,
    });
  }

  function makePathLayer(data) {
    return new PathLayer({
      id: 'path-layer',
      data,
      getPath: d => d.path,
      getColor: [249, 115, 22, 180],
      getWidth: 3,
      widthMinPixels: 1,
      widthMaxPixels: 4,
      rounded: false,           // 'false' accélère le rendu GPU par rapport à 'true' !
      billboard: false,
      updateTriggers: {}       // Empêche les recalculs inutiles de géométrie
    });
  }

  let deckgl = null;

  window.initMap = function (lat, lon, zoom) {
    deckgl = new DeckGL({
      container: 'map-container',
      initialViewState: { latitude: lat, longitude: lon, zoom, pitch: 0, bearing: 0 },
      controller: true,
      layers: [makeTileLayer('dark')],
    });
  };

  window.updateMap = function (data, mode, tileKey) {
    if (!deckgl) return;
    let layer;
    if      (mode === 'heatmap')     layer = makeHeatmap(data, false);
    else if (mode === 'heatmap_log') layer = makeHeatmap(data, true);
    else                             layer = makePathLayer(data);
    deckgl.setProps({ layers: [makeTileLayer(tileKey), layer] });
  };

  // Fetch data from Python's HTTP endpoint and render.
  // This decouples heavy data transfer from the WebSocket so the
  // NiceGUI heartbeat is never blocked by a large JS eval.
  window.fetchAndRender = function () {
    fetch('/mapdata')
      .then(r => r.json())
      .then(p => window.updateMap(p.data, p.mode, p.tile))
      .catch(e => console.error('fetchAndRender:', e));
  };

})();
"""


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@ui.page('/')
async def main_page(client: Client):

    state = {
        'df_clean':        pd.DataFrame(),
        'selected_sports': [],
        'date_from':       MIN_DATE,
        'date_to':         date.today(),
        'mode':            'heatmap',
        'tile':            'dark',
        'populating':      False,   # suppresses reactive handlers during _populate_ui
    }

    ui.colors(primary='#f97316')
    ui.dark_mode().enable()

    ui.add_head_html(
        '<script src="https://unpkg.com/deck.gl@8.9.36/dist.min.js"></script>'
        '<style>'
        '  body, html { margin:0; padding:0; overflow:hidden; background:#0f172a; }'
        f' #map-container {{'
        f'   position:fixed; top:50px; left:{DRAWER_W}px; right:0; bottom:0;'
        f'   transition: left 0.25s ease;'
        f' }}'
        '  .q-field--outlined .q-field__control { border-color:rgba(249,115,22,0.3) !important; }'
        '  .q-field--outlined:hover .q-field__control { border-color:rgba(249,115,22,0.7) !important; }'
        '  .q-chip { background:rgba(249,115,22,0.25) !important; color:#fdba74 !important; }'
        '  .q-separator { background:rgba(255,255,255,0.07) !important; }'
        '  .stat-lbl { font-size:0.68rem; color:#64748b; text-transform:uppercase; letter-spacing:.06em; }'
        '  .stat-val { font-size:0.92rem; color:#f1f5f9; font-variant-numeric:tabular-nums; }'
        '  .range-label { font-size:0.75rem; font-family:monospace; color:#fdba74; }'
        '  .diag-row { display:flex; flex-direction:column; width:100%; overflow:hidden; margin-bottom:6px; }'
        '  .diag-file { font-size:0.72rem; color:#cbd5e1; word-break:break-all; overflow-wrap:anywhere; }'
        '  .diag-err  { font-size:0.68rem; color:#94a3b8; padding-left:8px; }'
        '</style>'
    )

    # ── Header ──────────────────────────────────────────────────────────────
    with ui.header().style(
        'background:#0f172a; height:50px;'
        'border-bottom:1px solid rgba(249,115,22,0.25);'
    ).classes('items-center px-4 gap-3 text-white'):
        ui.button(icon='menu', on_click=lambda: drawer.toggle()) \
            .props('flat round dense color=white')
        ui.icon('directions_run').style('color:#f97316')
        ui.label(APP_TITLE).classes('text-sm font-semibold tracking-wide')

    # ── Sidebar ─────────────────────────────────────────────────────────────
    with ui.left_drawer(fixed=True).style(
        f'width:{DRAWER_W}px; background:#111827;'
        'border-right:1px solid rgba(255,255,255,0.06);'
    ).classes('q-pa-md column gap-0') as drawer:

        folder_input = (
            ui.input('Fit files folder', value=DEFAULT_FOLDER)
            .props('dense dark outlined color=orange')
            .classes('w-full')
        )
        reload_btn = (
            ui.button('Reload data', icon='refresh')
            .props('unelevated color=orange dense')
            .classes('w-full q-mt-sm')
        )

        ui.separator().classes('q-my-sm')

        mode_select = (
            ui.select(MODE_OPTIONS, value='heatmap', label="Display modes")
            .props('dense dark outlined color=orange options-dark')
            .classes('w-full')
        )
        tile_select = (
            ui.select(TILE_OPTIONS, value='dark', label='Map types')
            .props('dense dark outlined color=orange options-dark')
            .classes('w-full q-mt-sm')
        )

        ui.separator().classes('q-my-sm')

        sports_select = (
            ui.select([], multiple=True, label='Sports')
            .props('dense dark outlined color=orange use-chips options-dark')
            .classes('w-full')
        )

        ui.separator().classes('q-my-sm')

        with ui.row().classes('w-full items-center justify-between q-mb-xs'):
            ui.label('Period').classes('stat-lbl')
            range_dates_label = ui.label('—').classes('range-label')

        _sl_min = _days(MIN_DATE)
        _sl_max = _days(date.today())
        range_slider = (
            ui.range(min=_sl_min, max=_sl_max,
                     value={'min': _sl_min, 'max': _sl_max})
            .props('color=orange dark :step=1')
            .classes('w-full q-px-xs')
        )
        range_slider.on_value_change(lambda e: range_dates_label.set_text(
            f"{_fmt(_date(e.value['min']))}  →  {_fmt(_date(e.value['max']))}"
        ))

        ui.separator().classes('q-my-sm')

        with ui.grid(columns=2).classes('w-full gap-0'):
            with ui.row().classes('gap-1 items-center'):
                ui.label('Loaded points').classes('stat-lbl')
                ui.icon('info', size='xs').style('color:#475569').tooltip(
                    'Total loaded satellite locations,\n'
                    'before rounding and clustering.'
                )
            with ui.row().classes('gap-1 items-center'):
                ui.label('Displayed points').classes('stat-lbl')
                ui.icon('info', size='xs').style('color:#475569').tooltip(
                    f'Number of displayed locations after rounding (lat/lon rounded at {GPS_ROUNDING} decimals ≈ 11 m).\n'
                    'Several visit on same location = 1 displayed point, with a more intense color on heatmap.'
                )
            pts_label     = ui.label('—').classes('stat-val')
            display_label = ui.label('—').classes('stat-val')

        ui.separator().classes('q-my-sm')

        with ui.expansion('File loading diagnosis', icon='bug_report') \
                .props('dark').classes('w-full text-xs'):
            diag_container = ui.column().classes('w-full gap-1')

    # ── Map container ────────────────────────────────────────────────────────
    # The #map-container div is created by _MAP_JS and appended directly to
    # document.body, outside NiceGUI's Vue tree, so reactive updates never
    # destroy the deck.gl canvas.

    def on_drawer_change(e):
        left = DRAWER_W if e.value else 0
        client.run_javascript(
            f"const m=document.getElementById('map-container'); if(m) m.style.left='{left}px';"
        )
    drawer.on_value_change(on_drawer_change)

    # ── Map refresh ──────────────────────────────────────────────────────────
    async def refresh_map():
        if state['df_clean'].empty:
            return

        def _build() -> tuple[int, bytes]:
            filtered = _filter_df(state['df_clean'], state['selected_sports'],
                                  state['date_from'], state['date_to'])
            mode = state['mode']
            if mode in ('heatmap', 'heatmap_log'):
                data = _to_display_data(filtered)
            else:
                data = _to_path_data(filtered)

            raw = json.dumps({'data': data, 'mode': mode, 'tile': state['tile']})
            return len(data), gzip.compress(raw.encode(), compresslevel=1)

        n_items, compressed = await asyncio.to_thread(_build)

        global _map_payload
        _map_payload = compressed

        pts_label.set_text(f"{len(state['df_clean']):,}")
        display_label.set_text(f"{n_items:,}")
        client.run_javascript("window.fetchAndRender()")

    # ── Data loading ─────────────────────────────────────────────────────────
    async def load_data(force: bool = False):
        folder = os.path.expanduser(folder_input.value.strip())
        if not folder or not os.path.isdir(folder):
            ui.notify('Invalid folder', type='negative')
            return

        if force:
            for f in (CACHE_FILE, ERRORS_FILE):
                if os.path.exists(f):
                    os.remove(f)
            _data_cache.pop(folder, None)

        if folder in _data_cache:
            df, diag = _data_cache[folder]
            _populate_ui(df, diag)
            await refresh_map()
            return

        if os.path.exists(CACHE_FILE):
            df, diag = await asyncio.to_thread(load_and_process_all_data, folder, None)
            _data_cache[folder] = (df, diag)
            _populate_ui(df, diag)
            await refresh_map()
            ui.notify('Loaded data ✓', type='positive')
            return

        # No cache — process raw FIT files with a progress dialog.
        progress_state = {'current': 0, 'total': 0}

        def on_progress(i, total):
            progress_state['current'] = i
            progress_state['total']   = total

        with ui.dialog().props('persistent') as dlg, \
             ui.card().style('background:#1e293b; min-width:300px') \
                      .classes('items-center gap-3 q-pa-lg'):
            ui.spinner(size='xl', color='orange')
            pct_label  = ui.label('0 %').classes('text-3xl font-bold').style('color:#f97316')
            prog_bar   = ui.linear_progress(value=0, color='orange').classes('w-full')
            prog_label = ui.label('Scanning .fit files…') \
                           .classes('text-sm text-slate-400')

        dlg.open()

        load_task = asyncio.create_task(
            asyncio.to_thread(load_and_process_all_data, folder, on_progress)
        )

        def _tick():
            t = progress_state['total']
            c = progress_state['current']
            if t > 0:
                pct = c / t
                pct_label.set_text(f"{pct:.0%}")
                prog_bar.set_value(pct)
                prog_label.set_text(f"{c} / {t} processed files")

        timer = ui.timer(0.15, _tick)
        df, diag = await load_task
        timer.cancel()
        dlg.close()

        _data_cache[folder] = (df, diag)
        _populate_ui(df, diag)
        await refresh_map()
        ui.notify('Loaded data ✓', type='positive')

    # ── Populate sidebar after load ───────────────────────────────────────────
    def _populate_ui(df: pd.DataFrame, diag: dict):
        if df.empty:
            ui.notify('No data found.', type='warning')
            return

        state['populating'] = True
        df_clean = df[~df['sport'].isin(EXCLUDED_SPORTS)]
        state['df_clean'] = df_clean

        sports  = sorted(df_clean['sport'].unique().tolist())
        default = [s for s in sports if s.lower() == 'running'] or sports[:1]
        sports_select.options    = sports
        sports_select.value      = default
        state['selected_sports'] = default

        min_d  = df_clean['time'].min().date()
        d_from = max(min_d, MIN_DATE)
        d_to   = date.today()
        state['date_from'] = d_from
        state['date_to']   = d_to

        sl_from = _days(d_from)
        sl_to   = _days(d_to)
        range_slider.props(f':min={sl_from} :max={sl_to} :step=1')
        range_slider.set_value({'min': sl_from, 'max': sl_to})
        range_dates_label.set_text(f"{_fmt(d_from)}  →  {_fmt(d_to)}")

        errors      = diag.get('errors', [])
        no_gps      = diag.get('no_gps_count', 0)

        diag_container.clear()
        with diag_container:
            if not errors and not no_gps:
                ui.label('All files loaded ✓') \
                    .classes('text-xs').style('color:#4ade80')
            else:
                if no_gps:
                    ui.label(f"{no_gps} activitie(s) without GPS (interior)") \
                        .classes('text-xs').style('color:#94a3b8')
                if errors:
                    ui.label(f"{len(errors)} corrupted file(s)") \
                        .classes('text-xs q-mt-xs').style('color:#f87171')
                    for fname, err in errors:
                        with ui.element('div').classes('diag-row'):
                            ui.label(f"• {fname}").classes('diag-file')
                            ui.label(err).classes('diag-err')

        state['populating'] = False

    # ── Event wiring ─────────────────────────────────────────────────────────
    reload_btn.on_click(lambda: load_data(force=True))

    async def on_mode_change(e):
        if state['populating']: return
        state['mode'] = e.value
        await refresh_map()
    mode_select.on_value_change(on_mode_change)

    async def on_tile_change(e):
        if state['populating']: return
        state['tile'] = e.value
        await refresh_map()
    tile_select.on_value_change(on_tile_change)

    async def on_sport_change(e):
        if state['populating']: return
        state['selected_sports'] = e.value or []
        await refresh_map()
    sports_select.on_value_change(on_sport_change)

    async def on_range_change(e):
        if state['populating']: return
        v = e.args
        if not isinstance(v, dict) or 'min' not in v or 'max' not in v:
            return
        state['date_from'] = _date(v['min'])
        state['date_to']   = _date(v['max'])
        await refresh_map()
    range_slider.on('change', on_range_change)

    # ── Init ─────────────────────────────────────────────────────────────────
    async def _init():
        await client.connected()
        client.run_javascript(_MAP_JS)
        client.run_javascript(f'window.initMap({DEFAULT_LAT}, {DEFAULT_LON}, {DEFAULT_ZOOM})')
        if os.path.exists(CACHE_FILE):
            await load_data()

    ui.timer(0.1, _init, once=True)


# Raise socket.io buffer limit to 32 MB so large JSON payloads (162k+ points)
# don't silently drop the connection (default is 1 MB).
_ng_core.sio.eio.max_http_buffer_size = 32 * 1024 * 1024

ui.run(
    title=APP_TITLE, 
    port=APP_PORT, 
    reload=False, 
    show=False,
    favicon="🏃",
    gzip_middleware_factory=None
    )  # disabled: we pre-gzip /mapdata in a thread
