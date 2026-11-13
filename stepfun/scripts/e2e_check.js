// End-to-end check for the StepFun × seekdb demo.
// Requires: an running seekdb, `uvicorn server.app:app --port 8099`, and puppeteer-core.
//   node scripts/e2e_check.js
// Prints one line per stage so the log can be pasted into docs/evidence.md.
const puppeteer = require('puppeteer-core');

const BASE = process.env.BASE || 'http://127.0.0.1:8099/';
const CHROME = process.env.CHROME || '/usr/local/bin/chromium';

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  page.on('console', (m) => console.log('PAGE:', m.text()));
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });

  // 1) model load (browser side only)
  const model = await page.evaluate(async () => {
    const t = performance.now();
    const backend = await window.__stepfun.ensureModel('auto', 'q8');
    return { ms: Math.round(performance.now() - t), b: backend };
  });
  console.log('MODEL:', JSON.stringify(model));

  // 2) import batch 1
  await page.click('#importBtn');
  await page.waitForFunction(
    () => /已写入|失败/.test(document.querySelector('#importLog').textContent),
    { timeout: 120000 },
  );
  console.log('IMPORT:', await page.$eval('#importLog', (n) => n.textContent));

  // 3) hybrid search
  await page.type('#query', '遥控器有几个按键');
  await page.click('#searchBtn');
  await page.waitForFunction(
    () => /条结果|失败/.test(document.querySelector('#searchLog').textContent),
    { timeout: 120000 },
  );
  console.log('SEARCH:', await page.$eval('#searchLog', (n) => n.textContent));

  // 4) import batch 2, then reload and search again
  await page.evaluate(() => { document.querySelector('#batchSelect').value = '2'; });
  await page.click('#importBtn');
  await page.waitForFunction(
    () => /已写入|失败/.test(document.querySelector('#importLog').textContent),
    { timeout: 120000 },
  );
  console.log('IMPORT2:', await page.$eval('#importLog', (n) => n.textContent));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => /chunks \/ /.test(document.querySelector('#dbVal').textContent),
    { timeout: 30000 },
  );
  await page.type('#query', 'HNSW 索引延迟');
  await page.click('#searchBtn');
  await page.waitForFunction(
    () => /条结果|失败/.test(document.querySelector('#searchLog').textContent),
    { timeout: 120000 },
  );
  console.log('AFTER-REFRESH:', await page.$eval('#searchLog', (n) => n.textContent));

  await browser.close();
})().catch((err) => {
  console.error('E2E FAILED:', err.message);
  process.exit(1);
});
