// /cyber-view のスクリーンショット(検証レポート用)
// node shot.js <url> <out.png> [wait_ms] [link_interval_sec]
const { chromium } = require('playwright');
(async () => {
  const [url, out, wait = '6000', lint] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1400, height: 800 }, deviceScaleFactor: 1 });
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  await p.goto(url);
  if (lint) { try { await p.waitForTimeout(1500); await p.selectOption('#lInt', lint); } catch (e) { /* NETWORK 画面には無い */ } }
  await p.waitForTimeout(+wait);
  await p.screenshot({ path: out });
  if (errs.length) console.log('JSERR ' + JSON.stringify(errs));
  await b.close();
})();
