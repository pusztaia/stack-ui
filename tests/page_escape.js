// Does the page escape what the server tells it? node tests/page_escape.js <page.js> <base url> <config.json>
// The page's <script> (tests/test_page_escape.py writes it) runs in a sandbox whose DOM swallows everything but records
// every HTML string that is assigned (innerHTML, outerHTML, insertAdjacentHTML). Its fetch goes to the real server of the
// test, which is filled with hostile text (config.payloads). Each route in config.routes ({path, expect}) is rendered (one
// that expects hostile text is waited for until the escaped text shows, at most 8 s); the output is one JSON
// line: {violations: [...], seen: {route: true}} where a violation is a payload that reached the HTML unescaped and `seen`
// lists the routes whose HTML contains the payloads escaped (so a pass is not just "nothing was rendered").
'use strict';
const vm = require('vm');
const fs = require('fs');

const [, , scriptPath, base, configPath] = process.argv;
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const log = [];                      // {route, html}
let current = 'boot';

function recorder() {
  const target = function () {};
  const proxy = new Proxy(target, {
    get(_, key) {
      if (key === Symbol.toPrimitive) return () => '';
      if (key === Symbol.iterator) return function* () {};
      if (key === 'then') return undefined;
      if (key === 'length') return 0;
      if (key === 'insertAdjacentHTML') return (_pos, html) => { log.push({ route: current, html: String(html) }); };
      return proxy;
    },
    set(_, key, value) {
      if (key === 'innerHTML' || key === 'outerHTML') log.push({ route: current, html: String(value) });
      return true;
    },
    apply() { return proxy; },
    construct() { return proxy; },
  });
  return proxy;
}

const dom = recorder();
const sandbox = {
  document: dom, window: dom, navigator: dom, history: dom, URL: dom, Blob: function () {},
  location: { hash: '', pathname: '/', search: '', replace() {}, reload() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  fetch: (url, options) => fetch(new URL(url, base), options),
  setTimeout, setInterval, clearTimeout, clearInterval, requestAnimationFrame() {},
  confirm: () => false, alert() {}, console,
  URLSearchParams, Intl, encodeURIComponent, decodeURIComponent,
};
vm.createContext(sandbox);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const page = (code) => vm.runInContext(code, sandbox);

(async () => {
  vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox, { filename: 'page.js' });
  await sleep(config.bootMs || 1500);
  const escaped = Object.values(config.payloads).map((payload) => page('esc')(payload));
  const showsEscaped = (route) => log.some((l) => l.route === route && escaped.some((e) => l.html.includes(e)));
  for (const { path: route, expect } of config.routes) {
    current = route;
    sandbox.location.hash = '#/' + route;
    try { page('render()'); } catch (e) { console.error(`route ${route}: ${e.message}`); }
    const until = Date.now() + 8000;
    if (expect) {
      while (!showsEscaped(route) && Date.now() < until) await sleep(100);
      await sleep(150);                // the rest of the view's requests
    } else {
      await sleep(config.routeMs || 500);
    }
  }
  const violations = [];
  const seen = {};
  for (const { route, html } of log) {
    for (const [name, payload] of Object.entries(config.payloads)) {
      if (html.includes(payload)) {
        const at = html.indexOf(payload);
        violations.push({ route, payload: name, around: html.slice(Math.max(0, at - 60), at + payload.length + 40) });
      }
      if (html.includes(page('esc')(payload))) seen[`${route}:${name}`] = true;
    }
  }
  console.log(JSON.stringify({ violations, seen, rendered: log.length }));
  process.exit(0);
})().catch((e) => { console.error(e.stack || e.message); process.exit(2); });
