import { useState, useRef, useCallback, useEffect } from 'react';

interface GeolocationState {
  position: { lat: number; lng: number } | null;
  accuracy: number | null;
  error: string | null;
  isTracking: boolean;
}

export function useGeolocation(): GeolocationState & {
  startTracking: () => void;
  stopTracking: () => void;
} {
  const [state, setState] = useState<GeolocationState>({
    position: null,
    accuracy: null,
    error: null,
    isTracking: false,
  });
  const watchId = useRef<number | null>(null);

  const stopTracking = useCallback(() => {
    if (watchId.current !== null) {
      navigator.geolocation.clearWatch(watchId.current);
      watchId.current = null;
    }
    setState(prev => ({ ...prev, isTracking: false }));
  }, []);

  const startTracking = useCallback(() => {
    if (!navigator.geolocation) {
      setState(prev => ({ ...prev, error: 'Geolocation not supported' }));
      return;
    }

    setState(prev => ({ ...prev, isTracking: true, error: null }));

    watchId.current = navigator.geolocation.watchPosition(
      (pos) => {
        setState(prev => ({
          ...prev,
          position: { lat: pos.coords.latitude, lng: pos.coords.longitude },
          accuracy: pos.coords.accuracy,
          error: null,
        }));
      },
      (err) => {
        const messages: Record<number, string> = {
          1: 'Location permission denied',
          2: 'Position unavailable',
          3: 'Location request timed out',
        };
        setState(prev => ({
          ...prev,
          error: messages[err.code] || 'Unknown geolocation error',
          isTracking: false,
        }));
        if (watchId.current !== null) {
          navigator.geolocation.clearWatch(watchId.current);
          watchId.current = null;
        }
      },
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 }
    );
  }, []);

  useEffect(() => {
    return () => {
      if (watchId.current !== null) {
        navigator.geolocation.clearWatch(watchId.current);
      }
    };
  }, []);

  return { ...state, startTracking, stopTracking };
}
