'use client';
import { useState } from 'react';
import { useSnackbar } from '@/lib/SnackbarContext';
import type { MapPoint } from './MapPicker';

// A form's "Use where I am": asks the browser for the device's position and
// hands it to `onFound` (which moves the map there).
export function useWhereIAm(onFound: (point: MapPoint) => void): { locating: boolean; locate: () => void } {
  const { showSnackbar } = useSnackbar();
  const [locating, setLocating] = useState(false);

  const locate = () => {
    if (!('geolocation' in navigator)) {
      showSnackbar('This browser can’t share its location. Move the map instead.', 'warning');
      return;
    }
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLocating(false);
        onFound({ latitude: position.coords.latitude, longitude: position.coords.longitude });
      },
      () => {
        setLocating(false);
        showSnackbar('We couldn’t get your location. Allow location access, or move the map.', 'warning');
      },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  };

  return { locating, locate };
}
