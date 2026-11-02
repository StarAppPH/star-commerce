// Screenshots of the real chat cards for the landing page (public/landing/).
// Start the dev server first (`npm run dev`), then:
//   node scripts/landingShots.mjs [http://localhost:3000]
// The cards and their sample data are in app/dev/landing-shots/.
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// Playwright is not a client dependency: use the one installed on the machine.
const { chromium } = createRequire(import.meta.url)('playwright');

const base = process.argv[2] ?? 'http://localhost:3000';
const out = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'public', 'landing');
const SHOTS = ['shotPhotoChoice', 'shotPhotoColors', 'shotStoreProducts', 'shotCheckoutSummary'];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 800, height: 1200 }, deviceScaleFactor: 2 });
await page.goto(`${base}/dev/landing-shots`, { waitUntil: 'networkidle' });
// Next's dev badge sits over the page's corner: keep it out of the shots.
await page.addStyleTag({ content: 'nextjs-portal { display: none !important; }' });
await page.waitForFunction(() => [...document.images].every((image) => image.complete));
// Show each choosing card mid-choice: the hero photo picked, both brand colors set.
await page.locator('#shotPhotoChoice [data-component="productPhotoChoiceOption"]').nth(3).click();
const swatches = page.locator('#shotPhotoColors [data-component="photoColorsSwatch"]');
await swatches.nth(0).click();
await page.locator('#shotPhotoColors [data-component="photoColorsSecondarySlot"]').click();
await swatches.nth(1).click();
await page.mouse.move(0, 0);
for (const id of SHOTS) {
  const card = page.locator(`#${id} [data-component]`).first();
  await card.screenshot({ path: path.join(out, `${id}.png`), animations: 'disabled' });
}
await browser.close();
console.log(`Saved ${SHOTS.length} screenshots to ${out}`);
