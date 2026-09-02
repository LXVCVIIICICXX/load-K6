#!/usr/bin/env python3
"""Спецификации API (Swagger/OpenAPI): хранение файлов и разбор их в список запросов.

Раньше спецификацию скачивали руками и клали куда придётся, а список запросов
для точечного теста был вписан семью строками прямо в web.py. Теперь файлы
лежат в отдельном каталоге api/, кладёт их туда морда (вкладка «Настройки»),
а список запросов собирается из них.

Спецификация — это карта чужого стенда: пути, параметры, внутренние сервисы.
Репозиторий публичный, поэтому каталог целиком закрыт в .gitignore.
"""
import json, os, re, time

import config

DIR = config.APISPEC

# .json разбираем всегда; YAML — только если в системе есть PyYAML. Своих
# зависимостей у инструмента нет и заводить их из-за одного формата незачем:
# Swagger UI отдаёт ту же спецификацию в JSON.
try:
    import yaml
except Exception:
    yaml = None

EXT = ('.json', '.yaml', '.yml')
METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')
MAX = 24 * 1024 * 1024          # больше 24 МБ — это уже не спецификация
_CACHE = {}                     # file → (mtime, size, info): разбор 5 МБ не бесплатный


def ensure():
    os.makedirs(DIR, exist_ok=True)
    return DIR


def safe_name(name):
    """Имя файла приходит из браузера, то есть снаружи: каталогов в нём быть не должно."""
    n = os.path.basename((name or '').strip().replace('\\', '/'))
    n = re.sub(r'[^\w .()\-]', '_', n)
    stem, ext = os.path.splitext(n)
    if ext.lower() not in EXT:
        stem, ext = n, '.json'
    stem = stem.strip(' ._') or 'swagger'
    return stem[:100] + ext.lower()


def _decode(raw, fn):
    txt = raw.decode('utf-8-sig', 'replace') if isinstance(raw, (bytes, bytearray)) else raw
    try:
        # Расширение .yaml у файла с JSON внутри встречается сплошь и рядом,
        # поэтому сначала пробуем JSON независимо от имени.
        return json.loads(txt)
    except ValueError:
        pass
    if fn.lower().endswith('.json'):
        raise ValueError('файл не разбирается как JSON')
    if yaml is None:
        raise ValueError('это YAML, а разбирать его нечем: поставьте PyYAML '
                         '(pip3 install pyyaml) или выгрузите спецификацию в JSON')
    try:
        return yaml.safe_load(txt)
    except Exception as e:
        raise ValueError('файл не разбирается как YAML: ' + str(e)[:120])


def _base(doc):
    """Префикс, который живёт в спецификации, а не в путях.

    В paths лежит /v1/…, а тесту нужен полный путь /api/v1/… — недостающее
    /api спрятано в servers (OpenAPI 3) или в basePath (Swagger 2).
    """
    srv = doc.get('servers')
    url = (srv[0] or {}).get('url', '') if isinstance(srv, list) and srv else ''
    if url:
        tail = url.split('//', 1)[-1]
        p = '/' + tail.split('/', 1)[1] if '/' in tail else ''
        # Шаблон вида {basePath} подставить нечем — такой префикс не берём.
        return '' if '{' in p else p.rstrip('/')
    return str(doc.get('basePath') or '').rstrip('/')


def _ref(doc, node, depth=0):
    """$ref внутри того же файла. Без раскрытия подробности операции — это
    строка «#/components/schemas/ItemResource» и больше ничего."""
    while isinstance(node, dict) and '$ref' in node and depth < 6:
        p = str(node['$ref'])
        if not p.startswith('#/'):
            return node
        cur = doc
        for part in p[2:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return node
        node, depth = cur, depth + 1
    return node


def _type(doc, sch, depth=0):
    """Тип человеческими словами: «строка», «массив LessonResource»."""
    sch = _ref(doc, sch)
    if not isinstance(sch, dict) or depth > 4:
        return ''
    if sch.get('enum'):
        vals = [str(v) for v in sch['enum'][:6]]
        more = '…' if len(sch['enum']) > 6 else ''
        return 'одно из: ' + ', '.join(vals) + more
    if sch.get('const') is not None:
        return 'всегда ' + str(sch['const'])
    t = sch.get('type')
    if t == 'array':
        inner = sch.get('items') or {}
        name = str(inner.get('$ref') or '').rsplit('/', 1)[-1]
        return 'массив ' + (name or _type(doc, inner, depth + 1) or 'значений')
    if isinstance(t, list):
        t = '/'.join(str(x) for x in t)
    return str(t or sch.get('title') or 'объект')


def _fields(doc, sch, limit=40):
    """Поля объекта на один уровень: глубже — это уже чтение схемы целиком."""
    sch = _ref(doc, sch)
    if not isinstance(sch, dict):
        return []
    props = sch.get('properties') or {}
    req = set(sch.get('required') or [])
    return [{'name': k, 'type': _type(doc, v), 'req': k in req}
            for k, v in list(props.items())[:limit]]


def _details(doc, item, op):
    """То, ради чего операцию раскрывают: параметры, тело, ответы."""
    params = []
    for p in (item.get('parameters') or []) + (op.get('parameters') or []):
        p = _ref(doc, p)
        if not isinstance(p, dict) or not p.get('name'):
            continue
        params.append({'name': str(p['name']), 'in': str(p.get('in') or ''),
                       'req': bool(p.get('required')),
                       'type': _type(doc, p.get('schema') or {}),
                       'desc': str(p.get('description') or '')[:200]})
    body = None
    rb = _ref(doc, op.get('requestBody') or {})
    cts = list((rb.get('content') or {}).keys()) if isinstance(rb, dict) else []
    if cts:
        sch = (rb['content'][cts[0]] or {}).get('schema') or {}
        body = {'ct': cts, 'req': bool(rb.get('required')),
                'schema': str(sch.get('$ref') or '').rsplit('/', 1)[-1],
                'fields': _fields(doc, sch)}
    resp = []
    for code, r in (op.get('responses') or {}).items():
        ref = str(r.get('$ref') or '').rsplit('/', 1)[-1] if isinstance(r, dict) else ''
        rr = _ref(doc, r)
        desc = str((rr or {}).get('description') or '') if isinstance(rr, dict) else ''
        resp.append({'code': str(code), 'desc': desc[:160] or ref})
    # operationId и остальные теги операции наружу не отдаём: в морде их не
    # показывают, а в ответе они весили бы на каждой из сотни операций.
    return {'desc': str(op.get('description') or '')[:600],
            'params': params, 'body': body, 'resp': resp}


def requests_of(doc):
    """Плоский список запросов: метод, полный путь, название и раздел."""
    base = _base(doc)
    out = []
    for path, item in (doc.get('paths') or {}).items():
        if not isinstance(item, dict):
            continue
        for m, op in item.items():
            if m.lower() not in METHODS or not isinstance(op, dict):
                continue
            out.append({'m': m.upper(), 'p': base + str(path),
                        'n': str(op.get('summary') or op.get('operationId') or '').strip(),
                        'tag': str((op.get('tags') or [''])[0] or ''),
                        'det': _details(doc, item, op)})
    out.sort(key=lambda r: (r['p'], r['m']))
    return out


def _info(doc, fn, size, mtime):
    if not isinstance(doc, dict) or not isinstance(doc.get('paths'), dict):
        raise ValueError('в файле нет раздела paths — это не Swagger и не OpenAPI')
    i = doc.get('info') if isinstance(doc.get('info'), dict) else {}
    reqs = requests_of(doc)
    if not reqs:
        raise ValueError('в разделе paths нет ни одного запроса')
    return {'file': fn,
            'title': str(i.get('title') or '').strip(),
            'version': str(i.get('version') or '').strip(),
            'spec': str(doc.get('openapi') or doc.get('swagger') or ''),
            'base': _base(doc),
            'kb': max(1, round(size / 1024)),
            'when': time.strftime('%d.%m.%Y %H:%M', time.localtime(mtime)),
            'reqs': reqs}


def read(fn):
    """Разобранная спецификация. Повторный разбор — только если файл менялся."""
    p = os.path.join(DIR, safe_name(fn))
    st = os.stat(p)
    key = (st.st_mtime, st.st_size)
    got = _CACHE.get(p)
    if got and got[0] == key:
        return got[1]
    with open(p, 'rb') as f:
        raw = f.read()
    info = _info(_decode(raw, fn), os.path.basename(p), st.st_size, st.st_mtime)
    _CACHE[p] = (key, info)
    return info


def brief(info):
    """Вид для страницы: сами запросы наружу не отдаём.

    В крупной спецификации их тысячи, а на странице нужны только счётчики.
    Отдельная функция нужна, чтобы никто не выкинул reqs прямо из info: этот
    словарь лежит в кэше, и правка на месте испортила бы его для всех.
    """
    r = info.get('reqs') or []
    out = {k: v for k, v in info.items() if k != 'reqs'}
    out['n'] = len(r)
    out['get'] = len([q for q in r if q['m'] == 'GET' and '{' not in q['p']])
    return out


def files():
    try:
        got = os.listdir(DIR)
    except OSError:
        return []
    return sorted(f for f in got
                  if not f.startswith('.') and f.lower().endswith(EXT))


def specs():
    """Всё, что лежит в каталоге. Нечитаемый файл не прячем, а показываем с причиной."""
    out = []
    for fn in files():
        try:
            out.append(read(fn))
        except Exception as e:
            out.append({'file': fn, 'error': str(e)[:160], 'reqs': []})
    return out


def save(name, raw):
    """Кладёт файл в каталог. Разбираем ДО записи: битый файл на диск не попадёт."""
    if not raw:
        raise ValueError('пустой файл')
    if len(raw) > MAX:
        raise ValueError('файл больше 24 МБ — вряд ли это спецификация')
    fn = safe_name(name)
    info = _info(_decode(raw, fn), fn, len(raw), time.time())
    ensure()
    p = os.path.join(DIR, fn)
    tmp = p + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
    os.replace(tmp, p)
    _CACHE.pop(p, None)
    return read(fn)


def drop(fn):
    p = os.path.join(DIR, safe_name(fn))
    _CACHE.pop(p, None)
    if os.path.isfile(p):
        os.remove(p)


def endpoints(limit=500):
    """Пути для теста «разгон одного запроса».

    Только GET и только без {параметров}: подставить их значения тесту неоткуда,
    а бить POST-запросами по живому стенду — это уже запись, а не замер.
    """
    seen = []
    for s in specs():
        for r in s['reqs']:
            if r['m'] == 'GET' and '{' not in r['p'] and r['p'] not in seen:
                seen.append(r['p'])
    seen.sort()
    return seen[:limit]
