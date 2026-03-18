import React, { useState, useEffect, useMemo } from 'react';
import './App.css';
import 'leaflet/dist/leaflet.css';
import { MapContainer, TileLayer, Polyline, Marker, useMapEvents } from 'react-leaflet';
import L from 'leaflet';

// Fix for default marker icons in Leaflet with React
// @ts-ignore
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png',
  iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png',
});

const API_BASE = 'http://localhost:8000/api';

interface CityStats {
  city: string;
  total_ran_km: number;
  unique_covered_km: number;
  total_street_km: number;
  city_area_sq_km: number;
  percent_coverage: number;
  run_paths: [number, number][][];
  uncovered_streets: [number, number][][];
  covered_streets: { path: [number, number][], count: number }[];
  last_run?: {
    date: string;
    distance_km: number;
    duration_mins: number;
    speed_kmh: number;
    path: [number, number][];
  };
}

type TripType = 'round_trip' | 'one_way';
type CoveredBucket = {
  key: string;
  positions: [number, number][][];
  baseWeight: number;
  intensity: number;
  smoothFactor: number;
};
type CoveredRenderLayer = {
  key: string;
  positions: [number, number][][];
  pathOptions: L.PathOptions;
  smoothFactor: number;
};

function MapEvents({ onMapClick }: { onMapClick: (lat: number, lon: number) => void }) {
  useMapEvents({
    click: (e) => {
      onMapClick(e.latlng.lat, e.latlng.lng);
    },
  });
  return null;
}

function App() {
  const [cities, setCities] = useState<string[]>([]);
  const [selectedCity, setSelectedCity] = useState<string>('');
  const [stats, setStats] = useState<CityStats | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [loadingMsg, setLoadingMsg] = useState<string>('Processing Geographical Data...');
  const [progress, setProgress] = useState<number>(0);
  const [startPoint, setStartPoint] = useState<[number, number] | null>(null);
  const [targetDist, setTargetDist] = useState<number>(5);
  const [tripType, setTripType] = useState<TripType>('round_trip');
  const [generatedRoute, setGeneratedRoute] = useState<[number, number][] | null>(null);
  const [routeStats, setRouteStats] = useState<{ new_distance_km: number; coverage_contribution_pct: number } | null>(null);

  useEffect(() => {
    void initializeApp();
  }, []);

  const syncRuns = async () => {
    setLoading(true);
    setLoadingMsg('Checking Garmin for new runs...');
    setProgress(10);

    try {
      const res = await fetch(`${API_BASE}/fetch-runs`, { method: 'POST' });
      if (!res.ok) {
        const errorData = await res.json().catch(() => null);
        throw new Error(errorData?.detail || 'Failed to sync Garmin runs');
      }

      const data = await res.json();
      setLoadingMsg(data.message || 'Garmin sync complete.');
      setProgress(35);
      return true;
    } catch (err) {
      console.warn('Failed to sync latest Garmin runs, falling back to cached data', err);
      setLoadingMsg('Could not sync Garmin right now. Using cached data...');
      setProgress(20);
      return false;
    }
  };

  const initializeApp = async () => {
    await syncRuns();
    await fetchCities();
  };

  const fetchCities = async (preferredCity?: string) => {
    try {
      const res = await fetch(`${API_BASE}/cities`);
      const data = await res.json();
      setCities(data.cities);
      if (data.cities.length > 0) {
        const defaultCity =
          preferredCity && data.cities.includes(preferredCity)
            ? preferredCity
            : data.cities.includes("All Runs")
              ? "All Runs"
              : data.cities[0];
        setSelectedCity(defaultCity);
        await fetchCityStats(defaultCity);
      } else {
        setStats(null);
        setLoading(false);
      }
    } catch (err) {
      console.error("Failed to fetch cities", err);
      setLoading(false);
    }
  };

  const fetchCityStats = async (cityName: string) => {
    setLoading(true);
    setLoadingMsg(`Initializing ${cityName} analysis...`);
    setProgress(0);
    setGeneratedRoute(null);
    setRouteStats(null);
    setStartPoint(null);
    
    if (cityName === "All Runs") {
      try {
        const res = await fetch(`${API_BASE}/all-runs`);
        const data = await res.json();
        setStats(data);
      } catch (err) {
        console.error("Failed to fetch all runs", err);
      } finally {
        setLoading(false);
      }
      return;
    }

    try {
      const response = await fetch(`${API_BASE}/city/${cityName}`);
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) throw new Error("Failed to read response stream");

      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const dataStr = line.slice(6);
            try {
              const data = JSON.parse(dataStr);
              if (data.type === 'progress') {
                setLoadingMsg(data.status);
                setProgress(data.progress);
              } else if (data.type === 'result') {
                setStats(data.result);
                setLoading(false);
              }
            } catch (e) {
              console.error("Error parsing SSE data", e);
            }
          }
        }
      }
    } catch (err) {
      console.error("Failed to fetch city stats", err);
      setLoading(false);
    }
  };

  const handleGenerateRoute = async () => {
    if (!startPoint || !selectedCity) return;
    const tripLabel = tripType === 'round_trip' ? 'round trip' : 'one-way';
    setLoading(true);
    setLoadingMsg(`Generating ${tripLabel} unvisited route (${targetDist}km)...`);
    setGeneratedRoute(null);
    setRouteStats(null);
    console.log(`Generating route for ${selectedCity} from ${startPoint}`);
    
    try {
      const res = await fetch(`${API_BASE}/generate-route`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          city_name: selectedCity,
          start_point: startPoint,
          target_distance_km: targetDist,
          trip_type: tripType
        })
      });
      
      if (!res.ok) {
        const errorData = await res.json();
        throw new Error(errorData.detail || "Failed to generate route");
      }

      const data = await res.json();
      if (data.route && data.route.length > 0) {
        console.log(`Route received with ${data.route.length} points`);
        setGeneratedRoute(data.route);
        setRouteStats({
          new_distance_km: data.new_distance_km ?? 0,
          coverage_contribution_pct: data.coverage_contribution_pct ?? 0,
        });
      } else {
        alert("No suitable route found. Try a different start point or distance.");
      }
    } catch (err: any) {
      console.error("Failed to generate route", err);
      alert(`Failed to generate route: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleCityChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setSelectedCity(e.target.value);
    void fetchCityStats(e.target.value);
  };

  const handleSyncLatestRuns = async () => {
    const currentCity = selectedCity;
    await syncRuns();
    await fetchCities(currentCity);
  };

  const mapCenter: [number, number] = stats && stats.run_paths.length > 0 
    ? stats.run_paths[0][0] 
    : [32.0853, 34.7818];

  const uncoveredStreetPositions = useMemo<[number, number][][]>(
    () => stats?.uncovered_streets ?? [],
    [stats?.uncovered_streets]
  );

  const uncoveredStreetLayers = useMemo(() => {
    if (uncoveredStreetPositions.length === 0) return [];
    return [
      {
        key: 'uncovered-halo-outer',
        smoothFactor: 1.95,
        pathOptions: {
          color: 'rgba(60, 185, 255, 1)',
          weight: 7.4,
          opacity: 0.08,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      },
      {
        key: 'uncovered-halo-inner',
        smoothFactor: 1.9,
        pathOptions: {
          color: 'rgba(44, 165, 255, 1)',
          weight: 4.4,
          opacity: 0.14,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      },
      {
        key: 'uncovered-core',
        smoothFactor: 1.85,
        pathOptions: {
          color: '#35b2ff',
          weight: 2.1,
          opacity: 0.62,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      }
    ];
  }, [uncoveredStreetPositions]);

  const coveredBuckets = useMemo<CoveredBucket[]>(() => {
    if (!stats?.covered_streets?.length) return [];

    const buckets: CoveredBucket[] = [
      {
        key: 'covered-1',
        positions: [],
        baseWeight: 2.2,
        intensity: 0.35,
        smoothFactor: 1.8
      },
      {
        key: 'covered-2-3',
        positions: [],
        baseWeight: 3.0,
        intensity: 0.55,
        smoothFactor: 1.7
      },
      {
        key: 'covered-4-7',
        positions: [],
        baseWeight: 4.2,
        intensity: 0.78,
        smoothFactor: 1.6
      },
      {
        key: 'covered-8+',
        positions: [],
        baseWeight: 5.6,
        intensity: 1.0,
        smoothFactor: 1.5
      }
    ];

    for (const street of stats.covered_streets) {
      const count = street.count || 1;
      if (count >= 8) {
        buckets[3].positions.push(street.path);
      } else if (count >= 4) {
        buckets[2].positions.push(street.path);
      } else if (count >= 2) {
        buckets[1].positions.push(street.path);
      } else {
        buckets[0].positions.push(street.path);
      }
    }

    return buckets.filter((bucket) => bucket.positions.length > 0);
  }, [stats?.covered_streets]);

  const coveredStreetLayers = useMemo<CoveredRenderLayer[]>(() => {
    const layers: CoveredRenderLayer[] = [];

    for (const bucket of coveredBuckets) {
      const outerHaloOpacity = 0.08 + bucket.intensity * 0.07;
      const innerHaloOpacity = 0.15 + bucket.intensity * 0.10;
      const coreOpacity = 0.84 + bucket.intensity * 0.14;
      const coreLightness = 58 - bucket.intensity * 10;
      const coreColor = `hsl(350, 100%, ${coreLightness}%)`;

      layers.push(
        {
          key: `${bucket.key}-halo-outer`,
          positions: bucket.positions,
          smoothFactor: bucket.smoothFactor,
          pathOptions: {
            color: 'rgba(255, 42, 95, 1)',
            weight: bucket.baseWeight + 7.2,
            opacity: outerHaloOpacity,
            lineCap: 'round',
            lineJoin: 'round',
            interactive: false
          }
        },
        {
          key: `${bucket.key}-halo-inner`,
          positions: bucket.positions,
          smoothFactor: bucket.smoothFactor,
          pathOptions: {
            color: 'rgba(255, 25, 78, 1)',
            weight: bucket.baseWeight + 3.8,
            opacity: innerHaloOpacity,
            lineCap: 'round',
            lineJoin: 'round',
            interactive: false
          }
        },
        {
          key: `${bucket.key}-core`,
          positions: bucket.positions,
          smoothFactor: bucket.smoothFactor,
          pathOptions: {
            color: coreColor,
            weight: bucket.baseWeight,
            opacity: Math.min(1, coreOpacity),
            lineCap: 'round',
            lineJoin: 'round',
            interactive: false
          }
        }
      );
    }

    return layers;
  }, [coveredBuckets]);

  const generatedRouteLayers = useMemo(() => {
    if (!generatedRoute || generatedRoute.length === 0) return [];
    return [
      {
        key: 'route-halo-outer',
        smoothFactor: 1.2,
        pathOptions: {
          color: 'rgba(255, 70, 200, 1)',
          weight: 12,
          opacity: 0.1,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      },
      {
        key: 'route-halo-inner',
        smoothFactor: 1.15,
        pathOptions: {
          color: 'rgba(255, 44, 180, 1)',
          weight: 8,
          opacity: 0.2,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      },
      {
        key: 'route-core',
        smoothFactor: 1.1,
        pathOptions: {
          color: '#ff35b8',
          weight: 4.5,
          opacity: 0.98,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      }
    ];
  }, [generatedRoute]);

  const lastRunLayers = useMemo(() => {
    if (!stats?.last_run?.path || stats.last_run.path.length === 0) return [];
    return [
      {
        key: 'lastrun-halo-outer',
        smoothFactor: 1.2,
        pathOptions: {
          color: 'rgba(50, 255, 50, 1)',
          weight: 14,
          opacity: 0.15,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      },
      {
        key: 'lastrun-halo-inner',
        smoothFactor: 1.15,
        pathOptions: {
          color: 'rgba(0, 255, 127, 1)',
          weight: 9,
          opacity: 0.3,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      },
      {
        key: 'lastrun-core',
        smoothFactor: 1.1,
        pathOptions: {
          color: '#00ff66',
          weight: 5,
          opacity: 1,
          lineCap: 'round',
          lineJoin: 'round',
          interactive: false
        } as L.PathOptions
      }
    ];
  }, [stats?.last_run]);

  return (
    <div className="app-container">
      <div className="sidebar">
        <h1>Garmin City Explorer</h1>
        
        <div className="city-selector">
          <label>Select City</label>
          <select value={selectedCity} onChange={handleCityChange}>
            {cities.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <button
            type="button"
            onClick={() => void handleSyncLatestRuns()}
            disabled={loading}
            style={{ marginTop: '10px' }}
          >
            Sync Latest Runs
          </button>
        </div>

        {stats && (
          <div className="stats-panel">
            <h2>{stats.city} Statistics</h2>
            <div className="stat-item">
              <span className="stat-label">Total Running Distance</span>
              <span className="stat-value">{stats.total_ran_km.toFixed(2)} km</span>
            </div>
            
            {selectedCity !== "All Runs" && (
              <>
                <div className="stat-item">
                  <span className="stat-label">Unique Covered Distance</span>
                  <span className="stat-value highlight">{stats.unique_covered_km.toFixed(2)} km</span>
                </div>
                <div className="stat-item">
                  <span className="stat-label">Total Street Length</span>
                  <span className="stat-value">{stats.total_street_km.toFixed(2)} km</span>
                </div>
                <div className="stat-item">
                  <span className="stat-label">City Size (Area)</span>
                  <span className="stat-value">{stats.city_area_sq_km.toFixed(2)} km²</span>
                </div>
                <div className="stat-item">
                  <span className="stat-label">Street Coverage</span>
                  <span className="stat-value highlight">{stats.percent_coverage.toFixed(1)}%</span>
                </div>

                {stats.last_run && (
                  <div className="last-run-box">
                    <h3>Last Run Details</h3>
                    <div className="stat-item small">
                      <span className="stat-label">Date</span>
                      <span className="stat-value">{new Date(stats.last_run.date).toLocaleDateString()}</span>
                    </div>
                    <div className="stat-item small">
                      <span className="stat-label">Distance</span>
                      <span className="stat-value">{stats.last_run.distance_km.toFixed(2)} km</span>
                    </div>
                    <div className="stat-item small">
                      <span className="stat-label">Duration</span>
                      <span className="stat-value">{stats.last_run.duration_mins.toFixed(0)} mins</span>
                    </div>
                    <div className="stat-item small">
                      <span className="stat-label">Avg Speed</span>
                      <span className="stat-value">{stats.last_run.speed_kmh.toFixed(1)} km/h</span>
                    </div>
                  </div>
                )}

                <div className="route-generator">
                  <h3>Route Generator</h3>
                  <p style={{fontSize: '0.85rem', color: '#aaa', marginBottom: '15px'}}>
                    Click on the map to set a {tripType === 'round_trip' ? 'start/finish' : 'start'} point.
                  </p>
                  <div className="input-group">
                    <label>Target Distance (km)</label>
                    <input 
                      type="number" 
                      value={targetDist} 
                      onChange={(e) => setTargetDist(parseFloat(e.target.value))}
                      min="1"
                      max="50"
                    />
                  </div>
                  <div className="input-group">
                    <label>Trip Type</label>
                    <select
                      value={tripType}
                      onChange={(e) => setTripType(e.target.value as TripType)}
                    >
                      <option value="round_trip">Round Trip</option>
                      <option value="one_way">One Way</option>
                    </select>
                  </div>
                  <button
                    onClick={handleGenerateRoute}
                    disabled={!startPoint || loading}
                    style={{ backgroundColor: '#ff35b8' }}
                  >
                    {loading ? 'Generating...' : 'Generate Unvisited Route'}
                  </button>
                  {routeStats && (
                    <div className="route-stats">
                      <div className="stat-item">
                        <span className="stat-label">Discover Distance</span>
                        <span className="stat-value highlight">{routeStats.new_distance_km.toFixed(2)} km</span>
                      </div>
                      <div className="stat-item">
                        <span className="stat-label">Coverage Contribution</span>
                        <span className="stat-value highlight">+{routeStats.coverage_contribution_pct.toFixed(2)}%</span>
                      </div>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>

      <div className="map-container">
        {selectedCity !== "All Runs" && !startPoint && selectedCity && (
          <div className="map-hint">
            Click anywhere to set {tripType === 'round_trip' ? 'Start/Finish' : 'Start'} point
          </div>
        )}
        <MapContainer center={mapCenter} zoom={13} scrollWheelZoom={true} preferCanvas={true}>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          />
          <MapEvents onMapClick={(lat, lon) => setStartPoint([lat, lon])} />
          
          {/* Unvisited Streets in Neon Blue */}
          {uncoveredStreetLayers.map((layer) => (
            <Polyline
              key={layer.key}
              positions={uncoveredStreetPositions}
              smoothFactor={layer.smoothFactor}
              pathOptions={layer.pathOptions}
            />
          ))}

          {/* User Runs in Neon Crimson (batched glow + core layers) */}
          {coveredStreetLayers.map((layer) => (
            <Polyline
              key={layer.key}
              positions={layer.positions}
              smoothFactor={layer.smoothFactor}
              pathOptions={layer.pathOptions}
            />
          ))}

          {/* Fallback for All Runs or simple polylines if covered_streets is empty */}
          {coveredStreetLayers.length === 0 && stats?.run_paths.map((path, idx) => (
             <Polyline 
              key={`raw-path-${idx}`}
              positions={path}
              pathOptions={{ color: '#ff194e', weight: 3, opacity: 0.6 }}
             />
          ))}

          {/* Last Run in Shiny Green */}
          {lastRunLayers.map((layer) => (
            <Polyline
              key={layer.key}
              positions={stats!.last_run!.path}
              smoothFactor={layer.smoothFactor}
              pathOptions={layer.pathOptions}
            />
          ))}

          {/* Generated Route in Pink */}
          {generatedRouteLayers.map((layer) => (
            <Polyline
              key={layer.key}
              positions={generatedRoute!}
              smoothFactor={layer.smoothFactor}
              pathOptions={layer.pathOptions}
            />
          ))}

          {startPoint && (
            <Marker position={startPoint} />
          )}
        </MapContainer>
      </div>

      {loading && (
        <div className="loading-overlay">
          <div className="loading-content">
            <div className="spinner"></div>
            <div className="progress-container">
              <div className="progress-bar" style={{ width: `${progress}%` }}></div>
            </div>
            <div className="progress-text">{progress}%</div>
            <div className="loading-status">{loadingMsg}</div>
            <div style={{fontSize: '0.8rem', marginTop: '10px', color: '#aaa'}}>
              (First load for a city may take 30-60 seconds)
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


export default App;
