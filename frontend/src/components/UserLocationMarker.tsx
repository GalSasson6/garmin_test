import React, { useMemo } from 'react';
import { Marker, Circle } from 'react-leaflet';
import L from 'leaflet';

interface UserLocationMarkerProps {
  position: { lat: number; lng: number };
  heading: number | null;
  accuracy: number | null;
}

function UserLocationMarker({ position, heading, accuracy }: UserLocationMarkerProps) {
  const icon = useMemo(() => {
    const hasHeading = heading !== null;
    const html = hasHeading
      ? `<div style="transform: rotate(${heading}deg); transition: transform 0.3s ease; width: 30px; height: 30px;">
           <svg width="30" height="30" viewBox="0 0 30 30">
             <polygon points="15,2 25,26 15,20 5,26" fill="#00e5ff" stroke="#007a8a" stroke-width="1"/>
           </svg>
         </div>`
      : `<div class="user-location-dot"></div>`;

    return L.divIcon({
      html,
      className: 'user-location-icon',
      iconSize: [30, 30],
      iconAnchor: [15, 15],
    });
  }, [heading]);

  const center: [number, number] = [position.lat, position.lng];

  return (
    <>
      <Marker position={center} icon={icon} />
      {accuracy !== null && accuracy <= 200 && (
        <Circle
          center={center}
          radius={accuracy}
          pathOptions={{
            color: '#00e5ff',
            fillColor: '#00e5ff',
            fillOpacity: 0.08,
            weight: 1,
            opacity: 0.3,
          }}
        />
      )}
    </>
  );
}

export default UserLocationMarker;
