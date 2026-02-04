# Runner Heatmap

This project provides a python web application that helps you visualize your geolocalized sports history as an interactive **Heatmap**. It transforms raw `.fit` files into a clean, filterable map to visualize your training patterns over time.

## Main features

  - **Sport filter**: Quickly toggle activities (e.g. running, cycling, hiking)
  - **Date filter**: Choose the activity period you want to display.
  - **Map style**: Toggle between different map modes : satellite, terrain, light and dark.
  - **Caching**: A cache_data.parquet file is created locally. If you add new files, use the "Force Reload" button to refresh the cache.

## How to get your `.fit` files

To use this app, you need your historical activity data. You can request a full export from Garmin:

1. **Log in** to [Garmin Account Management](https://www.garmin.com/en-US/account/datamanagement/).
2. Select **Export Your Data**.
3. Click **Request Data Export**.
4. **Wait for the email**: Garmin will send you a download link (this can take anywhere from an hour to a few days depending on your history).
5. **Extract the ZIP**: Once downloaded, look for the `DI_CONNECT/DI-Connect-Fitness-Uploaded-Files` folder. This folder contains all your original `.fit` files.
6. **Point the App**: Copy the path to that folder and paste it into the "Garmin Folder" input in the app sidebar.


## Installation and lauch

1. **Set up and activate virtual python3 env**
```bash
# Create the environment
python3 -m venv venv

# Activate it (macOS/Linux)
source venv/bin/activate

# Activate it (Windows)
# .\venv\Scripts\activate
```

2. **Install dependencies**
```bash
pip3 install -r requirements.txt
```

3. **Run the app**
```bash
streamlit run app.py

Application can be reached on http://localhost:8501/
