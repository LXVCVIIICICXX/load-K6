#!/usr/bin/env python3
"""Общие настройки инструмента: .env, корень данных, имя стенда.

Раньше адрес стенда и пути были вписаны в каждый скрипт, а сам инструмент
жил внутри каталога конкретного проекта. Теперь всё, что относится к
площадке, приходит из .env (см. .env.example), а данные прогонов лежат
рядом с кодом — если не задан LOAD_ROOT.
"""
import json, os

# Каталог с кодом — он же корень репозитория.
HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=None):
    """Подтягивает .env в os.environ.

    Уже заданные переменные окружения сильнее файла: так разовый запуск
    вида `STAND=… python3 run.py` не спорит с сохранённым .env.
    """
    p = path or os.environ.get('LOAD_ENV') or os.path.join(HERE, '.env')
    try:
        f = open(p, encoding='utf-8')
    except OSError:
        return {}
    got = {}
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k = k.strip()
            v = v.strip()
            # Кавычки вокруг значения — привычка из shell, значением они не являются.
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            got[k] = v
            os.environ.setdefault(k, v)
    return got


load_env()

# Где лежат har/ и results/. По умолчанию — рядом с кодом, чтобы свежий
# git clone работал без единой настройки. LOAD_ROOT нужен, когда записи и
# прогоны уже накоплены в другом каталоге и переносить их некуда.
ROOT = os.path.abspath(os.environ.get('LOAD_ROOT') or HERE)

API_URL = (os.environ.get('API_URL') or '').rstrip('/')
SITE_URL = (os.environ.get('SITE_URL') or '').rstrip('/')


def _host(url):
    u = url.split('//', 1)[-1]
    return u.split('/', 1)[0].split('@')[-1]


def stand():
    """Имя площадки: под ним группируются прогоны в results/.

    Отдельная переменная нужна редко — по умолчанию берём хост из API_URL,
    иначе после смены стенда отчёты копились бы в чужой папке.
    """
    return os.environ.get('STAND') or _host(API_URL) or 'stand'


STAND = stand()

POOL = os.path.join(HERE, 'pool.json')


# Больше сорока id в одной переменной не отдаём: k6 читает окружение целиком
# на каждый поток, а разнообразия хватает и от сорока.
POOL_LIMIT = 40


def pool_env(env=None):
    """Раздаёт id стенда из pool.json в переменные окружения.

    Сценарии карту стенда не знают: в путях стоят подстановки вида
    `/api/v1/items/{ITEM_ID}`, а чем их заполнить — дело этого файла. Морда и
    запуск из терминала должны раздавать id одинаково, поэтому подстановка
    живёт здесь, а не в web.py.

    Каждый ключ pool.json со списком значений становится переменной с тем же
    именем: список через запятую, из которого сценарий берёт случайный элемент.
    """
    env = os.environ if env is None else env
    try:
        p = json.load(open(POOL, encoding='utf-8'))
    except Exception:
        return env
    for key, val in (p.get('vars') or {}).items():
        if not str(key).isupper():
            continue
        vals = [str(x) for x in (val if isinstance(val, list) else [val])]
        vals = [v for v in vals if v != '']
        if vals:
            env.setdefault(key, ','.join(vals[:POOL_LIMIT]))
    return env


RESULTS = os.path.join(ROOT, 'results')
HAR_DIR = os.path.join(ROOT, 'har')
RECORDED = os.path.join(HERE, 'recorded')
SECRETS = os.path.join(HERE, 'secrets.json')
# Спецификации API (Swagger/OpenAPI) лежат рядом с кодом, а не в ROOT:
# это настройка инструмента, как secrets.json, а не данные прогонов.
APISPEC = os.path.join(HERE, 'api')


# ---------------------------------------------------------------- вход на сайт
# Как устроен вход, инструмент не знает: путь, тело и необязательный второй шаг
# описаны в .env (см. .env.example). Один и тот же флоу нужен в трёх местах —
# проверке профиля в морде, входе за метриками Telescope и сценариях k6, —
# поэтому живёт он здесь, а не переписан в каждом.

def dig(obj, path):
    """Значение по пути через точку; числовой сегмент — индекс массива."""
    for k in str(path).split('.'):
        if isinstance(obj, list) and k.isdigit():
            obj = obj[int(k)] if int(k) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
        if obj is None:
            return None
    return obj


def fill(tpl, email, password, resp):
    """Тело запроса по шаблону: $USER, $PASS и $resp.<путь через точку>."""
    if isinstance(tpl, str):
        if tpl == '$USER':
            return email
        if tpl == '$PASS':
            return password
        if tpl.startswith('$resp.'):
            return dig(resp, tpl[6:])
        return tpl
    if isinstance(tpl, list):
        return [fill(v, email, password, resp) for v in tpl]
    if isinstance(tpl, dict):
        return {k: fill(v, email, password, resp) for k, v in tpl.items()}
    return tpl


XSRF_COOKIE = os.environ.get('XSRF_COOKIE', 'XSRF-TOKEN')
XSRF_HEADER = os.environ.get('XSRF_HEADER', 'X-XSRF-TOKEN')


def xsrf(cj):
    import urllib.parse
    for c in cj:
        if c.name == XSRF_COOKIE:
            return urllib.parse.unquote(c.value)
    return ''


def site_login(op, base, email, password, cj, timeout=25):
    """Проходит вход открывателем `op`, складывая куки в `cj`.

    Возвращает разобранный ответ последнего шага. Ошибки не глушит: вызывающий
    сам решает, что сказать человеку про код 401 или 429.
    """
    import urllib.request
    base = base.rstrip('/')
    path = os.environ.get('LOGIN_PATH') or ''
    if not path:
        raise ValueError('не задан LOGIN_PATH в .env')

    def call(p, data=None):
        h = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        t = xsrf(cj)
        if t and XSRF_HEADER:
            h[XSRF_HEADER] = t
        body = json.dumps(data).encode() if data is not None else None
        r = urllib.request.Request(base + p, data=body, headers=h)
        with op.open(r, timeout=timeout) as z:
            raw = z.read()
        try:
            return json.loads(raw or b'null')
        except ValueError:
            return None

    csrf_path = os.environ.get('CSRF_PATH')
    if csrf_path:
        call(csrf_path)
    body = json.loads(os.environ.get('LOGIN_BODY')
                      or '{"email":"$USER","password":"$PASS"}')
    got = call(path, fill(body, email, password, None))
    confirm = os.environ.get('LOGIN_CONFIRM_PATH')
    if confirm:
        cb = json.loads(os.environ.get('LOGIN_CONFIRM_BODY') or '{}')
        got = call(confirm, fill(cb, email, password, got))
    return got
