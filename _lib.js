/* Общие хелперы сценариев: заголовки, паузы, пачки запросов, загрузка файлов
 * и вход. Ими пользуются и готовые сценарии из корня, и всё, что собирает
 * кодоген из записи браузера.
 *
 * Ни одного адреса и ни одного пути здесь нет: что такое «вход» на конкретном
 * сайте, описывают переменные окружения (см. .env.example).
 */
import http from 'k6/http';
import { sleep } from 'k6';
import encoding from 'k6/encoding';

export { API, SITE } from './env.js';
import { API, SITE } from './env.js';

// Множитель пауз: 1 — как у живого человека, 0 — без пауз вообще.
// Смоук проверяет проходимость сценария, а не поведение человека: держать
// в нём настоящие паузы значит ждать столько же, сколько длилась запись.
const THINK    = Number(__ENV.THINK ?? (__ENV.SMOKE ? 0 : 1));
const IDLE_CAP = Number(__ENV.IDLE_CAP ?? 0);
const PREFLIGHT = (__ENV.PREFLIGHT ?? '1') !== '0';
export const STATIC = (__ENV.STATIC ?? '1') !== '0';

// Если фронт закрыт basic-авторизацией, без заголовка вся статика ответит 401.
const SITE_AUTH = __ENV.SITE_AUTH ?? '';

/* Имя куки и заголовка с CSRF-токеном: у каждого фреймворка своё.
   Пусто — значит сайт токен в куке не носит, и заголовок не ставим. */
const XSRF_COOKIE = __ENV.XSRF_COOKIE ?? 'XSRF-TOKEN';
const XSRF_HEADER = __ENV.XSRF_HEADER ?? 'X-XSRF-TOKEN';

export function xsrf() {
  if (!XSRF_COOKIE) return '';
  const c = http.cookieJar().cookiesForURL(API + '/');
  return c[XSRF_COOKIE] ? decodeURIComponent(c[XSRF_COOKIE][0]) : '';
}

// Заголовки JSON-запроса: X-XSRF-TOKEN добавляем только если токен есть.
function jsonHeaders() {
  const h = { Accept: 'application/json', 'Content-Type': 'application/json',
              Origin: SITE, Referer: SITE + '/' };
  const t = xsrf();
  if (t && XSRF_HEADER) h[XSRF_HEADER] = t;
  return h;
}

/* Хост каждого запроса помечаем тегом: в отчёте по нему видно, что ушло в API,
   что на фронт, а что вообще на сторону (картинки часто лежат в объектном
   хранилище). Из имени запроса это не выводится, а нагрузка у них разная. */
const hostOf = (u) => (String(u).match(/^https?:\/\/([^/]+)/) || [, ''])[1];
const HOST_API = hostOf(API);
const HOST_SITE = hostOf(SITE);

// Referer и Origin шлём всегда: многие бэкенды без них считают запрос чужим сайтом.
export const J = (name) => ({ headers: jsonHeaders(), tags: { name, host: HOST_API } });

export const G = (name, host) => {
  const h = { Accept: '*/*', Referer: SITE + '/' };
  if (SITE_AUTH) h.Authorization = 'Basic ' + encoding.b64encode(SITE_AUTH);
  return { headers: h, tags: { name, host: host || HOST_SITE } };
};

/* Подстановка id стенда в путь: `/api/v1/items/{ITEM_ID}` → значение
   переменной ITEM_ID. Если в переменной список через запятую — берётся
   случайный элемент, поэтому один и тот же сценарий ходит по разным объектам,
   а не долбит один. Карту id раздаёт pool.json (см. pool.example.json).

   Незаполненная переменная оставляется как есть: пусть сервер ответит 404 и
   это будет видно в отчёте — молча подставленная пустота даёт другой путь и
   другой замер. */
export function resolve(path) {
  return String(path).replace(/\{([A-Z0-9_]+)\}/g, (m, name) => {
    const v = __ENV[name];
    if (!v) return m;
    const parts = v.split(',');
    return parts[Math.floor(Math.random() * parts.length)].trim();
  });
}

export function think(sec) {
  let s = sec * THINK;
  if (IDLE_CAP > 0 && s > IDLE_CAP) s = IDLE_CAP;
  if (s > 0) sleep(s);
}

// Браузер шлёт OPTIONS перед каждым POST на чужой домен — по нагрузке он не бесплатный.
export function preflight(url) {
  if (!PREFLIGHT) return;
  http.request('OPTIONS', url, null, {
    headers: { Origin: SITE, 'Access-Control-Request-Method': 'POST',
               'Access-Control-Request-Headers': ('content-type' + (XSRF_HEADER ? ',' + XSRF_HEADER.toLowerCase() : '')) },
    tags: { name: 'preflight', host: hostOf(url) || HOST_API },
  });
}

// Пачка параллельных запросов — так грузится любая страница SPA. Последовательно
// те же запросы дали бы вдвое меньший мгновенный пик, чем настоящий браузер.
export function burst(reqs) {
  return http.batch(reqs.map(([url, name]) => ({ method: 'GET', url, params: J(name) })));
}

export function statics(paths, name) {
  if (!STATIC || !paths.length) return;
  // Путь без схемы — это файл самого фронта; полный адрес значит чужой хост
  // (в записи так приходят картинки из объектного хранилища). Клеить SITE
  // с таким адресом нельзя: вышел бы несуществующий путь и стена 404.
  http.batch(paths.map((p) => {
    const ext = /^https?:\/\//.test(p);
    return { method: 'GET', url: ext ? p : SITE + p, params: G(name, ext ? hostOf(p) : HOST_SITE) };
  }));
}

/* ---------- загрузка файлов ----------
   Записанное тело multipart переносить в сценарий дословно нельзя: внутри
   лежит сам файл, и у одной сдачи ДЗ это мегабайт двоичных данных. В исходнике
   он и читаться перестал бы, и репозиторий раздул, и до сервера дошёл бы
   испорченным. Поэтому берём настоящий файл подходящего типа из mocks/,
   а если его нет — набивку нужной длины: серверу важны байты, а не картинка. */
function mock(p) {
  try { return open(p, 'b'); } catch (e) { return null; }
}
const MOCKS = {
  'image/jpeg': mock('./mocks/photo-1.jpg'),
  'application/pdf': mock('./mocks/document.pdf'),
};

export function upload(mime, filename, size) {
  const m = MOCKS[mime] || MOCKS['image/jpeg'];
  return http.file(m || 'x'.repeat(Math.max(1, size || 1024)), filename, mime);
}

// Параметры для multipart: Content-Type ставит сам k6 вместе с границей,
// поэтому свой заголовок здесь только помешал бы.
export const M = (name) => {
  const h = jsonHeaders();
  delete h['Content-Type'];
  return { headers: h, tags: { name, host: HOST_API } };
};

/* ---------- вход ----------

   Как устроен вход, инструмент не знает и знать не должен: путь, тело и
   необязательный второй шаг описываются переменными окружения.

     CSRF_PATH           GET перед входом, если сайт выдаёт токен отдельным
                         запросом (например `/sanctum/csrf-cookie`)
     LOGIN_PATH          POST входа — обязателен для login()
     LOGIN_BODY          тело входа, JSON-шаблон.
                         По умолчанию {"email":"$USER","password":"$PASS"}
     LOGIN_CONFIRM_PATH  второй шаг, если вход двухступенчатый
     LOGIN_CONFIRM_BODY  тело второго шага; строки вида "$resp.data.token"
                         подставляются из ответа первого шага
     USER_ID_PATH        где в ответе лежит id вошедшего (по умолчанию data.id)

   Подстановки в шаблонах ровно три: $USER, $PASS и $resp.<путь через точку>.
   Числовой сегмент пути — индекс массива: $resp.data.options.0.id. */

const LOGIN_PATH = __ENV.LOGIN_PATH || '';
const CSRF_PATH = __ENV.CSRF_PATH || '';
const CONFIRM_PATH = __ENV.LOGIN_CONFIRM_PATH || '';
const LOGIN_BODY = JSON.parse(__ENV.LOGIN_BODY || '{"email":"$USER","password":"$PASS"}');
const CONFIRM_BODY = JSON.parse(__ENV.LOGIN_CONFIRM_BODY || '{}');
const USER_ID_PATH = __ENV.USER_ID_PATH || 'data.id';

// Значение по пути через точку. Отсутствующее звено даёт undefined, а не падение:
// сценарий должен сообщить «вход не получился», а не умереть на первой итерации.
export function dig(obj, path) {
  return String(path).split('.').reduce(
    (o, k) => (o == null ? o : o[/^\d+$/.test(k) ? Number(k) : k]), obj);
}

function fill(tpl, acc, resp) {
  if (typeof tpl === 'string') {
    if (tpl === '$USER') return acc.email;
    if (tpl === '$PASS') return acc.password;
    if (tpl.indexOf('$resp.') === 0) return dig(resp, tpl.slice(6));
    return tpl;
  }
  if (Array.isArray(tpl)) return tpl.map((v) => fill(v, acc, resp));
  if (tpl && typeof tpl === 'object') {
    const out = {};
    for (const k in tpl) out[k] = fill(tpl[k], acc, resp);
    return out;
  }
  return tpl;
}

/* Вход в текущем cookie-jar. Возвращает разобранное тело последнего шага
   (по нему сценарий берёт id вошедшего) либо null, если войти не удалось. */
export function login(acc) {
  if (!LOGIN_PATH) throw new Error(
    'не задан LOGIN_PATH: заполни .env (см. .env.example) или передай --env LOGIN_PATH=…');
  if (CSRF_PATH) http.get(API + CSRF_PATH, J('csrf'));

  preflight(API + LOGIN_PATH);
  let r = http.post(API + LOGIN_PATH, JSON.stringify(fill(LOGIN_BODY, acc, null)), J('login'));
  if (r.status !== 200) return null;
  if (!CONFIRM_PATH) return r.json() || {};

  const first = r.json();
  preflight(API + CONFIRM_PATH);
  r = http.post(API + CONFIRM_PATH, JSON.stringify(fill(CONFIRM_BODY, acc, first)),
                J('confirm-login'));
  return r.status === 200 ? (r.json() || {}) : null;
}

// id вошедшего — там, где он лежит на этом сайте.
export const userId = (me) => dig(me, USER_ID_PATH);

/* Вход один раз на весь прогон.

   k6 чистит cookie-jar в начале КАЖДОЙ итерации, поэтому сценарии, которые
   входят в setup(), должны носить куку заголовком вручную — иначе со второй
   итерации они меряют скорость выдачи 401, а не то, ради чего затевались. */
export function loginOnce(acc) {
  const jar = http.cookieJar();
  const me = login(acc);
  if (!me) throw new Error('вход не удался: проверь LOGIN_PATH, LOGIN_BODY и профиль в .env');
  const c = jar.cookiesForURL(API + '/');
  return {
    cookie: Object.keys(c).map((k) => k + '=' + c[k][0]).join('; '),
    xsrf: XSRF_COOKIE && c[XSRF_COOKIE] ? decodeURIComponent(c[XSRF_COOKIE][0]) : '',
    me,
  };
}

// Заголовки для сессии, полученной в setup(): кука ставится руками.
export const S = (s, name) => {
  const h = { Accept: 'application/json', 'Content-Type': 'application/json',
              Cookie: s.cookie, Origin: SITE, Referer: SITE + '/' };
  if (s.xsrf && XSRF_HEADER) h[XSRF_HEADER] = s.xsrf;
  return { headers: h, tags: { name, host: HOST_API } };
};

// Выход, если он у сайта есть: без LOGOUT_PATH шаг просто пропускается.
export function logout() {
  const p = __ENV.LOGOUT_PATH || '';
  if (p) http.post(API + p, null, J('logout'));
}
