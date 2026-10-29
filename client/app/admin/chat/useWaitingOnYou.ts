'use client';
import { useEffect, useState } from 'react';
import { useAuth } from '@/lib/AuthContext';
import { useServer } from '@/lib/ServerContext';
import type { StorePolicyResponse } from '@/app/schemas/StorePolicyResponseSchema';

// The most orders or returns one count reads (the list routes take 1-50).
const COUNT_LIMIT = 50;

export interface WaitingItem {
  key: 'handover' | 'returns' | 'policies';
  dataComponent: string;
  count: string;
  label: string;
  // What tapping it asks the assistant.
  prompt: string;
  tone: 'info' | 'warning';
}

// The policy questions the admin has not answered yet (a null answer).
function unansweredPolicyQuestions(policy: StorePolicyResponse): number {
  const answers = [
    policy.acceptsReturns,
    policy.returnWindowDays,
    policy.returnConditions,
    policy.nonReturnableItems,
    policy.refundMethod,
    policy.returnShippingPaidBy,
    policy.defectReportDays,
    policy.deliveryOptions,
    policy.deliveryAreas,
    policy.deliveryFee,
    policy.freeDeliveryMinimum,
    policy.handoverDays,
    policy.acceptedPaymentMethods,
    policy.paidOrderCancellation,
    policy.sendsMarketingMessages,
    policy.additionalTerms,
  ];
  return answers.filter((answer) => answer === null).length;
}

function shownCount(count: number): string {
  return count >= COUNT_LIMIT ? `${COUNT_LIMIT}+` : String(count);
}

// What needs the admin now, for the new-chat screen: paid orders to hand
// over, return requests to decide and policy questions left open. A count
// that fails to load is left out; nothing loads until `enabled` (a store
// not set up yet has none).
export function useWaitingOnYou(userId: string, enabled: boolean): WaitingItem[] {
  const { server } = useServer();
  const { adminRequest } = useAuth();
  const [items, setItems] = useState<WaitingItem[]>([]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      const [orders, returns, policy] = await Promise.all([
        adminRequest(() => server.getStoreOrders(userId, 'paid', null, null, null, COUNT_LIMIT)),
        adminRequest(() => server.getStoreReturns(userId, 'requested', COUNT_LIMIT)),
        adminRequest(() => server.getMyStorePolicy(userId)),
      ]);
      if (cancelled) return;
      const next: WaitingItem[] = [];
      if (orders.status === 200 && orders.data.orders.length > 0) {
        next.push({
          key: 'handover',
          dataComponent: 'adminHomeSuggestHandover',
          count: shownCount(orders.data.orders.length),
          label: orders.data.orders.length === 1 ? 'Order to hand over' : 'Orders to hand over',
          prompt: 'Show me the paid orders I still need to hand over.',
          tone: 'info',
        });
      }
      if (returns.status === 200 && returns.data.returns.length > 0) {
        next.push({
          key: 'returns',
          dataComponent: 'adminHomeSuggestReturns',
          count: shownCount(returns.data.returns.length),
          label: returns.data.returns.length === 1 ? 'Return request' : 'Return requests',
          prompt: 'Show me the return requests waiting for my decision.',
          tone: 'warning',
        });
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
