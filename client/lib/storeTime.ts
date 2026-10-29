// Every time the app shows is in the store's time, UTC+8 with no daylight
// saving (server AppConfig.Timezone). Asia/Manila is that fixed offset.
const STORE_TIME_ZONE = 'Asia/Manila';

const dayKeyFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: STORE_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

const timeFormat = new Intl.DateTimeFormat('en-PH', {
  timeZone: STORE_TIME_ZONE,
  hour: 'numeric',
  minute: '2-digit',
});

const shortDateFormat = new Intl.DateTimeFormat('en-PH', {
  timeZone: STORE_TIME_ZONE,
  day: 'numeric',
  month: 'short',
});

const greetingHourFormat = new Intl.DateTimeFormat('en-US', {
  timeZone: STORE_TIME_ZONE,
  hour: 'numeric',
  hourCycle: 'h23',
});

// The server sends instants as RFC 1123 or ISO strings; both parse.
export function parseInstant(value: string): Date | null {
  const time = Date.parse(value);
  return Number.isNaN(time) ? null : new Date(time);
}

function dayKey(date: Date): string {
  return dayKeyFormat.format(date);
}

export function formatStoreTime(value: string): string {
  const date = parseInstant(value);
  return date ? timeFormat.format(date) : '';
}

export type ChatDayGroup = 'Today' | 'Yesterday' | 'Previous 7 days' | 'Older';

export function chatDayGroup(value: string, now: Date = new Date()): ChatDayGroup {
  const date = parseInstant(value);
  if (!date) return 'Older';
  const today = dayKey(now);
  const yesterday = dayKey(new Date(now.getTime() - 24 * 60 * 60 * 1000));
  const key = dayKey(date);
  if (key === today) return 'Today';
  if (key === yesterday) return 'Yesterday';
  if (now.getTime() - date.getTime() < 7 * 24 * 60 * 60 * 1000) return 'Previous 7 days';
  return 'Older';
}

export function formatShortDate(value: string): string {
  const date = parseInstant(value);
  return date ? shortDateFormat.format(date) : '';
}

export function storeGreeting(now: Date = new Date()): string {
  const hour = Number(greetingHourFormat.format(now));
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

const CHAT_DAY_GROUPS: ChatDayGroup[] = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];

// Rows (newest first) under their day headings, the way a chat list shows them.
export function groupConversationsByDay<T>(
  rows: T[],
  instant: (row: T) => string,
  now: Date = new Date(),
): { label: ChatDayGroup; rows: T[] }[] {
  const byGroup = new Map<ChatDayGroup, T[]>();
  for (const row of rows) {
    const label = chatDayGroup(instant(row), now);
    byGroup.set(label, [...(byGroup.get(label) ?? []), row]);
  }
  return CHAT_DAY_GROUPS.flatMap((label) => {
    const grouped = byGroup.get(label);
    return grouped ? [{ label, rows: grouped }] : [];
  });
}
