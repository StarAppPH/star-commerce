'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ApiResponse } from '@/app/api/ApiResponse';
import type { ConversationListResponse } from '@/app/schemas/ConversationListResponseSchema';
import type { ConversationRow } from '@/app/schemas/ConversationRowSchema';

// How many chats a chat list loads at a time (listMyConversations takes 1-50).
export const CONVERSATIONS_PAGE = 30;

export interface ConversationList {
  conversations: ConversationRow[];
  conversationsLoading: boolean;
  hasOlderConversations: boolean;
  loadOlderConversations: () => Promise<void>;
  reloadConversations: () => Promise<void>;
}

// A chat list, newest first, a page at a time. `fetchPage` asks for the page
// before a cursor (null: the newest); nothing loads until `enabled`.
export function useConversationList(
  fetchPage: (before: string | null) => Promise<ApiResponse<ConversationListResponse>>,
  enabled: boolean,
): ConversationList {
  const [conversations, setConversations] = useState<ConversationRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const olderLoaded = useRef(false);

  // The newest page again, keeping the older pages already loaded: a chat
  // that moved to the top is not listed twice.
  const reloadConversations = useCallback(async () => {
    if (!enabled) return;
    const res = await fetchPage(null);
    if (res.status !== 200) return;
    const page = res.data.conversations;
    if (olderLoaded.current) {
      const ids = new Set(page.map((row) => row._id));
      setConversations((current) => [...page, ...current.filter((row) => !ids.has(row._id))]);
    } else {
      setConversations(page);
      setNextBefore(res.data.nextBefore);
    }
  }, [fetchPage, enabled]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      await reloadConversations();
      if (!cancelled) setLoaded(true);
    })();
    return () => { cancelled = true; };
  }, [enabled, reloadConversations]);

  const loadOlderConversations = useCallback(async () => {
    if (!nextBefore) return;
    const res = await fetchPage(nextBefore);
    if (res.status !== 200) return;
    olderLoaded.current = true;
    setConversations((current) => {
      const ids = new Set(current.map((row) => row._id));
      return [...current, ...res.data.conversations.filter((row) => !ids.has(row._id))];
    });
    setNextBefore(res.data.nextBefore);
  }, [fetchPage, nextBefore]);

  return useMemo(() => ({
    conversations,
    conversationsLoading: enabled && !loaded,
    hasOlderConversations: nextBefore !== null,
    loadOlderConversations,
    reloadConversations,
  }), [conversations, enabled, loaded, nextBefore, loadOlderConversations, reloadConversations]);
}

// The chats whose title holds `search` (any case); all of them for none.
export function matchingConversations(rows: ConversationRow[], search: string): ConversationRow[] {
  const query = search.trim().toLowerCase();
  return query ? rows.filter((row) => (row.title ?? '').toLowerCase().includes(query)) : rows;
}
