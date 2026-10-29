'use client';
import React, { createContext, useContext, useMemo } from 'react';
import { ToastProvider, toastManager } from '@/components/ui/toast';
import type { ApiResponse } from '@/app/api/ApiResponse';

type SnackbarType = 'success' | 'error' | 'info' | 'warning';

interface SnackbarContextType {
  // Every POST a button triggers reports its result here: the server's
  // message, green on a 200 and red otherwise.
  showApiResponseSnackbar: <T>(res: ApiResponse<T>) => void;
  showSnackbar: (message: string, type?: SnackbarType) => void;
}

function show(message: string, type: SnackbarType = 'info') {
  toastManager.add({ title: message, type });
}

const SnackbarContext = createContext<SnackbarContextType>({
  showApiResponseSnackbar: (res) => show(res.message, res.status === 200 ? 'success' : 'error'),
  showSnackbar: show,
});

export const useSnackbar = () => useContext(SnackbarContext);

export function SnackbarProvider({ children }: { children: React.ReactNode }) {
  const value = useMemo<SnackbarContextType>(() => ({
    showApiResponseSnackbar: (res) => show(res.message, res.status === 200 ? 'success' : 'error'),
    showSnackbar: show,
  }), []);

  return (
    <ToastProvider position="bottom-center">
      <SnackbarContext.Provider value={value}>{children}</SnackbarContext.Provider>
    </ToastProvider>
  );
}
