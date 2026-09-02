#!/usr/bin/env python3
"""Разбор сырого потока k6 в ряды для графиков.

Ответы 204 (пустое тело) в статистику ВРЕМЕНИ не идут: сервер по ним ничего
не считает и не отдаёт, они всегда быстрые и тянут перцентили вниз, создавая
картину «всё летает». В счёт запросов, rps и коды ответов они входят как есть.
"""
FAST_CODES={'204'}      # коды, исключаемые из статистики времени
import json,gzip,os,datetime,collections

def pct(v,q):
    if not v: return 0.0
    v=sorted(v); k=(len(v)-1)*q/100; f=int(k)
    return v[f] if f+1>=len(v) else v[f]+(v[f+1]-v[f])*(k-f)

def parse(path,bucket=2):
    op=gzip.open if path.endswith('.gz') else open
    dur=collections.defaultdict(list)      # name -> [ms]
    wait=collections.defaultdict(list)
    codes=collections.defaultdict(lambda: collections.Counter())
    bytes_recv=collections.defaultdict(int)
    tdur=collections.defaultdict(list)     # bucket -> [ms], все запросы (для rps)
    tlat=collections.defaultdict(list)     # bucket -> [ms] без быстрых кодов (для перцентилей)
    twait=collections.defaultdict(list)
    tlwait=collections.defaultdict(list)
    tcodes=collections.defaultdict(lambda: collections.Counter())
    # Разделение потока на статику и API по времени. Статику отдаёт nginx,
    # PHP на ней не просыпается, поэтому в одну высоту их складывать нельзя:
    # иначе нагрузка на приложение прячется за трафиком картинок и бандлов.
    tstatic=collections.Counter(); tapi=collections.Counter()
    hosts={}                               # имя запроса -> хост, куда он ушёл
    tvus={}; t0=None; t1=None; allms=[]; latms=[]
    # Чтобы отсеять 204 и в http_req_waiting, где статуса в тегах нет,
    # помним по имени запроса, каким кодом он ответил последний раз.
    last_code={}
    with op(path,'rt') as f:
        for line in f:
            try: o=json.loads(line)
            except: continue
            if o.get('type')!='Point': continue
            m=o.get('metric'); d=o['data']; tg=d.get('tags') or {}
            ts=d.get('time')
            if not ts: continue
            try: t=datetime.datetime.fromisoformat(ts.replace('Z','+00:00')).timestamp()
            except: continue
            if t0 is None or t<t0: t0=t
            if t1 is None or t>t1: t1=t
            meth=tg.get('method') or ''
            raw_n=tg.get('name') or tg.get('url') or '—'
            # Хост запроса. k6 подменяет системный тег url именем, поэтому
            # адрес из потока не достать — сценарии проставляют host сами.
            if tg.get('host'): hosts.setdefault(f"{meth} {raw_n}".strip(),tg['host'])
            if raw_n.startswith('http'):
                from urllib.parse import urlparse
                u=urlparse(raw_n); raw_n=u.path or raw_n
            n=f"{meth} {raw_n}".strip()
            b=int(t//bucket)*bucket
            if m=='http_req_duration':
                v=d['value']; dur[n].append(v); allms.append(v); tdur[b].append(v)
                c=tg.get('status','0'); codes[n][c]+=1; tcodes[b][c]+=1
                last_code[n]=c
                if raw_n.startswith('static.'): tstatic[b]+=1
                else: tapi[b]+=1
                if c not in FAST_CODES:
                    latms.append(v); tlat[b].append(v)
            elif m=='http_req_waiting':
                wait[n].append(d['value']); twait[b].append(d['value'])
                if last_code.get(n) not in FAST_CODES:
                    tlwait[b].append(d['value'])
            elif m=='data_received':
                bytes_recv[n]+=d['value']
            elif m=='vus':
                tvus[b]=max(tvus.get(b,0),d['value'])
    dursec=max(t1-t0,1) if t0 else 1
    # Эти два имени бьют по хешированным именам файлов сборки фронта: при
    # пересборке хеш меняется, и весь список превращается в стену 404, никак
    # не относящуюся к нагрузке. Пока пути в сценарии не обновлены (кнопка
    # «обновить статику» в морде), в отчёт их не тащим.
    STALE_STATIC={'GET static.shell','GET static.assets'}
    rows=[]
    for n in dur:
        if n in STALE_STATIC: continue
        v=dur[n]; w=wait.get(n,[]); cc=codes[n]
        tot=sum(cc.values())
        # 401 часто ожидаемое поведение (запрос со страницы входа, до того как
        # человек вошёл), поэтому не считаем его полноценной ошибкой наравне
        # с 403/404/5xx, а показываем отдельно.
        warn=cc.get('401',0)
        bad=sum(c for k,c in cc.items() if not k.startswith('2') and k!='401')
        mm,_,pp=n.partition(' ')
        rows.append({'endpoint':n,'host':hosts.get(n,''),
            'method':mm if mm.isupper() and len(mm)<8 else '',
            'path':pp or n,'n':len(v),'errors':bad,'err_pct':100*bad/tot if tot else 0,
            'warn':warn,'warn_pct':100*warn/tot if tot else 0,
            'avg':sum(v)/len(v),'min':min(v),'max':max(v),
            'p50':pct(v,50),'p90':pct(v,90),'p95':pct(v,95),'p99':pct(v,99),
            'ttfb_p50':pct(w,50) if w else None,'ttfb_p95':pct(w,95) if w else None,
            'tps':len(v)/dursec,'kb':bytes_recv.get(n,0)/1024,'codes':dict(cc)})
    rows.sort(key=lambda r:-r['p95'])
    codes_all=collections.Counter()
    for cc in codes.values(): codes_all.update(cc)
    codes_all_keys=list(codes_all.keys())
    buckets=sorted(tdur)
    series=[]
    for b in buckets:
        v=tdur[b]; cc=tcodes[b]
        # Если в интервале не было ничего, кроме быстрых кодов, брать нечего —
        # считаем по всему интервалу, иначе линия проваливается в ноль.
        lv=tlat.get(b) or v
        w=tlwait.get(b) or twait.get(b,[])
        ok=sum(c for k,c in cc.items() if k.startswith('2'))
        t429=cc.get('429',0)
        e5=sum(c for k,c in cc.items() if k.startswith('5'))
        e4=sum(c for k,c in cc.items() if k.startswith('4') and k!='429')
        row={'t':int(b-buckets[0]),
            'ts':datetime.datetime.fromtimestamp(b).strftime('%H:%M:%S'),'tsec':float(b),
            'rps':len(v)/bucket,'ok':ok/bucket,'t429':t429/bucket,'e5':e5/bucket,'e4':e4/bucket,
            'n_static':tstatic.get(b,0),'n_api':tapi.get(b,0),
            'p50':pct(lv,50),'p90':pct(lv,90),'p95':pct(lv,95),'p99':pct(lv,99),
            'ttfb_p95':pct(w,95) if w else 0,'vus':tvus.get(b,0)}
        for code,cnt in cc.items(): row['c'+code]=cnt/bucket
        series.append(row)
    hist=collections.Counter()
    step=25
    lat=latms or allms
    for v in lat: hist[int(v//step)*step]+=1
    curve=[{'q':q,'ms':pct(lat,q)} for q in (10,25,50,75,90,95,98,99,99.9,100)]
    for r in series:
        for code in codes_all_keys:
            r.setdefault('c'+code,0.0)
    return {'rows':rows,'series':series,'duration':dursec,
            'code_keys':sorted(codes_all_keys),
            'hist':[{'ms':k,'n':v} for k,v in sorted(hist.items())],'curve':curve,
            'total':len(allms),'codes':dict(codes_all),
            # Перцентили — по запросам, где сервер реально работал.
            'p50':pct(lat,50),'p90':pct(lat,90),'p95':pct(lat,95),'p99':pct(lat,99),
            'skipped_fast':len(allms)-len(latms),'fast_codes':sorted(FAST_CODES),
            'rps_avg':len(allms)/dursec}
