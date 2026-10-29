import { cn, initials } from '@/lib/utils';

// A store's mark: the first letter of its name on a rounded tile.
export function StoreMark({ name, size = 'default', className }: { name: string; size?: 'sm' | 'default' | 'lg'; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex shrink-0 items-center justify-center bg-primary font-heading text-primary-foreground',
        size === 'sm' && 'size-7 rounded-lg text-sm',
        size === 'default' && 'size-8 rounded-[10px] text-[15px]',
        size === 'lg' && 'size-15 rounded-[18px] text-[28px]',
        className,
      )}
    >
      {initials(name).slice(0, 1)}
    </span>
  );
}
