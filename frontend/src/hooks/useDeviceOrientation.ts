import { useState, useCallback, useEffect, useRef } from 'react';

interface DeviceOrientationState {
  heading: number | null;
  isSupported: boolean;
  permissionState: 'prompt' | 'granted' | 'denied' | 'unsupported';
}

export function useDeviceOrientation(): DeviceOrientationState & {
  requestPermission: () => Promise<void>;
} {
  const [state, setState] = useState<DeviceOrientationState>({
    heading: null,
    isSupported: 'DeviceOrientationEvent' in window,
    permissionState: 'DeviceOrientationEvent' in window ? 'prompt' : 'unsupported',
  });
  const listening = useRef(false);

  const handler = useCallback((event: DeviceOrientationEvent) => {
    let heading: number | null = null;

    // iOS provides webkitCompassHeading (0-360, 0=north, clockwise)
    if ((event as any).webkitCompassHeading != null) {
      heading = (event as any).webkitCompassHeading;
    } else if (event.alpha != null) {
      // Android/other: alpha is rotation around z-axis
      // When absolute is true, convert to compass bearing
      heading = (360 - event.alpha) % 360;
    }

    setState(prev => ({ ...prev, heading }));
  }, []);

  const startListening = useCallback(() => {
    if (!listening.current) {
      window.addEventListener('deviceorientation', handler, true);
      listening.current = true;
    }
  }, [handler]);

  const requestPermission = useCallback(async () => {
    if (!state.isSupported) return;

    // iOS 13+ requires explicit permission request from user gesture
    const DOE = DeviceOrientationEvent as any;
    if (typeof DOE.requestPermission === 'function') {
      try {
        const result = await DOE.requestPermission();
        if (result === 'granted') {
          setState(prev => ({ ...prev, permissionState: 'granted' }));
          startListening();
        } else {
          setState(prev => ({ ...prev, permissionState: 'denied' }));
        }
      } catch {
        setState(prev => ({ ...prev, permissionState: 'denied' }));
      }
    } else {
      // Android/desktop: no permission needed
      setState(prev => ({ ...prev, permissionState: 'granted' }));
      startListening();
    }
  }, [state.isSupported, startListening]);

  useEffect(() => {
    return () => {
      if (listening.current) {
        window.removeEventListener('deviceorientation', handler, true);
        listening.current = false;
      }
    };
  }, [handler]);

  return { ...state, requestPermission };
}
