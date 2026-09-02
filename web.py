#!/usr/bin/env python3
"""Веб-морда для запуска нагрузочных тестов сайта.
   python3 web.py   →  http://127.0.0.1:5057
   Только localhost: запускает процессы и знает секреты."""
import time,json,os,sys,re,signal,subprocess,threading,collections,glob,html,urllib.parse,datetime,shutil
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import config
import apispec
import coverage
ROOT=config.ROOT      # где лежат har/ и results/
SL=config.HERE        # где лежит код
PORT=int(os.environ.get('WEB_PORT','5057'))
STAND=config.STAND


# ---------- Список тестов ----------
# Списка тестов в коде нет: морда смотрит на диск. Тест — это любой файл
# сценария рядом с кодом или в recorded/, кроме помощников, которые сценарии
# импортируют. Так добавленный сценарий появляется в морде сам, а удалённый
# исчезает — держать те же имена ещё и здесь значит расходиться с диском на
# второй правке.
HELPERS={'_lib.js','env.js','errlog.js'}

# Какие поля показывать в форме запуска, видно по тому, какие переменные
# сценарий читает: он сам говорит, что ему задавать.
PARAM_ENV=[('target',('TARGET',)),('heavy',('HEAVY',)),('rounds',('ROUNDS',)),
           ('stages',('STAGES',)),('duration',('DURATION',))]
ENV_USE=re.compile(r"__ENV\.([A-Z0-9_]+)|need\(\s*'([A-Z0-9_]+)'")
HEAD=re.compile(r'\s*/\*+(.*?)\*/',re.S)

def scenario_doc(src):
    """Имя и описание теста — из шапки самого файла.

    Сценарий и так начинается с объяснения, зачем он нужен. Переписывать те же
    слова в морду значит завести вторую версию правды, которая разъедется с
    файлом при первой же правке.
    """
    m=HEAD.match(src)
    if m: text=m.group(1)
    else:
        lines=[]
        for line in src.splitlines():
            if line.startswith('//'): lines.append(line[2:])
            elif lines or line.strip(): break
        text='\n'.join(lines)
    text=re.sub(r'^\s*\*+','',text,flags=re.M).strip()
    if not text: return '',''
    head,_,rest=text.partition('\n')
    first=re.split(r'(?<=[.!?])\s',head.strip())[0].strip()
    parts=re.split(r'\s+[—–-]\s+|:\s+',first,maxsplit=1)
    # Заголовок — до тире или двоеточия. Но если перед ними стоит пометка из
    # плана вроде «Б1» или «T5», в списке нужна не она, а то, что после: имя
    # «Б1» ничего не говорит тому, кто плана не читал.
    if len(parts)==2 and re.fullmatch(r'[A-ZА-Я]{1,3}\d+',parts[0]):
        name=parts[1]
    else:
        name=parts[0]
    name=name.strip(' .')
    if name: name=name[0].upper()+name[1:]
    if len(name)>45: name=name[:44].rstrip()+'…'
    desc=' '.join((head+' '+rest).split())
    return name,(desc[:600]+'…') if len(desc)>600 else desc

def scenario_tests():
    """Все сценарии на диске как тесты для морды."""
    files=[f for f in sorted(glob.glob(os.path.join(SL,'*.js')))
           if os.path.basename(f) not in HELPERS]
    files+=sorted(glob.glob(os.path.join(RECORDED,'*.js')))
    hid=hidden_set(); out=[]
    for f in files:
        b=os.path.basename(f)
        rec=os.path.dirname(os.path.abspath(f))==os.path.abspath(RECORDED)
        if rec and b in hid: continue
        try: src=open(f,encoding='utf-8').read()
        except OSError: continue
        name,desc=scenario_doc(src)
        # У собранного сценария имя даёт человек, когда называет запись;
        # шапка там служебная, одна и та же у всех — в списке от неё толку нет.
        if rec: name=b[:-3]
        used={a or c for a,c in ENV_USE.findall(src)}
        params=[k for k,keys in PARAM_ENV if used & set(keys)]
        # Схему запросов кодоген кладёт рядом со сценарием; у написанного
        # руками её просто нет — тогда показывается одно описание.
        try: flow=json.load(open(f[:-3]+'.flow.json',encoding='utf-8'))
        except Exception: flow=[]
        out.append({'id':('recorded/'+b) if rec else b,
                    'name':(('Запись: '+name) if rec and name else name) or b[:-3],
                    'desc':desc,'params':params,'flow':flow,'recorded':rec})
    return out
DURATIONS=[
 {'v':'60s','n':'1 минута'},{'v':'3m','n':'3 минуты'},{'v':'5m','n':'5 минут'},
 {'v':'10m','n':'10 минут'},{'v':'20m','n':'20 минут'},{'v':'30m','n':'30 минут'},{'v':'60m','n':'1 час'},
]
# Профили ТЕМПА для тестов, где итерация = один вошедший человек (вход).
# Одна итерация тянет несколько запросов, поэтому 20 входов/с — это уже сотни rps.
PRESETS=[
 {'id':'target','name':'Целевая нагрузка','stages':'[{"target":4,"duration":"125s"}]'},
 {'id':'plateau','name':'Плато 5 минут','stages':'[{"target":4,"duration":"300s"}]'},
 {'id':'ramp','name':'Разгон до отказа','stages':'[{"target":4,"duration":"90s"},{"target":8,"duration":"90s"},{"target":12,"duration":"90s"},{"target":16,"duration":"90s"},{"target":20,"duration":"90s"}]'},
 {'id':'spike','name':'Спайк с возвратом','stages':'[{"target":2,"duration":"60s"},{"target":30,"duration":"10s"},{"target":30,"duration":"60s"},{"target":2,"duration":"120s"}]'},
 {'id':'soak','name':'Выдержка 30 минут','stages':'[{"target":4,"duration":"1800s"}]'},
]

# Профили ТЕМПА для тестов, где итерация = один запрос. Здесь target — это сразу
# запросы в секунду, поэтому значения должны быть в разы больше: профиль с пиком
# в два десятка до отказа не доведёт почти никакой сервер.
PRESETS_REQ=[
 {'id':'target-req','name':'Целевая нагрузка (35 запросов/с)','stages':'[{"target":35,"duration":"125s"}]'},
 {'id':'plateau-req','name':'Плато 60 запросов/с','stages':'[{"target":60,"duration":"300s"}]'},
 {'id':'ramp-req','name':'Разгон до отказа (20 → 120)','stages':'[{"target":20,"duration":"90s"},{"target":40,"duration":"90s"},{"target":60,"duration":"90s"},{"target":80,"duration":"90s"},{"target":100,"duration":"90s"},{"target":120,"duration":"90s"}]'},
 {'id':'spike-req','name':'Спайк с возвратом (20 → 150)','stages':'[{"target":20,"duration":"60s"},{"target":150,"duration":"10s"},{"target":150,"duration":"60s"},{"target":20,"duration":"120s"}]'},
 {'id':'soak-req','name':'Выдержка 30 минут (40 запросов/с)','stages':'[{"target":40,"duration":"1800s"}]'},
]

# Профили ПОТОКОВ. target здесь — доля от заданного числа потоков (в процентах):
# морда пересчитает её в штуки. Темп не ограничен — каждый поток шлёт запрос
# сразу, как получил ответ. Так ищут предел: сервер сам показывает, сколько тянет.
VU_PRESETS=[
 {'id':'vus-ramp','name':'Разгон потоков до предела','stages':'[{"target":25,"duration":"120s"},{"target":50,"duration":"120s"},{"target":75,"duration":"120s"},{"target":100,"duration":"120s"}]'},
 {'id':'vus-const','name':'Постоянная долбёжка','stages':'[{"target":100,"duration":"300s"}]'},
 {'id':'vus-spike','name':'Спайк: все потоки рывком','stages':'[{"target":10,"duration":"60s"},{"target":100,"duration":"10s"},{"target":100,"duration":"60s"},{"target":10,"duration":"60s"}]'},
 {'id':'vus-step','name':'Мелкими ступенями (10 шагов)','stages':'[{"target":10,"duration":"45s"},{"target":20,"duration":"45s"},{"target":30,"duration":"45s"},{"target":40,"duration":"45s"},{"target":50,"duration":"45s"},{"target":60,"duration":"45s"},{"target":70,"duration":"45s"},{"target":80,"duration":"45s"},{"target":90,"duration":"45s"},{"target":100,"duration":"45s"}]'},
]

def endpoints():
    """Что предложить в «разгоне одного запроса».

    Берём из спецификации в api/, а если её нет — из requests.json: список
    запросов сайта живёт там, а не в коде инструмента.
    """
    try: got=apispec.endpoints()
    except Exception: got=[]
    if got: return got
    try:
        m=json.load(open(os.path.join(SL,'requests.json'),encoding='utf-8'))
        return [r['path'] for r in m.get('requests') or [] if r.get('path')]
    except Exception:
        return []

STATE={'proc':None,'log':collections.deque(maxlen=400),'name':None,'started':None,'done':None,
       'dir':None,'progress':None,'stage':None,'errs':0}
LOCK=threading.Lock()

# k6 рисует прогресс строкой вида
#   ramp   [  42% ] 010/060 VUs  0m25.2s/1m0s  12.43 iters/s
# Для per-vu-iterations процент считается по итерациям, для arrival-rate — по времени;
# в обоих случаях сам k6 уже посчитал его правильно, поэтому берём готовое число.
PROG=re.compile(r'\[\s*(\d+)%\s*\]')
VUS=re.compile(r'(\d+)\s*/\s*(\d+)\s+VUs|(\d+)\s+VUs')
REQS=re.compile(r'(\d+) requests/(\d+) errors')
RPS=re.compile(r'RPS ([\d.]+)')
# «сессий 101» или «сессий 101+2» — завершённые и оборванные итерации.
SESS=re.compile(r'сессий (\d+)(?:\+(\d+))?')
CLOCK=re.compile(r'(\d[\dm.]*s)\s*/\s*(\d[\dm.]*s)')

def absorb(line):
    """Достаёт из строки k6 стадию и прогресс. Вызывается под LOCK."""
    if line.startswith('• '):
        STATE['stage']=line[2:].strip()
        m=re.search(r'ошибок с телом: (\d+)',line)
        if m: STATE['errs']=int(m.group(1))
        return
    # «Run [ 100% ] setup()» — это подготовка, а не прогон: если её пустить
    # в полосу, она прыгнет на 100% ещё до первого запроса.
    if line.startswith('Run '): 
        STATE['stage']='подготовка (вход в систему)'
        return
    m=PROG.search(line)
    if not m: return
    pr=STATE['progress'] or {}
    pr['pct']=int(m.group(1))
    v=VUS.search(line)
    if v: pr['vus']=int(v.group(1) or v.group(3) or 0); pr['vus_max']=int(v.group(2) or 0)
    c=CLOCK.search(line)
    if c: pr['elapsed'],pr['total']=c.group(1),c.group(2)
    q=REQS.search(line)
    if q: pr['reqs'],pr['errs']=int(q.group(1)),int(q.group(2))
    rp=RPS.search(line)
    if rp: pr['rps']=float(rp.group(1))
    se=SESS.search(line)
    if se: pr['sess'],pr['sess_bad']=int(se.group(1)),int(se.group(2) or 0)
    if STATE['stage'] is None or STATE['stage'].startswith(('подготовка','запускаем k6')):
        STATE['stage']='идёт нагрузка'
    # Профиль кончился, но прогон — нет: k6 ждёт, пока доработают начатые сессии
    # (gracefulStop). В прогоне 24.08 профиль был 10 минут, а всего вышло 13, и
    # все три минуты полоса стояла на 100% как вкопанная. Полоса меряет ВРЕМЯ
    # ПРОФИЛЯ и после 100% сказать ей больше нечего — за хвостом следят по числу
    # оставшихся потоков и завершённых сессий.
    if pr['pct']>=100 and STATE['stage']=='идёт нагрузка':
        STATE['stage']='доводим начатые сессии'
    if pr['pct']<100 and STATE['stage']=='доводим начатые сессии':
        STATE['stage']='идёт нагрузка'
    # Пик потоков нужен, чтобы показать, насколько хвост уже рассосался.
    if pr.get('vus') is not None:
        pr['vus_peak']=max(pr.get('vus_peak',0),pr['vus'])
    STATE['progress']=pr

USERS=os.path.join(SL,'users.json')

def load_users():
    try: return json.load(open(USERS,encoding='utf-8'))
    except Exception: return []

def save_users(lst):
    json.dump(lst,open(USERS,'w',encoding='utf-8'),ensure_ascii=False,indent=1)


# ---------- Роли ----------
# Роль — это группа профилей и одновременно префикс переменных. Список ролей
# заводит человек: инструмент не знает, как называются права на его сайте.
# Выключенная роль целиком выпадает из прогонов — удобнее, чем гасить профили
# по одному, когда надо «сегодня без администраторов».
ROLES=os.path.join(SL,'roles.json')

def load_roles():
    try: got=json.load(open(ROLES,encoding='utf-8'))
    except Exception: got=[]
    known={r.get('id') for r in got if isinstance(r,dict)}
    # Роль, которая есть у профиля, но не заведена в файле, показываем всё
    # равно: иначе профиль потерялся бы из таблицы вместе со своей группой.
    for u in load_users():
        r=(u.get('role') or '').strip()
        if r and r not in known:
            got.append({'id':r,'name':r,'enabled':True}); known.add(r)
    return got

def save_roles(lst):
    json.dump(lst,open(ROLES,'w',encoding='utf-8'),ensure_ascii=False,indent=1)

def role_on(role):
    """Включена ли группа. Незаведённая роль считается включённой."""
    for r in load_roles():
        if r.get('id')==role: return bool(r.get('enabled',True))
    return True

# ---------- Настройки внешних систем (Beszel, Telescope) ----------
# Лежат отдельно от кода в secrets.json и не попадают ни в репозиторий
# (см. .gitignore), ни обратно в браузер: наружу отдаётся только признак
# «пароль задан», сам пароль страница не получает никогда. Поэтому пустое
# поле пароля при сохранении означает «оставить как было», а не «стереть»:
# иначе любое редактирование адреса сносило бы пароль.
SECRETS=config.SECRETS
# Поля, которые наружу не отдаются ни при каких условиях.
HIDDEN=('password','cookie','token')
SETTINGS_SHAPE={
 'beszel':   ['url','email','password','systemId'],
 'telescope':['url','email','password','cookie'],
 # Фронт стенда закрыт basic-авторизацией nginx: без неё запись поймает
 # только 401 вместо страницы. Логин с паролем держим здесь, а не в коде.
 'site':     ['url','login','password'],
}

# Чем заполнить пустое поле, если secrets.json ещё не заведён: свежий clone
# настраивают одним .env, а морда потом правит поверх него.
ENV_FALLBACK={
 'beszel':   {'url':'BESZEL_URL','email':'BESZEL_EMAIL',
              'password':'BESZEL_PASSWORD','systemId':'BESZEL_SYSTEM_ID'},
 'telescope':{'url':'TELESCOPE_URL','email':'TELESCOPE_EMAIL',
              'password':'TELESCOPE_PASSWORD','cookie':'TELESCOPE_COOKIE'},
 'site':     {'url':'SITE_URL','login':'SITE_LOGIN','password':'SITE_PASSWORD'},
}

def load_secrets():
    try: d=json.load(open(SECRETS,encoding='utf-8'))
    except Exception: d={}
    for sec,keys in SETTINGS_SHAPE.items():
        cur=d.get(sec) or {}
        env=ENV_FALLBACK.get(sec,{})
        d[sec]={k:(cur.get(k,'') or os.environ.get(env.get(k,''),'') or '') for k in keys}
    return d

def save_secrets(new):
    """Слияние с тем, что лежит на диске: пустой секрет не затирает старый."""
    cur=load_secrets()
    for sec,keys in SETTINGS_SHAPE.items():
        got=(new.get(sec) or {})
        for k in keys:
            v=got.get(k)
            if v is None: continue
            v=str(v).strip()
            if k in HIDDEN and v=='': continue     # не трогаем сохранённый секрет
            cur[sec][k]=v
    tmp=SECRETS+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:
        json.dump(cur,f,ensure_ascii=False,indent=1)
    # 0600 до переименования: между созданием файла и chmod иначе есть окно,
    # в котором пароль лежит с правами по умолчанию.
    os.chmod(tmp,0o600); os.replace(tmp,SECRETS)
    return cur

def public_secrets():
    """Вид для страницы: вместо секретов — только «задан или нет»."""
    cur=load_secrets(); out={}
    for sec,keys in SETTINGS_SHAPE.items():
        out[sec]={k:('' if k in HIDDEN else cur[sec][k]) for k in keys}
        for k in keys:
            if k in HIDDEN: out[sec]['has_'+k]=bool(cur[sec][k])
    return out

def secrets_for(section):
    """Настройки для кода тестов и разборщиков. Пустой раздел — значит не задан."""
    d=load_secrets().get(section) or {}
    return d if d.get('url') else {}

def check_beszel(cfg):
    import urllib.request
    hub=(cfg.get('url') or '').rstrip('/')
    if not hub: return {'ok':False,'why':'не задан адрес'}
    try:
        r=urllib.request.Request(hub+'/api/collections/users/auth-with-password',
            data=json.dumps({'identity':cfg.get('email',''),
                             'password':cfg.get('password','')}).encode(),
            headers={'Content-Type':'application/json'})
        tok=json.load(urllib.request.urlopen(r,timeout=20))['token']
    except Exception as e:
        return {'ok':False,'why':'вход не прошёл: %s'%str(getattr(e,'code',e))[:60]}
    sid=cfg.get('systemId') or ''
    if not sid: return {'ok':True,'why':'вход есть, id системы не задан — метрики не привяжутся'}
    try:
        flt=urllib.parse.quote('system="%s" && type="1m"'%sid)
        r=urllib.request.Request(hub+'/api/collections/system_stats/records?perPage=1&sort=-created&filter=(%s)'%flt,
            headers={'Authorization':tok})
        items=json.load(urllib.request.urlopen(r,timeout=20))['items']
    except Exception as e:
        return {'ok':False,'why':'вход есть, но метрики не читаются: %s'%str(e)[:60]}
    if not items: return {'ok':False,'why':'вход есть, но по этому id системы записей нет'}
    return {'ok':True,'why':'последняя точка '+items[0].get('created','')[:19]}

def check_telescope(cfg):
    """Telescope закрыт гейтом Laravel, и пройти его можно двумя путями:
    профилем, который гейт пускает, либо готовой Cookie из браузера.
    Проверяем тот, который заполнен."""
    import urllib.request,http.cookiejar
    base=(cfg.get('url') or '').rstrip('/')
    if not base: return {'ok':False,'why':'не задан адрес'}
    api=base+'/telescope/telescope-api/requests'
    cj=http.cookiejar.CookieJar()
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    h={'Accept':'application/json','Content-Type':'application/json'}
    if cfg.get('cookie'): h['Cookie']=cfg['cookie']
    elif cfg.get('email'):
        # Гейт пускает по сессии приложения, поэтому сначала обычный вход —
        # тем же флоу из .env, что и везде.
        try:
            config.site_login(op,base,cfg['email'],cfg.get('password',''),cj)
        except Exception as e:
            return {'ok':False,'why':'вход в приложение не прошёл: %s'%str(getattr(e,'code',e))[:60]}
    try:
        r=urllib.request.Request(api,data=b'{"take":1}',headers=h,method='POST')
        z=op.open(r,timeout=25); body=z.read(400)
    except Exception as e:
        code=getattr(e,'code',0)
        if code==403:
            return {'ok':False,'why':'403 от гейта Laravel: пользователя не пускают в Telescope'}
        if code==404:
            return {'ok':False,'why':'404: Telescope на этом адресе не установлен'}
        return {'ok':False,'why':'нет ответа: %s'%str(code or e)[:60]}
    return {'ok':True,'why':'записи читаются (%d байт в пробном ответе)'%len(body)}


def check_site(cfg):
    """Проверка фронта: пустит ли basic-авторизация и куда фронт ходит за данными.

    Второе важнее, чем кажется. Адрес API зашит в бандл при сборке, и после
    передеплоя фронт может начать ходить в чужой контур — тогда запись сессии
    ломается ещё на входе, а по самому фронту всё выглядит исправным.
    """
    import urllib.request,base64
    site=(cfg.get('url') or '').strip().rstrip('/')
    if not site: return {'ok':False,'why':'не задан адрес'}
    if not re.match(r'https?://',site): site='https://'+site
    h={}
    if cfg.get('login') and cfg.get('password'):
        h['Authorization']='Basic '+base64.b64encode(
            f"{cfg['login']}:{cfg['password']}".encode()).decode()
    try:
        page=urllib.request.urlopen(urllib.request.Request(site+'/',headers=h),
                                    timeout=25).read().decode('utf-8','replace')
    except Exception as e:
        code=getattr(e,'code',0)
        if code==401:
            return {'ok':False,'why':'401: логин или пароль basic-авторизации не подошли'}
        return {'ok':False,'why':'нет ответа: %s'%str(code or e)[:60]}

    bundles=re.findall(r'/assets/([\w.-]+\.js)',page)
    hosts=set()
    for b in bundles[:3]:
        try:
            js=urllib.request.urlopen(urllib.request.Request(
                '%s/assets/%s'%(site,b),headers=h),timeout=30).read().decode('utf-8','replace')
        except Exception:
            continue
        hosts|=set(re.findall(r'https://api\.[\w.-]+',js))
    if not hosts:
        return {'ok':True,'why':'фронт отвечает, адрес API в бандле не найден'}
    want='https://'+STAND
    bad=[x for x in hosts if x!=want]
    if bad:
        return {'ok':False,'why':'фронт отвечает, но ходит в %s, а стенд — %s: '
                                'запись сессии упрётся в CORS'%(', '.join(sorted(bad)),want)}
    return {'ok':True,'why':'фронт отвечает и ходит в %s'%want}


def role_prefix(role):
    """Роль → префикс переменных: student даёт STUDENT_EMAIL/STUDENT_PASS.

    Какие роли бывают на сайте, инструмент не знает: их называет тот, кто
    заводит профили. Список ролей в коде был бы третьим местом, где одно и то
    же имя надо держать в согласии с .env и сценариями, — и первым, которое
    с ними разъедется.
    """
    return re.sub(r'[^A-Z0-9]+','_',(role or '').upper()).strip('_')


def creds_env(env):
    """Раздаёт включённые профили в тесты.

    Тесты берут профили двумя способами, и оба надо накрыть:
      - пулом из файла — им пишем временный файл и отдаём через ACCOUNTS;
      - парой <РОЛЬ>_EMAIL/<РОЛЬ>_PASS (все, кто входит одной ролью).
    Выключенный в морде профиль не должен попасть никуда.
    """
    # Если фронт закрыт basic-авторизацией, без неё вся статика в прогоне
    # отвечает 401, и отчёт показывает поломку там, где её нет.
    site,auth=site_cfg()
    env.setdefault('SITE_URL',site)
    if auth: env.setdefault('SITE_AUTH',auth)

    # Сценарии не знают карты стенда: id приходят из pool.json.
    config.pool_env(env)

    # Профиль идёт в прогон, только если включён он сам И включена его группа.
    users=[u for u in load_users() if u.get('enabled') and role_on(u.get('role') or '')]
    if not users: return env

    pool=[{'email':u['email'],'password':u['password'],'role':u.get('role') or '',
           'weight':int(u.get('weight') or 1)} for u in users]
    tmp=os.path.join(SL,'.pool.json')
    json.dump(pool,open(tmp,'w',encoding='utf-8'),ensure_ascii=False)
    env['ACCOUNTS']=tmp

    # Сценарию, который ходит одной ролью, morda называет её сама — по первому
    # включённому профилю. Иначе такой тест из морды не запустить, не открыв .env.
    if users and (users[0].get('role') or ''):
        env.setdefault('ROLE',users[0]['role'])

    # Первый включённый профиль каждой роли — тот, кем ходят сценарии,
    # входящие одной ролью. Роли перебираем те, что реально есть в файле.
    for role in dict.fromkeys(u.get('role') or '' for u in users):
        pre=role_prefix(role)
        if not pre: continue
        first=next(u for u in users if (u.get('role') or '')==role)
        env[pre+'_EMAIL'],env[pre+'_PASS']=first['email'],first['password']
    return env


def check_login(email,password):
    """Проверка профиля прямо из морды: рабочий или нет.

    Ходит тем же флоу, что и сценарии, — из .env. Иначе морда проверяла бы
    вход, которого на этом сайте нет, и врала бы в обе стороны.
    """
    import urllib.request,http.cookiejar
    API=(os.environ.get('API_URL') or ('https://'+STAND)).rstrip('/')
    cj=http.cookiejar.CookieJar()
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        d=config.site_login(op,API,email,password,cj)
    except ValueError as e:
        return {'ok':False,'why':str(e)}
    except Exception as e:
        return {'ok':False,'why':'вход %s'%str(getattr(e,'code',e))[:60]}
    probe=os.environ.get('PROBE_PATH')
    if probe:
        try:
            op.open(urllib.request.Request(API+probe,
                headers={'Accept':'application/json'}),timeout=25)
        except Exception as e:
            return {'ok':False,'why':'probe %s'%str(getattr(e,'code',e))[:40]}
    return {'ok':True,'uid':config.dig(d,os.environ.get('USER_ID_PATH') or 'data.id')}


def run_test(script,name,env):
    e=dict(os.environ); e.update(env)
    cmd=[sys.executable,os.path.join(SL,'run.py'),script,'--name',name]
    with LOCK:
        STATE['log'].clear(); STATE['name']=name; STATE['done']=None; STATE['dir']=None
        STATE['progress']=None; STATE['stage']='запускаем k6'; STATE['errs']=0
        STATE['started']=datetime.datetime.now().strftime('%H:%M:%S')
        STATE['started_ts']=time.time()
    p=subprocess.Popen(cmd,cwd=SL,env=e,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                       text=True,bufsize=1)
    with LOCK: STATE['proc']=p
    for line in p.stdout:
        line=line.rstrip()
        with LOCK:
            STATE['log'].append(line)
            if line.startswith('→ '): STATE['dir']=line[2:].strip()
            else: absorb(line)
    p.wait()
    with LOCK:
        STATE['proc']=None; STATE['done']=p.returncode
        STATE['stage']='завершён' if p.returncode==0 else 'упал'
        if STATE['progress']: STATE['progress']['pct']=100

def scenario_names():
    """id сценария → как он называется в списке тестов.

    Сверка знает только имена файлов: она читает диск, а не список тестов.
    Показывать в отметке покрытия «mix.js» вместо «Микс запросов» — значит
    заставлять человека держать соответствие в голове.
    """
    return {t['id']:t['name'] for t in scenario_tests()}


def coverage_report(file=None):
    r=coverage.report(file)
    names=scenario_names()
    for o in r['ops']: o['by']=[names.get(x,x) for x in o['by']]
    return r


def runs_base():
    return os.path.join(config.RESULTS,STAND)


def reindex():
    """Пересобрать index.html после удаления: он лежит рядом и ссылается на папки."""
    try:
        sys.path.insert(0,SL)
        import report_html
        report_html.index(runs_base())
    except Exception:
        pass


def del_report(dd):
    """Удалить один прогон целиком — вместе с сырыми данными.

    Имя папки приходит из браузера, поэтому проверяем, что это именно прямой
    потомок каталога прогонов: «../..» иначе стёр бы что угодно.
    """
    base=os.path.abspath(runs_base())
    if not dd or '/' in dd or '\\' in dd or dd in ('.','..'):
        raise ValueError('недопустимое имя прогона')
    d=os.path.abspath(os.path.join(base,dd))
    if os.path.dirname(d)!=base or not os.path.isdir(d):
        raise ValueError('такого прогона нет')
    shutil.rmtree(d)
    reindex()


def clear_reports():
    """Стереть все прогоны стенда. Сам каталог стенда остаётся."""
    base=runs_base(); n=0
    for d in sorted(glob.glob(os.path.join(base,'*'))):
        if os.path.isdir(d):
            shutil.rmtree(d); n+=1
    reindex()
    return n


def reports():
    base=os.path.join(config.RESULTS,STAND); out=[]
    # Порядок — по времени ЗАВЕРШЕНИЯ (mtime summary.json), новые сверху.
    # В имени папки лежит время СТАРТА, поэтому длинный прогон, начатый раньше,
    # при сортировке по имени всплывал выше короткого, законченного позже.
    paths=sorted(glob.glob(os.path.join(base,'*','summary.json')),
                 key=lambda x:os.path.getmtime(x),reverse=True)
    for p in paths:
        try: s=json.load(open(p))
        except: continue
        m=s['meta']; a=s.get('agg') or {}
        err=sum(r['errors'] for r in a.get('rows',[])); tot=a.get('total',0)
        dd=os.path.basename(os.path.dirname(p))
        secs=a.get('duration',0) or 0
        dur=f"{int(secs)//60}м {int(secs)%60:02d}с" if secs>=60 else f"{secs:.0f}с"
        out.append({'dir':dd,'name':m['name'],'when':m['when'],
            'finished':datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime('%d.%m.%Y %H:%M'),
            'dur':dur,'file':report_file(dd),
            'total':tot,'rps':round(a.get('rps_avg',0),1),'p95':round(a.get('p95',0)),
            'err':round(100*err/tot,1) if tot else 0})
    return out

HAR_DIR=config.HAR_DIR

def har_list():
    """Записи браузера и собранные по ним отчёты.

    Лежат отдельно от results/: там прогоны нагрузки, у которых своя структура
    (summary.json, сырой поток k6), и мешать одно с другим — значит сломать
    и список прогонов, и уборку отчётов.
    """
    out=[]
    if not os.path.isdir(HAR_DIR): return out
    for f in sorted(glob.glob(os.path.join(HAR_DIR,'*.har')),
                    key=lambda x:os.path.getmtime(x),reverse=True):
        base=os.path.basename(f)[:-4]
        rep=os.path.join(HAR_DIR,'otchet-'+base+'.html')
        tele=os.path.join(HAR_DIR,'tele-'+base+'.json')
        n=None
        try: n=len(json.load(open(f,encoding='utf-8'))['log']['entries'])
        except Exception: pass
        out.append({'har':os.path.basename(f),'name':base,'entries':n,
            'mb':round(os.path.getsize(f)/1048576,1),
            'when':datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%d.%m.%Y %H:%M'),
            'report':os.path.basename(rep) if os.path.exists(rep) else None,
            'tele':os.path.basename(tele) if os.path.exists(tele) else None,
            'script':os.path.exists(os.path.join(RECORDED,base+'.js'))})
    return out

def har_build(name,with_tele=False):
    """Собрать отчёт по одной записи. Telescope — по желанию: он есть не на
    каждом стенде, и без него отчёт всё равно собирается, только без сверки."""
    har=os.path.join(HAR_DIR,name)
    if not os.path.exists(har) or not os.path.abspath(har).startswith(HAR_DIR):
        return {'error':'нет такой записи'}
    base=os.path.basename(name)[:-4]
    tele=os.path.join(HAR_DIR,'tele-'+base+'.json')
    log=[]
    if with_tele:
        r=subprocess.run([sys.executable,os.path.join(SL,'telescope.py'),
                          '--har',har,'--out',tele],capture_output=True,text=True,timeout=600)
        log.append((r.stdout or r.stderr).strip()[-400:])
        if r.returncode!=0:
            return {'error':'Telescope: '+((r.stderr or r.stdout).strip()[-200:] or 'не вышло')}
    out=os.path.join(HAR_DIR,'otchet-'+base+'.html')
    cmd=[sys.executable,os.path.join(SL,'har_report.py'),har]
    if os.path.exists(tele): cmd.append(tele)
    cmd+=['--out',out]
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=600)
    if r.returncode!=0:
        return {'error':(r.stderr or r.stdout).strip()[-300:]}
    log.append((r.stdout or '').strip())
    return {'ok':True,'report':os.path.basename(out),'log':' · '.join(x for x in log if x)}

# ---------- Запись сессии через браузер ----------
# Раньше запись делалась руками: DevTools → «сохранить как HAR» → положить файл
# в har/. Здесь то же самое делает Playwright, зато честно помечает, что взято
# из кэша браузера, — DevTools этот признак теряет при экспорте.
REC_DIR=os.path.join(SL,'recorder')
REC={'proc':None,'name':None,'har':None,'started':None,'log':collections.deque(maxlen=60)}

def safe_har(name):
    """Имя записи приходит из браузера и становится именем файла в har/."""
    n=(name or '').strip()
    if not n or n.startswith('.') or '/' in n or '\\' in n:
        raise ValueError('в имени нельзя использовать «/», «\\» и точку в начале')
    return n[:80]+'.har'

def site_cfg():
    """Адрес фронта и basic-авторизация к нему.

    Адреса по умолчанию нет в настройках, поэтому выводим его из адреса API:
    на всех стендах фронт — это тот же домен без префикса `api.`.
    """
    c=load_secrets().get('site') or {}
    url=(c.get('url') or '').strip() or 'https://'+re.sub(r'^api\.','',STAND)
    login,pw=(c.get('login') or '').strip(),(c.get('password') or '').strip()
    return url,(f'{login}:{pw}' if login and pw else '')

def rec_pump(p):
    """Читаем вывод рекордера, иначе труба заполнится и он встанет."""
    for line in p.stdout:
        with LOCK: REC['log'].append(line.rstrip())
    p.wait()
    with LOCK:
        REC['proc']=None
        # 130 и -2 — это тот самый SIGINT, которым мы его и остановили: Node
        # успевает дописать HAR, но выходит с кодом сигнала. Считать это
        # падением значит пугать сообщением о поломке после каждой записи.
        ok=p.returncode in (0,130,-2)
        REC['log'].append('запись остановлена' if ok else f'рекордер упал (код {p.returncode})')

def rec_start(name,url=None,use_auth=True):
    with LOCK:
        if REC['proc'] is not None: return {'error':'запись уже идёт'}
    fn=safe_har(name)
    if os.path.exists(os.path.join(HAR_DIR,fn)):
        return {'error':'запись с таким именем уже есть — выберите другое'}
    if not os.path.isdir(os.path.join(REC_DIR,'node_modules')):
        return {'error':'рекордер не установлен: выполните ./setup.sh (или npm install в recorder/)'}
    os.makedirs(HAR_DIR,exist_ok=True)
    site,auth=site_cfg()
    start=(url or '').strip() or site
    # Без схемы рекордер не сможет вставить в адрес basic-авторизацию
    # (он подменяет «://»), да и Playwright такой адрес не откроет.
    if not re.match(r'https?://',start): start='https://'+start.lstrip('/')
    cmd=['node','record.mjs','--out',os.path.join(HAR_DIR,fn),'--url',start]
    # Заголовок авторизации идёт на КАЖДЫЙ запрос контекста, поэтому запись
    # чужого хоста должна уметь обойтись без него.
    if auth and use_auth: cmd+=['--auth',auth]
    try:
        p=subprocess.Popen(cmd,cwd=REC_DIR,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                           text=True,bufsize=1)
    except FileNotFoundError:
        return {'error':'не найден node — поставьте Node.js'}
    with LOCK:
        REC.update(proc=p,name=name.strip(),har=fn,started=time.time())
        REC['log'].clear()
    threading.Thread(target=rec_pump,args=(p,),daemon=True).start()
    return {'ok':True,'har':fn}

def rec_stop():
    """SIGINT, а не kill: рекордер дописывает HAR именно по нему."""
    with LOCK: p=REC['proc']
    if p is None: return {'error':'запись не идёт'}
    try: p.send_signal(signal.SIGINT)
    except Exception: pass
    try: p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        return {'error':'рекордер не ответил на остановку, процесс снят — запись могла не сохраниться'}
    with LOCK: har=REC['har']
    return {'ok':True,'har':har,'entries':har_entries(har)}

def har_entries(fn):
    try: return len(json.load(open(os.path.join(HAR_DIR,fn),encoding='utf-8'))['log']['entries'])
    except Exception: return None

def rec_status():
    with LOCK:
        return {'running':REC['proc'] is not None,'name':REC['name'],'har':REC['har'],
                'elapsed':int(time.time()-REC['started']) if REC['started'] else 0,
                'log':list(REC['log'])[-20:]}

def har_paths(fn):
    """Всё, что относится к одной записи: сама запись, отчёт по ней, выгрузка
    Telescope и собранный из неё сценарий. Переименование и удаление должны
    двигать их вместе, иначе сценарий останется привязанным к исчезнувшей записи."""
    base=fn[:-4]
    return [os.path.join(HAR_DIR,x) for x in (fn,'otchet-'+base+'.html','tele-'+base+'.json')] \
           +[os.path.join(RECORDED,base+'.js')]

def har_del(fn):
    """Имя приходит из браузера, поэтому сверяем, что файл лежит прямо в har/."""
    p=os.path.abspath(os.path.join(HAR_DIR,fn))
    if not fn.endswith('.har') or os.path.dirname(p)!=os.path.abspath(HAR_DIR):
        raise ValueError('нет такой записи')
    if not os.path.isfile(p): raise ValueError('нет такой записи')
    for f in har_paths(fn):
        if os.path.exists(f): os.remove(f)

def har_rename(fn,name):
    new=safe_har(name)
    if new==fn: return new
    old=os.path.abspath(os.path.join(HAR_DIR,fn))
    if not fn.endswith('.har') or os.path.dirname(old)!=os.path.abspath(HAR_DIR) \
            or not os.path.isfile(old):
        raise ValueError('нет такой записи')
    if os.path.exists(os.path.join(HAR_DIR,new)):
        raise ValueError('запись с таким именем уже есть')
    for src,dst in zip(har_paths(fn),har_paths(new)):
        if os.path.exists(src): os.rename(src,dst)
    return new

RECORDED=os.path.join(SL,'recorded')

# Скрытый сценарий остаётся файлом на диске, но не мозолит глаза в списке
# тестов. Нужно, когда записей набралось много, а гоняешь две-три из них.
HIDDEN=os.path.join(RECORDED,'.hidden.json')

def hidden_set():
    try: return set(json.load(open(HIDDEN,encoding='utf-8')))
    except Exception: return set()

def scenario_file(fn):
    """Проверенный путь к сценарию: имя приходит из браузера."""
    p=os.path.abspath(os.path.join(RECORDED,fn))
    if not fn.endswith('.js') or os.path.dirname(p)!=os.path.abspath(RECORDED) \
            or not os.path.isfile(p):
        raise ValueError('нет такого сценария')
    return p

def scenarios():
    """Все собранные сценарии — и показанные, и скрытые."""
    hid=hidden_set(); out=[]
    for f in sorted(glob.glob(os.path.join(RECORDED,'*.js'))):
        fn=os.path.basename(f)
        out.append({'file':fn,'name':fn[:-3],'hidden':fn in hid,
            'kb':round(os.path.getsize(f)/1024,1),
            'har':os.path.exists(os.path.join(HAR_DIR,fn[:-3]+'.har')),
            'when':datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%d.%m.%Y %H:%M')})
    return out

def scenario_hide(fn,hide):
    scenario_file(fn)
    hid=hidden_set()
    hid.add(fn) if hide else hid.discard(fn)
    json.dump(sorted(hid),open(HIDDEN,'w',encoding='utf-8'),ensure_ascii=False)

def scenario_drop(fn):
    """Удалить сценарий и его карту. Запись при этом остаётся."""
    p=scenario_file(fn)
    os.remove(p)
    fl=p[:-3]+'.flow.json'
    if os.path.exists(fl): os.remove(fl)
    hid=hidden_set()
    if fn in hid:
        hid.discard(fn)
        json.dump(sorted(hid),open(HIDDEN,'w',encoding='utf-8'),ensure_ascii=False)

def scenario_path(har):
    """Файл сценария для записи. Имя записи уже проверено safe_har."""
    return os.path.join(RECORDED,har[:-4]+'.js')

def scenario_del(har):
    """Убрать собранный сценарий, оставив саму запись.

    Отдельно от удаления записи: сценарий пересобирается из неё за секунду,
    а вот запись человек проходил руками — терять её ради чистки списка тестов
    незачем.
    """
    p=os.path.abspath(scenario_path(har))
    if not har.endswith('.har') or os.path.dirname(p)!=os.path.abspath(RECORDED):
        raise ValueError('нет такого сценария')
    if not os.path.isfile(p): raise ValueError('сценарий не собран')
    os.remove(p)

def gen_scenario(har):
    p=os.path.abspath(os.path.join(HAR_DIR,har))
    if not har.endswith('.har') or os.path.dirname(p)!=os.path.abspath(HAR_DIR) \
            or not os.path.isfile(p):
        return {'error':'нет такой записи'}
    os.makedirs(RECORDED,exist_ok=True)
    out=scenario_path(har)
    r=subprocess.run([sys.executable,os.path.join(SL,'codegen.py'),p,
                      '--out',out,'--name',har[:-4]],capture_output=True,text=True,timeout=600)
    if r.returncode!=0:
        return {'error':(r.stderr or r.stdout).strip()[-300:]}
    return {'ok':True,'script':'recorded/'+os.path.basename(out),'log':(r.stdout or '').strip()}

def start_smoke(har):
    """Смоук — обычный прогон в один поток и одну итерацию.

    Отдельного раннера ему не нужно: тот же run.py, тот же отчёт, те же профили.
    Разница только в SMOKE=1, которым сценарий выбирает короткий профиль, и в
    отключённых паузах — иначе проверка длилась бы столько же, сколько запись.
    """
    with LOCK:
        if STATE['proc'] is not None: return {'error':'прогон уже идёт'}
    if not os.path.isfile(scenario_path(har)):
        r=gen_scenario(har)
        if r.get('error'): return r
    fix=refresh_statics(har)
    # Подробности правки статики нужны в отчёте, а не в списке записей: там их
    # десятки строк. run.py положит их в summary.json, отчёт покажет их блоком.
    env=creds_env({'SMOKE':'1','STATICS_FIX':json.dumps(fix,ensure_ascii=False)})
    name='смоук '+har[:-4]
    threading.Thread(target=run_test,args=('recorded/'+har[:-4]+'.js',name,env),daemon=True).start()
    return {'ok':True,'name':name,'statics':fix}


def refresh_statics(har):
    """Обновить в сценарии имена файлов сборки под сегодняшний стенд."""
    site,auth=site_cfg()
    sys.path.insert(0,SL)
    import codegen
    try:
        return codegen.refresh_statics(scenario_path(har),site,auth)
    except Exception as e:
        return {'error':str(e)[:200]}


def report_file(dd):
    """Имя html-файла отчёта. Старые прогоны знают только report.html."""
    import report_html
    fn=report_html.report_name(None,dd)
    return fn if os.path.exists(os.path.join(config.RESULTS,STAND,dd,fn)) else 'report.html'

def restart_self():
    """Просьба перезапуститься. Сам exec делает ГЛАВНЫЙ поток.

    Раньше здесь же стоял os.execv, и это была гонка: shutdown() возвращает
    управление serve_forever() в главном потоке, main() доходит до конца, и
    интерпретатор выходит, НЕ дожидаясь daemon-потоков. Кто успеет первым —
    тот и выиграл, поэтому перезапуск то работал, то ронял сервер насовсем.
    Теперь поток только просит остановиться, а execv выполняется после
    serve_forever(), когда слушающий сокет уже гарантированно закрыт.
    """
    # Дать HTTP-ответу «ok» уйти в браузер до того, как закроем сокет.
    time.sleep(0.3)
    RESTART[0]=True
    # flush обязателен: os.execv затирает процесс вместе с небуферизованным
    # выводом, и лог перезапуска пропадает — а он единственный след для разбора.
    print('\nПерезапуск по просьбе из браузера...',flush=True)
    # shutdown() нельзя звать из потока, который крутит serve_forever, —
    # он ждёт его выхода и получился бы дедлок. Мы в отдельном потоке, всё честно.
    try: SRV[0].shutdown()
    except Exception: pass

RESTART=[False]
SRV=[None]

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,code,body,ctype='application/json'):
        b=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header('Content-Type',ctype+'; charset=utf-8')
        # Без no-store браузер кеширует и страницу, и /api/*: страница отдаётся
        # из памяти процесса, поэтому после перезапуска морда выглядит ровно
        # так же, как до него, и правка кажется не применившейся.
        self.send_header('Cache-Control','no-store, must-revalidate')
        self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u=urllib.parse.urlparse(self.path); p=u.path
        if p=='/': return self._send(200,PAGE,'text/html')
        if p=='/api/meta':
            return self._send(200,json.dumps({'tests':scenario_tests(),'presets':PRESETS,
                'presetsReq':PRESETS_REQ,'vuPresets':VU_PRESETS,
                'endpoints':endpoints(),'durations':DURATIONS,'stand':STAND},ensure_ascii=False))
        if p=='/api/status':
            with LOCK:
                return self._send(200,json.dumps({'running':STATE['proc'] is not None,
                    'name':STATE['name'],'started':STATE['started'],'done':STATE['done'],
                    'dir':os.path.basename(STATE['dir']) if STATE['dir'] else None,
                    'file':report_file(os.path.basename(STATE['dir'])) if STATE['dir'] else None,
                    'progress':STATE['progress'],'stage':STATE['stage'],'errs':STATE['errs'],
                    'elapsed':int(time.time()-STATE['started_ts']) if STATE.get('started_ts') else 0,
                    'log':list(STATE['log'])[-200:]},ensure_ascii=False))
        if p=='/api/reports': return self._send(200,json.dumps(reports(),ensure_ascii=False))
        if p=='/api/users': return self._send(200,json.dumps(load_users(),ensure_ascii=False))
        if p=='/api/roles': return self._send(200,json.dumps(load_roles(),ensure_ascii=False))
        if p=='/api/settings': return self._send(200,json.dumps(public_secrets(),ensure_ascii=False))
        if p=='/api/har': return self._send(200,json.dumps(har_list(),ensure_ascii=False))
        if p=='/api/scenarios': return self._send(200,json.dumps(scenarios(),ensure_ascii=False))
        if p=='/api/coverage':
            f=(urllib.parse.parse_qs(u.query).get('file') or [None])[0]
            try: res=coverage_report(f)
            except Exception as e: res={'error':str(e)[:200],'ops':[]}
            return self._send(200,json.dumps(res,ensure_ascii=False))
        if p=='/api/spec':
            out=[]
            for x in apispec.specs():
                b=apispec.brief(x)
                if not x.get('error'):
                    try: b.update(coverage.counts(x['file']))
                    except Exception: pass
                out.append(b)
            return self._send(200,json.dumps({'specs':out,'yaml':bool(apispec.yaml),
                'dir':os.path.relpath(apispec.DIR,SL)},ensure_ascii=False))
        if p=='/api/record':
            st=rec_status(); st['site']=site_cfg()[0]; st['auth']=bool(site_cfg()[1])
            return self._send(200,json.dumps(st,ensure_ascii=False))
        if p.startswith('/har/'):
            f=os.path.join(ROOT,urllib.parse.unquote(p[1:]))
            if os.path.isfile(f) and os.path.abspath(f).startswith(HAR_DIR):
                ct='text/html' if f.endswith('.html') else 'application/json'
                return self._send(200,open(f,'rb').read(),ct)
            return self._send(404,'not found','text/plain')
        if p.startswith('/results/'):
            f=os.path.join(ROOT,urllib.parse.unquote(p[1:]))
            if os.path.isfile(f) and os.path.abspath(f).startswith(config.RESULTS):
                ct='text/html' if f.endswith('.html') else 'application/json'
                return self._send(200,open(f,'rb').read(),ct)
            return self._send(404,'not found','text/plain')
        return self._send(404,'{}')
    def do_POST(self):
        if self.path=='/api/users':
            n=int(self.headers.get('Content-Length',0))
            try:
                lst=json.loads(self.rfile.read(n) or b'[]')
                save_users(lst)
                return self._send(200,json.dumps({'ok':True,'n':len(lst)},ensure_ascii=False))
            except Exception as e:
                return self._send(400,json.dumps({'error':str(e)[:200]},ensure_ascii=False))
        if self.path=='/api/roles':
            n=int(self.headers.get('Content-Length',0))
            try:
                lst=json.loads(self.rfile.read(n) or b'[]')
                save_roles(lst)
                return self._send(200,json.dumps({'ok':True,'n':len(lst)},ensure_ascii=False))
            except Exception as e:
                return self._send(400,json.dumps({'error':str(e)[:200]},ensure_ascii=False))
        if self.path=='/api/settings':
            n=int(self.headers.get('Content-Length',0))
            try:
                save_secrets(json.loads(self.rfile.read(n) or b'{}'))
                return self._send(200,json.dumps({'ok':True},ensure_ascii=False))
            except Exception as e:
                return self._send(400,json.dumps({'error':str(e)[:200]},ensure_ascii=False))
        if self.path=='/api/spec':
            # Файл приходит телом как есть: multipart разбирать нечем —
            # cgi из стандартной библиотеки убран, а зависимостей у нас нет.
            n=int(self.headers.get('Content-Length',0))
            if n>apispec.MAX:
                # Тело всё равно вычитываем: ответить не читая — значит оборвать
                # загрузку на полуслове, и браузер покажет невнятное
                # «Failed to fetch» вместо причины отказа.
                left=n
                while left>0:
                    chunk=self.rfile.read(min(left,1<<20))
                    if not chunk: break
                    left-=len(chunk)
                return self._send(200,json.dumps({'error':'файл больше 24 МБ'},ensure_ascii=False))
            name=urllib.parse.unquote(self.headers.get('X-File-Name','') or 'swagger.json')
            try:
                res=apispec.brief(apispec.save(name,self.rfile.read(n)))
            except ValueError as e: res={'error':str(e)}
            except Exception as e: res={'error':str(e)[:200]}
            return self._send(200,json.dumps(res,ensure_ascii=False))
        if self.path=='/api/spec/delete':
            n=int(self.headers.get('Content-Length',0))
            d=json.loads(self.rfile.read(n) or '{}')
            try: apispec.drop(d.get('file','')); res={'ok':True}
            except Exception as e: res={'error':str(e)[:200]}
            return self._send(200,json.dumps(res,ensure_ascii=False))
        if self.path=='/api/har':
            n=int(self.headers.get('Content-Length',0))
            d=json.loads(self.rfile.read(n) or b'{}')
            try: res=har_build(d.get('har',''),bool(d.get('tele')))
            except subprocess.TimeoutExpired: res={'error':'не уложилось в 10 минут'}
            except Exception as e: res={'error':str(e)[:200]}
            return self._send(200,json.dumps(res,ensure_ascii=False))
        if self.path in ('/api/record/start','/api/record/stop','/api/har/delete',
                         '/api/har/rename','/api/har/generate','/api/har/smoke',
                         '/api/har/unbuild','/api/scenarios/delete','/api/scenarios/hide'):
            n=int(self.headers.get('Content-Length',0))
            d=json.loads(self.rfile.read(n) or b'{}')
            try:
                if self.path=='/api/record/start':
                    res=rec_start(d.get('name'),d.get('url'),d.get('auth',True))
                elif self.path=='/api/record/stop': res=rec_stop()
                elif self.path=='/api/har/delete': har_del(d.get('har','')); res={'ok':True}
                elif self.path=='/api/har/rename': res={'ok':True,'har':har_rename(d.get('har',''),d.get('name'))}
                elif self.path=='/api/har/generate': res=gen_scenario(d.get('har',''))
                elif self.path=='/api/har/unbuild': scenario_del(d.get('har','')); res={'ok':True}
                elif self.path=='/api/scenarios/delete': scenario_drop(d.get('file','')); res={'ok':True}
                elif self.path=='/api/scenarios/hide':
                    scenario_hide(d.get('file',''),bool(d.get('hidden'))); res={'ok':True}
                else: res=start_smoke(d.get('har',''))
            except ValueError as e: res={'error':str(e)}
            except Exception as e: res={'error':str(e)[:200]}
            return self._send(200,json.dumps(res,ensure_ascii=False))
        if self.path=='/api/settings/check':
            n=int(self.headers.get('Content-Length',0))
            d=json.loads(self.rfile.read(n) or b'{}')
            sec=d.get('section')
            # Проверяем то, что лежит на диске, дополненное несохранённой правкой:
            # иначе «Проверить» до «Сохранить» всегда ругалось бы на пустой пароль.
            cfg=dict(load_secrets().get(sec) or {})
            for k,v in (d.get('values') or {}).items():
                if str(v).strip(): cfg[k]=str(v).strip()
            try:
                fn={'beszel':check_beszel,'telescope':check_telescope,'site':check_site}[sec]
            except KeyError:
                return self._send(400,json.dumps({'error':'нет такого раздела'},ensure_ascii=False))
            try: res=fn(cfg)
            except Exception as e: res={'ok':False,'why':str(e)[:120]}
            return self._send(200,json.dumps(res,ensure_ascii=False))
        if self.path=='/api/users/check':
            n=int(self.headers.get('Content-Length',0))
            d=json.loads(self.rfile.read(n) or b'{}')
            return self._send(200,json.dumps(check_login(d.get('email',''),d.get('password','')),
                                             ensure_ascii=False))
        if self.path=='/api/restart':
            # Идущий прогон перезапуск бы осиротил: его stdout читает ЭТОТ процесс,
            # и после перезапуска лог с прогрессом были бы потеряны безвозвратно.
            with LOCK:
                if STATE['proc'] is not None:
                    return self._send(409,json.dumps({'error':'идёт прогон, перезапуск его осиротит'},
                                                     ensure_ascii=False))
            self._send(200,'{"ok":true}')
            threading.Thread(target=restart_self,daemon=True).start()
            return
        if self.path!='/api/run': return self._send(404,'{}')
        n=int(self.headers.get('Content-Length',0))
        d=json.loads(self.rfile.read(n) or b'{}')
        with LOCK:
            if STATE['proc'] is not None:
                return self._send(409,json.dumps({'error':'прогон уже идёт'},ensure_ascii=False))
        # Потоки считаем сами от пика профиля. Наружу торчал только maxVUs,
        # а preAllocatedVUs был зашит числом 100 — из-за этого любое значение
        # меньше 100 роняло k6 ещё до старта («maxVUs can't be less than preAllocatedVUs»).
        stages=d.get('stages','')
        mode=d.get('mode') or 'rate'
        try:
            peak=max(x.get('target',0) for x in json.loads(stages))
        except Exception:
            peak=4
        # В режиме потоков ступени УЖЕ заданы в потоках, и пересчитывать нечего:
        # preAllocatedVUs/maxVUs там не используются вовсе.
        need=peak if mode=='vus' else max(int(peak*2.5)+5,10)
        # Значение пользователя уважаем как есть: если он сознательно ставит мало,
        # это его выбор, морда об этом предупредила. Мы лишь не даём конфигурации
        # упасть на старте — preAllocatedVUs никогда не больше максимума.
        mx=int(d.get('maxVus') or 0) or need*2
        mx=max(mx,1)
        pre=min(need,mx)
        env={'STAGES':stages,'PRE_VUS':str(pre),'MAX_VUS':str(mx),
             'MODE':mode,'START_RATE':str(d.get('startRate',2))}
        if d.get('duration'): env['DURATION']=str(d['duration'])
        if d.get('target'): env['TARGET']=d['target']; env['HEAVY']=d['target']
        if d.get('rounds'): env['ROUNDS']=str(d['rounds'])
        env=creds_env(env)
        try:
            script,name=d['script'],d['name']
        except KeyError as e:
            return self._send(400,json.dumps({'error':f'в запросе нет поля {e}'},ensure_ascii=False))
        threading.Thread(target=run_test,args=(script,name,env),daemon=True).start()
        return self._send(200,json.dumps({'ok':True},ensure_ascii=False))
    def do_DELETE(self):
        if self.path=='/api/run':
            with LOCK:
                if STATE['proc']: STATE['proc'].terminate()
            return self._send(200,'{"ok":true}')
        p=urllib.parse.urlparse(self.path).path
        if p=='/api/reports' or p.startswith('/api/reports/'):
            # Во время прогона папка результата занята: k6 пишет в неё сырой
            # поток, а по завершении туда же собирается отчёт.
            with LOCK:
                if STATE['proc'] is not None:
                    return self._send(409,json.dumps({'error':'идёт прогон — сначала остановите его'},
                                                     ensure_ascii=False))
            try:
                if p=='/api/reports':
                    return self._send(200,json.dumps({'ok':True,'n':clear_reports()},ensure_ascii=False))
                del_report(urllib.parse.unquote(p[len('/api/reports/'):]))
                return self._send(200,json.dumps({'ok':True},ensure_ascii=False))
            except Exception as e:
                return self._send(400,json.dumps({'error':str(e)[:200]},ensure_ascii=False))
        return self._send(404,'{}')

PAGE=open(os.path.join(SL,'web_ui.html'),encoding='utf-8').read() if os.path.exists(os.path.join(SL,'web_ui.html')) else '<h1>web_ui.html не найден</h1>'

def bind(tries=20):
    """Занять порт, не сдаваясь с первой попытки.

    После перезапуска сокет предыдущей копии может ещё держаться, и одиночный
    bind падал с «Address already in use». Процесс при этом умирал молча —
    морда запущена в фоне, трейсбека никто не видит, порт свободен, и снаружи
    это выглядит как «нажал перезапуск, и сервер не поднялся».
    """
    for i in range(tries):
        try: return ThreadingHTTPServer(('127.0.0.1',PORT),H)
        except OSError as e:
            print(f"порт {PORT} занят ({e}), попытка {i+1} из {tries}…",flush=True)
            time.sleep(0.5)
    print(f"Не удалось занять порт {PORT}. Кто его держит:  lsof -nP -iTCP:{PORT}",flush=True)
    sys.exit(1)

if __name__=='__main__':
    print(f"Веб-морда: http://127.0.0.1:{PORT}   (стенд {STAND})",flush=True)
    srv=bind()
    SRV[0]=srv
    srv.serve_forever()
    # Сюда попадаем только после shutdown(), то есть по просьбе о перезапуске.
    srv.server_close()          # отпускаем порт ДО старта новой копии
    if RESTART[0]:
        # Путь к себе — абсолютный: sys.argv[0] относительный, и перезапуск
        # сломался бы, запусти морду из другого каталога.
        me=os.path.abspath(sys.argv[0])
        os.execv(sys.executable,[sys.executable,me]+sys.argv[1:])
