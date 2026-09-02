#!/usr/bin/env python3
"""Разбор HAR-записи браузера: сколько запросов реально доходит до бэка.

  python3 har.py <файл.har> [ещё.har ...]
  python3 har.py <файл.har> --csv out.csv     выгрузка для сверки с Telescope
  python3 har.py <файл.har> --telescope t.json  сверка с Telescope

Вопрос, ради которого написано: в записи сессии видно N запросов, но нагрузку
на сервер создаёт не N. Часть отдаётся из кэша браузера и до сети не доходит
вовсе, часть — OPTIONS-предзапросы CORS (дешёвые, но не бесплатные), часть —
статика фронта (её отдаёт nginx, не будя приложение), и только остаток
идёт в бэкенд.

Разделение по слоям:
  cache      — браузер отдал сам, сервер не узнал о запросе
  aborted    — запрос отменён (страница ушла раньше ответа)
  websocket  — апгрейд соединения, считается один раз, а живёт всю сессию
  static     — ушло в сеть, но отдаёт веб-сервер: приложение не просыпается
  preflight  — OPTIONS, отвечает приложение, но без работы
  api        — вот это и есть нагрузка на бэк

Как определяется кэш. Chrome пишет это явно (`_fromCache`), Firefox — нет,
и у него признаком служит пара «нет serverIPAddress и time == 0»: соединения
не было. Признак проверен на записях 24.08: под него попадает ровно статика
с `max-age=31536000` и ни одного ответа API. Если запись сделана с включённой
галкой «Disable cache», кэша не будет вообще — скрипт про это предупредит.
"""
import json, os, sys, re, csv, datetime, collections
from urllib.parse import urlparse

import config   # подтягивает .env

# Сетевая база до стенда: столько уходит на дорогу туда-обратно даже у запроса,
# который ничего не считает. Вычитается из времени ожидания, чтобы получить
# оценку серверного времени. Измеряется просто: время самого пустого ответа
# в записи (редирект, 204, статика из nginx). Ноль — не вычитать вовсе.
NET_BASE = int(os.environ.get('NET_BASE') or 0)

# Сколько фронт держит успешный ответ в своём кэше и не повторяет запрос.
# Нужно, чтобы отличать законный повтор после истечения кэша от повтора,
# которого быть не должно.
FRONT_CACHE = int(os.environ.get('FRONT_CACHE') or 60)

# По каким кускам пути запрос считается запросом к бэкенду, а не статикой.
# Пути входа добавляются сами: они у каждого сайта свои, а к API относятся.
API_HINTS = tuple(x for x in (
    [h.strip() for h in (os.environ.get('API_HINTS') or '/api/,/graphql,/rpc/').split(',')]
    + [os.environ.get('CSRF_PATH') or '', os.environ.get('LOGIN_PATH') or '']) if x)
STATIC_EXT = ('.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.webp', '.woff',
              '.woff2', '.ttf', '.ico', '.gif', '.mp4', '.webm', '.json.gz')


def header(entry, name, where='response'):
    for h in entry[where].get('headers', []):
        if h['name'].lower() == name:
            return h['value']
    return None


def is_cache_hit(e):
    """Отдано браузером из своей памяти или диска, до сети не дошло."""
    if e.get('_fromCache'):          # Chrome: 'memory' | 'disk'
        return True
    if e.get('cache', {}).get('afterRequest') and e['response']['status'] == 200 \
            and e.get('_transferSize') == 0:
        return True
    # Firefox: явного признака нет, но у кэш-попадания нет ни адреса сервера,
    # ни времени. Статус при этом 200 — браузер повторяет прошлый ответ.
    if not e.get('serverIPAddress') and e['time'] == 0 and e['response']['status'] == 200:
        return True
    return False


def classify(e):
    st = e['response']['status']
    url = e['request']['url']
    path = urlparse(url).path
    if st == 101 or (header(e, 'upgrade') or '').lower() == 'websocket':
        return 'websocket'
    if st == 0:
        return 'aborted'
    if is_cache_hit(e):
        return 'cache'
    if e['request']['method'] == 'OPTIONS':
        return 'preflight'
    if st == 304:
        return 'revalidate'
    if any(h in path for h in API_HINTS):
        return 'api'
    if path.lower().endswith(STATIC_EXT) or path == '/' or '/assets/' in path:
        return 'static'
    # Документ SPA и всё, что не опознали, — тоже отдаёт nginx.
    return 'static'


def norm_path(path):
    """`/api/v1/catalog/items/2259` → `/api/v1/catalog/items/{id}`."""
    parts = []
    for p in path.split('/'):
        parts.append('{id}' if p.isdigit() else p)
    return '/'.join(parts)


def size_of(e):
    """Сколько байт реально прошло по сети.

    `_transferSize` (Chrome) — то, что нужно: он учитывает сжатие и заголовки.
    Firefox его не пишет, там остаётся `bodySize`, а у ответа из кэша и он
    нулевой — тогда берём распакованный размер, чтобы показать, сколько
    кэш сэкономил.
    """
    t = e['response'].get('_transferSize')
    if t and t > 0:
        return t
    body = e['response'].get('bodySize', 0)
    if body and body > 0:
        return body
    return e['response'].get('content', {}).get('size', 0) or 0


def started(e):
    s = e['startedDateTime'].replace('Z', '+00:00')
    return datetime.datetime.fromisoformat(s)


def load(paths):
    entries = []
    creators = set()
    for p in paths:
        with open(p, encoding='utf-8') as f:
            log = json.load(f)['log']
        creators.add(log.get('creator', {}).get('name', '?'))
        for e in log['entries']:
            e['_file'] = os.path.basename(p)
            entries.append(e)
    entries.sort(key=started)
    return entries, creators


def bar(share, width=28):
    return '#' * int(round(share * width))


def report(entries, creators):
    groups = collections.defaultdict(list)
    for e in entries:
        groups[classify(e)].append(e)

    total = len(entries)
    print('=' * 72)
    print('Запись: %d запросов, браузер: %s' % (total, ', '.join(sorted(creators))))
    print('=' * 72)

    if not groups['cache']:
        print('\n!! Кэш-попаданий в записи НЕТ. Скорее всего запись сделана с включённой')
        print('   галкой «Disable cache» — тогда доля кэша неизвестна, а не равна нулю.')
        print('   Перепиши сессию с выключенной галкой, иначе статика посчитается дважды.')

    order = ['cache', 'aborted', 'websocket', 'static', 'revalidate', 'preflight', 'api']
    label = {'cache': 'кэш браузера (до сети не дошло)',
             'aborted': 'отменено браузером',
             'websocket': 'websocket-апгрейд',
             'static': 'статика фронта (отдаёт веб-сервер)',
             'revalidate': 'перепроверка кэша (304)',
             'preflight': 'OPTIONS-предзапросы CORS',
             'api': 'API — работа приложения'}
    print('\nПо слоям:')
    for k in order:
        n = len(groups[k])
        if not n:
            continue
        mb = sum(size_of(e) for e in groups[k]) / 1048576.0
        print('  %-34s %4d  %5.1f%%  %6.2f МБ  %s'
              % (label[k], n, 100.0 * n / total, mb, bar(n / total)))

    net = sum(len(groups[k]) for k in ('static', 'revalidate', 'preflight', 'api'))
    php = len(groups['preflight']) + len(groups['api'])
    print('\nИтог: из %d запросов страницы' % total)
    print('  %d ушло в сеть (%.0f%%)' % (net, 100.0 * net / total))
    print('  %d дошло до приложения (%.0f%%), из них %d — реальная работа'
          % (php, 100.0 * php / total, len(groups['api'])))
    print('  на каждый запрос к API приходится %.1f запроса, не создающего нагрузки'
          % ((total - len(groups['api'])) / max(1, len(groups['api']))))

    # Пересчёт в единицу, которой считают нагрузку: сколько один живой человек
    # даёт запросов в минуту. Через неё же считается и обратное — сколько людей
    # умещается в измеренный потолок стенда по rps. Потолок берётся из
    # CEILING_RPS: пока разгон не сделан, называть его нечем, и обратный счёт
    # не печатается вовсе — придуманное число хуже отсутствующего.
    span = (started(entries[-1]) - started(entries[0])).total_seconds()
    php_rate = php / span * 60 if span else 0
    ceiling = float(os.environ.get('CEILING_RPS') or 0)
    print('\n  Записанная сессия длилась %.0f с (%.1f мин).' % (span, span / 60))
    print('  Один человек за это время дал %.1f запроса в минуту до приложения (%.2f rps).'
          % (php_rate, php / span if span else 0))
    if php_rate and ceiling:
        print('  Обратный счёт: при измеренном потолке %g rps столько людей' % ceiling)
        print('  одновременно на сайте — %d, если все ведут себя как в этой записи.'
              % int(ceiling / (php / span)))
        print('  Оговорка: в записи человек кликал непрерывно. Реальный пользователь')
        print('  часть времени читает страницу и не шлёт ничего — эта оценка нижняя.')
    elif php_rate:
        print('  Обратный счёт «сколько людей выдержит сервер» появится здесь, когда')
        print('  потолок будет измерен разгоном и записан в CEILING_RPS.')

    # --- Проверка деления по заголовку самого сервера ----------------------
    # `X-Powered-By` ставит сам сервер приложений (PHP-FPM — всегда): если
    # заголовок есть, приложение точно запускалось. Это независимая проверка
    # деления выше — и заодно ответ на вопрос «а точно ли статику отдаёт
    # веб-сервер», не выходя из HAR. Если бэкенд заголовок не ставит, проверка
    # просто ничего не найдёт — деление остаётся на эвристике из API_HINTS.
    php_hdr = [e for e in entries if header(e, 'x-powered-by')]
    php_layers = collections.Counter(classify(e) for e in php_hdr)
    # «Ушло в сеть» определяем по слою, а не по наличию serverIPAddress: Chrome
    # проставляет адрес и у ответа, поднятого с диска, и без этого вся кэш-статика
    # попадала в отчёт как «сеть без PHP» и ломала проверку.
    net_no_php = collections.Counter(classify(e) for e in entries
                                     if not header(e, 'x-powered-by')
                                     and classify(e) in ('static', 'revalidate', 'preflight', 'api'))
    print('\n' + '-' * 72)
    print('Проверка: кого разбудило приложение (по заголовку X-Powered-By)')
    print('-' * 72)
    print('  заголовок есть у %d ответов: %s'
          % (len(php_hdr), ', '.join('%s %d' % kv for kv in php_layers.most_common())))
    print('  ушло в сеть без заголовка: %s'
          % (', '.join('%s %d' % kv for kv in net_no_php.most_common()) or '—'))
    if set(php_layers) <= {'api', 'preflight', 'websocket'} and set(net_no_php) <= {'static', 'revalidate'}:
        print('  → деление сходится: приложение запускалось ровно на том, что отнесено к бэку.')
    else:
        print('  → деление НЕ сходится, разбирать вручную: либо статику отдаёт приложение,')
        print('    либо часть API проходит мимо (кэш nginx, отдельный сервис).')

    # --- Кэшируемость: что можно снять с сервера --------------------------
    print('\n' + '-' * 72)
    print('Кэшируемость ответов, ушедших в сеть')
    print('-' * 72)
    cc = collections.Counter()
    for e in groups['api']:
        cc[header(e, 'cache-control') or '(заголовка нет)'] += 1
    for v, n in cc.most_common():
        print('  API   %-28s %3d' % (v[:28], n))
    if all('no-cache' in v or 'no-store' in v or 'заголовка' in v for v in cc):
        print('  → ни один ответ API браузер закэшировать не может: каждый вызов')
        print('    из записи — это отдельная работа сервера, скидки на кэш нет.')

    seen = {}
    repeats = []
    for e in entries:
        if classify(e) not in ('static', 'api'):
            continue
        key = (e['request']['method'], e['request']['url'])
        if key in seen:
            prev_at, prev_ms = seen[key]
            gap = (started(e) - prev_at).total_seconds()
            repeats.append((key, gap, classify(e), gap - prev_ms / 1000.0))
        seen[key] = (started(e), e['time'])
    # Статика делится на две разные истории: закэшированную по-человечески
    # и запрещённую к кэшу заголовком. Вторая возвращается по сети всегда.
    nostore, longlived, other = [], [], []
    for e in groups['static'] + groups['revalidate']:
        v = (header(e, 'cache-control') or '').lower()
        if 'no-store' in v or 'no-cache' in v:
            nostore.append(e)
        elif 'max-age' in v and int((re.search(r'max-age=(\d+)', v) or [0, 0])[1]) > 3600:
            longlived.append(e)
        else:
            other.append(e)
    print('\n  Статика, ушедшая в сеть: %d' % (len(groups['static']) + len(groups['revalidate'])))
    print('    %3d с длинным max-age — в сеть пошли только в первый раз' % len(longlived))
    print('    %3d с `no-store` — кэшировать запрещено, качаются каждый раз' % len(nostore))
    if other:
        print('    %3d без внятного заголовка' % len(other))
    if nostore:
        # Впустую потрачено не всё, что помечено no-store: первую загрузку
        # пришлось бы сделать в любом случае. Лишнее — это ПОВТОРЫ, и считать
        # надо только их, иначе цифра завышена вдвое и разговор с разработчиком
        # начинается с неверного числа.
        seen_url, waste, waste_n = set(), collections.Counter(), 0
        first_mb = 0
        for e in nostore:
            u = e['request']['url']
            if u in seen_url:
                waste[urlparse(u).path] += size_of(e)
                waste_n += 1
            else:
                seen_url.add(u)
                first_mb += size_of(e)
        print('    первая загрузка (неизбежная): %.2f МБ' % (first_mb / 1048576.0))
        print('    ПОВТОРЫ по сети: %.2f МБ и %d лишних запросов —'
              % (sum(waste.values()) / 1048576.0, waste_n))
        print('    вот это и есть цена отсутствующего кэша:')
        for pth, b in waste.most_common(6):
            print('      %6.2f МБ  %s' % (b / 1048576.0, pth))

    if repeats:
        print('\n  Повторно скачано по сети (тот же адрес второй раз): %d' % len(repeats))
        stat_rep = [r for r in repeats if r[2] == 'static']
        if stat_rep:
            print('  из них статики %d — это промах кэша, лечится заголовками nginx:' % len(stat_rep))
            for (m, u), dt, _, _idle in stat_rep[:5]:
                print('    +%6.1f с  %s' % (dt, urlparse(u).path))
        api_rep = [r for r in repeats if r[2] == 'api']
        if api_rep:
            print('  из них API %d — фронт спрашивает одно и то же дважды:' % len(api_rep))
            for (m, u), dt, _, _idle in api_rep[:8]:
                print('    +%6.1f с  %s %s' % (dt, m, urlparse(u).path))

    # --- Кэш фронта на минуту: работает ли он на самом деле ---------------
    # Фронт держит успешный ответ FRONT_CACHE минуту и в это окно повторно
    # не спрашивает — так задумано. Проверяем по записи: повтор того же самого
    # адреса раньше, чем истекло окно, означает, что на этом ключе кэш не сыграл
    # и сервер сделал работу заново. Ключ — ПОЛНЫЙ адрес вместе с параметрами:
    # `?dateFrom` соседних недель — это разные запросы, а не повтор.
    api_rep = [r for r in repeats if r[2] == 'api']
    if api_rep:
        inflight = [r for r in api_rep if r[3] < 0]
        inside = [r for r in api_rep if 0 <= r[3] and r[1] < FRONT_CACHE]
        expired = [r for r in api_rep if r[1] >= FRONT_CACHE]
        print('\n' + '-' * 72)
        print('Кэш фронта: обещано «повтора не будет %d с после успешного ответа»' % FRONT_CACHE)
        print('-' * 72)
        print('  повторов одного адреса всего: %d' % len(api_rep))
        print('    %3d ушло, пока первый ответ ещё летел — дедуп параллельных вызовов'
              % len(inflight))
        print('    %3d ВНУТРИ окна кэша — сервер сделал работу заново' % len(inside))
        print('    %3d позже окна — кэш истёк, повтор законный' % len(expired))
        if inside:
            print('  Внутри окна (сколько прошло с момента, как пришёл прошлый ответ):')
            for (m, u), gap, _, idle in sorted(inside, key=lambda r: r[3])[:10]:
                q = urlparse(u).query
                print('    %5.1f с  %s%s' % (idle, urlparse(u).path, '?' + q[:34] if q else ''))
            print('  Повтор через доли секунды — это обычно префетч по наведению и следом')
            print('  запрос самой страницы: ответ префетча уже пришёл, но лёг под другим')
            print('  ключом, и рендер сходил ещё раз. На нагрузку это умножение вдвое.')

    # --- Кто именно нагружает бэк -----------------------------------------
    print('\n' + '-' * 72)
    print('Запросы к API: сколько раз и почём (wait — ожидание ответа целиком,')
    print('сервер — за вычетом сетевой базы %d мс)' % NET_BASE)
    print('-' * 72)
    agg = collections.defaultdict(lambda: {'n': 0, 'wait': [], 'bytes': 0})
    for e in groups['api']:
        k = '%s %s' % (e['request']['method'], norm_path(urlparse(e['request']['url']).path))
        a = agg[k]
        a['n'] += 1
        a['wait'].append(e['timings'].get('wait', 0))
        a['bytes'] += size_of(e)
    rows = sorted(agg.items(), key=lambda kv: -sum(kv[1]['wait']))
    total_wait = sum(sum(v['wait']) for v in agg.values()) or 1
    print('  %-52s %3s %7s %7s %6s' % ('запрос', 'n', 'wait', 'сервер', 'доля'))
    for k, v in rows[:25]:
        med = sorted(v['wait'])[len(v['wait']) // 2]
        print('  %-52s %3d %6d мс %6d мс %5.1f%%'
              % (k[:52], v['n'], med, max(0, med - NET_BASE), 100.0 * sum(v['wait']) / total_wait))
    print('\n  Всего ожидания API за сессию: %.1f с' % (total_wait / 1000.0))

    # Сессия: сервер переписывает куку на каждом ответе — это скрытая работа
    with_cookie = sum(1 for e in groups['api'] if header(e, 'set-cookie'))
    if with_cookie:
        print('\n  Set-Cookie в %d из %d ответов API — сессия переписывается почти'
              % (with_cookie, len(groups['api'])))
        print('  на каждом запросе. Это запись в хранилище сессий на каждый вызов,')
        print('  её не видно в HAR, но она видна в Telescope (redis/запросы к БД).')
    return groups


def dump_csv(entries, path):
    """Выгрузка для сверки с Telescope: точное время старта и ответа каждого вызова."""
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['started_utc', 'ended_utc', 'layer', 'method', 'path', 'query',
                    'status', 'total_ms', 'wait_ms', 'bytes'])
        for e in entries:
            st = started(e)
            u = urlparse(e['request']['url'])
            w.writerow([st.isoformat(),
                        (st + datetime.timedelta(milliseconds=e['time'])).isoformat(),
                        classify(e), e['request']['method'], u.path, u.query,
                        e['response']['status'], round(e['time']),
                        round(e['timings'].get('wait', 0)), size_of(e)])
    print('\nВыгружено для сверки: %s (%d строк)' % (path, len(entries)))


def load_telescope(path):
    """Записи Telescope из любого удобного вида выгрузки.

    Понимает два: ответ `/telescope/telescope-api/requests` (ключ `entries`,
    полезное — в `content`) и прямой дамп таблицы `telescope_entries`,
    где `content` лежит строкой JSON.
    """
    raw = json.load(open(path, encoding='utf-8'))
    rows = raw.get('entries', raw) if isinstance(raw, dict) else raw
    out = []
    for r in rows:
        c = r.get('content', r)
        if isinstance(c, str):
            c = json.loads(c)
        when = r.get('created_at') or c.get('created_at')
        if not when:
            continue
        when = datetime.datetime.fromisoformat(str(when).replace('Z', '+00:00'))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        out.append({'at': when,
                    'uri': c.get('uri', ''),
                    'method': c.get('method', ''),
                    'status': c.get('response_status'),
                    'duration': float(c.get('duration') or 0),
                    'queries': c.get('queries'),
                    'query_ms': float(c.get('query_ms') or 0),
                    'slow': c.get('slow_queries') or 0,
                    'jobs': c.get('jobs') or 0,
                    'batch': r.get('batch_id') or c.get('batch_id')})
    out.sort(key=lambda x: x['at'])
    return out


def correlate(entries, tele, window=3.0):
    """Сводит запрос из браузера с записью Telescope по времени, методу и пути.

    Совпадением считается запись Telescope того же метода и пути, начавшаяся
    в пределах `window` секунд от старта браузерного запроса. Это надёжно,
    только если в момент записи по стенду больше никто не ходил, — иначе
    в окно попадут чужие вызовы. Поэтому несовпадения печатаются отдельно,
    а не прячутся.
    """
    print('\n' + '=' * 72)
    print('Сверка с Telescope: %d записей на бэке' % len(tele))
    print('=' * 72)
    used = set()
    matched, missed = [], []
    for e in entries:
        layer = classify(e)
        if layer in ('cache', 'aborted', 'websocket'):
            continue
        u = urlparse(e['request']['url'])
        st = started(e)
        best = None
        for i, t in enumerate(tele):
            if i in used or t['method'] != e['request']['method']:
                continue
            if t['uri'].split('?')[0].rstrip('/') != u.path.rstrip('/'):
                continue
            d = abs((t['at'] - st).total_seconds())
            if d <= window and (best is None or d < best[1]):
                best = (i, d, t)
        if best:
            used.add(best[0])
            matched.append((e, layer, best[2]))
        else:
            missed.append((e, layer))

    by_layer = collections.Counter(l for _, l, _ in matched)
    miss_layer = collections.Counter(l for _, l in missed)
    print('\n  слой          дошло до бэка   не дошло')
    for l in ('api', 'preflight', 'static', 'revalidate'):
        if by_layer[l] or miss_layer[l]:
            print('  %-12s %10d %12d' % (l, by_layer[l], miss_layer[l]))
    print('\n  Строка «static / не дошло» — подтверждение, что статику отдаёт nginx')
    print('  и приложение она не будит. Строка «api / не дошло» — либо запись Telescope')
    print('  выключена для этих путей, либо ответ пришёл из кэша приложения.')

    orphan = len(tele) - len(used)
    if orphan > 0:
        print('\n  %d записей Telescope не нашли пары в HAR: фоновые задачи, чужой' % orphan)
        print('  трафик на стенде или запросы, порождённые самим бэком.')

    agg = collections.defaultdict(lambda: {'n': 0, 'ms': [], 'q': [], 'qms': [], 'slow': 0, 'jobs': 0})
    for e, layer, t in matched:
        if layer != 'api':
            continue
        k = '%s %s' % (t['method'], norm_path(t['uri'].split('?')[0]))
        a = agg[k]
        a['n'] += 1
        a['ms'].append(t['duration'])
        a['qms'].append(t.get('query_ms') or 0)
        a['slow'] += t.get('slow') or 0
        a['jobs'] += t.get('jobs') or 0
        if t['queries'] is not None:
            a['q'].append(t['queries'])
    if agg:
        print('\n  %-40s %3s %8s %6s %8s %5s' %
              ('запрос', 'n', 'сервер', 'в БД', 'время БД', 'задач'))
        for k, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]['ms']))[:25]:
            med = sorted(v['ms'])[len(v['ms']) // 2]
            q = '%.0f' % (sum(v['q']) / len(v['q'])) if v['q'] else '—'
            qms = sorted(v['qms'])[len(v['qms']) // 2] if v['qms'] else 0
            print('  %-40s %3d %6.0f мс %6s %6.0f мс %5d'
                  % (k[:40], v['n'], med, q, qms, v['jobs']))
        slow = sum(v['slow'] for v in agg.values())
        print('\n  Столбец «в БД» — то, ради чего сверка и нужна: два вызова с одинаковым')
        print('  временем ответа могут отличаться числом запросов в БД на порядок.')
        if slow:
            print('  Медленных запросов в БД (по порогу Telescope): %d.' % slow)
        # Доля времени, которая уходит в БД, а не в код: по ней видно, куда смотреть,
        # если упрёмся в потолок — в индексы или в приложение.
        tot_ms = sum(sum(v['ms']) for v in agg.values())
        tot_q = sum(sum(v['qms']) for v in agg.values())
        if tot_ms:
            print('  Из %.1f с серверного времени в БД проведено %.1f с (%.0f%%).'
                  % (tot_ms / 1000, tot_q / 1000, 100.0 * tot_q / tot_ms))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    csv_out = tele_path = None
    if '--csv' in args:
        i = args.index('--csv'); csv_out = args[i + 1]; del args[i:i + 2]
    if '--telescope' in args:
        i = args.index('--telescope'); tele_path = args[i + 1]; del args[i:i + 2]
    missing = [a for a in args if not os.path.exists(a)]
    if missing:
        print('нет файла: %s' % ', '.join(missing))
        return 1
    entries, creators = load(args)
    report(entries, creators)
    if csv_out:
        dump_csv(entries, csv_out)
    if tele_path:
        correlate(entries, load_telescope(tele_path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
