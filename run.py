#!/usr/bin/env python3
"""Запуск k6 + автоматический HTML-отчёт на каждый прогон.

  python3 run.py <скрипт.js> [--name ИМЯ] [ENV=VAL ...]

Кладёт результат в results/<стенд>/<имя>-<ДД-ММ-ГГГГ>-<ЧЧ-ММ>/ :
  report.html   — самодостаточный отчёт (его и смотреть)
  summary.json  — агрегаты по запросам
  raw.json.gz   — сырой поток k6
"""
import json,os,re,sys,time,threading,subprocess,datetime,gzip,statistics,urllib.request,urllib.parse,html,shutil

sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import config
ROOT=config.ROOT
HERE=config.HERE
HOST=config.STAND

_blank=[False]

def say(msg):
    # k6 сыплет пустые строки между обновлениями прогресса — подряд идущие
    # схлопываем, иначе лог в морде состоит наполовину из пустоты.
    if not str(msg).strip():
        if _blank[0]: return
        _blank[0]=True
    else:
        _blank[0]=False
    # Без flush морда получает лог рывками по 8 КБ и «зависает» на глазах.
    print(msg,flush=True)

MSG=re.compile(r'msg="((?:[^"\\]|\\.)*)"')

class Tally:
    """Живой счётчик запросов и ошибок.

    k6 в строке прогресса даёт только проценты, потоки и итерации — ни числа
    запросов, ни ошибок там нет. Зато он тут же пишет их в raw.json, поэтому
    дочитываем этот файл по мере роста и считаем сами.
    """
    def __init__(self,path):
        self.path=path; self.reqs=0; self.e4=0; self.e5=0
        self.mark=(time.time(),0); self.rps=0.0
        self.stop=False
        threading.Thread(target=self._loop,daemon=True).start()

    def _loop(self):
        f=None; buf=''
        while not self.stop:
            if f is None:
                if not os.path.exists(self.path): time.sleep(0.3); continue
                f=open(self.path,'r',errors='replace')
            chunk=f.read()
            if not chunk:
                self._rps(); time.sleep(0.5); continue
            buf+=chunk
            # Последняя строка может быть дописана не до конца — придержим её.
            lines=buf.split('\n'); buf=lines.pop()
            for ln in lines:
                if '"http_req_duration"' not in ln: continue
                try: o=json.loads(ln)
                except Exception: continue
                if o.get('type')!='Point': continue
                self.reqs+=1
                st=str(((o.get('data') or {}).get('tags') or {}).get('status','0'))
                if st.startswith('4'): self.e4+=1
                elif st.startswith('5'): self.e5+=1
            self._rps()

    def _rps(self):
        t=time.time(); t0,n0=self.mark
        if t-t0>=2.0:
            self.rps=(self.reqs-n0)/(t-t0); self.mark=(t,self.reqs)

# k6 печатает счётчик сессий отдельной строкой, ПЕРЕД строкой с процентом:
#   running (12m59.0s), 02/50 VUs, 101 complete and 0 interrupted iterations
# Саму строку мы выбрасываем (она дублирует сводку), но числа из неё забираем:
# для сценариев, где итерация = сессия человека, это единственная честная мера
# сделанной работы. Проценты меряют время профиля, а не сессии.
ITERS_LINE=re.compile(r'(\d+) complete and (\d+) interrupted iterations')
_iters=[0,0]

PROG_LINE=re.compile(r'\[\s*(\d+)%\s*\]')
CLOCK_LINE=re.compile(r'(\d[\dm.]*s)\s*/\s*(\d[\dm.]*s)')
VUS_LINE=re.compile(r'(\d+\s*/\s*\d+)\s+VUs|(\d+)\s+VUs')

def progress_line(line,tally):
    """Сводка вместо сырых строк k6.

    Держим в ней и «прошло/всего», и «потоки», и процент — по ним морда рисует
    полосу, поэтому формат менять нельзя безнаказанно.
    """
    m=PROG_LINE.search(line)
    if not m: return None
    c=CLOCK_LINE.search(line); v=VUS_LINE.search(line)
    when=f"{c.group(1)}/{c.group(2)}" if c else '—'
    vus=(v.group(1) or v.group(2)) if v else '—'
    errs=tally.e4+tally.e5
    det=f" ({tally.e4}×4xx {tally.e5}×5xx)" if errs else ""
    ses=f", сессий {_iters[0]}" + (f"+{_iters[1]}" if _iters[1] else "")
    return (f"running ({when}), {vus} VUs, {tally.reqs} requests/{errs} errors{det}, "
            f"RPS {tally.rps:.1f}{ses} [ {m.group(1)}% ]")

def parse_err(line):
    """Достаёт ERR{...} из строки k6.

    k6 печатает console.log как  msg="ERR{\\"n\\":...}"  — внутренние кавычки
    экранированы по правилам Go %q, что совпадает с экранированием JSON-строки.
    Поэтому сначала раскрываем msg как JSON-строку, потом читаем сам объект."""
    m=MSG.search(line)
    body=None
    if m:
        try: body=json.loads('"'+m.group(1)+'"')
        except Exception: body=None
    if body is None: body=line          # запуск без обёртки k6
    at=body.find('ERR{')
    if at<0: return None
    try: return json.loads(body[at+3:body.rindex('}')+1])
    except Exception: return None

def pct(v,q):
    if not v: return 0.0
    v=sorted(v); k=(len(v)-1)*q/100; f=int(k)
    return v[f] if f+1>=len(v) else v[f]+(v[f+1]-v[f])*(k-f)

def _bz_fetch(hub,tok,coll,sys_id,per,a,b):
    """Записи одной коллекции Beszel за окно [a;b] в минутном разрешении.

    Разрешение фильтруем прямо в запросе: Beszel держит 1m/10m/20m/120m/480m
    в одной коллекции, и без фильтра сотня последних записей — это агрегаты
    за часы вперемешку с минутными, а минутных на прогон не остаётся.
    """
    flt=urllib.parse.quote(f'system="{sys_id}" && type="1m"')
    r=urllib.request.Request(
        f'{hub}/api/collections/{coll}/records?perPage={per}&sort=-created&filter=({flt})',
        headers={'Authorization':tok})
    items=json.load(urllib.request.urlopen(r,timeout=25))['items']
    fmt='%Y-%m-%d %H:%M:%S'
    out={}
    for it in items:
        if it.get('type')!='1m': continue
        try: ts=datetime.datetime.strptime(it['created'][:19].replace('T',' '),fmt)
        except Exception: continue
        if a<=ts<=b: out[it['created'][:19]]=(ts,it['stats'])
    return out

def beszel_cfg():
    """Настройки Beszel: сначала введённые в морде, иначе .env.

    Порядок именно такой, чтобы стенд, переключённый через морду, не требовал
    правки .env, — а запуск без морды продолжал работать на переменных окружения.
    Метрики сервера необязательны: без настроек отчёт просто соберётся без них.
    """
    try:
        cfg=json.load(open(config.SECRETS,encoding='utf-8')).get('beszel') or {}
        if cfg.get('url') and cfg.get('email'): return cfg
    except Exception: pass
    return {'url':os.environ.get('BESZEL_URL',''),'email':os.environ.get('BESZEL_EMAIL',''),
            'password':os.environ.get('BESZEL_PASSWORD',''),
            'systemId':os.environ.get('BESZEL_SYSTEM_ID','')}

def beszel(t0,t1,pad_sec=150):
    try:
        cfg=beszel_cfg()
        if not cfg.get('url') or not cfg.get('email'): return []
        hub=cfg['url'].rstrip('/')
        sys_id=cfg.get('systemId') or ''
        r=urllib.request.Request(hub+'/api/collections/users/auth-with-password',
            data=json.dumps({'identity':cfg['email'],'password':cfg['password']}).encode(),
            headers={'Content-Type':'application/json'})
        tok=json.load(urllib.request.urlopen(r,timeout=20))['token']
        fmt='%Y-%m-%d %H:%M:%S'
        a=datetime.datetime.strptime(t0,fmt)-datetime.timedelta(seconds=pad_sec)
        b=datetime.datetime.strptime(t1,fmt)+datetime.timedelta(seconds=pad_sec)
        # Точек надо на всё окно плюс запас: у минутного разрешения одна точка
        # в минуту, и на часовом прогоне тридцати записей не хватит.
        per=min(500,int((b-a).total_seconds()//60)+30)
        sysr=_bz_fetch(hub,tok,'system_stats',sys_id,per,a,b)
        try: conr=_bz_fetch(hub,tok,'container_stats',sys_id,per,a,b)
        except Exception: conr={}
        out=[]
        for key in sorted(sysr):
            ts,st=sysr[key]
            # created приходит в UTC, а k6 пишет в местном времени.
            # Без перевода графики прогона и сервера разъезжаются на смещение часового пояса.
            loc=ts.replace(tzinfo=datetime.timezone.utc).astimezone()
            la=st.get('la') or []
            # Контейнеры пишутся тем же агентом в тот же момент, но метка времени
            # отличается на миллисекунды — берём запись с той же секундой.
            cst=conr.get(key)
            cont={}
            if cst:
                for c in (cst[1] or []):
                    if c.get('n'): cont[c['n']]=c.get('c',0)
            out.append({'t':loc.strftime('%H:%M:%S'),'ts':loc.strftime('%H:%M:%S'),
                        'tsec':ts.replace(tzinfo=datetime.timezone.utc).timestamp(),
                        'cpu':st.get('cpu',0),'ram':st.get('mp',0),
                        'la':(la or [0])[0],
                        'la5':la[1] if len(la)>1 else 0,
                        'la15':la[2] if len(la)>2 else 0,
                        # cpus — загрузка каждого ядра, cont — CPU по контейнерам Docker.
                        'cores':list(st.get('cpus') or []),
                        'cont':cont})
        return out
    except Exception as e:
        return []

def main():
    args=sys.argv[1:]
    if not args: print(__doc__); sys.exit(1)
    script=args[0]; name=None; env=config.pool_env(dict(os.environ))
    i=1
    while i<len(args):
        if args[i]=='--name': name=args[i+1]; i+=2
        elif '=' in args[i]: k,v=args[i].split('=',1); env[k]=v; i+=1
        else: i+=1
    name=name or os.path.basename(script).replace('.js','')
    # Сценарий может лежать и в подкаталоге (собранные кодогеном — в recorded/),
    # поэтому берём путь относительно каталога с кодом, а не одно имя файла. Выйти за
    # каталог при этом нельзя: имя приходит в том числе из браузера.
    sl=HERE
    rel=os.path.relpath(os.path.abspath(os.path.join(sl,script)),sl)
    if rel.startswith('..') or not rel.endswith('.js'):
        print('✗ недопустимое имя сценария: '+script); sys.exit(2)
    now=datetime.datetime.now()
    d=os.path.join(config.RESULTS,HOST,f"{name}-{now:%d-%m-%Y-%H-%M}")
    os.makedirs(d,exist_ok=True)
    raw=os.path.join(d,'raw.json')
    t0=datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    say(f"→ {d}")
    say(f"• запускаем k6: {os.path.basename(script)}")

    # k6 читаем ПОТОКОМ, а не capture_output: иначе морда молчит до самого конца
    # прогона и ни лога, ни прогресса не видно. Пишем сразу в три места:
    # в k6.log, в свой stdout (его читает web.py) и в сборщик тел ошибок.
    p6=subprocess.Popen(['k6','run','--no-color','--out',f'json={raw}',rel],
                        cwd=sl,env=env,
                        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    lines=[]; errs=[]
    tally=Tally(raw)
    with open(os.path.join(d,'k6.log'),'w') as lg:
        for line in p6.stdout:
            line=line.rstrip('\n')
            lg.write(line+'\n'); lines.append(line)
            # Тесты печатают тела неуспешных ответов маркером ERR{...} через console.log.
            # k6 заворачивает это в logfmt-строку msg="..." и экранирует кавычки,
            # поэтому сначала разэкранируем, а уже потом читаем JSON.
            if 'ERR{' in line:
                e=parse_err(line)
                if e: errs.append(e)
            else:
                it=ITERS_LINE.search(line)
                if it: _iters[0],_iters[1]=int(it.group(1)),int(it.group(2))
                # Сырые строки прогресса k6 заменяем сводкой: время, потоки,
                # запросы, ошибки, rps, сессии и процент — всё в одной строке.
                sm=progress_line(line,tally)
                # «running (0m22.0s), 00/12 VUs, 46 complete…» после сводки лишняя:
                # то же самое, но без запросов и ошибок.
                if sm is None and line.lstrip().startswith('running ('): continue
                say(sm or line)
    p6.wait()
    tally.stop=True
    t1=datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    log='\n'.join(lines)
    say(f"• k6 завершён (код {p6.returncode}), ошибок с телом: {len(errs)}")

    if not os.path.exists(raw) or os.path.getsize(raw)==0:
        # k6 не отдал данных — значит упал на старте. Показываем ЕГО ошибку, а не свой трейсбек.
        why=[l for l in log.splitlines() if 'level=error' in l or 'SyntaxError' in l or 'GoError' in l]
        print("✗ k6 не выдал данных, прогон не состоялся.")
        for l in (why[:5] or log.splitlines()[-8:]):
            msg=l.split('msg=',1)[1] if 'msg=' in l else l
            print("   "+msg.strip('"')[:300])
        print(f"   полный вывод: {os.path.join(d,'k6.log')}")
        sys.exit(2)

    sys.path.insert(0,HERE)
    import aggregate
    say("• разбираем сырой поток k6…")
    agg=aggregate.parse(raw)
    say(f"• запросов {agg['total']}, различных {len(agg['rows'])}")
    say("• тянем метрики сервера из Beszel…")
    meta={'name':name,'script':script,'dir':d,'when':now.strftime('%d.%m.%Y %H:%M'),
          'stand':HOST,'exit':p6.returncode,'stages':env.get('STAGES','—'),
          'server':beszel(t0,t1),'errors':errs[:300],'err_total':len(errs),
          # Правка имён файлов сборки перед смоуком — часть истории прогона:
          # без неё непонятно, почему в сценарии не те файлы, что в записи.
          'statics':json.loads(env['STATICS_FIX']) if env.get('STATICS_FIX') else None,
          # Параметры запуска — чтобы отчёт мог показать, чем именно грузили.
          'env':{k:env[k] for k in ('MODE','TARGET','HEAVY','BG_PATHS','STAGES',
                                    'PRE_VUS','MAX_VUS','START_RATE','START_VUS',
                                    'DURATION','ROUNDS','REQUESTS','ACCOUNTS')
                 if env.get(k)}}
    json.dump({'meta':meta,'agg':agg},open(os.path.join(d,'summary.json'),'w'),
              ensure_ascii=False,indent=1)
    with open(raw,'rb') as fi, gzip.open(raw+'.gz','wb') as fo: shutil.copyfileobj(fi,fo)
    os.remove(raw)
    say("• собираем отчёт…")
    import report_html
    report_html.build(d); report_html.index(os.path.join(config.RESULTS,HOST))
    print(f"✓ отчёт: {d}/report.html")
    print(f"  индекс: results/{HOST}/index.html")

if __name__=='__main__': main()
