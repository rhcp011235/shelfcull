// Browser test of the page against the demo stack (tests/demo.py must be running).
//   node tests/ui_test.js <base url> <screenshot dir>      needs playwright-core + a Chromium (CHROMIUM=/path, PLAYWRIGHT=/path/to/playwright-core)
const { chromium } = require(process.env.PLAYWRIGHT || 'playwright-core');
(async () => {
  const base = process.argv[2] || 'http://127.0.0.1:8765/', shots = process.argv[3];
  const br = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined, headless: true, args: ['--no-sandbox'] });
  let n = 0, bad = 0; const errs = [];
  const ok = (name, cond) => { n++; if (!cond) bad++; console.log((cond ? 'ok   ' : 'FAIL ') + name); };
  for (const [label, vp] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
    const ctx = await br.newContext({ viewport: vp, hasTouch: label === 'phone' }); const p = await ctx.newPage();
    p.on('pageerror', e => errs.push(label + ': ' + e.message));
    p.on('console', m => { if (m.type() === 'error') errs.push(label + ' console: ' + m.text()); });
    p.on('dialog', d => d.accept());
    let posts = 0; p.on('request', r => { if (r.method() === 'POST') posts++; });
    await p.goto(base); await p.waitForSelector('.title');
    const T = () => p.textContent('.title');
    ok(label + ': library filter lists both kinds and all three instances', (await p.$$eval('#fLib option', o => o.map(x => x.textContent))).join('|') === 'Everything|All movies|All TV shows|Movies HD|Movies 4K|TV');
    ok(label + ': poster is shown (data URL allowed by the page policy)', await p.waitForFunction(() => document.getElementById('poster').style.backgroundImage.startsWith('url("data:image/svg'), null, { timeout: 5000 }).then(() => true, () => false));
    // looking through without deciding
    const t1 = await T(); ok(label + ': Previous is off on the first title', await p.isDisabled('#bPrev'));
    await p.click('#bNext'); const t2 = await T(); await p.keyboard.press('ArrowRight'); const t3 = await T(); await p.keyboard.press('ArrowLeft');
    ok(label + ': Next / arrow keys look through, nothing is saved', t1 !== t2 && t2 !== t3 && (await T()) === t2 && posts === 0);
    if (label === 'phone') {
      const swipe = dx => p.evaluate(dx => { const c = document.getElementById('card'); const mk = (type, x) => { const t = new Touch({ identifier: 1, target: c, clientX: x, clientY: 300 }); c.dispatchEvent(new TouchEvent(type, { bubbles: true, touches: type === 'touchend' ? [] : [t], changedTouches: [t] })); }; mk('touchstart', 200); mk('touchend', 200 + dx); }, dx);
      await swipe(-120); const a = await T(); await swipe(120); ok(label + ': swipe left = next, right = back', a !== t2 && (await T()) === t2);
      ok(label + ': no horizontal scroll', await p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    }
    // TV shows
    await p.selectOption('#fLib', 'kind:show'); await p.selectOption('#fPlay', 'all'); await p.waitForFunction(() => queue.length && queue.every(f => f.kind === 'show'));
    ok(label + ': TV shows have their own cards (seasons, episodes)', /season/.test(await p.textContent('.facts')) && (await p.textContent('.tags')).includes('TV show'));
    const partly = await p.evaluate(() => { const i = queue.findIndex(f => f.n > 0 && !f.full); if (i < 0) return false; pos = i; show(); return true; });
    ok(label + ': a partly watched show says how many episodes', partly && /You watched \d+ of \d+ episodes/.test(await p.textContent('.tags')) && !(await p.isDisabled('#bSeen')));
    const fully = await p.evaluate(() => { const i = queue.findIndex(f => f.full); if (i < 0) return false; pos = i; show(); return true; });
    ok(label + ': Seen it is off for a fully watched show', fully && await p.isDisabled('#bSeen'));
    // poster wall
    await p.selectOption('#fLib', ''); await p.selectOption('#fPlay', 'nobody'); await p.click('#gridBtn'); await p.waitForSelector('#wall .tile');
    const tiles = (await p.$$('#wall .tile')).length;
    ok(label + ': Browse shows the poster wall', tiles > 20 && tiles <= 120);
    if (shots) await p.screenshot({ path: `${shots}/${label}-wall.png` });
    const want = await p.evaluate(() => queue[5].t); await p.click('#wall .tile >> nth=5'); await p.waitForSelector('#pick:not(.hide) .title');
    ok(label + ': tapping a poster opens that title', (await T()) === want);
    if (shots) await p.screenshot({ path: `${shots}/${label}-card.png` });
    // Seen it -> Plex
    posts = 0; const seenKey = await p.evaluate(() => queue[pos].k);
    await p.click('#bSeen'); await p.waitForFunction(k => dec[k] && dec[k].d === 'seen', seenKey);
    ok(label + ': Seen it saves and moves on', posts === 1 && (await T()) !== want);
    const marked = await p.waitForFunction(async k => { const j = await (await fetch('api/items')).json(); const f = j.items.find(x => x.k === k); return f && f.full; }, seenKey, { timeout: 20000 }).then(() => true, () => false);
    ok(label + ': the worker marked it played in Plex and rebuilt the list', marked);
    // Delete + Keep + undo
    const d1 = await T(); const delKey = await p.evaluate(() => queue[pos].k); await p.keyboard.press('d'); await p.waitForFunction(k => dec[k] && dec[k].d === 'delete', delKey);
    const k1 = await p.evaluate(() => queue[pos].k); await p.click('#bKeep'); await p.waitForFunction(k => dec[k] && dec[k].d === 'keep', k1);
    await p.click('#bDel'); await p.waitForFunction(() => Object.values(dec).filter(v => v.d === 'delete' && !v.done).length === 2);
    await p.click('#bUndo'); await p.waitForFunction(() => Object.values(dec).filter(v => v.d === 'delete' && !v.done).length === 1);
    ok(label + ': Delete, Keep and Undo are saved; the counter shows 1', (await p.textContent('#cnt')).startsWith('1 ('));
    await p.fill('#fSearch', d1); await p.waitForFunction(k => queue[pos] && queue[pos].k === k, delKey);
    ok(label + ': search finds a marked title and says so', (await p.textContent('.tags')).includes('You marked this Delete'));
    await p.fill('#fSearch', '');
    // review + run
    await p.click('#reviewBtn'); await p.waitForSelector('#runBtn');
    ok(label + ': review lists the marked title', (await p.textContent('#list')).includes(d1) && (await p.textContent('#runBtn')).startsWith('Remove the 1 marked title now'));
    await p.click('#tabKeep'); await p.waitForFunction(() => document.querySelectorAll('#list .item').length >= 1);
    ok(label + ': Keep tab lists the kept title', true); await p.click('#tabDel');
    if (shots) await p.screenshot({ path: `${shots}/${label}-review.png` });
    await p.click('#runBtn');
    const finished = await p.waitForFunction(() => /Last run .*removed 1 title/.test(document.getElementById('runBox').textContent), null, { timeout: 30000 }).then(() => true, () => false);
    ok(label + ': Remove runs and the page reports "removed 1 title"', finished);
    ok(label + ': the removed title is gone from the list, counter back to 0', await p.evaluate(k => !items.some(f => f.k === k), delKey) && (await p.textContent('#cnt')) === '0');
    await p.click('#back'); await p.waitForSelector('#pick:not(.hide) .title');
    ok(label + ': back to the list', true);
    await ctx.close();
  }
  console.log(errs.length ? 'JS errors: ' + errs.join(' | ') : 'no JS errors'); console.log(`${n - bad}/${n} checks passed`);
  await br.close(); process.exit(bad || errs.length ? 1 : 0);
})();
