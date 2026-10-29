'use client';
import { useState } from 'react';
import { Undo2Icon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ChatCard, SentNote } from './ChatCard';
import {
  RETURN_REASON_LABELS, RETURN_STATUS_LABELS, formatMoney, formatWhen, itemsText, orderRef,
} from './format';
import type { ComponentProps } from './types';

// The server's limit on a reject reason (RETURN_REJECT_REASON_MAX_LENGTH).
const REJECT_REASON_MAX = 300;

type BadgeVariant = 'info' | 'success' | 'warning' | 'error' | 'secondary';

const STATUS_VARIANTS: Record<string, BadgeVariant> = {
  requested: 'warning',
  approved: 'info',
  rejected: 'error',
  received: 'success',
  cancelled: 'secondary',
};

function StatusBadge({ status }: { status: string }) {
  return <Badge variant={STATUS_VARIANTS[status] ?? 'secondary'}>{RETURN_STATUS_LABELS[status] ?? status}</Badge>;
}

// The store's returns: approve or reject a requested one, and the assistant
// does it.
export function StoreReturnsCard({ component, answered, busy, onAnswer }: ComponentProps<'storeReturns'>) {
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const { formId } = component;
  const canDecide = formId !== null && !answered;

  return (
    <ChatCard
      dataComponent="storeReturnsCard"
      icon={<Undo2Icon />}
      title={component.returns.length === 1 ? '1 return' : `${component.returns.length} returns`}
      footer={answered ? <SentNote text="Decision sent" /> : undefined}
    >
      <ul className="divide-y">
        {component.returns.map((request) => (
          <li key={request._id} className="flex flex-col gap-1.5 px-4 py-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs">{orderRef(request.orderId)}</span>
              <StatusBadge status={request.status} />
              <span className="ms-auto font-medium text-sm">{formatMoney(request.refundAmount, request.currency)}</span>
            </div>
            <span className="text-sm">{itemsText(request.items)}</span>
            <span className="text-muted-foreground text-xs">
              {RETURN_REASON_LABELS[request.reason] ?? request.reason} · requested {formatWhen(request.createdAt)}
              {request.restocked !== null && ` · ${request.restocked ? 'restocked' : 'not restocked'}`}
            </span>
            {request.note && <p className="rounded-md bg-muted px-2.5 py-1.5 text-sm">“{request.note}”</p>}
            {request.rejectReason && (
              <p className="text-muted-foreground text-xs">Rejected: {request.rejectReason}</p>
            )}
            <a className="text-muted-foreground text-xs underline-offset-2 hover:underline" href={`tel:${request.contactNumber}`}>
              {request.contactNumber}
            </a>
            {canDecide && request.status === 'requested' && (
              rejecting === request._id ? (
                <div className="mt-1 flex flex-col gap-2">
                  <label htmlFor={`reject-${request._id}`} className="font-medium text-sm">Why? The customer sees this.</label>
                  <Textarea
                    id={`reject-${request._id}`}
                    data-component="storeReturnsRejectReason"
                    value={reason}
                    maxLength={REJECT_REASON_MAX}
                    onChange={(event) => setReason(event.target.value)}
                  />
                  <div className="flex justify-end gap-2">
                    <Button data-component="storeReturnsRejectCancel" variant="ghost" size="sm" onClick={() => setRejecting(null)}>
                      Back
                    </Button>
                    <Button
                      data-component="storeReturnsRejectSend"
                      variant="destructive"
                      size="sm"
                      disabled={reason.trim() === '' || busy}
                      onClick={() => onAnswer({
                        kind: 'returnDecision', formId, returnId: request._id, decision: 'reject', reason: reason.trim(),
                      })}
                    >
                      Reject
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="mt-1 flex justify-end gap-2">
                  <Button
                    data-component="storeReturnsReject"
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => { setRejecting(request._id); setReason(''); }}
                  >
                    Reject
                  </Button>
                  <Button
                    data-component="storeReturnsApprove"
                    size="sm"
                    disabled={busy}
                    onClick={() => onAnswer({ kind: 'returnDecision', formId, returnId: request._id, decision: 'approve', reason: null })}
                  >
                    Approve
                  </Button>
                </div>
              )
            )}
          </li>
        ))}
      </ul>
    </ChatCard>
  );
}

// The shopper's own returns.
export function MyReturnsCard({ component }: ComponentProps<'myReturns'>) {
  return (
    <ChatCard
      dataComponent="myReturnsCard"
      icon={<Undo2Icon />}
      title={component.returns.length === 1 ? 'Your return' : `${component.returns.length} returns`}
    >
      <ul className="divide-y">
        {component.returns.map((request) => (
          <li key={request._id} className="flex flex-col gap-1 px-4 py-3">
            <div className="flex items-center gap-2">
              <StatusBadge status={request.status} />
              <span className="ms-auto font-medium text-sm">{formatMoney(request.refundAmount, request.currency)}</span>
            </div>
            <span className="text-sm">{itemsText(request.items)}</span>
            <span className="text-muted-foreground text-xs">
              {RETURN_REASON_LABELS[request.reason] ?? request.reason} · requested {formatWhen(request.createdAt)}
              {request.refundedAt && ` · refunded ${formatWhen(request.refundedAt)}`}
            </span>
            {request.rejectReason && <p className="text-muted-foreground text-xs">The store said: {request.rejectReason}</p>}
          </li>
        ))}
      </ul>
    </ChatCard>
  );
}
