# Garmin City Explorer

Garmin City Explorer is a local web app that syncs your Garmin activities, groups your runs by city, shows how much of a city's street network you have covered, and generates routes that prioritize streets you have not run yet.

It uses a FastAPI backend for Garmin sync, GIS processing, and route generation, plus a Vite + React frontend for the map UI.

## Features

- Syncs your latest Garmin runs on startup
- Lets you manually refresh with a `Sync Latest Runs` button
- Groups runs by city based on the route start point
- Shows total distance, unique covered distance, city street length, city area, and coverage percentage
- Highlights covered streets, uncovered streets, your last run, and a generated route on the map
- Generates round-trip or one-way routes that prefer unvisited streets

## Tech Stack

- Backend: FastAPI, Garmin Connect, GeoPandas, Shapely, OSMnx, Geopy
- Frontend: React, TypeScript, Vite, Leaflet, React Leaflet
- Data: local Garmin caches in `.garmin_cache/`

## Requirements

- Python 3.11 recommended
- Node.js 20+ recommended
- A Garmin Connect account

## Setup

### 1. Create your local env file

Copy `.env.example` to `.env` and add your Garmin credentials:

```env
GARMIN_EMAIL=your_email@example.com
GARMIN_PASSWORD=your_garmin_password
```

### 2. Install backend dependencies

```bash
pip install -r requirements.txt
```

### 3. Install frontend dependencies

```bash
cd frontend
npm install
cd ..
```

## Running the App

### Windows one-click launcher

```bash
run_app.bat
```

This starts:

- Backend at `http://localhost:8000`
- Frontend at `http://localhost:3000`

### Manual start

Backend:

```bash
python -m backend.main
```

Frontend:

```bash
cd frontend
npm start
```

## How to Use

1. Launch the app.
2. Wait for the initial Garmin sync to finish.
3. Pick a city from the dropdown.
4. Review your city stats and the highlighted streets on the map.
5. Click the map to choose a route start point.
6. Pick a target distance and trip type.
7. Generate a route that favors uncovered streets.

If you want to force a refresh later, use the `Sync Latest Runs` button in the UI.

## Testing

Backend tests:

```bash
set PYTHONPATH=%CD%
pytest -q
```

Frontend production build:

```bash
cd frontend
npm run build
```

## Project Structure

```text
backend/
  data_manager.py      Garmin sync, caches, coverage stats
  main.py              FastAPI API
  route_generator.py   Route generation logic
frontend/
  src/App.tsx          Main UI
  vite.config.ts       Frontend dev/build config
tests/
  test_coverage_logic.py
run_app.bat            Windows launcher
```

## Local Data and Privacy

This repo is set up to keep private local data out of Git:

- `.env`
- `.garmin_cache/`
- logs
- temp files
- local frontend env files

The repository is safe to publish through Git, but do not manually upload your whole working folder because that would include local caches and credentials that are intentionally ignored.

## Notes

- Garmin activity summaries are cached locally.
- Missing run polylines are fetched on demand when needed.
- The first load for a city can take longer because street network and GIS calculations are cached locally.
