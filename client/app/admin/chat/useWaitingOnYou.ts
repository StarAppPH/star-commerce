'use client';
import { useEffect, useState } from 'react';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import { WAITING_COUNT_CAP } from '@/lib/generated/serverConstants';
import { unansweredPolicyQuestions } from '@/lib/policyQuestions';

export interface WaitingItem {
  key: 'payments' | 'handover' | 'returns' | 'policies';
  dataComponent: string;
  count: string;
  label: string;
  // What tapping it asks the assistant.
  prompt: string;
  tone: 'info' | 'warning';
}

// getWaitingOnYou caps each count at WAITING_COUNT_CAP; a count at the cap
// means at least that many.
function shownCount(count: number): string {
  return count >= WAITING_COUNT_CAP ? `${WAITING_COUNT_CAP}+` : String(count);
}

// What needs the admin now, for the new-chat screen: payments made by hand to
// confirm, paid orders to hand over, return requests to decide and policy
// questions left open. A count that fails to load is left out; nothing loads
// until `enabled` (a store not set up yet has none).
export function useWaitingOnYou(userId: string, enabled: boolean): WaitingItem[] {
  const { server } = useServer();
  const { adminRequest } = useAuth();
  const [items, setItems] = useState<WaitingItem[]>([]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      const [waiting, policy] = await Promise.all([
        adminRequest(() => server.getWaitingOnYou(userId)),
        adminRequest(() => server.getMyStorePolicy(userId)),
      ]);
      if (cancelled) return;
      const next: WaitingItem[] = [];
      if (waiting.status === 200) {
        const { paymentsToConfirm, ordersToHandOver, returnsToDecide } = waiting.data;
        if (paymentsToConfirm > 0) {
          next.push({
            key: 'payments',
            dataComponent: 'adminHomeSuggestPayments',
            count: shownCount(paymentsToConfirm),
            label: paymentsToConfirm === 1 ? 'Payment to confirm' : 'Payments to confirm',
            prompt: 'Show me the orders waiting for me to confirm their payment.',
            tone: 'warning',
          });
        }
        if (ordersToHandOver > 0) {
          next.push({
            key: 'handover',
            dataComponent: 'adminHomeSuggestHandover',
            count: shownCount(ordersToHandOver),
            label: ordersToHandOver === 1 ? 'Order to hand over' : 'Orders to hand over',
            prompt: 'Show me the paid orders I still need to hand over.',
            tone: 'info',
          });
        }
        if (returnsToDecide > 0) {
          next.push({
            key: 'returns',
            dataComponent: 'adminHomeSuggestReturns',
            count: shownCount(returnsToDecide),
            label: returnsToDecide === 1 ? 'Return request' : 'Return requests',
            prompt: 'Show me the return requests waiting for my decision.',
            tone: 'warning',
          });
        }
      }
      if (policy.status === 200) {
        const open = unansweredPolicyQuestions(policy.data);
        if (open > 0) {
          next.push({
            key: 'policies',
            dataComponent: 'adminHomeSuggestPolicies',
            count: String(open),
            label: open === 1 ? 'Unanswered policy question' : 'Unanswered policy questions',
            prompt: 'Let’s answer my store’s open policy questions.',
            tone: 'warning',
          });
        }
      }
      setItems(next);
    })();
    return () => { cancelled = true; };
  }, [adminRequest, server, userId, enabled]);

  return items;
}
