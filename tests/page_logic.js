// The page's logic, run in Node: node tests/page_logic.js <page.js>
// page.js is the page's <script> (tests/test_page_logic.py writes it). It is loaded whole into a sandbox whose DOM
// swallows everything (no browser needed); the checks below then call the page's own functions. Prints one line
// per check and "FAILED n" or "OK n"; exits 1 on a failure.
'use strict';
const vm = require('vm');
const fs = require('fs');

function swallow() {            // any property, call or assignment works and gives this back
  const target = function () {};
  const proxy = new Proxy(target, {
    get(_, key) {
      if (key === Symbol.toPrimitive) return () => '';
      if (key === Symbol.iterator) return function* () {};
      if (key === 'then') return undefined;              // not a promise
      if (key === 'length') return 0;
      return proxy;
    },
    set() { return true; },
    apply() { return proxy; },
    construct() { return proxy; },
  });
  return proxy;
}

const dom = swallow();
const sandbox = {
  document: dom, window: dom, navigator: dom, history: dom, URL: dom, Blob: function () {},
  location: { hash: '', pathname: '/', search: '', replace() {}, reload() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  fetch: () => new Promise(() => {}),                   // nothing answers: no load ever finishes
  setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {}, requestAnimationFrame() {},
  confirm: () => false, alert() {}, console,
  URLSearchParams, Intl, encodeURIComponent, decodeURIComponent,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox, { filename: 'page.js' });

let failed = 0;
let ran = 0;
function check(name, fn) {
  ran += 1;
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`FAIL ${name}: ${e.message}`);
  }
}
function eq(got, want, what = '') {
  const a = JSON.stringify(got);
  const b = JSON.stringify(want);
  if (a !== b) throw new Error(`${what} got ${a}, want ${b}`);
}
const page = (code) => vm.runInContext(code, sandbox);   // the page's top-level names (let / const too)

check('esc makes text safe in HTML', () => {
  eq(page(`esc('<a href="x">&\\'</a>')`), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
});

check('plural', () => {
  eq(page(`[plural(1, 'stack'), plural(2, 'stack'), plural(0, 'restart')]`), ['1 stack', '2 stacks', '0 restarts']);
});

check('memClass: none without a limit, ok, warn from 80 %, bad from 95 %', () => {
  eq(page(`[memClass(5, 0), memClass(79, 100), memClass(80, 100), memClass(94, 100), memClass(95, 100)]`),
     ['', 't-ok', 't-warn', 't-warn', 't-bad']);
});

check('checkFailures names each stack whose registry could not be asked, with the first reason', () => {
  eq(page(`checkFailures({
    web: { status: 'current', images: [] },
    'private-app': { status: 'unknown', images: [{ status: 'current', message: '' }, { status: 'unknown', message: 'registry: HTTP 401' }] },
    odd: { status: 'unknown', images: [] },
  })`), [['private-app', 'registry: HTTP 401'], ['odd', 'no reason given']]);
  eq(page('checkFailures(undefined)'), []);
});

check('without the server\'s limits nothing is judged (the page has no number of its own)', () => {
  page(`HEALTH = null; NEAR = {}; NEAR_AT = 0;
    RES = { host: null, containers: { app: { mem: 999, limit: 1000 } } }; trackNear(); NEAR_AT += 3600000;`);
  eq(page('nearLimit()'), []);
  const loop = page(`restartTag({ restarts: 9, looping: true })`);
  if (!loop.includes('Restart loop') || /\d+ minutes/.test(loop)) throw new Error(loop);
});

check('the limits come from the server: other numbers, other checks and words', () => {
  page(`HEALTH = { limits: { nearLimit: 0.5, nearHoldSeconds: 5, loopRestarts: 3, loopWindowSeconds: 1200 } };
    NEAR = {}; NEAR_AT = 0; globalThis.__clock = 5000000; Date.now = () => globalThis.__clock;
    RES = { host: null, containers: { app: { mem: 600, limit: 1000 } } }; trackNear();
    __clock += 6000; trackNear();`);
  eq(page('nearLimit().map((n) => n.name)'), ['app'], 'over 50 % for 6 s:');
  const loop = page(`restartTag({ restarts: 9, looping: true })`);
  if (!loop.includes('within 20 minutes')) throw new Error(loop);
  if (!page('nearText(nearLimit()[0])').includes('60 %')) throw new Error(page('nearText(nearLimit()[0])'));
});

check('memory near the limit: only after 30 s of readings, and gone when it drops', () => {
  page(`HEALTH = { limits: { nearLimit: 0.9, nearHoldSeconds: 30, loopRestarts: 3, loopWindowSeconds: 600 } };
    NEAR = {}; NEAR_AT = 0;
    globalThis.__clock = 1000000; Date.now = () => globalThis.__clock;
    globalThis.__read = (mem) => { RES = { host: null, containers: { app: { mem, limit: 1000 }, small: { mem: 10, limit: 1000 } } }; trackNear(); };`);
  page('__read(950)');
  eq(page('nearLimit().map((n) => n.name)'), [], 'first reading:');
  page('__clock += 1000; __read(950)');                  // the page reads twice when it opens
  eq(page('nearLimit().map((n) => n.name)'), [], 'one second later:');
  page('__clock += 29000; __read(960)');
  eq(page('nearLimit().map((n) => n.name)'), ['app'], '30 s later:');
  page('__clock += 10000; __read(500)');
  eq(page('nearLimit()'), [], 'below again:');
  page('__clock += 10000; __read(990)');
  eq(page('nearLimit()'), [], 'a new spike starts the 30 s again:');
  page('RES = { host: null, containers: { app: { mem: 990, limit: 0 } } }; __clock += 60000; trackNear()');
  eq(page('nearLimit()'), [], 'no limit:');
});

check('restartTag: nothing, a count, a loop, out of memory', () => {
  eq(page(`restartTag({ restarts: 0 })`), '');
  const count = page(`restartTag({ restarts: 2, looping: false })`);
  if (!count.includes('2× restarted') || !count.includes('t-idle')) throw new Error(count);
  const loop = page(`restartTag({ restarts: 9, looping: true, oom: true })`);
  if (!loop.includes('Restart loop') || !loop.includes('Out of memory') || !loop.includes('t-bad')) throw new Error(loop);
  const oomOnly = page(`restartTag({ restarts: 0, oom: true })`);
  if (!oomOnly.includes('Out of memory')) throw new Error(oomOnly);
});

check('activityResult colours', () => {
  const cls = (e) => /tag (t-[a-z]+)/.exec(page(`activityResult(${JSON.stringify(e)})`))[1];
  eq([cls({ result: 'ok' }), cls({ result: 'failed' }), cls({ result: 'rolled-back' }), cls({ result: 'skipped' }),
      cls({ type: 'warning', kind: 'restart-loop', result: 'started' }),
      cls({ type: 'warning', kind: 'memory-near', result: 'started' }),
      cls({ type: 'warning', kind: 'memory-near', result: 'ended' }), cls({ result: 'something new' })],
     ['t-ok', 't-bad', 't-warn', 't-idle', 't-bad', 't-warn', 't-ok', 't-idle']);
});

check('activityRows escapes what a container or an error message says', () => {
  const html = page(`activityRows([{ at: '2026-11-17T06:00:00Z', type: 'change', kind: '<b>x</b>', target: '<img src=x onerror=alert(1)>',
    stack: 'web"x', result: 'failed', message: '<script>alert(1)</script>', seconds: 1.5, source: 'page' }])`);
  for (const bad of ['<img', '<script', '<b>x', 'web"x']) {
    if (html.includes(bad)) throw new Error(`not escaped: ${bad}`);
  }
  if (!html.includes('page · 1.5 s')) throw new Error('the source and the time are missing');
});

check('progressHtml: nothing without progress, a bar of images done, a busy bar when the total is unknown', () => {
  eq(page('progressHtml(null)'), '');
  eq(page("progressHtml({ text: 'x' })"), '');
  const known = page(`progressHtml({ progress: { step: 'Pulling images', detail: 'jellyfin, 5 layers', done: 1, total: 4 } })`);
  for (const want of ['width:25%', 'aria-valuenow="1"', 'aria-valuemax="4"', 'Pulling images 1/4', 'jellyfin, 5 layers']) {
    if (!known.includes(want)) throw new Error(`missing ${want}: ${known}`);
  }
  const busy = page(`progressHtml({ progress: { step: 'Recreating containers', detail: '', done: null, total: null } })`);
  if (!busy.includes('progress busy') || busy.includes('aria-valuenow') || busy.includes('null')) throw new Error(busy);
  const over = page(`progressHtml({ progress: { step: 'x', detail: '', done: 5, total: 4 } })`);
  if (!over.includes('width:100%')) throw new Error(over);
});

check('progressHtml escapes what podman said', () => {
  const html = page(`progressHtml({ progress: { step: '<b>s</b>', detail: '<img src=x onerror=alert(1)>', done: 0, total: 1 } })`);
  if (html.includes('<img') || html.includes('<b>')) throw new Error(html);
});

check('the routes', () => {
  const route = (hash) => page(`location.hash = ${JSON.stringify(hash)}; parseHash()`);
  eq(route('#/activity'), { view: 'activity', stack: '' });
  eq(route('#/activity/web'), { view: 'activity', stack: 'web' });
  eq(route('#/stacks/web/files'), { view: 'detail', name: 'web', tab: 'files' });
  eq(route('#/stacks/web/nope'), { view: 'detail', name: 'web', tab: 'summary' });
  eq(route('#/health'), { view: 'health' });
  eq(route('#/whatever'), { view: 'stacks' });
});

console.log(failed ? `FAILED ${failed} of ${ran}` : `OK ${ran}`);
process.exit(failed ? 1 : 0);
