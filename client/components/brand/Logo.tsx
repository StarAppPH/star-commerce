import { StarIcon } from 'lucide-react';
import { cn } from '@/lib/utils';

// The Star Commerce mark: a star on a dark rounded tile.
export function Logo({ size = 'default', className }: { size?: 'sm' | 'default' | 'lg'; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex shrink-0 items-center justify-center bg-primary text-primary-foreground',
        size === 'sm' && 'size-6 rounded-md [&_svg]:size-3',
        size === 'default' && 'size-8 rounded-lg [&_svg]:size-4',
        size === 'lg' && 'size-10 rounded-xl [&_svg]:size-5',
        className,
      )}
    >
      <StarIcon />
    </span>
  );
}
