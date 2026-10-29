import Link from 'next/link';
import {
  CheckCircle2Icon,
  CheckIcon,
  ChevronDownIcon,
  MicIcon,
  PackageIcon,
  ImageIcon,
  ShoppingBagIcon,
  Undo2Icon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardPanel } from '@/components/ui/card';
import { Logo } from '@/components/brand/Logo';
import { GoogleIcon } from '@/components/brand/GoogleIcon';

// Fill these in before launch: the landing page never makes up a price.
const PLAN_NAME = '[PLAN NAME]';
const PLAN_PRICE = '[YOUR PRICE]';
const PRICING_ANSWER = '[YOUR PRICING ANSWER]';

const NAV = [
  { href: '#how', label: 'How it works' },
  { href: '#shoppers', label: 'For shoppers' },
  { href: '#owners', label: 'For you' },
  { href: '#pricing', label: 'Pricing' },
  { href: '#faq', label: 'FAQ' },
];

const STEPS = [
  { title: 'Sign in and tell it about your shop', body: 'Your name, what you sell, your address and hours. Pin your door on the map and paste your product list.' },
  { title: 'Share one link', body: 'Put your store link in your bio, your message replies and your flyers. That link is your whole shop.' },
  { title: 'Sell in the chat', body: 'Customers ask and buy in the chat. You get a short list of what to hand over, and what needs your yes or no.' },
];

const OWNER_FEATURES = [
  { icon: ShoppingBagIcon, title: 'Hand over orders', body: 'Ask what’s waiting, tick what’s done. Cancel and refund in the same place.' },
  { icon: PackageIcon, title: 'Add stock by typing a list', body: '“Barako 500g, 620, 24 pieces” becomes a product you check and save. Up to 50 at once.' },
  { icon: Undo2Icon, title: 'Returns, already checked', body: 'Each request arrives with your policy check: inside the window or not. Approve, reject, and refund when it’s back.' },
  { icon: ImageIcon, title: 'Product photos in your style', body: 'Snap a few phone photos and get styled shots in your store’s light and colors.' },
  { icon: MicIcon, title: 'Talk instead of type', body: 'Voice mode listens, answers out loud and shows the same buttons. Handy with flour on your hands.' },
];

const TRUST = [
  { title: 'Asks before it acts', body: 'Completing an order, refunding, changing a price: it shows you what it’s about to do and waits for your tap.' },
  { title: 'Uses only your answers', body: 'Answer your return, delivery and payment questions once. Leave one blank and customers are told to ask you.' },
  { title: 'Everything stays findable', body: 'Every chat is saved. Search them, reopen one, or start fresh with New chat.' },
];

const PLAN_FEATURES = [
  'Your own store link and shop chat',
  'Up to 2,000 products',
  'Up to 100 styled product photos a day',
  'GCash, Maya, GrabPay, card and bank transfer',
  'Voice mode for you and your customers',
];

const FAQ = [
  { q: 'Do my customers need to download anything?', a: 'No. They open your store link in their phone’s browser and chat. There’s no account. A text code confirms their number when they place an order.' },
  { q: 'What if a customer asks something I haven’t answered?', a: 'The assistant tells them the store hasn’t said and gives them your number. It never guesses a price, a stock count or a policy.' },
  { q: 'Does it change things on its own?', a: 'No. Before it completes an order, refunds, approves a return or changes a product, it shows you exactly what it will do and waits for you.' },
  { q: 'Can I still see all my orders at once?', a: 'Yes. Ask for today’s orders, last week’s, or only the unpaid ones, and you get a list you can act on right there.' },
  { q: 'How much does it cost?', a: PRICING_ANSWER },
];

function SectionHeading({ eyebrow, title, id, center = false }: { eyebrow: string; title: string; id: string; center?: boolean }) {
  return (
    <div className={center ? 'flex max-w-2xl flex-col items-center gap-3 self-center text-center' : 'flex max-w-2xl flex-col gap-3'}>
      <span className="font-medium text-muted-foreground text-sm">{eyebrow}</span>
      <h2 id={id} className="m-0 font-heading font-normal text-4xl leading-[1.1] sm:text-[44px]">{title}</h2>
    </div>
  );
}

export default function LandingPage() {
  return (
    <div data-component="landingPage" className="min-h-svh bg-background">
      <header className="border-b bg-background">
        <nav aria-label="Main" className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-6 py-3">
          <Link href="/" className="flex items-center gap-2.5 text-foreground no-underline">
            <Logo />
            <span className="font-heading text-lg">Star Commerce</span>
          </Link>
          <div className="flex flex-wrap items-center gap-1 text-sm">
            {NAV.map((item) => (
              <Button key={item.href} variant="ghost" size="sm" render={<a href={item.href} />}>{item.label}</Button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Button data-component="landingSignIn" variant="ghost" size="sm" render={<Link href="/admin" />}>Sign in</Button>
            <Button data-component="landingNavCta" size="sm" render={<Link href="/admin" />}>Open your store</Button>
          </div>
        </nav>
      </header>

      <main>
        <section aria-labelledby="heroTitle" className="drafting-grid border-b bg-sidebar">
          <div className="mx-auto flex max-w-6xl flex-col items-center gap-12 px-6 pt-22 pb-18">
            <div className="flex max-w-3xl flex-col items-center gap-5 text-center">
              <Badge variant="outline" size="lg" className="gap-2 rounded-full px-3">
                <span aria-hidden="true" className="size-2 rounded-full bg-teal-600" />
                Made for small shops
              </Badge>
              <h1 id="heroTitle" className="m-0 font-heading font-normal text-[clamp(44px,7vw,76px)] leading-[1.02] tracking-tight text-balance">
                Your store, as a chat.
              </h1>
              <p className="m-0 max-w-xl text-pretty text-lg text-muted-foreground leading-relaxed">
                Customers ask, choose and pay in one conversation, with no app and no account. You run the whole store the same way: type it, or just say it.
              </p>
              <div className="flex flex-wrap justify-center gap-2.5 pt-1">
                <Button data-component="landingHeroCta" size="xl" render={<Link href="/admin" />}>
                  <span className="flex size-5.5 items-center justify-center rounded-full bg-white"><GoogleIcon className="size-3.5" /></span>
                  Open your store with Google
                </Button>
                <Button variant="outline" size="xl" render={<a href="#how" />}>See how it works</Button>
              </div>
            </div>
            <HeroPreview />
          </div>
        </section>

        <section id="how" aria-labelledby="howTitle" className="mx-auto flex max-w-6xl flex-col gap-10 px-6 pt-24 pb-10">
          <SectionHeading eyebrow="How it works" title="Open a store in one conversation." id="howTitle" />
          <ol className="m-0 grid list-none grid-cols-1 gap-4 p-0 md:grid-cols-3">
            {STEPS.map((step, index) => (
              <li key={step.title}>
                <Card className="h-full">
                  <CardPanel className="flex flex-col gap-3 p-7">
                    <span className="font-heading text-4xl text-neutral-400 leading-none">{index + 1}</span>
                    <h3 className="m-0 font-semibold text-lg">{step.title}</h3>
                    <p className="m-0 text-muted-foreground leading-relaxed">{step.body}</p>
                  </CardPanel>
                </Card>
              </li>
            ))}
          </ol>
        </section>

        <section id="shoppers" aria-labelledby="shoppersTitle" className="mx-auto flex max-w-6xl flex-col gap-10 px-6 py-18">
          <SectionHeading eyebrow="For your customers" title="Buying feels like messaging the shop." id="shoppersTitle" />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <ShopperFeature title="No app, no account" body="They open your link and start typing. A one-time text code confirms their number when they order.">
              <div className="grid grid-cols-6 gap-1.5">
                {['4', '8', '1', '0', '', ''].map((digit, index) => (
                  <span key={index} className={`flex h-9 items-center justify-center rounded-md border font-medium ${index === 4 ? 'border-2 border-foreground' : ''}`}>{digit}</span>
                ))}
              </div>
            </ShopperFeature>
            <ShopperFeature title="Answers from your own rules" body="Stock, delivery areas, return windows: answered from what you’ve told it. Anything you haven’t, it sends to you.">
              <div className="flex flex-col gap-1.5 text-[13px] leading-[18px]">
                <span className="self-end rounded-xl bg-primary px-2.5 py-1.5 text-primary-foreground">Do you deliver to Marikina?</span>
                <span className="self-start rounded-xl bg-muted px-2.5 py-1.5">Yes, for ₱80. Free from ₱1,500.</span>
              </div>
            </ShopperFeature>
            <ShopperFeature title="Pay the way they already do" body="E-wallets, cards or bank transfer, right inside the chat. Unpaid orders let go of their items after 30 minutes.">
              <div className="flex flex-wrap gap-1.5 text-[13px]">
                {['GCash', 'Maya', 'GrabPay', 'Card', 'Bank transfer'].map((method, index) => (
                  <span key={method} className={`inline-flex h-7.5 items-center rounded-md px-2.5 ${index === 0 ? 'border-2 border-foreground font-medium' : 'border'}`}>{method}</span>
                ))}
              </div>
            </ShopperFeature>
            <ShopperFeature title="Drop a pin for delivery" body="When they pick delivery, a map shows up in the chat. They move the pin to their door and add a landmark.">
              <div aria-hidden="true" className="relative h-24 overflow-hidden rounded-lg bg-neutral-100 [background-image:linear-gradient(rgba(0,0,0,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(0,0,0,0.05)_1px,transparent_1px)] [background-size:24px_24px]">
                <span className="absolute inset-x-0 top-[48%] h-2.5 bg-white" />
                <span className="absolute inset-y-0 left-[30%] w-2 bg-white" />
                <span className="absolute top-[calc(50%-32px)] left-[calc(50%-13px)] flex size-6.5 -rotate-45 items-center justify-center rounded-[999px_999px_999px_0] bg-foreground">
                  <span className="size-2.5 rounded-full bg-white" />
                </span>
              </div>
            </ShopperFeature>
          </div>
        </section>

        <section id="owners" aria-labelledby="ownersTitle" className="border-y bg-sidebar">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-14 px-6 py-24">
            <div className="flex min-w-0 flex-[1_1_440px] flex-col gap-7">
              <SectionHeading eyebrow="For you" title="Run the store from one message box." id="ownersTitle" />
              <ul className="m-0 flex list-none flex-col gap-5 p-0">
                {OWNER_FEATURES.map((feature) => (
                  <li key={feature.title} className="flex gap-3.5">
                    <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-background shadow-xs/5">
                      <feature.icon className="size-4.5" />
                    </span>
                    <span className="flex flex-col gap-0.5">
                      <span className="font-semibold">{feature.title}</span>
                      <span className="text-muted-foreground leading-relaxed">{feature.body}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
            <VoicePreview />
          </div>
        </section>

        <section aria-labelledby="trustTitle" className="mx-auto flex max-w-6xl flex-col gap-10 px-6 pt-24 pb-18">
          <div className="flex max-w-2xl flex-col gap-3">
            <span className="font-medium text-muted-foreground text-sm">Your rules</span>
            <h2 id="trustTitle" className="m-0 font-heading font-normal text-4xl leading-[1.1] sm:text-[44px]">It never makes things up.</h2>
            <p className="m-0 text-lg text-muted-foreground leading-relaxed">The assistant works only from your products and your answers. When it doesn’t know, it says so.</p>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {TRUST.map((item) => (
              <div key={item.title} className="flex flex-col gap-2.5 rounded-2xl border p-6">
                <span className="flex items-center gap-2 font-semibold"><CheckCircle2Icon aria-hidden="true" className="size-4.5 text-success" />{item.title}</span>
                <span className="text-muted-foreground leading-relaxed">{item.body}</span>
              </div>
            ))}
          </div>
        </section>

        <section id="pricing" aria-labelledby="pricingTitle" className="mx-auto flex max-w-6xl flex-col items-center gap-8 px-6 pt-12 pb-18">
          <SectionHeading eyebrow="Pricing" title="One plan. Everything in it." id="pricingTitle" center />
          <div className="w-full max-w-md rounded-[22px] bg-muted/72 p-1">
            <Card>
              <CardPanel className="flex flex-col gap-6 p-8">
                <div className="flex flex-col gap-1.5">
                  <span className="font-semibold">{PLAN_NAME}</span>
                  <span className="flex items-baseline gap-1.5">
                    <span className="font-heading text-[44px] leading-none">{PLAN_PRICE}</span>
                    <span className="text-muted-foreground">/ month</span>
                  </span>
                </div>
                <ul className="m-0 flex list-none flex-col gap-3 p-0">
                  {PLAN_FEATURES.map((feature) => (
                    <li key={feature} className="flex gap-2.5"><CheckIcon aria-hidden="true" className="mt-0.5 size-4.5 shrink-0" />{feature}</li>
                  ))}
                </ul>
                <Button data-component="landingPricingCta" size="xl" render={<Link href="/admin" />}>Open your store</Button>
              </CardPanel>
            </Card>
          </div>
        </section>

        <section id="faq" aria-labelledby="faqTitle" className="mx-auto flex max-w-3xl flex-col gap-7 px-6 pt-12 pb-24">
          <h2 id="faqTitle" className="m-0 text-center font-heading font-normal text-4xl leading-[1.1] sm:text-[44px]">Questions</h2>
          <div className="flex flex-col rounded-2xl border">
            {FAQ.map((item, index) => (
              <details key={item.q} open={index === 0} className="group border-b last:border-b-0">
                <summary data-component="landingFaqQuestion" className="flex min-h-15 cursor-pointer list-none items-center justify-between gap-4 px-5 font-medium [&::-webkit-details-marker]:hidden">
                  {item.q}
                  <ChevronDownIcon aria-hidden="true" className="size-4.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
                </summary>
                <p className="m-0 px-5 pb-5 text-muted-foreground leading-relaxed">{item.a}</p>
              </details>
            ))}
          </div>
        </section>

        <section aria-labelledby="ctaTitle" className="px-4 pb-4">
          <div className="mx-auto flex max-w-6xl flex-col items-center gap-6 rounded-[28px] bg-primary px-6 py-20 text-center text-primary-foreground">
            <h2 id="ctaTitle" className="m-0 max-w-2xl text-balance font-heading font-normal text-[clamp(34px,5vw,54px)] leading-[1.08]">Open your store today. Sell by tonight.</h2>
            <p className="m-0 max-w-lg text-lg text-neutral-300 leading-relaxed">Sign in with Google, chat your store into shape, and share your link.</p>
            <Button data-component="landingFooterCta" size="xl" variant="outline" render={<Link href="/admin" />}>Open your store with Google</Button>
          </div>
        </section>
      </main>

      <footer className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-6 pt-8 pb-10 text-muted-foreground text-sm">
        <span className="flex items-center gap-2"><Logo size="sm" />© Star Commerce</span>
        <span className="flex flex-wrap gap-5">
          <a href="#top" className="text-muted-foreground no-underline">[Terms]</a>
          <a href="#top" className="text-muted-foreground no-underline">[Privacy]</a>
          <Link href="/admin" className="text-muted-foreground no-underline">Sign in</Link>
        </span>
      </footer>
    </div>
  );
}

function ShopperFeature({ title, body, children }: { title: string; body: string; children: React.ReactNode }) {
  return (
    <article className="flex flex-col gap-4 rounded-2xl border bg-sidebar p-6">
      <div aria-hidden="true" className="rounded-xl border bg-background p-3.5">{children}</div>
      <h3 className="m-0 font-semibold text-lg">{title}</h3>
      <p className="m-0 text-muted-foreground leading-relaxed">{body}</p>
    </article>
  );
}

function HeroPreview() {
  return (
    <div
      role="img"
      aria-label="The store owner's chat listing orders to hand over, next to a customer paying for an order in the shop chat on a phone"
      className="flex w-full flex-wrap items-end justify-center gap-6"
    >
      <div aria-hidden="true" className="flex min-w-0 max-w-3xl flex-[1_1_560px] overflow-hidden rounded-[20px] border bg-background text-[13px] leading-[18px] shadow-[0_24px_64px_rgba(0,0,0,0.08)]">
        <div className="flex w-42 shrink-0 flex-col gap-1.5 border-e bg-sidebar p-3 max-sm:hidden">
          <span className="flex items-center gap-2 p-1"><span className="size-5 rounded-md bg-orange-900" /><span className="font-medium">Kalye Coffee</span></span>
          <span className="flex h-7 items-center rounded-md border bg-background px-2 font-medium">+ New chat</span>
          <span className="px-2 pt-2 font-medium text-[11px] text-muted-foreground">Today</span>
          <span className="truncate rounded-md bg-accent px-2 py-1.5 font-medium">Hand over today’s orders</span>
          <span className="truncate px-2 py-1.5">Return for the dripper</span>
          <span className="truncate px-2 py-1.5">Add the new products</span>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-3 p-5">
          <span className="self-end rounded-2xl bg-muted px-3 py-2">What needs handing over?</span>
          <span>5 paid orders are waiting. Tick the ones you’ve handed over.</span>
          <div className="flex flex-col overflow-hidden rounded-xl border">
            {[
              { id: '#4F1A09', items: 'Barako Blend 500g × 2', total: '₱1,240', checked: true },
              { id: '#4F1B3C', items: 'Drip Bags, Cold Brew Pack', total: '₱740', checked: true },
              { id: '#4F1C77', items: 'Sagada Medium Roast 250g', total: '₱520', checked: false },
            ].map((row) => (
              <span key={row.id} className={`flex items-center gap-2.5 border-b px-3 py-2.5 ${row.checked ? 'bg-accent' : ''}`}>
                <span className={`flex size-4 items-center justify-center rounded-sm ${row.checked ? 'bg-primary text-primary-foreground' : 'border'}`}>
                  {row.checked && <CheckIcon className="size-2.5" />}
                </span>
                <span className="flex-1 truncate"><b className="font-medium">{row.id}</b> · {row.items}</span>
                <span className="font-medium">{row.total}</span>
              </span>
            ))}
            <span className="flex justify-end bg-muted/40 px-3 py-2.5">
              <span className="inline-flex h-7 items-center rounded-md bg-primary px-2.5 font-medium text-primary-foreground">Mark 2 handed over</span>
            </span>
          </div>
        </div>
      </div>

      <div aria-hidden="true" className="flex w-72 shrink-0 flex-col gap-2.5 rounded-[36px] border bg-background px-3 pt-3.5 pb-4 text-[13px] leading-[18px] shadow-[0_24px_64px_rgba(0,0,0,0.12),0_0_0_6px_#f1f1f1]">
        <span className="h-1.25 w-16 self-center rounded-full bg-neutral-200" />
        <span className="self-center font-medium">Kalye Coffee</span>
        <span className="max-w-[86%] self-end rounded-[14px] rounded-br-sm bg-primary px-3 py-2 text-primary-foreground">2 bags of the Sagada please</span>
        <span>Order placed. Pay within 30 minutes.</span>
        <div className="flex flex-col gap-1.5 rounded-2xl border p-2.5">
          <span className="flex items-center justify-between">
            <span className="font-heading text-lg">₱1,120</span>
            <span className="rounded-md bg-warning/10 px-1.5 font-medium text-[11px] text-warning-foreground">28:41 left</span>
          </span>
          <span className="flex h-9 items-center justify-between rounded-lg border-2 border-primary px-2.5 font-medium">GCash<span className="size-3.5 rounded-full bg-primary" /></span>
          <span className="flex h-9 items-center justify-between rounded-lg border px-2.5">Maya<span className="size-3.5 rounded-full border" /></span>
          <span className="flex h-9.5 items-center justify-center rounded-lg bg-primary font-medium text-primary-foreground">Pay ₱1,120 with GCash</span>
        </div>
        <span className="flex h-9.5 items-center gap-2 rounded-full border ps-3 pe-1.5 text-muted-foreground">Ask Kalye Coffee<span className="ms-auto size-7 rounded-full bg-primary" /></span>
      </div>
    </div>
  );
}

function VoicePreview() {
  return (
    <div
      role="img"
      aria-label="Voice mode: the owner asks how many orders are waiting and the assistant answers out loud"
      className="relative flex min-h-110 min-w-0 flex-[1_1_420px] flex-col justify-end gap-4 overflow-hidden rounded-3xl border bg-background p-7 shadow-[inset_0_0_28px_2px_rgba(13,148,136,0.32),inset_0_0_120px_16px_rgba(59,130,246,0.14),0_24px_64px_rgba(0,0,0,0.06)]"
    >
      <span aria-hidden="true" className="text-muted-foreground text-sm">“How many orders are waiting?”</span>
      <span aria-hidden="true" className="font-heading text-[28px] leading-tight">5 paid orders are waiting. The oldest was paid at 9:12 this morning.</span>
      <div aria-hidden="true" className="flex items-center gap-3.5 rounded-full border bg-background py-2.5 ps-2.5 pe-3 shadow-[0_12px_32px_rgba(0,0,0,0.08)]">
        <span className="relative flex size-14 shrink-0 items-center justify-center">
          <span className="absolute inset-0 rounded-full bg-teal-600/10" />
          <span className="relative flex size-10 items-center justify-center gap-0.75 rounded-full bg-[radial-gradient(circle_at_34%_28%,#ffffff_0%,rgba(59,130,246,0.8)_34%,#0D9488_82%)]">
            <span className="h-3 w-0.75 rounded-full bg-white/90" />
            <span className="h-4.5 w-0.75 rounded-full bg-white/90" />
            <span className="h-2.5 w-0.75 rounded-full bg-white/90" />
          </span>
        </span>
        <span className="flex flex-col gap-0.5"><span className="text-muted-foreground text-xs">Speaking</span><span className="font-medium">Tap to ask a follow-up</span></span>
      </div>
    </div>
  );
}
