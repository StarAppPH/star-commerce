'use client';

import { useAuth } from '../../lib/AuthContext';

interface RoleGuardProps {
  roles: string[];
  children: React.ReactNode;
  fallback?: React.ReactNode;
  showUnauthorizedMessage?: boolean;
  unauthorizedMessage?: string;
}

export default function RoleGuard({ roles, children, fallback = null, showUnauthorizedMessage = false, unauthorizedMessage }: RoleGuardProps) {
  const { userData } = useAuth();

  const userRoles = userData?.roles ?? [];
  const hasAccess = roles.some(role =>
    userRoles.some(ur => ur._id === role && ur.value === true)
  );

  if (!hasAccess) {
    if (fallback) return <>{fallback}</>;
    if (showUnauthorizedMessage) return <p className="text-sm text-red-600">{unauthorizedMessage || 'You do not have permission to access this section.'}</p>;
    return null;
  }

  return (
    <div data-component="RoleGuard" data-role-guard={JSON.stringify(roles)}>
      {children}
    </div>
  );
}
