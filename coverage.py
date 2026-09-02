#!/usr/bin/env python3
"""Сверка сценариев со спецификацией: что на стенде нагрузка не трогает вовсе.

Спецификация говорит, из чего стенд состоит, сценарии — что мы из этого шлём.
Пересечение и есть покрытие. Считается по КОДУ сценариев, а не по прогонам:
вопрос здесь «что мы вообще умеем нагружать», а не «что было в прошлый раз».

Пути берутся из трёх источников:
  * requests.json — карта запросов сайта, по ней ходят mix.js и baseline.js.
    В самих сценариях адресов нет: они читают этот файл;
  * recorded/*.flow.json — карта запросов, которую кодоген снял с записи.
    Там метод и путь уже машинные, с {id} вместо живых номеров;
  * остальные *.js — разбором исходника: адрес в них шаблонная строка
    `${API}/…`, а метод виден в вызове перед ней.
"""
import glob, json, os, re

import config, apispec

HERE = config.HERE
RECORDED = config.RECORDED
REQUESTS = os.path.join(HERE, 'requests.json')

# Не сценарии, а общий код. Адреса из _lib.js попадают в сверку через карты
# собранных сценариев, которые его и зовут, — считать их ещё и отдельно значило
# бы приписать покрытие тому, что само по себе ничего не шлёт.
HELPERS = {'env.js', '_lib.js', 'errlog.js', 'config.js'}

VAR = re.compile(r'\$\{[^}]*\}')                 # ${id}, ${pick(P.lessons)}
LIT = re.compile(r'`(\$\{API\}[^`]*)`')          # сам адрес
CALL = re.compile(r'http\.(get|post|put|patch|del|delete)\(\s*$')
REQ = re.compile(r"http\.request\(\s*'([A-Z]+)'\s*,\s*$")
# preflight() шлёт OPTIONS перед пишущим запросом. Это разговор браузера с
# nginx, а не операция продукта: в спецификации таких строк нет и быть не
# должно, поэтому в сверку они не идут вовсе.
PRE = re.compile(r'\bpreflight\(\s*$')
_CACHE = {}


def norm(p):
    """Путь к сравнимому виду: без запроса, с {} вместо подставляемых кусков."""
    p = p.split('?', 1)[0].split('#', 1)[0]
    p = VAR.sub('{}', p)
    p = re.sub(r'/{2,}', '/', p)
    return p.rstrip('/') or '/'


def _from_js(path):
    """Адреса из исходника сценария."""
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return set()
    got = set()
    for m in LIT.finditer(src):
        raw = m.group(1)[len('${API}'):]
        before = src[max(0, m.start() - 48):m.start()]
        if PRE.search(before):
            continue
        c = CALL.search(before)
        r = None if c else REQ.search(before)
        # По умолчанию GET: так шлёт и http.get, и пачки http.batch, где метод
        # задан один раз внутри хелпера, а сюда приходит только адрес.
        meth = (c.group(1).upper() if c else r.group(1).upper() if r else 'GET')
        if meth == 'DEL':
            meth = 'DELETE'
        if meth == 'OPTIONS':
            continue
        p = norm(raw)
        # Адрес целиком из переменной (`${API}${TARGET}` у endpoint.js) — это
        # цель, заданная при запуске. Сказать по коду о ней нечего, поэтому в
        # сверку она не идёт: иначе покрытие выглядело бы полнее, чем есть.
        if p.startswith('/'):
            got.add((meth, p))
    return got


def _from_flow(path):
    """Адреса из карты запросов собранного сценария."""
    try:
        flow = json.load(open(path, encoding='utf-8'))
    except Exception:
        return set()
    got = set()
    for track in flow or []:
        for s in track.get('steps') or []:
            p = str(s.get('p') or '')
            m = str(s.get('m') or 'GET').upper()
            # В карте бывают строки для человека — «статика фронта, 14 файлов».
            if p.startswith('/') and m != 'OPTIONS':
                got.add((m, norm(p)))
    return got


def _from_requests():
    """Запросы из карты сайта. Метод по умолчанию GET — как и в самой карте."""
    try:
        m = json.load(open(REQUESTS, encoding='utf-8'))
    except Exception:
        return set()
    got = set()
    for r in m.get('requests') or []:
        path = r.get('path')
        if not path:
            continue
        meth = str(r.get('method') or 'GET').upper()
        if meth == 'OPTIONS':
            continue
        got.add((meth, norm(path)))
    return got


def _files():
    hand = [f for f in sorted(glob.glob(os.path.join(HERE, '*.js')))
            if os.path.basename(f) not in HELPERS]
    return hand + sorted(glob.glob(os.path.join(RECORDED, '*.js')))


def scenarios():
    """{id сценария: {'reqs': {(метод, путь)}, 'unknown': [...]}}."""
    files = _files()
    stamp = tuple((f, os.path.getmtime(f)) for f in files + [REQUESTS]
                  if os.path.exists(f))
    if _CACHE.get('stamp') == stamp:
        return _CACHE['out']
    out = {}
    for f in files:
        b = os.path.basename(f)
        sid = ('recorded/' + b) if os.path.dirname(f) == RECORDED else b
        # У собранного сценария карта полнее исходника: в ней уже разложены
        # пачки и подставлены {id}. Исходник читаем, только если карты нет.
        flow = f[:-3] + '.flow.json'
        reqs = _from_flow(flow) if os.path.isfile(flow) else _from_js(f)
        if reqs:
            out[sid] = {'reqs': reqs}
    # Карта запросов — самостоятельный источник, а не приписка к сценарию:
    # по ней ходят те сценарии, у которых адресов в коде нет вовсе, и на
    # свежем клоне она бывает единственным, что вообще известно про сайт.
    reqs = _from_requests()
    if reqs:
        out['requests.json'] = {'reqs': reqs}
    _CACHE['stamp'], _CACHE['out'] = stamp, out
    return out


def match(spec_path, got_path):
    """Совпадение путей посегментно: {id} в любом из них — любой сегмент.

    Переменная на месте постоянного слова тоже считается совпадением: в ней
    может лежать ровно это слово, и назвать такой запрос непокрытым было бы
    хуже, чем изредка засчитать лишний.
    """
    a = spec_path.strip('/').split('/')
    b = got_path.strip('/').split('/')
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x.startswith('{') or y.startswith('{'):
            continue
        if x != y:
            return False
    return True


def report(file=None):
    """Спецификация с отметкой покрытия у каждой операции."""
    scen = scenarios()
    ops, n_cov, n_get, n_get_cov = [], 0, 0, 0
    for s in apispec.specs():
        if file and s['file'] != file:
            continue
        for r in s.get('reqs') or []:
            by = sorted(sid for sid, d in scen.items()
                        if any(m == r['m'] and match(r['p'], p) for m, p in d['reqs']))
            ops.append(dict(r, by=by, file=s['file']))
            n_cov += bool(by)
            if r['m'] == 'GET':
                n_get += 1
                n_get_cov += bool(by)
    return {'ops': ops, 'n': len(ops), 'covered': n_cov,
            'nGet': n_get, 'coveredGet': n_get_cov}


def counts(file=None):
    """Только цифры — для строки «покрыто N из M» рядом с файлом."""
    r = report(file)
    return {'n': r['n'], 'covered': r['covered'],
            'nGet': r['nGet'], 'coveredGet': r['coveredGet']}
