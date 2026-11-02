import Link from 'next/link';
import Image from 'next/image';
import {
  ArrowRightIcon,
  BarChart3Icon,
  BellRingIcon,
  CheckCircle2Icon,
  CheckIcon,
  ChevronDownIcon,
  ListPlusIcon,
  MapIcon,
  MicIcon,
  Undo2Icon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardPanel } from '@/components/ui/card';
import { Logo } from '@/components/brand/Logo';
import { GoogleIcon } from '@/components/brand/GoogleIcon';
// Product photos and chat cards of a sample store (Kalye Coffee). The cards
// are screenshots of the real chat cards: scripts/landingShots.mjs.
import photoOriginal from '@/public/landing/original.webp';
import photoCatalog from '@/public/landing/catalog.webp';
import photoInUse from '@/public/landing/inuse.webp';
import photoHero from '@/public/landing/hero.webp';
import groupPhoto from '@/public/landing/group.webp';
import logoPhoto from '@/public/landing/logo.webp';
import cardPhotoChoice from '@/public/landing/shotPhotoChoice.png';
import cardStoreProducts from '@/public/landing/shotStoreProducts.png';
import cardPhotoColors from '@/public/landing/shotPhotoColors.png';
import cardCheckoutSummary from '@/public/landing/shotCheckoutSummary.png';

const CTA_HREF = '/admin';

const NAV = [
  { href: '#photos', label: 'Photos' },
  { href: '#features', label: 'Features' },
  { href: '#shoppers', label: 'For shoppers' },
  { href: '#faq', label: 'FAQ' },
];

const PHOTO_SHOTS = [
  { src: photoCatalog, label: 'Catalog', body: 'Clean and bright, for your product list.' },
  { src: photoInUse, label: 'In use', body: 'Your product where it’s used.' },
  { src: photoHero, label: 'Hero', body: 'Bold, in your brand colors, for your banner.' },
];

const STEPS = [
  { title: 'Sign in and chat your store into shape', body: 'Your name, what you sell, your colors and your hours. Pin your door on the map and send a photo of your products.' },
  { title: 'Share one link', body: 'Put your store link in your bio, your message replies and your flyers. That link is your whole shop.' },
  { title: 'Sell in the chat', body: 'Customers ask and buy in the chat. You get a short list of what to hand over, and what needs your yes or no.' },
];

const TRUST = [
  { title: 'Asks before it acts', body: 'Completing an order, refunding, changing a price: it shows you what it’s about to do and waits for your tap.' },
  { title: 'Uses only your answers', body: 'Answer your return, delivery and payment questions once. Leave one blank and customers are told to ask you.' },
  { title: 'Everything stays findable', body: 'Every chat is saved. Search them, reopen one, or start fresh with New chat.' },
];

const FAQ = [
  { q: 'Do my customers need to download anything?', a: 'No. They open your store link in their phone’s browser and chat. There’s no account. A text code confirms their number when they place an order.' },
  { q: 'How do the product photos work?', a: 'Send a phone photo of your product. You get 3 new photos of it, made in your brand colors: a clean catalog shot, one in use and a bold hero shot. You pick the one your shop shows, or keep your own.' },
  { q: 'How do my customers pay?', a: 'Cash, e-wallets such as GCash and Maya, or bank transfer, to your own accounts. The customer can send the reference and a photo of the receipt, and you confirm the payment in the chat.' },
  { q: 'What if a customer asks something I haven’t answered?', a: 'The assistant tells them the store hasn’t said and gives them your number. It never guesses a price, a stock count or a policy.' },
  { q: 'Does it change things on its own?', a: 'No. Before it completes an order, refunds, approves a return or changes a product, it shows you exactly what it will do and waits for you.' },
  { q: 'How much does it cost?', a: 'It’s free while we’re in demo. Every store gets every feature, with no card needed.' },
];

function SectionHeading({ eyebrow, title, id, body, center = false }: { eyebrow: string; title: string; id: string; body?: string; center?: boolean }) {
  return (
    <div className={center ? 'flex max-w-2xl flex-col items-center gap-3 self-center text-center' : 'flex max-w-2xl flex-col gap-3'}>
      <span className="font-medium text-muted-foreground text-sm">{eyebrow}</span>
      <h2 id={id} className="m-0 text-balance font-heading font-normal text-4xl leading-[1.1] sm:text-[44px]">{title}</h2>
      {body && <p className="m-0 text-pretty text-lg text-muted-foreground leading-relaxed">{body}</p>}
    </div>
  );
}

function CtaButton({ dataComponent, children = 'Start free with Google', variant = 'default' }: { dataComponent: string; children?: React.ReactNode; variant?: 'default' | 'outline' }) {
  return (
    <Button data-component={dataComponent} size="xl" variant={variant} render={<Link href={CTA_HREF} />}>
      <span className="flex size-5.5 items-center justify-center rounded-full bg-white"><GoogleIcon className="size-3.5" /></span>
      {children}
    </Button>
  );
}

export default function LandingPage() {
  return (
    <div data-component="landingPage" className="min-h-svh bg-background pb-20 sm:pb-0">
      <header className="sticky top-0 z-20 border-b bg-background/90 backdrop-blur">
        <nav aria-label="Main" className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <Link href="/" className="flex items-center gap-2.5 text-foreground no-underline">
            <Logo />
            <span className="font-heading text-lg">Star Commerce</span>
          </Link>
          <div className="hidden items-center gap-1 text-sm md:flex">
            {NAV.map((item) => (
              <Button key={item.href} variant="ghost" size="sm" render={<a href={item.href} />}>{item.label}</Button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Button data-component="landingSignIn" variant="ghost" size="sm" render={<Link href={CTA_HREF} />}>Sign in</Button>
            <Button data-component="landingNavCta" size="sm" render={<Link href={CTA_HREF} />}>Start free</Button>
          </div>
        </nav>
      </header>

      <main>
        <section aria-labelledby="heroTitle" className="drafting-grid border-b bg-sidebar">
          <div className="mx-auto flex max-w-6xl flex-col items-center gap-12 px-4 pt-16 pb-16 sm:px-6 sm:pt-22 sm:pb-18">
            <div className="flex max-w-3xl flex-col items-center gap-5 text-center">
              <Badge variant="outline" size="lg" className="gap-2 rounded-full px-3">
                <span aria-hidden="true" className="size-2 rounded-full bg-teal-600" />
                Made for small shops
              </Badge>
              <h1 id="heroTitle" className="m-0 text-balance font-heading font-normal text-[clamp(44px,7vw,76px)] leading-[1.02] tracking-tight">
                Your store, as a chat.
              </h1>
              <p className="m-0 max-w-xl text-pretty text-lg text-muted-foreground leading-relaxed">
                Customers ask, choose and pay in one conversation, with no app and no account. You run the whole store the same way, and it even takes your product photos.
              </p>
              <div className="flex flex-wrap justify-center gap-2.5 pt-1">
                <CtaButton dataComponent="landingHeroCta" />
                <Button data-component="landingHeroSecondary" variant="outline" size="xl" render={<a href="#photos" />}>See what it makes</Button>
              </div>
              <ul aria-label="What you get" className="m-0 flex list-none flex-wrap justify-center gap-x-5 gap-y-1.5 p-0 text-muted-foreground text-sm">
                {['Free demo, no card', 'Set up in one chat', 'No app for your customers'].map((point) => (
                  <li key={point} className="flex items-center gap-1.5"><CheckIcon aria-hidden="true" className="size-4 text-teal-600" />{point}</li>
                ))}
              </ul>
            </div>
            <HeroPreview />
          </div>
        </section>

        <section id="photos" aria-labelledby="photosTitle" className="mx-auto flex max-w-6xl flex-col gap-10 px-4 pt-20 pb-10 sm:px-6 sm:pt-24">
          <SectionHeading
            eyebrow="Product photos"
            title="From a phone snap to a shop photo."
            body="Send one photo from your phone. You get 3 new photos of the same product, in your brand colors, and you pick the one your shop shows."
            id="photosTitle"
          />
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            <figure className="m-0 flex flex-col gap-2.5">
              <div className="relative overflow-hidden rounded-2xl border">
                <Image src={photoOriginal} alt="A phone photo of a coffee bag on a kitchen counter" sizes="(min-width: 1024px) 270px, 45vw" placeholder="blur" className="h-auto w-full" />
                <span className="absolute top-2.5 left-2.5 rounded-md bg-background/90 px-2 py-0.5 font-medium text-xs">Your photo</span>
              </div>
              <figcaption className="text-muted-foreground text-sm">Taken on a phone, on the counter.</figcaption>
            </figure>
            {PHOTO_SHOTS.map((shot) => (
              <figure key={shot.label} className="m-0 flex flex-col gap-2.5">
                <div className="relative overflow-hidden rounded-2xl border shadow-[0_12px_32px_rgba(0,0,0,0.08)]">
                  <Image src={shot.src} alt={`The same coffee bag, ${shot.label.toLowerCase()} photo made by the AI`} sizes="(min-width: 1024px) 270px, 45vw" placeholder="blur" className="h-auto w-full" />
                  <span className="absolute top-2.5 left-2.5 rounded-md bg-primary px-2 py-0.5 font-medium text-primary-foreground text-xs">{shot.label}</span>
                </div>
                <figcaption className="text-muted-foreground text-sm">{shot.body}</figcaption>
              </figure>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-10 rounded-3xl border bg-sidebar p-6 sm:p-10">
            <div className="flex min-w-0 flex-[1_1_320px] flex-col gap-5">
              <h3 className="m-0 font-heading font-normal text-3xl leading-tight">You choose, right in the chat.</h3>
              <ul className="m-0 flex list-none flex-col gap-3 p-0">
                {[
                  'Tap the one you like, or just say “use the 4th”.',
                  'Want something different? Say so, and get 3 more.',
                  'Every photo shows the prompt that made it.',
                  'Your own photo is always one of the choices.',
                ].map((point) => (
                  <li key={point} className="flex gap-2.5"><CheckIcon aria-hidden="true" className="mt-1 size-4.5 shrink-0 text-teal-600" />{point}</li>
                ))}
              </ul>
              <div><CtaButton dataComponent="landingPhotosCta">Make your first photos free</CtaButton></div>
            </div>
            <ScreenshotCard src={cardPhotoChoice} alt="The photo choice card in the chat: the original photo and 3 new ones, with the hero photo picked" className="flex-[1_1_420px]" />
          </div>
        </section>

        <section aria-labelledby="catalogTitle" className="mx-auto flex max-w-6xl flex-col gap-10 px-4 py-18 sm:px-6">
          <SectionHeading
            eyebrow="Set up in minutes"
            title="One photo of your shelf. A whole catalog."
            id="catalogTitle"
          />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <BeforeAfter
              title="Snap your products together"
              body="Send one photo of everything you sell. The AI names each product, cuts it out, and fills in your new products form. Add prices and save."
              before={<Image src={groupPhoto} alt="One phone photo of a coffee bag, a box of drip bags and a mug together" sizes="(min-width: 1024px) 200px, 40vw" placeholder="blur" className="h-auto w-full" />}
              after={cardStoreProducts}
              afterAlt="The products card: the coffee bag, the drip bags and the mug, each with its own cut-out photo and price"
            />
            <BeforeAfter
              title="Your colors, read from your logo"
              body="Send your logo or a photo of your shop sign. You get its exact colors and pick your primary and secondary. Your shop, your chat and your photos use them."
              before={<Image src={logoPhoto} alt="The Kalye Coffee logo: a brown circle with a gold star" sizes="(min-width: 1024px) 200px, 40vw" placeholder="blur" className="h-auto w-full" />}
              after={cardPhotoColors}
              afterAlt="The colors card: the logo's brown and gold picked as primary and secondary"
            />
          </div>
        </section>

        <section id="features" aria-labelledby="featuresTitle" className="border-y bg-sidebar">
          <div className="mx-auto flex max-w-6xl flex-col gap-10 px-4 py-20 sm:px-6 sm:py-24">
            <SectionHeading
              eyebrow="For you"
              title="A store manager in your chat."
              body="Ask in your own words. It answers from your store’s data and tells you what needs you before you ask."
              id="featuresTitle"
            />
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              <FeatureTile className="lg:col-span-2" icon={BellRingIcon} title="It tells you what’s slipping" body="Orders paid but not handed over, stock running low, a product with no photo, a return waiting for your answer. You hear it once, not every time.">
                <div className="flex flex-col gap-1.5 text-[13px] leading-[18px]">
                  <span className="self-end rounded-xl bg-muted px-2.5 py-1.5">Show me my orders</span>
                  <span className="self-start">Here are today’s 6 orders. 3 things need you:</span>
                  {[
                    { tone: 'bg-destructive', text: '2 paid orders not handed over for 7 days' },
                    { tone: 'bg-warning', text: 'Drip Bags, 10 pack: 3 left' },
                    { tone: 'bg-neutral-400', text: 'Kalye Enamel Mug has no photo' },
                  ].map((row) => (
                    <span key={row.text} className="flex items-center gap-2 rounded-lg border bg-background px-2.5 py-2">
                      <span className={`size-2 shrink-0 rounded-full ${row.tone}`} />{row.text}
                    </span>
                  ))}
                </div>
              </FeatureTile>
              <FeatureTile icon={BarChart3Icon} title="Sales reports on request" body="“How did last week go?” Sales by day, by product or by pickup and delivery, with refunds apart.">
                <div className="flex flex-col gap-2 text-[13px]">
                  <span className="flex items-baseline justify-between"><span className="text-muted-foreground">Last week</span><span className="font-heading text-xl">₱18,420</span></span>
                  <div aria-hidden="true" className="flex h-20 items-end gap-1.5">
                    {[38, 52, 30, 64, 80, 100, 72].map((height, index) => (
                      <span key={index} className="flex-1 rounded-t-sm bg-teal-600/80" style={{ height: `${height}%` }} />
                    ))}
                  </div>
                  <span className="flex justify-between text-[11px] text-muted-foreground">{['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((day, index) => <span key={index} className="flex-1 text-center">{day}</span>)}</span>
                </div>
              </FeatureTile>
              <FeatureTile icon={MapIcon} title="Delivery zones you draw" body="Draw where you deliver on a map. Each area has its own fee, days, minimum order and free delivery total.">
                <DeliveryZonesPreview />
              </FeatureTile>
              <FeatureTile icon={Undo2Icon} title="Returns, already checked" body="Each request arrives checked against your return policy. Approve or reject, then refund when it’s back.">
                <div className="flex flex-col gap-2 text-[13px]">
                  <span className="flex items-center justify-between"><span className="font-medium">Return · Enamel Mug × 1</span><Badge variant="outline" size="sm">Defective</Badge></span>
                  <span className="flex items-center gap-1.5 text-muted-foreground"><CheckCircle2Icon aria-hidden="true" className="size-4 text-success" />Inside your 7-day defect window</span>
                  <span className="flex gap-1.5 pt-1">
                    <span className="inline-flex h-7 items-center rounded-md bg-primary px-2.5 font-medium text-primary-foreground">Approve</span>
                    <span className="inline-flex h-7 items-center rounded-md border px-2.5">Reject</span>
                  </span>
                </div>
              </FeatureTile>
              <FeatureTile icon={ListPlusIcon} title="Add stock by typing a list" body="“Barako 500g, 620, 24 pieces” becomes a product you check and save. Up to 50 at once.">
                <div className="flex flex-col gap-1.5 text-[13px]">
                  <span className="self-end rounded-xl bg-muted px-2.5 py-1.5">Barako 500g, 620, 24 pcs</span>
                  <span className="grid grid-cols-[1fr_auto_auto] gap-x-3 rounded-lg border bg-background px-2.5 py-2">
                    <span className="font-medium">Barako 500g</span><span>₱620</span><span className="text-muted-foreground">24</span>
                  </span>
                </div>
              </FeatureTile>
              <VoiceTile />
            </div>
          </div>
        </section>

        <section id="shoppers" aria-labelledby="shoppersTitle" className="mx-auto flex max-w-6xl flex-wrap items-center gap-12 px-4 py-20 sm:px-6 sm:py-24">
          <div className="flex min-w-0 flex-[1_1_420px] flex-col gap-8">
            <SectionHeading eyebrow="For your customers" title="Buying feels like messaging the shop." id="shoppersTitle" />
            <ul className="m-0 flex list-none flex-col gap-5 p-0">
              {[
                { title: 'No app, no account', body: 'They open your link and start typing. A one-time text code confirms their number when they order.' },
                { title: 'Answers from your own rules', body: 'Stock, delivery areas, return windows: answered from what you’ve told it. Anything you haven’t, it sends to you.' },
                { title: 'A cart that’s always right', body: 'Prices and stock are checked again at checkout, so nobody buys what you no longer have.' },
                { title: 'Drop a pin for delivery', body: 'The zone under the pin sets the fee. The street address is filled in from the pin for them to confirm.' },
                { title: 'Pay the way they already do', body: 'Cash, GCash, Maya or bank transfer to your accounts. They send the receipt in the chat and you confirm it.' },
              ].map((item) => (
                <li key={item.title} className="flex gap-3.5">
                  <CheckCircle2Icon aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-teal-600" />
                  <span className="flex flex-col gap-0.5">
                    <span className="font-semibold">{item.title}</span>
                    <span className="text-muted-foreground leading-relaxed">{item.body}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <ScreenshotCard src={cardCheckoutSummary} alt="The customer’s order card: 2 products, free delivery to Marikina, the address and the pin, and a Place order button" className="flex-[1_1_380px]" />
        </section>

        <section id="how" aria-labelledby="howTitle" className="border-t bg-sidebar">
          <div className="mx-auto flex max-w-6xl flex-col gap-10 px-4 py-20 sm:px-6">
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
          </div>
        </section>

        <section aria-labelledby="trustTitle" className="mx-auto flex max-w-6xl flex-col gap-10 px-4 pt-20 pb-12 sm:px-6 sm:pt-24">
          <SectionHeading
            eyebrow="Your rules"
            title="It never makes things up."
            body="The assistant works only from your products and your answers. When it doesn’t know, it says so."
            id="trustTitle"
          />
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {TRUST.map((item) => (
              <div key={item.title} className="flex flex-col gap-2.5 rounded-2xl border p-6">
                <span className="flex items-center gap-2 font-semibold"><CheckCircle2Icon aria-hidden="true" className="size-4.5 text-success" />{item.title}</span>
                <span className="text-muted-foreground leading-relaxed">{item.body}</span>
              </div>
            ))}
          </div>
        </section>

        <section id="faq" aria-labelledby="faqTitle" className="mx-auto flex max-w-3xl flex-col gap-7 px-4 pt-12 pb-24 sm:px-6">
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
            <p className="m-0 max-w-lg text-lg text-neutral-300 leading-relaxed">Sign in with Google, chat your store into shape, send a photo of your products, and share your link.</p>
            <CtaButton dataComponent="landingFooterCta" variant="outline" />
            <span className="text-neutral-400 text-sm">Free demo · No card needed</span>
          </div>
        </section>
      </main>

      <footer className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-6 pt-8 pb-10 text-muted-foreground text-sm">
        <span className="flex items-center gap-2"><Logo size="sm" />© Star Commerce</span>
        <span className="flex flex-wrap gap-5">
          <a href="#top" className="text-muted-foreground no-underline">[Terms]</a>
          <a href="#top" className="text-muted-foreground no-underline">[Privacy]</a>
          <Link href={CTA_HREF} className="text-muted-foreground no-underline">Sign in</Link>
        </span>
      </footer>

      <div data-component="landingMobileCtaBar" className="fixed inset-x-0 bottom-0 z-30 flex items-center gap-3 border-t bg-background/95 px-4 py-3 backdrop-blur sm:hidden">
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate font-medium text-sm">Your store, as a chat</span>
          <span className="text-muted-foreground text-xs">Free demo · No card</span>
        </span>
        <Button data-component="landingMobileCta" render={<Link href={CTA_HREF} />}>Start free<ArrowRightIcon aria-hidden="true" /></Button>
      </div>
    </div>
  );
}

function ScreenshotCard({ src, alt, className }: { src: typeof cardPhotoChoice; alt: string; className?: string }) {
  return (
    <div className={`min-w-0 rounded-[22px] bg-muted/72 p-1.5 shadow-[0_24px_64px_rgba(0,0,0,0.08)] ${className ?? ''}`}>
      <Image src={src} alt={alt} sizes="(min-width: 1024px) 560px, 92vw" className="h-auto w-full rounded-2xl" />
    </div>
  );
}

function BeforeAfter({ title, body, before, after, afterAlt }: { title: string; body: string; before: React.ReactNode; after: typeof cardPhotoChoice; afterAlt: string }) {
  return (
    <article className="flex flex-col gap-6 rounded-3xl border p-6 sm:p-8">
      <div className="flex flex-col gap-2">
        <h3 className="m-0 font-semibold text-xl">{title}</h3>
        <p className="m-0 text-muted-foreground leading-relaxed">{body}</p>
      </div>
      <div className="grid grid-cols-1 items-center gap-3 sm:grid-cols-[minmax(0,2fr)_auto_minmax(0,3fr)]">
        <div className="w-1/2 overflow-hidden rounded-xl border sm:w-auto">{before}</div>
        <ArrowRightIcon aria-hidden="true" className="size-5 rotate-90 text-muted-foreground sm:rotate-0" />
        <Image src={after} alt={afterAlt} sizes="(min-width: 1024px) 300px, 55vw" className="h-auto w-full rounded-xl border shadow-[0_12px_32px_rgba(0,0,0,0.06)]" />
      </div>
    </article>
  );
}

function FeatureTile({ icon: Icon, title, body, children, className }: { icon: typeof MapIcon; title: string; body: string; children: React.ReactNode; className?: string }) {
  return (
    <article className={`flex flex-col gap-4 rounded-2xl border bg-background p-6 ${className ?? ''}`}>
      <div aria-hidden="true" className="rounded-xl border bg-sidebar p-3.5">{children}</div>
      <h3 className="m-0 flex items-center gap-2 font-semibold text-lg"><Icon aria-hidden="true" className="size-4.5 text-teal-600" />{title}</h3>
      <p className="m-0 text-muted-foreground leading-relaxed">{body}</p>
    </article>
  );
}

function DeliveryZonesPreview() {
  return (
    <div className="relative h-28 overflow-hidden rounded-lg bg-neutral-100 [background-image:linear-gradient(rgba(0,0,0,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(0,0,0,0.05)_1px,transparent_1px)] [background-size:20px_20px]">
      <span className="absolute inset-x-0 top-[55%] h-2 bg-white" />
      <span className="absolute inset-y-0 left-[46%] w-2 bg-white" />
      <svg viewBox="0 0 300 112" className="absolute inset-0 size-full" preserveAspectRatio="none">
        <polygon points="18,14 138,8 150,62 96,100 22,86" fill="rgba(13,148,136,0.18)" stroke="#0D9488" strokeWidth="2" />
        <polygon points="150,62 138,8 262,18 280,90 196,104 96,100" fill="rgba(59,130,246,0.14)" stroke="#3B82F6" strokeWidth="2" />
      </svg>
      <span className="absolute top-3 left-4 rounded-md bg-background px-1.5 py-0.5 font-medium text-[11px]">Marikina · ₱60 · free from ₱1,500</span>
      <span className="absolute right-4 bottom-3 rounded-md bg-background px-1.5 py-0.5 font-medium text-[11px]">Pasig · ₱90</span>
    </div>
  );
}

function VoiceTile() {
  return (
    <article className="flex flex-col gap-6 rounded-2xl border bg-background p-6 shadow-[inset_0_0_28px_2px_rgba(13,148,136,0.22),inset_0_0_120px_16px_rgba(59,130,246,0.1)] md:col-span-2 lg:col-span-3 lg:flex-row-reverse lg:items-center lg:gap-12 lg:p-10">
      <div aria-hidden="true" className="flex min-w-0 flex-1 flex-col gap-3 p-1">
        <span className="text-muted-foreground text-sm">“How many orders are waiting?”</span>
        <span className="font-heading text-xl leading-snug">5 paid orders are waiting. The oldest was paid at 9:12 this morning.</span>
        <span className="flex items-center gap-3 rounded-full border bg-background py-1.5 ps-1.5 pe-3 shadow-[0_12px_32px_rgba(0,0,0,0.08)]">
          <span className="relative flex size-9 items-center justify-center gap-0.5 rounded-full bg-[radial-gradient(circle_at_34%_28%,#ffffff_0%,rgba(59,130,246,0.8)_34%,#0D9488_82%)]">
            <span className="h-2.5 w-0.5 rounded-full bg-white/90" />
            <span className="h-3.5 w-0.5 rounded-full bg-white/90" />
            <span className="h-2 w-0.5 rounded-full bg-white/90" />
          </span>
          <span className="font-medium text-sm">Speaking</span>
        </span>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <h3 className="m-0 flex items-center gap-2 font-semibold text-lg lg:text-2xl"><MicIcon aria-hidden="true" className="size-4.5 text-teal-600" />Talk instead of type</h3>
        <p className="m-0 text-muted-foreground leading-relaxed">Voice mode listens and answers out loud, in your store’s own voice, for you and your customers. Handy with flour on your hands, and for customers who’d rather talk than type.</p>
      </div>
    </article>
  );
}

function HeroPreview() {
  return (
    <div
      role="img"
      aria-label="The store owner's chat listing orders to hand over, next to a customer choosing how to pay in the shop chat on a phone"
      className="flex w-full flex-wrap items-end justify-center gap-6"
    >
      <div aria-hidden="true" className="flex min-w-0 max-w-3xl flex-[1_1_560px] overflow-hidden rounded-[20px] border bg-background text-[13px] leading-[18px] shadow-[0_24px_64px_rgba(0,0,0,0.08)]">
        <div className="flex w-42 shrink-0 flex-col gap-1.5 border-e bg-sidebar p-3 max-sm:hidden">
          <span className="flex items-center gap-2 p-1"><span className="size-5 rounded-md bg-orange-900" /><span className="font-medium">Kalye Coffee</span></span>
          <span className="flex h-7 items-center rounded-md border bg-background px-2 font-medium">+ New chat</span>
          <span className="px-2 pt-2 font-medium text-[11px] text-muted-foreground">Today</span>
          <span className="truncate rounded-md bg-accent px-2 py-1.5 font-medium">Hand over today’s orders</span>
          <span className="truncate px-2 py-1.5">Photos for the Barako</span>
          <span className="truncate px-2 py-1.5">Add the new products</span>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-3 p-5">
          <span className="self-end rounded-2xl bg-muted px-3 py-2">What needs handing over?</span>
          <span>5 paid orders are waiting. Tick the ones you’ve handed over.</span>
          <div className="flex flex-col overflow-hidden rounded-xl border">
            {[
              { id: '#4F1A09', items: 'Barako Blend 500g × 2', total: '₱1,240', checked: true },
              { id: '#4F1B3C', items: 'Drip Bags, 10 pack', total: '₱380', checked: true },
              { id: '#4F1C77', items: 'Kalye Enamel Mug', total: '₱450', checked: false },
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
        <span className="max-w-[86%] self-end rounded-[14px] rounded-br-sm bg-primary px-3 py-2 text-primary-foreground">2 bags of the Barako please</span>
        <span>Order placed. How would you like to pay?</span>
        <div className="flex flex-col gap-1.5 rounded-2xl border p-2.5">
          <span className="font-heading text-lg">₱1,240</span>
          <span className="flex h-9 items-center justify-between rounded-lg border-2 border-primary px-2.5 font-medium">GCash<span className="size-3.5 rounded-full bg-primary" /></span>
          <span className="flex h-9 items-center justify-between rounded-lg border px-2.5">Bank transfer<span className="size-3.5 rounded-full border" /></span>
          <span className="flex h-9 items-center justify-between rounded-lg border px-2.5">Cash on pickup<span className="size-3.5 rounded-full border" /></span>
          <span className="flex h-9.5 items-center justify-center rounded-lg bg-primary font-medium text-primary-foreground">Pay ₱1,240 with GCash</span>
        </div>
        <span className="flex h-9.5 items-center gap-2 rounded-full border ps-3 pe-1.5 text-muted-foreground">Ask Kalye Coffee<span className="ms-auto size-7 rounded-full bg-primary" /></span>
      </div>
    </div>
  );
}
