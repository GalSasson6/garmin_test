from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Literal
from backend.data_manager import DataManager
from backend.route_generator import RouteGenerator
import uvicorn
import json
import asyncio

app = FastAPI()

# Add CORS middleware to allow the React frontend to communicate with the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # For development, we allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

data_manager = DataManager()
route_generator = RouteGenerator()

class RouteRequest(BaseModel):
    city_name: str
    start_point: List[float] # [lat, lon]
    target_distance_km: float
    trip_type: Literal["round_trip", "one_way"] = "round_trip"

@app.get("/api/cities")
async def get_cities():
    try:
        cities = data_manager.get_cities()
        return {"cities": ["All Runs"] + cities}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/all-runs")
async def get_all_runs():
    try:
        result = data_manager.get_all_runs()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/city/{city_name}")
async def get_city_stats(city_name: str):
    async def event_generator():
        # Run the generator in a thread to avoid blocking the event loop
        # since DataManager GIS code is CPU intensive and synchronous
        loop = asyncio.get_event_loop()
        
        def run_sync_gen():
            return data_manager.get_city_stats_stream(city_name)
            
        gen = await loop.run_in_executor(None, run_sync_gen)
        
        for update in gen:
            yield f"data: {json.dumps(update)}\n\n"
            # Small sleep to allow other tasks to run
            await asyncio.sleep(0.01)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/generate-route")
async def generate_route(request: RouteRequest):
    try:
        # Get city stats to have the run paths for buffer calculation
        stats = data_manager.get_city_stats(request.city_name)
        if not stats:
            raise HTTPException(status_code=404, detail="City not found")
        
        result = route_generator.generate_route(
            request.city_name,
            request.start_point,
            request.target_distance_km,
            stats['run_paths'],
            request.trip_type
        )

        if not result:
            raise HTTPException(status_code=404, detail="Could not generate route with given parameters")

        route, new_distance_km = result
        total_street_km = stats.get('total_street_km', 0)
        coverage_contribution_pct = (new_distance_km / total_street_km * 100) if total_street_km > 0 else 0

        return {
            "route": route,
            "new_distance_km": round(new_distance_km, 2),
            "coverage_contribution_pct": round(coverage_contribution_pct, 2),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/fetch-runs")
async def fetch_runs():
    try:
        success, message = data_manager.fetch_new_activities()
        if success:
            return {"message": message}
        else:
            raise HTTPException(status_code=400, detail=message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
