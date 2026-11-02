import type { DeliveryZoneRule } from '@/app/schemas/DeliveryZoneRuleSchema';
import type { PolicyCard } from '@/app/schemas/PolicyCardSchema';
import type { StorePolicyResponse } from '@/app/schemas/StorePolicyResponseSchema';
import { POLICY_OPTIONAL_QUESTIONS, POLICY_RETURN_QUESTIONS, POLICY_ZONES_QUESTION } from '@/lib/generated/serverConstants';

// A store's policy answers (the policy card's, the policy route's):
// authorizedPaymentMethods is absent on a policy card saved before each
// payment method had its details, deliveryMethod on one saved before it.
export type PolicyAnswers = Omit<StorePolicyResponse, 'updatedAt' | 'deliveryZones' | 'authorizedPaymentMethods' | 'deliveryMethod'> & {
  authorizedPaymentMethods?: StorePolicyResponse['authorizedPaymentMethods'];
  deliveryMethod?: StorePolicyResponse['deliveryMethod'];
};
export type PolicyQuestion = keyof PolicyAnswers;
// A required question the server counts on the policy card: an answer, or the
// delivery zones.
export type UnansweredQuestion = NonNullable<PolicyCard['unansweredQuestions']>[number];

// The answers with the store's delivery zones beside them (null or absent on
// a policy card saved before zones had policies), and the required questions
// the server counted as not answered (absent or null on a card saved before
// it sent them).
export type PolicyWithZones = PolicyAnswers & {
  deliveryZones?: readonly DeliveryZoneRule[] | null;
  unansweredQuestions?: readonly UnansweredQuestion[] | null;
};

// Every policy question.
export const POLICY_QUESTIONS: PolicyQuestion[] = [
  'authorizedPaymentMethods', 'paidOrderCancellation', 'paidOrderCancellationHours', 'deliveryOptions', 'deliveryMethod',
  'deliveryAreas', 'handoverDays', 'acceptsReturns', 'returnWindowDays', 'returnConditions', 'nonReturnableItems',
  'refundMethod', 'returnShippingPaidBy', 'defectReportDays', 'additionalTerms',
];
const RETURN_QUESTIONS: readonly string[] = POLICY_RETURN_QUESTIONS;
const DELIVERY_QUESTIONS: PolicyQuestion[] = ['deliveryMethod', 'deliveryAreas'];
// Questions the admin may skip: never counted as not answered.
const OPTIONAL_QUESTIONS: readonly string[] = POLICY_OPTIONAL_QUESTIONS;

export function offersDelivery(answers: PolicyAnswers): boolean {
  return answers.deliveryOptions?.includes('delivery') ?? false;
}

// Whether `question` still applies to the store's answers, as the server's
// objects/policy.py policyQuestionApplies says: the return questions only
// while returns are accepted, the hours to cancel a paid order only with
// withinHours, the days until a pickup is ready only while pickup is offered
// (or not said yet), and the delivery answers only while delivery is offered.
export function policyQuestionApplies(question: PolicyQuestion, answers: PolicyAnswers): boolean {
  if (RETURN_QUESTIONS.includes(question)) return answers.acceptsReturns === true;
  if (question === 'paidOrderCancellationHours') return answers.paidOrderCancellation === 'withinHours';
  if (question === 'handoverDays') return answers.deliveryOptions === null || answers.deliveryOptions.includes('pickup');
  if (DELIVERY_QUESTIONS.includes(question)) return offersDelivery(answers);
  return true;
}

// Whether the store delivers but has no delivery zone yet, so it takes no
// delivery order: the server's count when the card has it, else from the
// zones (unknown on a card saved before zones had policies).
export function missingDeliveryZones(policy: PolicyWithZones): boolean {
  if (policy.unansweredQuestions) return policy.unansweredQuestions.includes(POLICY_ZONES_QUESTION);
  return offersDelivery(policy) && Array.isArray(policy.deliveryZones) && policy.deliveryZones.length === 0;
}

// How many required policy questions the admin has not answered yet: the
// policy card's unansweredQuestions (the server's count, the one source of
// truth), or, for the policy route and a card saved before the server sent
// them, the same rules worked out here (the server's sharedConstants feed
// them; test_policies.py pins both to agree), counting a store that delivers
// without a zone as one.
export function unansweredPolicyQuestions(policy: PolicyWithZones): number {
  if (policy.unansweredQuestions) return policy.unansweredQuestions.length;
  const open = POLICY_QUESTIONS.filter((question) => {
    if (OPTIONAL_QUESTIONS.includes(question) || !policyQuestionApplies(question, policy)) return false;
    // Absent (a card older than the question) is unknown, never counted.
    const answer = policy[question];
    return answer === null || (Array.isArray(answer) && answer.length === 0);
  }).length;
  return open + (missingDeliveryZones(policy) ? 1 : 0);
}
