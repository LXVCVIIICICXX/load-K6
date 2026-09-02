#!/usr/bin/env python3
"""Запись браузера → сценарий k6.

  python3 codegen.py <файл.har> --out <сценарий.js> [--name «Имя»]

Что делает и чего не делает. Сценарий получается из записи механически:
порядок запросов, паузы между ними и параллельные пачки берутся из HAR как
есть. Разбор на шаги и отделение фонового опроса от действий человека —
готовые функции из har_report.py, они же считают отчёт по записи.

Три вещи кодоген решает сам:
  • вход подменяется на общий `login()` из _lib.js — иначе в сценарии остался
    бы вшитый пароль одного человека, и все потоки дрались бы за один профиль;
  • числовые id в адресах, которые пришли из ответа предыдущего запроса,
    становятся переменными — иначе сценарий ходил бы по идентификаторам того
    дня, когда его записали;
  • тела POST и PUT переносятся дословно. Если внутри тела есть id, он
    останется прежним — это видно смоук-прогоном по ответам 422.
"""
import json, os, sys, re, datetime, collections
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config          # подтягивает .env: пути входа кодоген берёт оттуда
import har as harmod
import har_report as R

# Запросы ближе этого друг к другу человек не разделял: браузер выпустил их
# одной пачкой. В сценарии они должны идти параллельно, иначе мгновенный пик
# получится вдвое ниже настоящего.
BURST_GAP = 0.25

# Вход собирается хелпером, а не переносится из записи: в записи лежит пароль.
# Какие это пути, знает только .env — здесь их не может быть по определению.
LOGIN_PATHS = tuple(x for x in (os.environ.get('LOGIN_PATH'),
                                os.environ.get('LOGIN_CONFIRM_PATH'),
                                os.environ.get('CSRF_PATH')) if x)


def api_path(e):
    return urlparse(e['request']['url']).path


def tag_name(method, path):
    """`/api/v1/catalog/items/{id}` → `catalog.items.show`.

    Имя попадает в тег `name`, по которому отчёт группирует перцентили,
    поэтому оно должно быть коротким и узнаваемым, а не полным путём.
    """
    p = re.sub(r'^/api(/v\d+)?', '', path).strip('/')
    segs = [s for s in p.split('/') if s]
    tail = segs and segs[-1] == '{id}'
    segs = [s for s in segs if s != '{id}']
    name = '.'.join(segs[-3:]) or 'root'
    if tail:
        name += '.show'
    if method not in ('GET', 'OPTIONS'):
        name += '.' + method.lower()
    return name


def body_of(e):
    t = (e['response'].get('content') or {}).get('text')
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def id_source(data):
    """Как из ответа достать список id — и достаётся ли он вообще.

    Разбираем только две формы, которые и отдаёт этот API: список объектов
    и один объект. Угадывать глубже смысла нет: неверно угаданный источник
    даст сценарий, который молча ходит не туда.
    """
    d = data.get('data') if isinstance(data, dict) else None
    if isinstance(d, list) and d and all(isinstance(x, dict) and isinstance(x.get('id'), int) for x in d):
        return "(r.json('data') || []).map(x => x.id)", [x['id'] for x in d]
    if isinstance(d, dict) and isinstance(d.get('items'), list) and d['items'] \
            and all(isinstance(x, dict) and isinstance(x.get('id'), int) for x in d['items']):
        return "((r.json('data') || {}).items || []).map(x => x.id)", [x['id'] for x in d['items']]
    if isinstance(d, dict) and isinstance(d.get('id'), int):
        return "[(r.json('data') || {}).id]", [d['id']]
    return None, []


def path_ids(path):
    return [int(s) for s in path.split('/') if s.isdigit()]


def correlate(seq):
    """Кто у кого берёт id: для каждого запроса — ближайший предыдущий ответ,
    в котором этот id действительно был."""
    produced = []          # (индекс запроса, выражение-источник, множество id)
    links = {}             # индекс потребителя → (индекс источника, позиция id в списке)
    for i, e in enumerate(seq):
        for want in path_ids(api_path(e)):
            for j, expr, ids in reversed(produced):
                if want in ids:
                    links.setdefault(i, {})[want] = (j, ids.index(want))
                    break
        data = body_of(e)
        if data is not None:
            expr, ids = id_source(data)
            if expr:
                produced.append((i, expr, ids))
    return links


def group_bursts(items):
    """Соседние по времени запросы — в одну пачку."""
    out, cur, prev = [], [], None
    for e in items:
        st = harmod.started(e)
        if prev is not None and (st - prev).total_seconds() > BURST_GAP:
            out.append(cur); cur = []
        cur.append(e)
        prev = st
    if cur:
        out.append(cur)
    return out


def self_id(entries):
    """id того, под кем делали запись.

    Он рассыпан по записи параметрами вида `userId=70` в адресах и именами
    каналов в телах подписок. Если оставить его как есть, все потоки пойдут под
    одним человеком, сколько бы профилей ни было в пуле, — поэтому подменяем
    на id того, кто вошёл в этом потоке.

    Где искать — говорит USER_ID_PATH из .env: ответ на вход у каждого сайта
    свой, и угадывать его форму кодоген не должен.
    """
    path = os.environ.get('USER_ID_PATH') or 'data.id'
    last = (os.environ.get('LOGIN_CONFIRM_PATH')
            or os.environ.get('LOGIN_PATH') or '')
    if not last:
        return None
    for e in entries:
        if last not in api_path(e):
            continue
        val = body_of(e)
        for key in path.split('.'):
            if isinstance(val, list) and key.isdigit():
                val = val[int(key)] if int(key) < len(val) else None
            elif isinstance(val, dict):
                val = val.get(key)
            else:
                val = None
            if val is None:
                break
        if isinstance(val, int):
            return val
    return None


def js_url(e, links, idx, varname, me):
    """Адрес запроса в виде шаблонной строки JS."""
    u = urlparse(e['request']['url'])
    path, q = u.path, ('?' + u.query if u.query else '')
    sub = links.get(idx) or {}
    for want, (j, pos) in sub.items():
        path = re.sub(r'(?<=/)%d(?=/|$)' % want, '${pick(%s, %d, %d)}' % (varname[j], pos, want), path, count=1)
    q = q.replace('`', '\\`')
    if me:
        q = re.sub(r'(?<==)%d(?=&|$)' % me, '${uid}', q)
    return '`${API}%s%s`' % (path, q)


def multipart_parts(text, boundary):
    """Разбор записанного multipart на части: имя поля, файл, тип, размер.

    Нужен, чтобы НЕ тащить в сценарий само тело: внутри лежит загруженный
    файл целиком. Из записи берём только форму запроса, а байты сценарий
    подставит свои.
    """
    out = []
    for chunk in text.split('--' + boundary)[1:]:
        head, _, body = chunk.partition('\r\n\r\n')
        if not _ or head.strip() in ('--', ''):
            continue
        name = re.search(r'name="([^"]*)"', head)
        fn = re.search(r'filename="([^"]*)"', head)
        ct = re.search(r'Content-Type:\s*([^\r\n;]+)', head, re.I)
        out.append({'name': name.group(1) if name else '',
                    'filename': fn.group(1) if fn else None,
                    'ctype': (ct.group(1).strip() if ct else 'application/octet-stream'),
                    'size': len(body.rstrip('\r\n-').encode('utf-8', 'ignore'))})
    return [p for p in out if p['name']]


def emit_multipart(e, url, name, lhs, mime):
    """Запрос с файлом: форма из записи, содержимое — своё."""
    text = (e['request'].get('postData') or {}).get('text') or ''
    b = re.search(r'boundary=([^;]+)', mime)
    parts = multipart_parts(text, b.group(1).strip()) if b else []
    if not parts:
        return None
    fields = []
    for p in parts:
        if p['filename']:
            fields.append("    %s: upload(%r, %r, %d)," % (js_key(p['name']), p['ctype'],
                                                          p['filename'], p['size']))
        else:
            # Обычное поле формы: его значение короткое и переносится как есть.
            val = text.split('name="%s"' % p['name'], 1)[-1].split('\r\n\r\n', 1)[-1]
            fields.append("    %s: %r," % (js_key(p['name']), val.split('\r\n')[0][:200]))
    return ('  const body = {\n%s\n  };\n  %shttp.post(%s, body, M(%r));'
            % ('\n'.join(fields), lhs, url, name))


def js_key(k):
    """`files[]` в объектном литерале обязан быть строкой."""
    return k if re.fullmatch(r'[A-Za-z_$][\w$]*', k) else repr(k)


def emit_request(e, links, idx, varname, keep, me):
    """Одна строка сценария. keep — нужен ли ответ следующим шагам."""
    m = e['request']['method']
    name = tag_name(m, harmod.norm_path(api_path(e)))
    url = js_url(e, links, idx, varname, me)
    lhs = 'r = ' if keep else ''
    if m == 'GET':
        return '  %shttp.get(%s, J(%r));' % (lhs, url, name)
    mime = (e['request'].get('postData') or {}).get('mimeType') or ''
    if 'multipart/form-data' in mime:
        pre = '  preflight(%s);\n' % url
        got = emit_multipart(e, url, name, lhs, mime)
        if got:
            return pre + got
    # Тело — шаблонная строка, а не обычная: в него подставляется id вошедшего.
    # Поэтому сначала гасим то, что уже похоже на подстановку, и лишь потом
    # вставляем свою, иначе запись со строкой `${…}` внутри сломала бы файл.
    body = (e['request'].get('postData') or {}).get('text') or ''
    body = body.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${').replace('\n', ' ')
    if me:
        body = re.sub(r'(?<=[.:"])%d(?=["},]|$)' % me, '${uid}', body)
    pre = '  preflight(%s);\n' % url if m in ('POST', 'PUT', 'PATCH', 'DELETE') else ''
    if m == 'POST':
        return '%s  %shttp.post(%s, `%s`, J(%r));' % (pre, lhs, url, body, name)
    return "%s  %shttp.request('%s', %s, `%s`, J(%r));" % (pre, lhs, m, url, body, name)


def statics_for(step_statics, site_host):
    """Пути статики шага. Свои — относительным путём, чужие — полным адресом.

    Раньше от адреса оставался только путь, и файл из S3 превращался в путь
    на фронте, которого там нет. Хост чужой статики надо сохранять — и чтобы
    запрос уходил куда надо, и чтобы в отчёте было видно, что он не к нам.
    """
    paths = []
    for e in step_statics:
        u = urlparse(e['request']['url'])
        p = u.path if u.netloc == site_host else e['request']['url']
        if p not in paths:
            paths.append(p)
    return paths


def build(har_path, out_path, title=None):
    if not LOGIN_PATHS:
        # Без путей входа запросы входа не отличить от остальных, и пароль из
        # записи уехал бы прямо в исходник сценария.
        raise ValueError('не задан LOGIN_PATH в .env: без него вход из записи '
                         'попадёт в сценарий вместе с паролем')
    entries, _ = harmod.load([har_path])
    me = self_id(entries)
    layer = {id(e): harmod.classify(e) for e in entries}
    api = [e for e in entries
           if layer[id(e)] == 'api' and e['response']['status'] != 101
           and e.get('_resourceType') != 'websocket'
           and not any(p in api_path(e) for p in LOGIN_PATHS)]
    if not api:
        raise ValueError('в записи нет ни одного запроса к API — сценарий собрать не из чего')
    api.sort(key=harmod.started)

    span = (harmod.started(api[-1]) - harmod.started(api[0])).total_seconds()
    polls = R.find_polls(api, span)
    key = lambda e: (e['request']['method'], harmod.norm_path(api_path(e)))
    seq = [e for e in api if key(e) not in polls]

    gaps, prev = [], None
    for e in seq:
        st = harmod.started(e)
        if prev is not None:
            gaps.append(max(0.0, (st - prev).total_seconds()))
        prev = st
    thr = R.step_threshold(gaps)

    steps, cur, prev, pause = [], [], None, 0.0
    for e in seq:
        st = harmod.started(e)
        if prev is not None and (st - prev).total_seconds() > thr:
            steps.append({'items': cur, 'pause': pause})
            pause = (st - prev).total_seconds()
            cur = []
        cur.append(e)
        prev = st
    if cur:
        steps.append({'items': cur, 'pause': pause})

    # Статику приписываем шагу, во время которого её потянул браузер.
    static = [e for e in entries if layer[id(e)] == 'static']
    # Хост фронта берём из самой записи — по документу страницы, а не из
    # настроек: сценарий должен ходить туда же, где его снимали.
    site_host = next((urlparse(e['request']['url']).netloc for e in entries
                      if (e.get('_resourceType') or '') == 'document'),
                     urlparse(static[0]['request']['url']).netloc if static else '')
    for s in steps:
        s['static'] = []
    if steps:
        bounds = [harmod.started(s['items'][0]) for s in steps]
        for e in static:
            st, i = harmod.started(e), 0
            for j, b in enumerate(bounds):
                if b <= st:
                    i = j
                else:
                    break
            steps[i]['static'].append(e)

    flat = [e for s in steps for e in s['items']]
    links = correlate(flat)
    varname = {}
    for n, j in enumerate(sorted({v[0] for sub in links.values() for v in sub.values()})):
        varname[j] = 'ids%d' % (n + 1)

    # Параллельно со сценарием собираем его карту — ту же схему запросов, что
    # морда рисует для рукописных тестов. Из кода её не восстановить, а понять
    # «что этот сценарий вообще делает», не открывая файл, надо.
    lines, flow, k = [], [], 0
    for si, s in enumerate(steps):
        lines.append('')
        lines.append('  /* шаг %d — %d запросов */' % (si + 1, len(s['items'])))
        if s['pause'] > 0.05:
            lines.append('  think(%.1f);' % s['pause'])
        track = {'role': 'Шаг %d' % (si + 1), 'steps': [],
                 'note': ('пауза %.1f с перед шагом' % s['pause']) if s['pause'] > 0.05 else ''}
        for pack in group_bursts(s['items']):
            gets = [e for e in pack if e['request']['method'] == 'GET']
            if len(pack) > 1 and len(gets) == len(pack) and not any(k + i in links for i in range(len(pack))) \
                    and not any(k + i in varname for i in range(len(pack))):
                lines.append('  burst([')
                for i, e in enumerate(pack):
                    lines.append('    [%s, %r],' % (js_url(e, links, k + i, varname, me),
                                                    tag_name('GET', harmod.norm_path(api_path(e)))))
                    track['steps'].append({'d': 0, 'm': 'GET',
                                           'p': harmod.norm_path(api_path(e)),
                                           'n': 'параллельно' if i == 0 else ''})
                lines.append('  ]);')
                k += len(pack)
                continue
            for e in pack:
                lines.append(emit_request(e, links, k, varname, k in varname, me))
                note = ''
                if k in links:
                    src = sorted({varname[j] for j, _ in (links[k] or {}).values()})
                    note = 'id из ответа выше (%s)' % ', '.join(src)
                track['steps'].append({'d': 1 if k in links else 0,
                                       'm': e['request']['method'],
                                       'p': harmod.norm_path(api_path(e)), 'n': note})
                if k in varname:
                    expr, _ = id_source(body_of(e) or {})
                    lines.append('  const %s = %s;' % (varname[k], expr))
                    track['steps'][-1]['n'] = (note + ' · ' if note else '') + 'отсюда берём id'
                k += 1
        st = statics_for(s['static'], site_host)
        if st:
            lines.append('  statics(%s, %r);' % (json.dumps(st, ensure_ascii=False), 'static.step%d' % (si + 1)))
            track['steps'].append({'d': 0, 'm': 'GET', 'p': 'статика фронта, %d файлов' % len(st),
                                   'n': 'пачкой, отдаёт nginx'})
        if track['steps']:
            flow.append(track)

    poll_lines = []
    for (m, p), v in sorted(polls.items(), key=lambda x: -x[1]['n']):
        poll_lines.append('//   %s %s — раз в %.0f с, за сессию %d раз'
                          % (m, p, v['period'], v['n']))

    title = title or os.path.basename(har_path)[:-4]
    js = TEMPLATE % {
        'title': title,
        'when': datetime.datetime.now().strftime('%d.%m.%Y %H:%M'),
        'nreq': len(api), 'nsteps': len(steps), 'thr': thr,
        'span': span,
        'polls': '\n'.join(poll_lines) or '//   не найдено',
        'body': '\n'.join(lines),
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(js)
    # Карта запросов лежит рядом со сценарием: морда читает её и показывает
    # на вкладке «Запуск» так же, как схемы рукописных тестов.
    if flow and polls:
        flow.append({'role': 'Фоновый опрос', 'parallel': True,
                     'note': 'шлёт таймер фронта, в сценарий не включён',
                     'steps': [{'d': 0, 'm': m, 'p': p,
                                'n': 'раз в %.0f с, за сессию %d раз' % (v['period'], v['n'])}
                               for (m, p), v in sorted(polls.items(), key=lambda x: -x[1]['n'])]})
    with open(out_path[:-3] + '.flow.json', 'w', encoding='utf-8') as f:
        json.dump(flow, f, ensure_ascii=False)
    return {'steps': len(steps), 'requests': len(api), 'polls': len(polls),
            'vars': len(varname), 'threshold': round(thr, 2)}


TEMPLATE = '''/* Сценарий собран автоматически из записи браузера «%(title)s» %(when)s.
   Правки перетрутся при следующей пересборке — меняйте запись, а не файл.

   В записи: %(nreq)d запросов к API за %(span).0f с, разложены на %(nsteps)d шагов
   (шагом считается пачка запросов с паузой больше %(thr).1f с перед ней).

   Фоновый опрос — шлёт таймер фронта, а не человек; в сценарий не попал:
%(polls)s
*/
import http from 'k6/http';
import { SharedArray } from 'k6/data';
import { Rate } from 'k6/metrics';
import { hook } from '../errlog.js';
import { API, J, G, M, think, preflight, burst, statics, upload, login, userId } from '../_lib.js';

hook(http);

const accounts = new SharedArray('accounts', () => {
  const all = JSON.parse(open(__ENV.ACCOUNTS || '../accounts.json'));
  // ROLE не задана — берём пул целиком: какие роли бывают, знает не сценарий.
  const st = __ENV.ROLE ? all.filter((a) => a.role === __ENV.ROLE) : all;
  const out = [];
  for (const a of (st.length ? st : all)) {
    const n = Math.max(1, Math.round(Number(a.weight) || 1));
    for (let i = 0; i < n; i++) out.push(a);
  }
  return out;
});

const STAGES = __ENV.STAGES ? JSON.parse(__ENV.STAGES) : null;

/* SMOKE — прогон в один поток и одну итерацию: проверить, что сценарий вообще
   проходит на сегодняшнем стенде, прежде чем звать им нагрузку. */
export const options = __ENV.SMOKE
  ? { vus: 1, iterations: 1, thresholds: {} }
  : {
      scenarios: {
        recorded: STAGES
          ? { executor: 'ramping-vus', startVUs: 0, stages: STAGES,
              gracefulRampDown: __ENV.GRACEFUL || '2m' }
          : { executor: 'constant-vus', vus: Number(__ENV.VUS || 5),
              duration: __ENV.DURATION || '10m', gracefulStop: __ENV.GRACEFUL || '2m' },
      },
      thresholds: { login_failed: ['rate<0.02'] },
    };

const loginFail = new Rate('login_failed');

/* Если список пуст — берём id из записи. Так сценарий не падает на стенде,
   где сегодня нужных объектов нет, но и не притворяется, что сходил по живому. */
const pick = (a, i, dflt) => (a && a.length ? a[i %% a.length] : dflt);

export default function () {
  const acc = accounts[(__VU + Number(__ENV.VU_OFFSET || 0) + __ITER) %% accounts.length];
  let r;

  const me = login(acc);
  if (!me) { loginFail.add(true); return; }
  loginFail.add(false);
  // id вошедшего: в записи он был вшит в адреса и в тела подписок, здесь
  // берётся у того, кто реально вошёл в этом потоке (путь — USER_ID_PATH).
  const uid = userId(me);
%(body)s
}
'''


ASSET = re.compile(r'/assets/([\w.-]+\.(?:js|css))')


def _get(url, auth):
    import urllib.request, base64
    r = urllib.request.Request(url)
    if auth:
        r.add_header('Authorization', 'Basic ' + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(r, timeout=30) as f:
        return f.read().decode('utf-8', 'replace')


def stem(name):
    """`index-Bi1oQ5mN.js` → `index.js`. Хеш меняется при каждой сборке фронта,
    а вот основа имени — нет, по ней файл и опознаётся между сборками."""
    m = re.match(r'(.+?)-[\w-]{6,}\.(js|css)$', name)
    return '%s.%s' % (m.group(1), m.group(2)) if m else name


def _alive(url, auth):
    """Есть ли файл на стенде. HEAD, потому что нужен только код ответа."""
    import urllib.request, base64
    r = urllib.request.Request(url, method='HEAD')
    if auth:
        r.add_header('Authorization', 'Basic ' + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(r, timeout=20) as f:
            return f.status < 400
    except Exception as e:
        return getattr(e, 'code', 0) not in (403, 404, 410)


def live_assets(site, auth):
    """Файлы сборки, которые лежат на стенде сейчас.

    В HTML страницы виден только входной бандл: остальные куски фронт грузит
    сам, уже из кода. Поэтому имена вычитываем ещё и из самого бандла — Vite
    перечисляет их там строками вида `assets/имя-хеш.js`.

    Набор получается НЕПОЛНЫЙ: имена, на которые ссылается не входной бандл,
    а другие куски, сюда не попадут. Поэтому он годится только чтобы ИСКАТЬ
    замену, но не чтобы решать, что файла нет, — для этого спрашиваем стенд.
    """
    names = set(ASSET.findall(_get(site.rstrip('/') + '/', auth)))
    for n in list(names):
        if n.endswith('.js'):
            try:
                names |= set(ASSET.findall(_get('%s/assets/%s' % (site.rstrip('/'), n), auth)))
            except Exception:
                pass
    return names


def refresh_statics(script_path, site, auth):
    """Подставить в сценарий сегодняшние имена файлов сборки.

    Записи живут дольше одной сборки фронта, а имена файлов в них — нет:
    после пересборки прежний `index-Bi1oQ5mN.js` отдаёт 404, и прогон меряет
    выдачу ошибок вместо выдачи статики. Сопоставляем по основе имени.
    """
    src = open(script_path, encoding='utf-8').read()
    used = set(ASSET.findall(src))
    if not used:
        return {'renamed': 0, 'dropped': 0, 'kept': 0, 'details': []}
    base = site.rstrip('/')
    # Про КАЖДЫЙ файл спрашиваем стенд напрямую. Раньше решали по списку,
    # вычитанному из входного бандла, и он врал: имена, на которые ссылается
    # не сам бандл, а другие куски, в него не попадают — из-за этого полсотни
    # живых файлов помечались как отсутствующие и вырезались из сценария.
    missing = sorted(n for n in used if not _alive('%s/assets/%s' % (base, n), auth))
    if not missing:
        return {'renamed': 0, 'dropped': 0, 'kept': len(used), 'details': []}
    by_stem = {}
    for n in live_assets(site, auth):
        by_stem.setdefault(stem(n), n)
    renamed, dropped, details = 0, 0, []
    for old in missing:
        new = by_stem.get(stem(old))
        if new:
            src = src.replace('/assets/' + old, '/assets/' + new)
            renamed += 1
            details.append('%s → %s' % (old, new))
        else:
            # Файла с такой основой на стенде нет вовсе: кусок фронта переименован
            # или выпилен. Оставить — значит гарантированно ловить 404.
            src = re.sub(r'\s*"/assets/%s",?' % re.escape(old), '', src)
            dropped += 1
            details.append('%s — на стенде нет, убран' % old)
    if renamed or dropped:
        open(script_path, 'w', encoding='utf-8').write(src)
    return {'renamed': renamed, 'dropped': dropped, 'kept': len(used) - renamed - dropped,
            'details': details}


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    har = args[0]
    out = None
    title = None
    i = 1
    while i < len(args):
        if args[i] == '--out':
            out = args[i + 1]; i += 2
        elif args[i] == '--name':
            title = args[i + 1]; i += 2
        else:
            i += 1
    if not out:
        print('нужен --out <сценарий.js>')
        sys.exit(1)
    r = build(har, out, title)
    print('шагов %(steps)d · запросов %(requests)d · фоновых опросов %(polls)d '
          '· переменных %(vars)d · порог паузы %(threshold)s с' % r)


if __name__ == '__main__':
    main()
