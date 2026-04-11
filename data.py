import json
import os
from typing import Callable, Optional

import fitdecode
import pandas as pd
import yaml

with open("config.yaml", "r") as f:
    _cfg = yaml.safe_load(f)

DEFAULT_FOLDER  = os.path.expanduser(_cfg['paths']['default_folder'])
CACHE_FILE      = _cfg['paths']['cache_file']
ERRORS_FILE     = CACHE_FILE.replace('.parquet', '_errors.json')
GPS_ROUNDING    = _cfg['performance']['gps_rounding']
EXCLUDED_SPORTS = _cfg['ui']['excluded_sports']


class _SafeProcessor(fitdecode.DefaultDataProcessor):
    """fitdecode processor that tolerates composite (tuple) datetime values.

    Some older Garmin FIT files store date_time fields as multi-component tuples
    instead of plain ints. fitdecode's default processor crashes with:
      TypeError: '>=' not supported between instances of 'tuple' and 'int'
    We skip the conversion for those values, leaving them as raw tuples (which
    we later coerce/drop via pd.to_datetime(..., errors='coerce').
    """
    def process_type_date_time(self, reader, field_data):
        try:
            super().process_type_date_time(reader, field_data)
        except TypeError:
            pass  # leave raw tuple value; filtered out downstream


def _process_fit_file(file_path: str):
    """Open a .fit file once and extract GPS points.

    Returns:
        {'skip': True}    — not an activity file (device/settings file)
        {'no_gps': True}  — valid activity but no GPS (indoor: treadmill, gym…)
        {'error': str}    — unreadable / truncated / permission denied
        pd.DataFrame      — success
    """
    points = []
    sport, sub_sport, activity_name = "Unknown", "Unknown", "Sans nom"
    confirmed_activity = False

    try:
        with fitdecode.FitReader(
            file_path, check_crc=False, processor=_SafeProcessor()
        ) as fit:
            for frame in fit:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue

                if frame.name == 'file_id':
                    file_type = frame.get_value('type') if frame.has_field('type') else None
                    if file_type not in ['activity', 4]:
                        return {'skip': True}
                    confirmed_activity = True

                elif frame.name in ('session', 'sport'):
                    if frame.has_field('sport'):
                        sport = str(frame.get_value('sport'))
                    if frame.has_field('sub_sport'):
                        sub_sport = str(frame.get_value('sub_sport'))
                    if frame.has_field('name'):
                        activity_name = str(frame.get_value('name'))

                elif (frame.name == 'record'
                      and frame.has_field('position_lat')
                      and frame.has_field('position_long')):
                    raw_lat = frame.get_value('position_lat')
                    raw_lon = frame.get_value('position_long')
                    if raw_lat is not None and raw_lon is not None:
                        ts = frame.get_value('timestamp') if frame.has_field('timestamp') else None
                        points.append({
                            'lat':  raw_lat * (180 / 2**31),
                            'lon':  raw_lon * (180 / 2**31),
                            'time': ts,
                        })

    except PermissionError:
        return {'error': "Accès refusé (permission)"}
    except ValueError as e:
        if not points:
            # 'size' = fitdecode's internal error when file is truncated at byte level
            return {'error': "Fichier tronqué / corrompu"}
    except Exception as e:
        if not points:
            return {'error': f"Lecture impossible : {e}"}

    if not confirmed_activity:
        return {'skip': True}
    if not points:
        return {'no_gps': True}

    df = pd.DataFrame(points)
    df['sport']         = sport
    df['sub_sport']     = sub_sport
    df['activity_name'] = activity_name
    df['file_name']     = os.path.basename(file_path)
    return df


def _save_errors(payload: dict) -> None:
    with open(ERRORS_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)


def load_errors() -> dict:
    """Return {'errors': [...], 'no_gps_count': int} from the last processing run."""
    if not os.path.exists(ERRORS_FILE):
        return {'errors': [], 'no_gps_count': 0}
    try:
        with open(ERRORS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'errors': [], 'no_gps_count': 0}


def load_and_process_all_data(
    folder_path: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple:
    """Load from Parquet cache when available, otherwise process all .fit files.

    Returns (DataFrame, {'errors': list, 'no_gps_count': int}).
    """
    if os.path.exists(CACHE_FILE):
        return pd.read_parquet(CACHE_FILE), load_errors()

    fit_files = [
        os.path.join(root, fname)
        for root, _, files in os.walk(folder_path)
        for fname in files
        if fname.endswith('.fit')
    ]

    if not fit_files:
        return pd.DataFrame(), {'errors': [], 'no_gps_count': 0}

    all_dfs:    list[pd.DataFrame] = []
    errors:     list[list[str]]    = []
    no_gps      = 0
    total       = len(fit_files)

    for i, path in enumerate(fit_files):
        result = _process_fit_file(path)
        if isinstance(result, dict):
            if result.get('skip'):
                pass
            elif result.get('no_gps'):
                no_gps += 1
            else:
                errors.append([os.path.basename(path), result['error']])
        else:
            all_dfs.append(result)
        if on_progress:
            on_progress(i + 1, total)

    diag = {'errors': errors, 'no_gps_count': no_gps}
    _save_errors(diag)

    if not all_dfs:
        return pd.DataFrame(), diag

    full_df = pd.concat(all_dfs, ignore_index=True)
    # Coerce bad timestamps (tuple values from _SafeProcessor) to NaT then drop
    full_df['time'] = pd.to_datetime(full_df['time'], utc=True, errors='coerce')
    full_df = full_df.dropna(subset=['time'])
    full_df['lat'] = full_df['lat'].round(GPS_ROUNDING)
    full_df['lon'] = full_df['lon'].round(GPS_ROUNDING)
    full_df.to_parquet(CACHE_FILE)
    return full_df, diag
