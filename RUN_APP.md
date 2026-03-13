# Garmin City Explorer - UI App

This application allows you to visualize your Garmin runs in different cities, view statistics about your coverage, and generate optimized routes that maximize unvisited land.

## Prerequisites
- Python 3.8+
- Node.js & npm
- Garmin Connect credentials (set in `.env` file in the root directory)

## Setup & Running

### 🚀 Recommended: One-Click Script (Windows)
Just run:
```bash
run_app.bat
```
This will automatically install any missing dependencies and launch both the backend and frontend in separate windows.

### Manual Setup
#### 1. Backend (FastAPI)
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the FastAPI server:
   ```bash
   python -m backend.main
   ```
   The backend will be available at `http://localhost:8000`.

### 2. Frontend (React)
1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```
2. Install dependencies:
   ```bash
   npm install
   ```
3. Start the React app:
   ```bash
   npm start
   ```
   The UI will open in your browser at `http://localhost:3000`.

## Features
- **City Picker**: Automatically detects cities from your run history.
- **Detailed Stats**: Shows total distance, unique coverage km, city size, and completion percentage.
- **Route Generator**:
  1. Pick a city.
  2. Click on the map to set your Start/Finish point.
  3. Set a target distance.
  4. Click "Generate Unvisited Route" to get a loop that prioritizes streets you haven't run on yet!
