import type { StorePolicyResponse } from '@/app/schemas/StorePolicyResponseSchema';

// A store's policy answers (the policy card's, the policy route's).
export type PolicyAnswers = Omit<StorePolicyResponse, 'updatedAt' | 'deliveryFeePerKm'>;
export type PolicyQuestion = keyof PolicyAnswers;

// Every policy question.
export const POLICY_QUESTIONS: PolicyQuestion[] = [
  'acceptedPaymentMethods', 'paidOrderCancellation', 'paidOrderCancellationHours', 'deliveryOptions', 'deliveryAreas',
  'offersFreeDelivery', 'freeDeliveryMinimum', 'handoverDays', 'acceptsReturns', 'returnWindowDays', 'returnConditions', 'nonReturnableItems',
  'refundMethod', 'returnShippingPaidBy', 'defectReportDays', 'additionalTerms',
];
const RETURN_QUESTIONS: PolicyQuestion[] = ['returnWindowDays', 'returnConditions', 'refundMethod', 'returnShippingPaidBy'];
const DELIVERY_QUESTIONS: PolicyQuestion[] = ['deliveryAreas', 'offersFreeDelivery', 'freeDeliveryMinimum'];

// Whether `question` still applies to the store's answers: an earlier "no"
// (no returns, no delivery, no free delivery) drops the questions that follow
// it, as the server's Policy.normalized clears them, and the hours to cancel a
// paid order apply only to withinHours.
export function policyQuestionApplies(question: PolicyQuestion, answers: PolicyAnswers): boolean {
  if (question === 'paidOrderCancellationHours') return answers.paidOrderCancellation === 'withinHours';
  if (RETURN_QUESTIONS.includes(question) && answers.acceptsReturns === false) return false;
  if (DELIVERY_QUESTIONS.includes(question) && answers.deliveryOptions !== null && !answers.deliveryOptions.includes('delivery')) {
    return false;
  }
  return !(question === 'freeDeliveryMinimum' && answers.offersFreeDelivery === false);
}

// The policy questions the admin has not answered yet, of those that apply.
export function unansweredPolicyQuestions(answers: PolicyAnswers): number {
  return POLICY_QUESTIONS.filter((question) => answers[question] === null && policyQuestionApplies(question, answers)).length;
}
