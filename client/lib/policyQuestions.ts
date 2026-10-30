import type { DeliveryZoneRule } from '@/app/schemas/DeliveryZoneRuleSchema';
import type { StorePolicyResponse } from '@/app/schemas/StorePolicyResponseSchema';

// A store's policy answers (the policy card's, the policy route's):
// authorizedPaymentMethods is absent on a policy card saved before each
// payment method had its details.
export type PolicyAnswers = Omit<StorePolicyResponse, 'updatedAt' | 'deliveryZones' | 'authorizedPaymentMethods'> & {
  authorizedPaymentMethods?: StorePolicyResponse['authorizedPaymentMethods'];
};
export type PolicyQuestion = keyof PolicyAnswers;

// The answers with the store's delivery zones beside them: null or absent on
// a policy card saved before zones had policies.
export type PolicyWithZones = PolicyAnswers & { deliveryZones?: readonly DeliveryZoneRule[] | null };

// Every policy question.
export const POLICY_QUESTIONS: PolicyQuestion[] = [
  'authorizedPaymentMethods', 'paidOrderCancellation', 'paidOrderCancellationHours', 'deliveryOptions', 'deliveryAreas',
  'handoverDays', 'acceptsReturns', 'returnWindowDays', 'returnConditions', 'nonReturnableItems',
  'refundMethod', 'returnShippingPaidBy', 'defectReportDays', 'additionalTerms',
];
const RETURN_QUESTIONS: PolicyQuestion[] = ['returnWindowDays', 'returnConditions', 'refundMethod', 'returnShippingPaidBy'];
const DELIVERY_QUESTIONS: PolicyQuestion[] = ['deliveryAreas'];

export function offersDelivery(answers: PolicyAnswers): boolean {
  return answers.deliveryOptions?.includes('delivery') ?? false;
}

// Whether `question` still applies to the store's answers: an earlier "no"
// (no returns, no delivery) drops the questions that follow it, as the
// server's Policy.normalized clears them, and the hours to cancel a paid order
// apply only to withinHours.
export function policyQuestionApplies(question: PolicyQuestion, answers: PolicyAnswers): boolean {
  if (question === 'paidOrderCancellationHours') return answers.paidOrderCancellation === 'withinHours';
  if (RETURN_QUESTIONS.includes(question) && answers.acceptsReturns === false) return false;
  return !(DELIVERY_QUESTIONS.includes(question) && answers.deliveryOptions !== null && !offersDelivery(answers));
}

// Whether the store delivers but has no delivery zone yet, so it takes no
// delivery order (unknown on a card saved before zones had policies).
export function missingDeliveryZones(policy: PolicyWithZones): boolean {
  return offersDelivery(policy) && Array.isArray(policy.deliveryZones) && policy.deliveryZones.length === 0;
}

// The policy questions the admin has not answered yet, of those that apply,
// counting a store that delivers without a zone as one.
export function unansweredPolicyQuestions(policy: PolicyWithZones): number {
  const open = POLICY_QUESTIONS.filter((question) => policy[question] === null && policyQuestionApplies(question, policy)).length;
  return open + (missingDeliveryZones(policy) ? 1 : 0);
}
