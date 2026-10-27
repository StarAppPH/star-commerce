'use client';
import React, { createContext, useContext, useMemo } from 'react';
import ServerRequests from '@/app/api/ServerRequests';

const environment = process.env.NEXT_PUBLIC_ENVIRONMENT || 'localdev';

interface ServerContextType {
  server: ServerRequests;
  environment: string;
}

const ServerContext = createContext<ServerContextType | null>(null);

export const useServer = () => {
  const context = useContext(ServerContext);
  if (!context) {
    throw new Error('useServer must be used within a ServerProvider');
  }
  return context;
};

export function ServerProvider({ children }: { children: React.ReactNode }) {
  const value = useMemo(() => ({
    server: new ServerRequests(environment),
    environment,
  }), []);

  return (
    <ServerContext.Provider value={value}>
      <div data-component="ServerProvider">{children}</div>
    </ServerContext.Provider>
  );
}
