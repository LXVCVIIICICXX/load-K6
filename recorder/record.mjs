#!/usr/bin/env node
/**
 * Запись браузерной сессии в HAR: человек проходит сценарий руками, мы слушаем.
 *
 *   node record.mjs --out ../../har/moya-zapis.har [--url https://...] [--auth логин:пароль]
 *
 * Останов — SIGINT (Ctrl+C или кнопка «Остановить» в морде) либо закрытие окна
 * браузера. HAR дописывается на выходе.
 *
 * Почему не встроенный в Playwright `recordHar`: он не отдаёт признак
 * «ответ взят из кэша браузера», а без него запись врёт о нагрузке — часть
 * запросов до сервера не доходит вовсе. Поэтому HAR собираем сами из событий
 * CDP `Network.*`, где Chrome сообщает `fromDiskCache` напрямую, и пишем его
 * в то же поле `_fromCache`, которое ждёт site-load/har.py.
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const arg = (name, def) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
};

const OUT = arg('out');
// Адрес стенда приходит снаружи: из морды, из --url или из SITE_URL в .env.
const URL = arg('url', process.env.SITE_URL || '');
const AUTH = arg('auth', process.env.SITE_AUTH || '');
const BODY_CAP = 2 * 1024 * 1024;

if (!OUT) {
  console.error('нужен --out <файл.har>');
  process.exit(2);
}
if (!URL) {
  console.error('нужен --url <адрес фронта> или SITE_URL в .env');
  process.exit(2);
}

const entries = [];
const pending = new Map();
const served = new Set();

const hdr = (o) => Object.entries(o || {}).map(([name, value]) => ({ name, value: String(value) }));

function query(url) {
  try {
    return [...new global.URL(url).searchParams].map(([name, value]) => ({ name, value }));
  } catch {
    return [];
  }
}

// CDP отдаёт смещения в мс от `requestTime`, а -1 значит «фазы не было».
// HAR ждёт длительности фаз, поэтому считаем разности и сохраняем -1 как есть.
function timings(t, total) {
  if (!t) return { blocked: -1, dns: -1, connect: -1, send: 0, wait: total, receive: 0, ssl: -1 };
  const first = [t.dnsStart, t.connectStart, t.sendStart].find((v) => v >= 0);
  const dur = (a, b) => (a >= 0 && b >= 0 ? Math.max(0, b - a) : -1);
  const wait = dur(t.sendEnd, t.receiveHeadersEnd);
  return {
    blocked: first >= 0 ? first : -1,
    dns: dur(t.dnsStart, t.dnsEnd),
    connect: dur(t.connectStart, t.connectEnd),
    ssl: dur(t.sslStart, t.sslEnd),
    send: dur(t.sendStart, t.sendEnd) < 0 ? 0 : dur(t.sendStart, t.sendEnd),
    wait: wait < 0 ? 0 : wait,
    receive: t.receiveHeadersEnd >= 0 ? Math.max(0, total - t.receiveHeadersEnd) : 0,
  };
}

function push(p, res, total, transfer, failed) {
  const status = failed ? 0 : res?.status ?? 0;
  const fromCache = served.has(p.requestId) ? 'memory' : res?.fromDiskCache ? 'disk' : null;
  const e = {
    startedDateTime: new Date(p.wallTime * 1000).toISOString(),
    time: Math.max(0, total),
    request: {
      method: p.method,
      url: p.url,
      httpVersion: 'HTTP/1.1',
      headers: hdr(p.headers),
      queryString: query(p.url),
      cookies: [],
      headersSize: -1,
      bodySize: p.postData ? Buffer.byteLength(p.postData) : 0,
      ...(p.postData
        ? { postData: { mimeType: p.headers?.['content-type'] || 'application/json', text: p.postData } }
        : {}),
    },
    response: {
      status,
      statusText: res?.statusText || (failed ? 'aborted' : ''),
      httpVersion: res?.protocol || 'HTTP/1.1',
      headers: hdr(res?.headers),
      cookies: [],
      content: {
        size: p.body ? Buffer.byteLength(p.body) : 0,
        mimeType: res?.mimeType || '',
        ...(p.body ? { text: p.body } : {}),
      },
      redirectURL: res?.headers?.location || res?.headers?.Location || '',
      headersSize: -1,
      bodySize: transfer ?? -1,
    },
    cache: {},
    timings: timings(res?.timing, total),
    // har.py читает эти три поля напрямую: по ним он и делит запросы на слои.
    serverIPAddress: res?.remoteIPAddress || '',
    _transferSize: transfer ?? 0,
    _resourceType: (p.type || '').toLowerCase(),
  };
  if (fromCache) e._fromCache = fromCache;
  entries.push(e);
}

const attached = new WeakSet();

async function attach(page) {
  // К первой вкладке ведут два пути: явный вызов до перехода и событие
  // context.on('page'). Без этой отметки на ней висели бы две сессии CDP,
  // и каждое событие обрабатывалось бы дважды.
  if (attached.has(page)) return;
  attached.add(page);
  let cdp;
  try {
    cdp = await page.context().newCDPSession(page);
  } catch {
    return;
  }
  await cdp.send('Network.enable', { maxResourceBufferSize: 32 * 1024 * 1024 });
  // Playwright поднимает вкладку с выключенным HTTP-кэшем. Для записи это
  // порча замера: вся статика попадёт в неё как сетевая, хотя у живого
  // пользователя она берётся из кэша и до сервера не доходит.
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: false });

  cdp.on('Network.requestWillBeSent', (ev) => {
    // Редирект приходит как новый requestWillBeSent с телом прошлого ответа:
    // прежнюю запись надо закрыть, иначе 302 потеряется, а цепочка склеится в один запрос.
    if (ev.redirectResponse) {
      const prev = pending.get(ev.requestId);
      if (prev) {
        const t = ev.redirectResponse.timing;
        const total = t ? (ev.timestamp - t.requestTime) * 1000 : 0;
        push(prev, ev.redirectResponse, total, ev.redirectResponse.encodedDataLength);
      }
    }
    pending.set(ev.requestId, {
      requestId: ev.requestId,
      url: ev.request.url,
      method: ev.request.method,
      headers: ev.request.headers,
      postData: ev.request.postData,
      wallTime: ev.wallTime,
      type: ev.type,
    });
  });

  cdp.on('Network.requestServedFromCache', (ev) => served.add(ev.requestId));

  cdp.on('Network.responseReceived', (ev) => {
    const p = pending.get(ev.requestId);
    if (p) {
      p.response = ev.response;
      p.type = ev.type || p.type;
    }
  });

  cdp.on('Network.loadingFinished', async (ev) => {
    const p = pending.get(ev.requestId);
    if (!p) return;
    pending.delete(ev.requestId);
    const res = p.response;
    const t = res?.timing;
    const total = t ? (ev.timestamp - t.requestTime) * 1000 : 0;
    // Тела нужны кодогену: из них он вытаскивает id, которые фронт потом
    // подставляет в URL следующих запросов. Берём только JSON и только мелкие.
    const wantBody =
      /json/i.test(res?.mimeType || '') && (ev.encodedDataLength ?? 0) < BODY_CAP && !served.has(ev.requestId);
    if (wantBody) {
      try {
        const b = await cdp.send('Network.getResponseBody', { requestId: ev.requestId });
        if (!b.base64Encoded) p.body = b.body;
      } catch {
        /* тело уже вытеснено из буфера — не беда, запись остаётся валидной */
      }
    }
    push(p, res, total, ev.encodedDataLength);
  });

  cdp.on('Network.loadingFailed', (ev) => {
    const p = pending.get(ev.requestId);
    if (!p) return;
    pending.delete(ev.requestId);
    const t = p.response?.timing;
    push(p, p.response, t ? (ev.timestamp - t.requestTime) * 1000 : 0, 0, true);
  });
}

function save() {
  const har = {
    log: {
      version: '1.2',
      creator: { name: 'site-load recorder', version: '1.0' },
      pages: [],
      entries: entries.sort((a, b) => a.startedDateTime.localeCompare(b.startedDateTime)),
    },
  };
  fs.mkdirSync(path.dirname(path.resolve(OUT)), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(har, null, 1));
  console.log(`записано ${entries.length} запросов → ${OUT}`);
}

const browser = await chromium.launch({ headless: false, args: ['--start-maximized'] });
// Basic-авторизацию шлём заголовком на каждый запрос. Два очевидных способа
// не годятся:
//   • `httpCredentials` включает перехват запросов, а перехват выключает
//     HTTP-кэш браузера — запись перестаёт отличать кэш от сети (проверено:
//     кэш-попаданий 0 против 10 без него);
//   • логин с паролем в адресе работает только на первом переходе, а дальше
//     любой повторный запрос пароля упирается в стену: Playwright гасит
//     нативное окно basic-авторизации, и ввести его человеку негде.
// Заголовок ставится через `Network.setExtraHTTPHeaders`, перехват при этом
// не включается — кэш остаётся живым.
const context = await browser.newContext({
  viewport: null,
  ...(AUTH.includes(':')
    ? { extraHTTPHeaders: { Authorization: 'Basic ' + Buffer.from(AUTH).toString('base64') } }
    : {}),
});

// Вкладки, открытые по ходу записи, пишутся тоже. Единственное, что теряется, —
// самый первый запрос новой вкладки: подключиться к ней успеваем уже после того,
// как браузер отправил документ. Остальное её содержимое записывается целиком.
context.on('page', (p) => { attach(p); });
const page = await context.newPage();
// К первой вкладке подключаемся до перехода и дожидаемся подключения:
// иначе `Network.enable` успеет отработать уже после загрузки страницы,
// и в записи не будет ни документа, ни первой волны статики.
await attach(page);
await page.goto(URL, { waitUntil: 'domcontentloaded' }).catch(() => {});

console.log(`запись идёт: ${URL}`);
console.log('проходи сценарий руками; остановить — Ctrl+C или закрыть окно браузера');

let stopped = false;
async function stop() {
  if (stopped) return;
  stopped = true;
  save();
  await browser.close().catch(() => {});
  process.exit(0);
}

process.on('SIGINT', stop);
process.on('SIGTERM', stop);
browser.on('disconnected', stop);
