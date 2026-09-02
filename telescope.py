#!/usr/bin/env python3
"""Выгрузка записей Telescope за окно прогона — в вид, понятный har.py.

  python3 telescope.py --har har/файл.har --out tele.json
  python3 telescope.py --from "2026-08-25 08:39:24" --to "2026-08-25 08:44:36" --out tele.json
  python3 telescope.py --har har/файл.har --out tele.json --take 200

Адрес и доступ берутся из secrets.json (вкладка «Настройки» в морде) или из .env.
Где Telescope открыт без авторизации (дев), хватает одного адреса.

Данные забираются POST-ом на `/telescope/telescope-api/<тип>`: GET по этому пути
отдаёт HTML самой морды Telescope, а не записи. Нужны кука XSRF и заголовок
`X-XSRF-TOKEN` — их снимаем, открыв страницу Telescope перед выгрузкой.

Зачем отдельный скрипт, а не строчка внутри har.py. В Telescope запрос и его
запросы в БД — РАЗНЫЕ записи, связанные только `batch_id`: у записи типа
`request` нет поля «сколько запросов в БД», там лежат только время и статус.
Поэтому тянем оба типа и сшиваем: на выходе у каждого запроса появляются
`queries` (сколько), `query_ms` (сколько времени в БД) и `jobs`.

Время. Laravel пишет `created_at` в UTC, HAR — тоже в UTC. Окно берём с запасом
в PAD секунд с обеих сторон: запись в Telescope создаётся в конце обработки,
а HAR отмечает старт запроса, и на границе окна пары иначе теряются.
"""
import json, os, sys, datetime, urllib.request, urllib.parse, http.cookiejar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
ROOT = config.ROOT
SECRETS = config.SECRETS
PAD = 10          # секунд запаса с каждой стороны окна
TYPES = ('requests', 'queries', 'jobs')


def cfg():
    try:
        d = json.load(open(SECRETS, encoding='utf-8')).get('telescope') or {}
    except Exception:
        d = {}
    if not d.get('url'):
        d = {'url': os.environ.get('TELESCOPE_URL', ''),
             'email': os.environ.get('TELESCOPE_EMAIL', ''),
             'password': os.environ.get('TELESCOPE_PASSWORD', ''),
             'cookie': os.environ.get('TELESCOPE_COOKIE', '')}
    if not d.get('url'):
        sys.exit('Telescope не настроен: заполни TELESCOPE_* в .env '
                 'или раздел в морде → вкладка «Настройки».')
    return d


def opener(c):
    """Готовим сессию: где гейт открыт — просто берём CSRF, где закрыт — входим.

    Порядок такой: сперва пробуем открыть страницу Telescope как есть. Если она
    открылась — авторизация не нужна и пользователь не понадобится вовсе. Логин или
    готовая Cookie идут в ход, только если получили от гейта отказ.
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    base = c['url'].rstrip('/')
    hdrs = {'Accept': 'application/json', 'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest', 'Referer': base + '/telescope/requests'}
    if c.get('cookie'):
        hdrs['Cookie'] = c['cookie']
    try:
        op.open(urllib.request.Request(base + '/telescope/requests',
                                       headers={'Cookie': c['cookie']} if c.get('cookie') else {}),
                timeout=25)
        hdrs['X-XSRF-TOKEN'] = csrf(cj)
        return op, base, hdrs
    except urllib.error.HTTPError as e:
        if e.code != 403 or not c.get('email'):
            if e.code == 403:
                sys.exit('403 от гейта Laravel, а пользователя в настройках нет.')
            raise
    # Гейт пускает по сессии приложения: входим обычным флоу из .env.
    config.site_login(op, base, c['email'], c.get('password', ''), cj)
    hdrs['X-XSRF-TOKEN'] = csrf(cj)
    return op, base, hdrs


def csrf(cj):
    for k in cj:
        if k.name == 'XSRF-TOKEN':
            return urllib.parse.unquote(k.value)
    return ''


def fetch(op, base, hdrs, kind, since, take):
    """Страницы Telescope от свежих к старым, пока не уйдём раньше `since`.

    Листаем через `before=<id последней записи>` — так листает и сама морда
    Telescope. Останавливаемся, как только страница целиком старше окна:
    записи идут по убыванию времени, дальше смотреть нечего.
    """
    out, before = [], None
    url = '%s/telescope/telescope-api/%s' % (base, kind)
    while True:
        body = {'take': take}
        if before is not None:
            # Именно `before`: `before_sequence` Telescope молча игнорирует
            # и отдаёт ту же первую страницу — выгрузка уходит в вечный цикл.
            body['before'] = before
        r = urllib.request.Request(url, data=json.dumps(body).encode(),
                                   headers=hdrs, method='POST')
        try:
            page = json.load(op.open(r, timeout=40))
        except urllib.error.HTTPError as e:
            if e.code == 403:
                sys.exit('403 от гейта Laravel: доступ к Telescope закрыт.')
            if e.code == 419:
                sys.exit('419: не принят CSRF-токен. Открой Telescope в браузере и '
                         'вставь строку Cookie в настройки.')
            raise
        rows = page.get('entries', page) if isinstance(page, dict) else page
        if not rows:
            return out
        out += rows
        last = rows[-1]
        when = last.get('created_at')
        if when and parse_dt(when, 0) < since:
            return out
        before = last.get('sequence') or last.get('id')
        if before is None or len(out) > 20000:
            return out


def parse_dt(s, off_h=0):
    """Метка Telescope → UTC. `off_h` — часовой сдвиг сервера, см. detect_offset."""
    s = str(s).replace('T', ' ')[:19]
    d = datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    return d.replace(tzinfo=datetime.timezone.utc) - datetime.timedelta(hours=off_h)


def detect_offset(op, base, hdrs):
    """На сколько часов метки Telescope сдвинуты относительно UTC.

    Laravel пишет `created_at` в часовом поясе приложения, а не в UTC, и на этом
    стенде это +3. Если сдвиг не учесть, окно записи не пересечётся с окном
    Telescope вовсе и сверка молча даст ноль пар — молчаливый ноль хуже ошибки,
    поэтому определяем сдвиг сами: берём самую свежую запись и сравниваем
    её метку с текущим временем UTC, округляя до получаса.
    """
    rows = fetch(op, base, hdrs, 'requests',
                 datetime.datetime.now(datetime.timezone.utc), 1)
    if not rows:
        return 0
    newest = parse_dt(rows[0].get('created_at'), 0)
    now = datetime.datetime.now(datetime.timezone.utc)
    return round((newest - now).total_seconds() / 1800.0) / 2.0


def content(row):
    c = row.get('content', row)
    return json.loads(c) if isinstance(c, str) else (c or {})


def har_window(path):
    es = json.load(open(path, encoding='utf-8'))['log']['entries']
    ts = [datetime.datetime.fromisoformat(e['startedDateTime'].replace('Z', '+00:00'))
          for e in es]
    return min(ts), max(ts)


def main():
    a = sys.argv[1:]
    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a else default
    out_path = opt('--out', 'tele.json')
    take = int(opt('--take', '100'))
    if opt('--har'):
        t0, t1 = har_window(opt('--har'))
    elif opt('--from') and opt('--to'):
        t0, t1 = parse_dt(opt('--from')), parse_dt(opt('--to'))
    else:
        print(__doc__)
        return 1
    since = t0 - datetime.timedelta(seconds=PAD)
    until = t1 + datetime.timedelta(seconds=PAD)
    print('окно UTC: %s → %s' % (since.isoformat()[:19], until.isoformat()[:19]))

    c = cfg()
    op, base, hdrs = opener(c)
    off = detect_offset(op, base, hdrs)
    if off:
        print('метки Telescope сдвинуты на %+g ч от UTC — учитываю' % off)
    raw = {}
    for kind in TYPES:
        try:
            raw[kind] = fetch(op, base, hdrs, kind, since + datetime.timedelta(hours=off), take)
            print('  %-9s получено %d' % (kind, len(raw[kind])))
        except Exception as e:
            raw[kind] = []
            print('  %-9s не читается: %s' % (kind, str(e)[:80]))

    # Сшивка: запросы в БД и задачи привязаны к запросу общим batch_id.
    q_by_batch, j_by_batch = {}, {}
    for row in raw.get('queries', []):
        b = row.get('batch_id')
        if not b:
            continue
        d = q_by_batch.setdefault(b, {'n': 0, 'ms': 0.0, 'slow': 0})
        cc = content(row)
        d['n'] += 1
        try: d['ms'] += float(cc.get('time') or 0)
        except Exception: pass
        if cc.get('slow'): d['slow'] += 1
    for row in raw.get('jobs', []):
        b = row.get('batch_id')
        if b: j_by_batch[b] = j_by_batch.get(b, 0) + 1

    entries, skipped = [], 0
    for row in raw.get('requests', []):
        when = parse_dt(row.get('created_at') or '1970-01-01 00:00:00', off)
        if not (since <= when <= until):
            skipped += 1
            continue
        cc = content(row)
        b = row.get('batch_id')
        q = q_by_batch.get(b, {})
        entries.append({
            'batch_id': b,
            'created_at': when.isoformat(),
            'content': {
                'uri': cc.get('uri', ''),
                'method': cc.get('method', ''),
                'response_status': cc.get('response_status'),
                'duration': cc.get('duration'),
                'memory': cc.get('memory'),
                'controller_action': cc.get('controller_action'),
                # ip нужен, чтобы отделить свою сессию от чужого трафика на общем
                # стенде: на деве параллельно ходят и другие люди, и интеграции.
                'ip': cc.get('ip_address'),
                'hostname': cc.get('hostname'),
                'queries': q.get('n', 0),
                'query_ms': round(q.get('ms', 0.0), 1),
                'slow_queries': q.get('slow', 0),
                'jobs': j_by_batch.get(b, 0),
            }})
    entries.sort(key=lambda x: x['created_at'])
    json.dump({'entries': entries}, open(out_path, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('в окно попало %d запросов (мимо окна отброшено %d) → %s'
          % (len(entries), skipped, out_path))
    if entries and not any(e['content']['queries'] for e in entries):
        print('ВНИМАНИЕ: ни у одного запроса нет запросов в БД. Скорее всего, у Telescope')
        print('выключен наблюдатель QueryWatcher — тогда столбец «запросов в БД» будет пуст.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
