'use client';
import { CheckoutCard } from './CheckoutCard';
import { MyOrdersCard, StoreOrdersCard } from './OrderCards';
import { MyReturnsCard, StoreReturnsCard } from './ReturnCards';
import { FulfillmentForm } from './FulfillmentForm';
import { PhotosCard, PolicyCard, StoreCard, StoreProductsCard } from './InfoCards';
import { LocationForm } from './LocationForm';
import { NewProductsForm } from './NewProductsForm';
import { PhoneVerificationForm } from './PhoneVerificationForm';
import { PhotoRequestForm } from './PhotoRequestForm';
import { PolicyForm } from './PolicyForm';
import { ReturnForm } from './ReturnForm';
import { ShopProductsCard } from './ShopProductsCard';
import { StoreDetailsForm } from './StoreDetailsForm';
import type { ChatComponent, ChatFormAnswer } from './types';

interface ChatComponentsProps {
  components: ChatComponent[];
  answeredFormIds: ReadonlySet<string>;
  busy: boolean;
  onAnswer: (answer: ChatFormAnswer) => void;
}

function formIdOf(component: ChatComponent): string | null {
  return 'formId' in component ? component.formId : null;
}

// The cards and forms under one assistant reply.
export function ChatComponents({ components, answeredFormIds, busy, onAnswer }: ChatComponentsProps) {
  if (components.length === 0) return null;
  return (
    <div data-component="chatComponents" className="flex flex-col gap-3">
      {components.map((component, index) => {
        const formId = formIdOf(component);
        const shared = { answered: formId !== null && answeredFormIds.has(formId), busy, onAnswer };
        const key = formId ?? `${component.kind}-${index}`;
        switch (component.kind) {
          case 'shopProducts': return <ShopProductsCard key={key} component={component} {...shared} />;
          case 'storeProducts': return <StoreProductsCard key={key} component={component} {...shared} />;
          case 'storeOrders': return <StoreOrdersCard key={key} component={component} {...shared} />;
          case 'myOrders': return <MyOrdersCard key={key} component={component} {...shared} />;
          case 'checkout': return <CheckoutCard key={key} component={component} {...shared} />;
          case 'storeReturns': return <StoreReturnsCard key={key} component={component} {...shared} />;
          case 'myReturns': return <MyReturnsCard key={key} component={component} {...shared} />;
          case 'policy': return <PolicyCard key={key} component={component} {...shared} />;
          case 'photos': return <PhotosCard key={key} component={component} {...shared} />;
          case 'store': return <StoreCard key={key} component={component} {...shared} />;
          case 'storeDetailsForm': return <StoreDetailsForm key={key} component={component} {...shared} />;
          case 'locationForm': return <LocationForm key={key} component={component} {...shared} />;
          case 'newProductsForm': return <NewProductsForm key={key} component={component} {...shared} />;
          case 'policyForm': return <PolicyForm key={key} component={component} {...shared} />;
          case 'returnForm': return <ReturnForm key={key} component={component} {...shared} />;
          case 'phoneVerificationForm': return <PhoneVerificationForm key={key} component={component} {...shared} />;
          case 'photoRequestForm': return <PhotoRequestForm key={key} component={component} {...shared} />;
          case 'fulfillmentForm': return <FulfillmentForm key={key} component={component} {...shared} />;
          default: return null;
        }
      })}
    </div>
  );
}
