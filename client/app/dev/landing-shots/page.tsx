import { notFound } from 'next/navigation';
import { LandingShots } from './LandingShots';

// The real chat cards with sample data, for the landing page's screenshots
// (client/scripts/landingShots.mjs). Development only: a 404 in a build.
export default function LandingShotsPage() {
  if (process.env.NODE_ENV !== 'development') notFound();
  return <LandingShots />;
}
