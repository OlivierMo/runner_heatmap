import streamlit as st
import fitdecode
import pandas as pd
import folium
from folium.plugins import HeatMap
from streamlit_folium import st_folium
import os
import yaml
from datetime import datetime, date


with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

DEFAULT_FOLDER = os.path.expanduser(config['paths']['default_folder'])
CACHE_FILE = config['paths']['cache_file']

MAX_DISPLAY_POINTS = config['performance']['max_displayed_points']
GPS_ROUNDING = config['performance']['gps_rounding']

APP_TITLE = config['ui']['title']
DEFAULT_COORD = (config['ui']['default_lat'], config['ui']['default_lon'])
DEFAULT_ZOOM = config['ui']['default_zoom']
EXCLUDED_SPORTS = config['ui']['excluded_sports']


MAP_TILES = {
    "Clair (OpenStreetMap)": "OpenStreetMap",
    "Relief (OpenTopoMap)": "OpenTopoMap",
    "Satellite (Esri World Imagery)": "Esri WorldImagery",
    "Sombre (CartoDB DarkMatter)": "CartoDB dark_matter",
}


st.set_page_config(page_title=APP_TITLE, layout="wide")


def is_activity_file(file_path):
    try:
        with fitdecode.FitReader(file_path) as fit:
            for frame in fit:
                if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.name == 'file_id':
                    file_type = frame.get_value('type')
                    return file_type in ['activity', 4]
            return False
    except Exception:
        return False

def process_single_fit_file(file_path):
    """Reads fit file and returns a DataFrame."""
    points = []
    sport = "Unknown"
    sub_sport = "Unknown"
    activity_name = "Sans nom"
    try:
        with fitdecode.FitReader(file_path, check_crc=False) as fit:
            for frame in fit:
                if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                    if frame.name == 'session' or frame.name == 'sport':
                        if frame.has_field('sport'):
                            sport = str(frame.get_value('sport'))
                        if frame.has_field('sub_sport'):
                            sub_sport = str(frame.get_value('sub_sport'))
                        if frame.has_field('name'):
                            activity_name = str(frame.get_value('name'))

                    if frame.name == 'record' and frame.has_field('position_lat') and frame.has_field('position_long'):
                        raw_lat = frame.get_value('position_lat')
                        raw_lon = frame.get_value('position_long')

                        if raw_lat is not None and raw_lon is not None:
                            points.append({
                                'lat': raw_lat * (180 / 2**31),
                                'lon': raw_lon * (180 / 2**31),
                                'time': frame.get_value('timestamp') if frame.has_field('timestamp') else None
                            })
    except Exception as e:
        if not points: return {'error': f"Crash: {e}"}

    if not points: return {'error': "Aucun point GPS"}

    df = pd.DataFrame(points)
    df['sport'] = str(sport)
    df['sub_sport'] = str(sub_sport)
    df['activity_name'] = str(activity_name)
    df['file_name'] = os.path.basename(file_path)
    return df


@st.cache_resource
def load_and_process_all_data(folder_path):
    if os.path.exists(CACHE_FILE):
        return pd.read_parquet(CACHE_FILE)

    all_dfs, error_list = [], []
    files_to_process = []

    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.endswith('.fit'):
                path = os.path.join(root, file)
                if is_activity_file(path): files_to_process.append(path)

    if not files_to_process: return pd.DataFrame()

    prog_bar = st.sidebar.progress(0)
    for i, path in enumerate(files_to_process):
        result = process_single_fit_file(path)
        if isinstance(result, dict):
            error_list.append((os.path.basename(path), result['error']))
        else:
            all_dfs.append(result)
        prog_bar.progress((i + 1) / len(files_to_process))

    prog_bar.empty()
    st.session_state['fit_errors'] = error_list

    if all_dfs:
        full_df = pd.concat(all_dfs, ignore_index=True)
        full_df['time'] = pd.to_datetime(full_df['time'], utc=True)
        full_df['lat'] = full_df['lat'].round(GPS_ROUNDING)
        full_df['lon'] = full_df['lon'].round(GPS_ROUNDING)
        full_df.to_parquet(CACHE_FILE)
        return full_df
    return pd.DataFrame()


def render_diagnostics(df):
    """Display loading errors."""
    with st.sidebar.expander("🛠️ Menu Diagnostic & Erreurs", expanded=False):
        if 'fit_errors' in st.session_state and st.session_state['fit_errors']:
            st.subheader("❌ Erreurs de lecture")
            error_df = pd.DataFrame(st.session_state['fit_errors'], columns=["Fichier", "Détail"])
            st.dataframe(error_df, use_container_width=True)
            st.divider()

        to_check_df = df[df['sport'].isin(EXCLUDED_SPORTS + ["Unknown"])]
        if not to_check_df.empty:
            st.subheader("🔍 À vérifier")
            summary = to_check_df.groupby(['file_name', 'sport']).size().reset_index(name='Pts')
            st.dataframe(summary, use_container_width=True)
        else:
            st.success("Données propres.")

def render_map(display_df, mode, tiles):
    m = folium.Map(location=DEFAULT_COORD, zoom_start=DEFAULT_ZOOM, tiles=tiles)

    if mode == "Heatmap (Densité)":
        heat_data = display_df[['lat', 'lon', 'weight']].values.tolist()
        HeatMap(heat_data, radius=7, blur=5, min_opacity=0.2).add_to(m)
    else:
        for _, row in display_df.iterrows():
            folium.CircleMarker(
                location=[row['lat'], row['lon']],
                radius=1, color='red', fill=True, weight=0
            ).add_to(m)

    st_folium(m, use_container_width=True, height=800, key=f"map_{mode}_{tiles}", returned_objects=[])



def main():
    with st.sidebar:
        st.title(f"🏃‍♂️ {APP_TITLE}")

        folder_path = st.text_input("Dossier .fit", value=DEFAULT_FOLDER)

        if st.button("🔄 Forcer le rechargement"):
            if os.path.exists(CACHE_FILE): os.remove(CACHE_FILE)
            st.cache_resource.clear()
            st.rerun()

    if not folder_path or not os.path.isdir(folder_path):
        st.info("Sélectionnez un dossier valide dans la barre latérale.")
        return

    df_raw = load_and_process_all_data(folder_path)

    if df_raw.empty:
        st.sidebar.warning("Aucune donnée trouvée.")
        return

    df_clean = df_raw[~df_raw['sport'].isin(EXCLUDED_SPORTS)]
    all_sports = sorted(df_clean['sport'].unique())
    default_selection = [s for s in all_sports if s.lower() == 'running']

    with st.sidebar:
        display_mode = st.selectbox("Mode d'affichage", ["Heatmap (Densité)", "Parcours (Points)"])
        tile_name = st.selectbox("Fond de carte", list(MAP_TILES.keys()))
        selected_sports = st.pills(
            "Sports",
            all_sports,
            selection_mode="multi",
            default=default_selection
        )

        if not selected_sports:
            selected_sports = default_selection

        min_d = max(df_clean['time'].min().date(), date(2023, 1, 1))
        max_d = datetime.now().date()

        date_range = pd.date_range(start=min_d, end=max_d).date.tolist()

        dates = st.select_slider(
            "Sélectionnez la période",
            options=date_range,
            value=(min_d, max_d) # Valeur par défaut : tout l'historique
        )

    mask = (df_clean['sport'].isin(selected_sports))
    if isinstance(dates, list) or isinstance(dates, tuple):
        if len(dates) == 2:
            mask &= (df_clean['time'].dt.date >= dates[0]) & (df_clean['time'].dt.date <= dates[1])

    filtered_df = df_clean[mask]

    render_diagnostics(df_raw)

    if not filtered_df.empty:
        display_df = filtered_df.groupby(['lat', 'lon']).size().reset_index(name='weight')

        if len(display_df) > MAX_DISPLAY_POINTS:
            display_df = display_df.sample(MAX_DISPLAY_POINTS, random_state=42)

        st.sidebar.metric("Points chargés", f"{len(df_clean):,}")
        st.sidebar.metric("Points affichés", f"{len(display_df):,}")

        render_map(display_df, display_mode, MAP_TILES[tile_name])
    else:
        st.warning("Aucune donnée pour les filtres sélectionnés.")

if __name__ == "__main__":
    main()