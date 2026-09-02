#!/usr/bin/env python3
"""HTML-отчёт по прогону. Состав панелей — по образцу дашборда JMeter."""
import json,os,re,html,glob,sys,datetime,collections
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import charts as C

CSS="""<style>
:root{--bg:#fbfaf9;--fg:#1a1a19;--mut:#6b6b68;--line:#e3e1de;--card:#fff;--grid:#eeecea;--code:#f4f2f0;
--c1:#c8613f;--c2:#d9b26a;--c3:#3f6f9e;--c4:#4e8c5f;--c5:#8a5f9e;
--ok:#3f7a52;--ok-bg:#eef5f0;--bad:#b3452c;--warn:#b3822c;--mk:#e02b1d;
--st-ok:#3f7a52;--st-4xx:#c8791f;--st-429:#8a8a86;--st-5xx:#c0392b;
--o1:#d8622f;--o2:#b32a86;--o3:#2f7fc0;--o4:#6b4ec8;--o5:#2f8b57;--o6:#a8801a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#191817;--fg:#eceae7;--mut:#a3a09c;
--line:#333130;--card:#211f1e;--grid:#2b2928;--code:#262322;--c1:#e08059;--c2:#b87a5e;--c3:#7fa3c9;
--c4:#95b585;--c5:#bf9cbf;--ok:#7ab88e;--ok-bg:#1f2a23;--bad:#e0755a;--warn:#d4a747;--mk:#ff5c4d;
--st-ok:#7ab88e;--st-4xx:#e0a34f;--st-429:#9d9a96;--st-5xx:#e2564e;
--o1:#f0955a;--o2:#ef7ac4;--o3:#6fb0e8;--o4:#a992f0;--o5:#6dc48b;--o6:#e0bf55}}
:root[data-theme="dark"]{--bg:#191817;--fg:#eceae7;--mut:#a3a09c;--line:#333130;--card:#211f1e;--grid:#2b2928;
--code:#262322;--c1:#e08059;--c2:#d9b56a;--c3:#6fa8dc;--c4:#7cc48e;--c5:#c08fd4;--ok:#7ab88e;--ok-bg:#1f2a23;
--bad:#e0755a;--warn:#d4a747;--mk:#ff5c4d;--st-ok:#7ab88e;--st-4xx:#e0a34f;--st-429:#9d9a96;--st-5xx:#e2564e;
--o1:#f0955a;--o2:#ef7ac4;--o3:#6fb0e8;--o4:#a992f0;--o5:#6dc48b;--o6:#e0bf55}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif;margin:0;padding:28px 18px 70px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:16px;margin:30px 0 4px;letter-spacing:-.01em}
h2+.hint{margin:0 0 10px}
a{color:var(--c1)}
.meta{color:var(--mut);font-size:13px;margin-bottom:20px}
.hint{color:var(--mut);font-size:12.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:9px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpi .v{font-size:20px;font-weight:640;letter-spacing:-.02em}
.kpi .k{color:var(--mut);font-size:11.5px;margin-top:1px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px;min-width:680px}
th,td{text-align:left;padding:5px 7px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:560;font-size:11.5px;white-space:nowrap}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.b{font-weight:640}td.m{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--c3);font-weight:640}td.ep{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.good{color:var(--ok)}.bad{color:var(--bad);font-weight:640}.warn{color:var(--warn)}
svg{width:100%;height:auto;display:block}
.chart{margin:2px 0}
.lbl{fill:var(--mut);font-size:10.5px;font-family:ui-monospace,Menlo,monospace}
.val{fill:var(--fg);font-size:10.5px;font-variant-numeric:tabular-nums}
.tick{fill:var(--mut);font-size:9.5px}
.seg{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:9px}
.seg button{background:transparent;color:var(--mut);border:1px solid var(--line);
border-radius:7px;padding:4px 11px;font:560 12px ui-sans-serif,system-ui,sans-serif;cursor:pointer}
.seg button.on{color:var(--fg);border-color:var(--c1)}
.seg button i{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:6px;vertical-align:-1px}
.seg button:not(.on) i{opacity:.35}
.seg button .sc{color:var(--mut);font-weight:400;margin-left:6px;font-variant-numeric:tabular-nums}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:6px}
.lg{color:var(--mut);font-size:11.5px;display:flex;align-items:center;gap:5px}
.lg i{width:9px;height:9px;border-radius:2px;display:inline-block}
/* Подпись, по которой можно кликнуть, чтобы убрать линию с графика. */
.lg.tog{cursor:pointer;user-select:none}
.lg.tog:hover{color:var(--fg)}
.lg.tog.off{opacity:.4;text-decoration:line-through}
.empty{color:var(--mut);font-size:13px;padding:14px 0}
details.det{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;margin:24px 0}
details.det summary{cursor:pointer;font-weight:600;font-size:15px}
details.det[open] summary{margin-bottom:10px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:12px}
.note{border-left:3px solid var(--c1);padding-left:11px;margin:10px 0;color:var(--mut);font-size:13px}
.pill{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11.5px;font-variant-numeric:tabular-nums}
.pill.ok{background:var(--ok-bg);color:var(--ok)}
.ovwrap{position:relative}
.ovwrap svg{cursor:crosshair}
.ovtip{position:absolute;display:none;pointer-events:none;white-space:nowrap;z-index:3;
background:var(--card);border:1px solid var(--line);border-radius:7px;padding:5px 9px;
font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.16)}
.ovtip .v{margin-left:8px;font-weight:640;font-variant-numeric:tabular-nums}
.ovtip .t{margin-left:8px;color:var(--mut);font-variant-numeric:tabular-nums}
</style>"""

# Пороги, которые рисуются зелёной чертой на графиках. CPU берём из analyze,
# чтобы черта и вердикт не разъезжались; ядер — сколько их у сервера.
LAT_THR=1000     # мс: договорённый потолок времени ответа
CPU_THR=85       # %: выше запаса на всплеск уже нет
try:
    import analyze as _an
    CPU_THR=_an.CPU_LIMIT; LAT_THR=_an.LAT_LIMIT
except Exception:
    pass

ENV_RU={'MODE':'способ нагрузки','TARGET':'разгоняемый запрос','HEAVY':'тяжёлый запрос',
 'BG_PATHS':'фоновые запросы','STAGES':'ступени','PRE_VUS':'потоков заранее',
 'MAX_VUS':'потолок потоков','START_RATE':'стартовый темп','START_VUS':'стартовых потоков',
 'DURATION':'длительность','ROUNDS':'проходов','REQUESTS':'карта запросов',
 'ACCOUNTS':'пул профилей'}
MODE_RU={'rate':'темпом (запросов в секунду)','vus':'потоками (клиенты без пауз)'}

# Имена контейнеров на стенде обычно вида <проект>-backend-fpm-1. Префикс
# проекта свой у каждой площадки, поэтому приходит из окружения, а не из кода.
CONTAINER_PREFIX=os.environ.get('CONTAINER_PREFIX','')

def short_name(n):
    """myproject-backend-fpm-1 → fpm. Полное имя съедает всю легенду."""
    x=re.sub(r'-\d+$','',n)          # номер реплики: он одинаковый у всех
    if CONTAINER_PREFIX:
        x=re.sub(r'^'+re.escape(CONTAINER_PREFIX)+r'[-_]','',x)
    # Слой (backend/frontend) отрезаем, только если после него что-то осталось.
    x=re.sub(r'^(backend|frontend)-(?=.)','',x)
    return x or n

def config_block(m,a):
    """Свёрнутая конфигурация прогона в подвале отчёта.

    Отчёт живёт отдельно от морды и часто уходит заказчику файлом, поэтому в нём
    должно быть видно, чем именно грузили: схему запросов, профиль и параметры.
    """
    try:
        import web
        test=next((t for t in web.TESTS if t['id']==m.get('script')),None)
        flow=web.FLOW.get(m.get('script')) or []
    except Exception:
        test,flow=None,[]

    rows=[]
    env=m.get('env') or {}
    if test: rows.append(('тест',test['name']))
    rows.append(('скрипт',m.get('script','—')))
    rows.append(('стенд',m.get('stand','—')))
    rows.append(('запущен',m.get('when','—')))
    rows.append(('длительность',f"{a['duration']/60:.1f} мин"))
    if env.get('MODE'): rows.append((ENV_RU['MODE'],MODE_RU.get(env['MODE'],env['MODE'])))
    for k,v in env.items():
        if k in ('MODE','STAGES'): continue
        rows.append((ENV_RU.get(k,k),v))
    st=m.get('stages') or env.get('STAGES')
    if st and st!='—':
        try:
            steps=json.loads(st)
            rows.append(('ступени',' → '.join(f"{x['target']} за {x['duration']}" for x in steps)))
        except Exception:
            rows.append(('ступени',str(st)))
    tbl="".join(f"<tr><td class='hint'>{html.escape(str(k))}</td>"
                f"<td class='ep'>{html.escape(str(v))}</td></tr>" for k,v in rows)

    tree=''
    if flow:
        parts=[]
        for br in flow:
            head=f"<div style='font-weight:640;margin:8px 0 4px'>{html.escape(br.get('role',''))}"
            if br.get('note'): head+=f" <span class='hint' style='font-weight:400'>{html.escape(br['note'])}</span>"
            head+="</div>"
            steps=[]
            for stp in br.get('steps',[]):
                pad='&nbsp;'*(4*int(stp.get('d',0)))
                steps.append(f"<div class='st'>{pad}<span class='mth m{html.escape(stp.get('m',''))}'>"
                             f"{html.escape(stp.get('m',''))}</span> <span class='pth'>{html.escape(stp.get('p',''))}</span>"
                             f"  <span class='cmt'>— {html.escape(stp.get('n',''))}</span></div>")
            parts.append(head+"<div class='steps'>"+"".join(steps)+"</div>")
        tree="<div class='card' style='margin-top:10px'>"+"".join(parts)+"</div>"

    desc=f"<div class='hint' style='margin-bottom:8px'>{html.escape(test['desc'])}</div>" if test else ''
    return (f"<details class='det'><summary>Конфигурация прогона</summary>{desc}"
            f"<div class='scroll'><table>{tbl}</table></div>{tree}</details>")


def slug(x):
    """Имя файла из имени прогона: скачанный отчёт должен быть опознаваем."""
    x=re.sub(r'[\\/:*?"<>|]+','-',str(x)).strip(' .-')
    return re.sub(r'-{2,}','-',x) or 'report'

def report_name(m,dd):
    # Дата уже вшита в имя папки (имя-ДД-ММ-ГГГГ-ЧЧ-ММ) — берём её оттуда,
    # чтобы имя файла совпадало с папкой и не разъезжалось при перегенерации.
    return slug(dd)+'.html'

def _cross(pts,key,thr):
    """Первый момент, когда линия переходит порог снизу вверх.

    Beszel пишет раз в минуту, поэтому между двумя отсчётами линейно
    интерполируем: иначе черта прыгает на целую минуту и уезжает от реального
    момента перехода дальше, чем длится сам перелом.
    """
    prev=None
    for x in pts:
        v=x.get(key); t=x.get('tsec')
        if v is None or t is None: continue
        if v>=thr:
            if prev is None or prev[0]>=thr: return t
            v0,t0=prev
            f=(thr-v0)/(v-v0) if v!=v0 else 0
            return t0+(t-t0)*max(0.0,min(1.0,f))
        prev=(v,t)
    return None

def _knee(ser,key='p95',win=5):
    """Момент, когда рост задержки из пологого становится скачком.

    Метод хорды: кривую сглаживаем скользящим средним, соединяем её концы
    прямой и берём точку, максимально отставшую ВНИЗ от этой прямой, — до неё
    рост медленнее среднего, после неё быстрее. Сглаживание нужно, чтобы
    одиночный шумный отсчёт не объявил себя переломом.

    Перелом ищем только до конца плато, не в хвосте: после gracefulStop
    начатые сессии доигрываются, потоков становится всё меньше, и любой
    скачок задержки там — статистика по горстке последних сессий, а не упор
    в потолок. На угасающем участке ставить черту нельзя вообще, а вот на
    разгоне и на самом плато — можно, даже если нагрузка там ещё далека от
    пика (например, сервер задохнулся на 20 потоках из будущих 50 — это не
    хвост, это часть разгона, и в разбор должно попасть). Поэтому обрезаем
    ряд не по абсолютному уровню потоков в моменте, а по времени: всё, что
    идёт раньше последнего момента, когда потоков было почти как на пике,
    остаётся как есть; отрезается только то, что идёт СТРОГО ПОСЛЕ него —
    то есть сам угасающий хвост.
    """
    pts=[(x['tsec'],x[key],x.get('vus')) for x in ser
         if x.get(key) is not None and x.get('tsec') is not None]
    if len(pts)<12: return None
    vus_vals=[v for _,_,v in pts if v is not None]
    if vus_vals:
        peak=max(vus_vals)
        if peak>0:
            last_near_peak=max((i for i,(_,_,v) in enumerate(pts) if v is not None and v>=peak*0.95),
                                default=len(pts)-1)
            pts=pts[:last_near_peak+1]
    if len(pts)<12: return None
    pts=[(t,y) for t,y,_ in pts]
    ys=[]
    for i in range(len(pts)):
        lo=max(0,i-win//2); hi=min(len(pts),i+win//2+1)
        ys.append(sum(z[1] for z in pts[lo:hi])/(hi-lo))
    x0,x1=pts[0][0],pts[-1][0]; y0,y1=ys[0],ys[-1]
    if x1<=x0 or y1<=y0*1.25: return None    # роста нет — отмечать нечего
    best=None; bd=0
    for i,(t,_) in enumerate(pts):
        f=(t-x0)/(x1-x0)
        dv=(y0+(y1-y0)*f)-ys[i]
        if dv>bd: bd=dv; best=t
    return best

def build(d):
    s=json.load(open(os.path.join(d,'summary.json')))
    m=s['meta']; a=s['agg']
    rows=a['rows']; ser=a['series']
    err=sum(r['errors'] for r in rows); tot=a['total']
    ep=100*err/tot if tot else 0
    # Статику отдаёт веб-сервер, приложение на ней не просыпается — в потолок она
    # упирается совсем не так, как вызовы API. Складывать их в один rps значит
    # прятать настоящую нагрузку на приложение за трафиком картинок и бандлов.
    front=sum(r['tps'] for r in rows if r['path'].startswith('static.'))
    backend=sum(r['tps'] for r in rows) - front

    # Правка имён файлов сборки перед смоуком. Строк бывает под сотню, поэтому
    # блок свёрнутый: он нужен, только когда в прогоне вылезли 404 по статике.
    fx=m.get('statics') or {}
    statics_block=''
    if fx.get('error'):
        statics_block=(f"<div class='note'>Имена файлов сборки обновить не удалось: "
                       f"{html.escape(str(fx['error']))}. Если в прогоне есть 404 по статике — "
                       f"причина в этом.</div>")
    elif fx.get('renamed') or fx.get('dropped'):
        li="".join(f"<li>{html.escape(x)}</li>" for x in fx.get('details') or [])
        statics_block=(
         "<details class='det'><summary>Статика приведена к сегодняшней сборке "
         f"({fx.get('renamed',0)} переименовано, {fx.get('dropped',0)} убрано)</summary>"
         "<div class='hint' style='margin-bottom:8px'>Имена файлов сборки содержат хеш и "
         "меняются при каждой пересборке фронта. Записи живут дольше: без этой правки "
         "сценарий мерил бы выдачу 404 вместо выдачи статики. Убраны те, которых на стенде "
         "нет совсем.</div>"
         f"<ul class='hint' style='margin:0;padding-left:18px;line-height:1.7'>{li}</ul></details>")
    rtt=min((r['ttfb_p50'] for r in rows if r['ttfb_p50'] is not None),default=0)

    def cell(r):
        t95=f"{r['ttfb_p95']:.0f}" if r['ttfb_p95'] is not None else '—'
        srv=f"{max(r['ttfb_p95']-rtt,0):.0f}" if r['ttfb_p95'] is not None else '—'
        # 401 — не «чисто», но и не поломка, поэтому красим серым (var(--mut)),
        # а не в цвет настоящих ошибок (403/404/5xx).
        warn_pct=r.get('warn_pct',0)
        if r['err_pct']>1:
            err_td=f"<td class='n bad'>{r['err_pct']:.1f}%</td>"
        elif warn_pct>1:
            err_td=f"<td class='n' style='color:var(--mut)'>{warn_pct:.1f}%</td>"
        else:
            err_td=f"<td class='n good'>{r['err_pct']:.1f}%</td>"
        return (f"<tr><td class='m'>{html.escape(r.get('method',''))}</td>"
                f"<td class='ep'>{html.escape(r.get('path',r['endpoint']))}</td><td class='n'>{r['n']}</td>"
                f"{err_td}<td class='n'>{r['avg']:.0f}</td>"
                f"<td class='n'>{r['min']:.0f}</td><td class='n'>{r['p50']:.0f}</td>"
                f"<td class='n'>{r['p90']:.0f}</td><td class='n b'>{r['p95']:.0f}</td>"
                f"<td class='n'>{r['p99']:.0f}</td><td class='n'>{r['max']:.0f}</td>"
                f"<td class='n'>{t95}</td><td class='n' style='color:var(--c1);font-weight:600'>{srv}</td>"
                f"<td class='n'>{r['tps']:.2f}</td></tr>")
    # Группируем по хосту: прямые вызовы API, статика фронта и сторонние
    # хранилища — это три разные нагрузки, и складывать их в один список значит
    # прятать, куда на самом деле уходит трафик. Порядок групп — по числу
    # запросов, чтобы главная была сверху. Если хост неизвестен (так у старых
    # прогонов и рукописных сценариев без тега), таблица остаётся плоской.
    by_host=collections.OrderedDict()
    for r in rows:
        by_host.setdefault(r.get('host') or '',[]).append(r)
    if len(by_host)>1 or (len(by_host)==1 and '' not in by_host):
        parts=[]
        for h,rs in sorted(by_host.items(),key=lambda kv:-sum(x['n'] for x in kv[1])):
            n=sum(x['n'] for x in rs)
            e=sum(x['errors'] for x in rs)
            lab=html.escape(h) if h else 'хост не помечен'
            # «82 запросов» резало бы глаз, а склонять число на каждый случай
            # незачем: форма «запросов: 82» верна при любом количестве.
            bad=f" · <span class='bad'>ошибок: {e}</span>" if e else ''
            parts.append(f"<tr><td colspan='14' style='padding-top:14px'>"
                         f"<b>{lab}</b> <span class='hint'>— запросов: {n}{bad}</span></td></tr>")
            parts+= [cell(r) for r in rs]
        stat=''.join(parts)
    else:
        stat=''.join(cell(r) for r in rows)

    cd=a['codes']; ctot=sum(cd.values()) or 1
    crows=''.join(
      f"<tr><td><b>{k}</b></td><td class='n'>{v}</td><td class='n'>{100*v/ctot:.1f}%</td>"
      f"<td>{html.escape(CODE_RU.get(k,''))}</td></tr>"
      for k,v in sorted(cd.items(),key=lambda x:-x[1]))

    code_series=[]
    for c in a.get('code_keys',[]):
        code_series.append(('c'+c, c))
    if not code_series: code_series=[('rps','запросов')]

    st=m.get('stages')
    def human(stages):
        try:
            js=json.loads(stages) if isinstance(stages,str) else stages
        except Exception:
            return html.escape(str(stages))
        if not isinstance(js,list): return html.escape(str(stages))
        parts=[]
        for i,x in enumerate(js):
            tgt=x.get('target'); dur=x.get('duration','')
            sec=dur.replace('s',' с').replace('m',' мин')
            parts.append(f"<b>{tgt}</b> польз./с в течение {sec}")
        return "Нарастание: "+", затем ".join(parts)+f". Всего {a['duration']/60:.1f} мин, отправлено {a['total']} запросов."
    profile=human(st)

    bad_rows=[r for r in rows if r['errors']>0]
    err_badge=f" — {err} шт ({ep:.1f}%)" if err else " — нет"
    open_attr=" open" if ep>5 else ""
    if bad_rows:
        err_by_ep="<div class='scroll'><table><tr><th>Метод</th><th>Путь</th><th>Ошибок</th><th>Доля</th><th>Коды</th></tr>"+"".join(
          f"<tr><td class='m'>{html.escape(r.get('method',''))}</td><td class='ep'>{html.escape(r.get('path',r['endpoint']))}</td>"
          f"<td class='n bad'>{r['errors']}</td><td class='n'>{r['err_pct']:.1f}%</td>"
          f"<td class='hint'>{html.escape(', '.join(f'{k}:{v}' for k,v in sorted(r['codes'].items()) if not k.startswith('2')))}</td></tr>"
          for r in bad_rows)+"</table></div>"
    else:
        err_by_ep="<div class='hint' style='padding:10px 0'>Запросов с ошибками нет.</div>"

    # Тела неуспешных ответов: код говорит ЧТО, а message — почему.
    # Группируем по (запрос, код, текст), иначе 3578 одинаковых 422 не читаются.
    emsgs=m.get('errors') or []
    if emsgs:
        grp={}
        for e in emsgs:
            k=(e.get('n','—'),str(e.get('s','')),(e.get('m') or '')[:400])
            grp[k]=grp.get(k,0)+1
        top=sorted(grp.items(),key=lambda kv:-kv[1])
        shown=m.get('err_total',len(emsgs))
        cap=f"<div class='hint' style='margin:10px 0 4px'>Собрано тел: {len(emsgs)}"
        cap+=f" из {shown}" if shown>len(emsgs) else ""
        cap+=f" · различных сообщений: {len(grp)}</div>"
        err_bodies=cap+"<div class='scroll'><table><tr><th>Запрос</th><th>Код</th><th>Раз</th><th>Сообщение</th></tr>"+"".join(
          f"<tr><td class='ep'>{html.escape(n)}</td><td class='n bad'>{html.escape(code)}</td>"
          f"<td class='n'>{cnt}</td><td class='hint' style='white-space:pre-wrap;word-break:break-word'>{html.escape(msg) or '—'}</td></tr>"
          for (n,code,msg),cnt in top[:60])+"</table></div>"
        err_by_ep+=f"<details class='det' style='margin:14px 0 0'><summary>Тексты ошибок ({len(grp)})</summary>{err_bodies}</details>"
    # Общий временной домен = окно прогона. Все графики раскладываются по нему,
    # поэтому одна и та же позиция по горизонтали означает один и тот же момент.
    dom=None
    if ser and 'tsec' in ser[0]:
        dom=(ser[0]['tsec'], ser[-1]['tsec'])

    # Из чего сложен поток во времени. Кэша браузера здесь нет и быть не может:
    # k6 ходит без него, каждый поток — «холодный» браузер. У живого человека
    # часть статики снимает кэш, поэтому по статике сценарий грузит стенд
    # СИЛЬНЕЕ реального пользователя — об этом и говорит подпись под графиком.
    layers_block=''
    if any(p.get('n_static') for p in ser):
        layers_block=(f'''<h2>Из чего сложен поток</h2>
<div class="hint">Высота — все запросы за интервал. Оранжевое отдаёт веб-сервер и до приложения не доходит,
синее — единственная часть, которую обрабатывает приложение.</div>
<div class="card">{C.stackarea([('n_static','статика (nginx)'),('n_api','API — работа бэка')],ser,
   unit='',domain=dom,
   note='Кэша браузера в прогоне нет: k6 ходит без него, каждый поток — холодный браузер. '
        'У живого человека часть статики снимает кэш, поэтому здесь её больше, чем в записи.')}</div>''')

    # Метрики сервера снимаются с запасом ±150 с (лежат в summary.json целиком),
    # а на графике режутся по окну прогона + по одной точке с краёв,
    # чтобы линия входила и выходила из кадра, а не обрывалась.
    srv_all=m.get('server') or []
    srv_pts=srv_all
    if dom and srv_all and 'tsec' in srv_all[0]:
        inside=[i for i,x in enumerate(srv_all) if dom[0]<=x['tsec']<=dom[1]]
        if inside:
            # НЕ называть переменные a/b — a здесь это агрегат прогона (s['agg']).
            lo=max(inside[0]-1,0); hi=min(inside[-1]+2,len(srv_all))
            srv_pts=srv_all[lo:hi]
    # Ядра и контейнеры лежат в тех же точках Beszel. Ключи-массивы и словари
    # раскладываем в плоские поля: график умеет рисовать только их.
    core_series=[]; cont_series=[]; srv_flat=[]
    ncore=max((len(x.get('cores') or []) for x in srv_pts),default=0)
    cnames={}
    for x in srv_pts:
        for n_,v_ in (x.get('cont') or {}).items():
            cnames[n_]=max(cnames.get(n_,0),v_ or 0)
    # Контейнеры, простоявшие весь прогон в нуле, только забивают легенду.
    cnames={n_:v_ for n_,v_ in cnames.items() if v_>=0.05}
    order=sorted(cnames,key=lambda n_:-cnames[n_])[:12]
    for x in srv_pts:
        q={'t':x.get('t'),'ts':x.get('ts'),'tsec':x.get('tsec')}
        cs=x.get('cores') or []
        for i in range(ncore): q[f'k{i}']=cs[i] if i<len(cs) else 0
        cc=x.get('cont') or {}
        for i,n_ in enumerate(order): q[f'n{i}']=cc.get(n_,0)
        srv_flat.append(q)
    core_series=[(f'k{i}',f'CPU {i}') for i in range(ncore)]
    cont_series=[(f'n{i}',short_name(n_)) for i,n_ in enumerate(order)]
    has_la=any(x.get('la5') is not None and (x.get('la') or x.get('la5') or x.get('la15'))
               for x in srv_pts)

    # ——— Моменты упора в предел ———————————————————————————————————
    # На каждом графике черта своя и по его собственному пределу: у CPU это 85%,
    # у средней загрузки — число ядер, у задержки — слом характера роста. Свести
    # их в одну черту нельзя: у графиков разные пределы, и общая черта на двух из
    # трёх стояла бы там, где на самой линии ничего не происходит.
    def hhmm(t): return datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')
    def inwin(t): return t if (t is not None and (not dom or dom[0]<=t<=dom[1])) else None
    ncores=ncore or 8
    mk_cpu=inwin(_cross(srv_pts,'cpu',CPU_THR))
    mk_la=inwin(_cross(srv_pts,'la',ncores)) if has_la else None
    mk_knee=inwin(_knee(ser,'p95'))
    def mknote(t,what):
        return (f'<div class="hint" style="margin-top:6px">Красная черта — {hhmm(t)}: {what}.</div>'
                if t is not None else '')
    note_cpu=mknote(mk_cpu,f'CPU перешёл {CPU_THR}%')
    note_la=mknote(mk_la,f'средняя загрузка перешла {ncores} ядер')
    note_knee=mknote(mk_knee,'ровный рост задержки сменился скачком')
    # На общем графике черта одна — самая ранняя из трёх: деградация начинается
    # с первого сработавшего признака, остальные к этому моменту уже следствие.
    cands=sorted([z for z in ((mk_la,f'средняя загрузка перешла {ncores} ядер'),
                              (mk_cpu,f'CPU перешёл {CPU_THR}%'),
                              (mk_knee,'ровный рост задержки сменился скачком'))
                  if z[0] is not None])
    mark=cands[0][0] if cands else None
    mark_lab=f'предел {hhmm(mark)}' if mark else ''

    # ——— Общий график: разные единицы, у каждой линии своя шкала от нуля до её
    # максимума. Время ответа переводим в секунды: на кнопке и в подсказке рядом
    # стоят «0.48 с» и «108 rps», и миллисекунды там читались бы как четвёртая
    # единица подряд. Оно же помечено axis — по нему подписана ось.
    def ovp(src,k,mul=1.0): return [(x['tsec'],x[k]*mul) for x in src
                                    if x.get(k) is not None and x.get('tsec') is not None]
    # Единицу времени выбираем один раз на весь график: ось, кнопки и подсказка
    # должны говорить одно и то же. Прогон, где всё уложилось в полсекунды,
    # честнее показать миллисекундами — «0.12 с» на оси читается хуже, чем «120 мс».
    lat_s=max([x['p99'] for x in ser if x.get('p99') is not None] or [0])>=1000
    lmul,lunit=(0.001,' с') if lat_s else (1.0,' мс')
    ov_items=[
      {'id':'p90','lab':'Полное время, p90','unit':lunit,'axis':True,'pts':ovp(ser,'p90',lmul)},
      {'id':'p99','lab':'Полное время, p99','unit':lunit,'axis':True,'pts':ovp(ser,'p99',lmul)},
      {'id':'rps','lab':'Запросов в секунду','unit':' rps','pts':ovp(ser,'rps')},
      {'id':'vus','lab':'Нагружающих потоков','unit':'','pts':ovp(ser,'vus')},
      {'id':'cpu','lab':'CPU сервера','unit':'%','abs100':True,'pts':ovp(srv_pts,'cpu')},
      {'id':'la','lab':'Средняя загрузка','unit':'','pts':ovp(srv_pts,'la')},
    ]
    ov_items=[it for it in ov_items if it['pts']]
    ov_svg,ov_tops=C.overlay(ov_items,dom,mark=mark,mark_lab=mark_lab)
    ov_btns=''.join(
      f'<button class="on" data-ov="{it["id"]}">{C.ov_swatch(i)}{html.escape(it["lab"])}'
      f'<span class="sc">0–{C.numfmt(ov_tops[i])}{it["unit"]}</span></button>'
      for i,it in enumerate(ov_items)) if ov_tops else ''
    ov_block=(f'''<h2>Всё на одном графике</h2>
<div class="hint">Единицы разные, общей оси у них быть не может. Подписи оси — по времени ответа; у остальных линий своя шкала от нуля до максимума за прогон, он написан на кнопке. У CPU шкала настоящая: верх кадра — это 100% процессора. График отвечает не «сколько», а «что за чем» — где поднялись потоки, где следом лёг процессор, где после этого поехала задержка. «Сколько» — наведите курсор на линию. Кнопкой линию можно убрать.</div>
<div class="card">
 <div class="seg" id="ovSeg">{ov_btns}</div>
 {ov_svg}
</div>
<script>
// Линии уже нарисованы, каждая в своей группе — кнопка только прячет группу.
// Шкалы у линий независимые, поэтому пересчитывать при скрытии нечего.
document.getElementById('ovSeg').onclick=function(e){{
  var b=e.target.closest('button[data-ov]'); if(!b) return;
  b.classList.toggle('on');
  var g=document.querySelector('[data-ovline="'+b.dataset.ov+'"]');
  if(g) g.style.display=b.classList.contains('on')?'':'none';
}};
</script>''' if ov_btns else '')

    # 204 — пустой ответ: сервер по нему ничего не считает, и в перцентилях
    # такие запросы создавали бы картину «всё летает».
    nfast=a.get('skipped_fast') or 0
    fast_note=(f'<div class="note">Ответы {", ".join(a.get("fast_codes") or [])} '
               f'({nfast} шт., {100*nfast/a["total"]:.0f}% запросов) в статистику времени '
               f'не входят — ни в перцентили, ни в графики задержки, ни в вывод. '
               f'В счёт запросов, коды ответов и график rps они включены, '
               f'а в выводе rps посчитан без них — общий поток там в скобках.</div>'
               if nfast else '')
    cfg=config_block(m,a)

    # Короткий вердикт словами — то, что человек хочет прочесть первым,
    # не разглядывая четыре графика. Падение анализа не должно ронять отчёт.
    try:
        import analyze
        verdict='<br>'.join(html.escape(x) for x in analyze.as_text(analyze.analyze(d)).split('\n'))
        verdict=f'<div class="card" style="border-left:3px solid var(--c1)"><b>Итог</b><div style="margin-top:6px;line-height:1.6">{verdict}</div></div>'
    except Exception as e:
        verdict=f'<div class="hint">вердикт не собрался: {html.escape(str(e))}</div>'

    # Разрезы CPU из Beszel. Каждый показывается, только если данные есть:
    # старые прогоны в summary.json этих полей не содержат.
    cores_block=(f'''<h2>CPU по ядрам</h2>
<div class="hint">Слои сложены: высота стопки — сумма по восьми ядрам. Перекос одного ядра над остальными означает, что упёрлись в однопоточную работу, а не в железо.</div>
<div class="card">{C.stackarea(core_series,srv_flat,unit="%",domain=dom)}</div>''' if core_series else '')
    cont_block=(f'''<h2>CPU по контейнерам</h2>
<div class="hint">Тот же CPU, разложенный по контейнерам Docker: видно, кто именно съел процессор — приложение, база или очередь.</div>
<div class="card">{C.stackarea(cont_series,srv_flat,unit="%",domain=dom)}</div>''' if cont_series else '')
    la_block=(f'''<h2>Средняя загрузка</h2>
<div class="hint">Сколько процессов в среднем ждали процессор за последнюю минуту. Ядер восемь: пока линия ниже восьми, очереди нет.</div>
<div class="card">{C.lines([('la','1 мин')],srv_pts,xkey='t',unit='',domain=dom,dots=True,thr=ncore or 8,thr_lab=f'{ncore or 8} ядер',mark=mk_la)}{note_la}</div>''' if has_la else '')

    H=f"""<title>{html.escape(m['name'])} — {m['when']}</title>{CSS}
<script>{C.OV_TIP_JS}
{C.LEG_TOGGLE_JS}</script>
<div class="wrap">
<h1>{html.escape(m['name'])}</h1>
<div class="meta">{m['when']} · <code>{html.escape(m['stand'])}</code> · скрипт <code>{html.escape(m['script'])}</code>
 · длительность {a['duration']/60:.1f} мин · <a href="../index.html">все прогоны</a></div>

<div class="kpis">
 <div class="kpi"><div class="v">{tot}</div><div class="k">запросов</div></div>
 <div class="kpi"><div class="v">{a['rps_avg']:.1f}</div><div class="k">rps средний</div></div>
 {f'<div class="kpi"><div class="v">{backend:.1f}</div><div class="k">rps по API</div></div>'
  f'<div class="kpi"><div class="v">{front:.1f}</div><div class="k">rps по статике</div></div>' if front else ''}
 <div class="kpi"><div class="v">{a['p50']:.0f} мс</div><div class="k">p50</div></div>
 <div class="kpi"><div class="v">{a['p90']:.0f} мс</div><div class="k">p90</div></div>
 <div class="kpi"><div class="v">{a['p95']:.0f} мс</div><div class="k">p95</div></div>
 <div class="kpi"><div class="v">{a['p99']:.0f} мс</div><div class="k">p99</div></div>
 <div class="kpi"><div class="v {'bad' if ep>1 else 'good'}">{ep:.1f}%</div><div class="k">ошибок</div></div>
</div>
{verdict}
{fast_note}
<div class="card"><b>Профиль нагрузки.</b> {profile}</div>

<h2>Запросов в секунду</h2>
<div class="hint">Сколько реально прошло и сколько отбито. Расхождение с заданным темпом — само по себе симптом.</div>
<div class="card">
 <div class="seg" id="rpsSeg"><button class="on" data-v="codes">По кодам ответа</button><button data-v="total">Общий rps</button></div>
 <div data-rps="codes">{C.lines(code_series,ser,unit=' rps',fill=True,domain=dom)}</div>
 <div data-rps="total" style="display:none">{C.lines([('rps','всего запросов')],ser,unit=' rps',fill=True,domain=dom)}</div>
</div>

{layers_block}
<script>
// Оба графика уже нарисованы и лежат рядом — кнопка просто переключает видимый.
// Отчёт статический, пересчитывать нечего.
document.getElementById('rpsSeg').onclick=function(e){{
  var b=e.target.closest('button[data-v]'); if(!b) return;
  this.querySelectorAll('button').forEach(function(x){{x.classList.toggle('on',x===b);}});
  document.querySelectorAll('[data-rps]').forEach(function(x){{
    x.style.display = x.dataset.rps===b.dataset.v ? '' : 'none';}});
}};
</script>

<h2>Перцентили задержки во времени</h2>
<div class="hint">Момент, когда p99 отрывается от p95, и есть начало деградации.</div>
<div class="card">{C.lines([('p50','p50'),('p90','p90'),('p95','p95'),('p99','p99')],ser,domain=dom,robust=True,thr=LAT_THR,thr_lab='1 с',mark=mk_knee)}{note_knee}</div>

<h2>Задержка против фактического rps</h2>
<div class="hint">Главный график про потолок: до какого темпа p95 держится и где загибается.</div>
<div class="card">{C.scatter_rps_lat(ser,thr=LAT_THR,thr_lab='1 с')}</div>

<h2>Полное время и время до первого байта</h2>
<div class="hint">Если расходятся — узкое место в передаче тела, если идут вместе — в обработке.</div>
<div class="card">{C.lines([('p95','полное время, p95'),('ttfb_p95','до первого байта, p95')],ser,domain=dom,robust=True,thr=LAT_THR,thr_lab='1 с')}</div>

<h2>Нагружающих потоков</h2>
<div class="card">{C.lines([('vus','Потоки')],ser,unit='',domain=dom)}</div>

<h2>Перцентильная кривая</h2>
<div class="card">{C.curve(a['curve'],thr=LAT_THR,thr_lab='1 с')}</div>

<h2>p95 по запросам</h2>
<div class="card">{C.barsh(rows[:18],'p95','endpoint',thr=300,sub_key='ttfb_p95')}
<div class="legend"><span class="lg"><i style="background:var(--c1)"></i>TTFB</span>
<span class="lg"><i style="background:var(--c2)"></i>полное время</span>
<span class="lg">зелёная зона — порог 300 мс</span></div></div>

<h2>Запросы</h2>
<div class="scroll"><table>
<tr><th>Метод</th><th>Путь</th><th>n</th><th>ошиб</th><th>сред</th><th>min</th><th>p50</th><th>p90</th><th>p95</th><th>p99</th><th>max</th><th>TTFB p95</th><th>сервер</th><th>rps</th></tr>
{stat}</table></div>
<div class="note">Колонка <b>сервер</b> — это TTFB за вычетом сетевого круга ({rtt:.0f} мс).
Круг взят как наименьший TTFB в этом же прогоне: у самой лёгкой запросы сервер почти не думает,
поэтому её TTFB и есть сеть. Без такой поправки замер выглядит втрое хуже, чем есть.</div>



<h2>CPU сервера сайта</h2>
<div class="card">{C.lines([('cpu','CPU, % от 8 ядер'),('ram','RAM, % от 15.5 ГБ')],srv_pts,xkey='t',unit='%',domain=dom,dots=True,thr=CPU_THR,thr_lab=f'{CPU_THR}% CPU',mark=mk_cpu,step=20) if srv_pts else '<div class="empty">метрики за окно прогона не получены</div>'}{note_cpu if srv_pts else ''}</div>
{cores_block}
{cont_block}
{la_block}

<details class="det"{open_attr}>
<summary>Ошибки и коды ответов{err_badge}</summary>
<div class="scroll"><table><tr><th>Код</th><th>Запросов</th><th>Доля</th><th></th></tr>{crows}</table></div>
{err_by_ep}
</details>

{statics_block}

{ov_block}

{cfg}
</div>"""
    fn=report_name(m,os.path.basename(d))
    open(os.path.join(d,fn),'w',encoding='utf-8').write(H)
    # report.html оставляем как псевдоним: на него ссылаются старые отчёты и закладки.
    open(os.path.join(d,'report.html'),'w',encoding='utf-8').write(H)

CODE_RU={'200':'успех','204':'успех, без тела','401':'не авторизован','403':'запрещено',
 '404':'не найдено','422':'ошибка валидации','429':'отбито лимитом','500':'ошибка сервера',
 '502':'плохой шлюз','503':'сервис недоступен','0':'нет ответа / таймаут'}

def index(base):
    runs=[]
    # Сортируем по времени ЗАВЕРШЕНИЯ прогона (mtime summary.json), новые сверху.
    # По имени папки сортировать нельзя: там время старта, и длинный прогон,
    # начатый раньше, оказывается выше короткого, завершённого позже.
    paths=sorted(glob.glob(os.path.join(base,'*','summary.json')),
                 key=lambda x:os.path.getmtime(x),reverse=True)
    for p in paths:
        try: s=json.load(open(p))
        except: continue
        m=s['meta']; a=s.get('agg') or {}
        dd=os.path.basename(os.path.dirname(p))
        secs=a.get('duration',0) or 0
        dur=f"{int(secs)//60}м {int(secs)%60:02d}с" if secs>=60 else f"{secs:.0f}с"
        fin=datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime('%d.%m.%Y %H:%M')
        tot=a.get('total',0); err=sum(r['errors'] for r in a.get('rows',[]))
        e=100*err/tot if tot else 0
        runs.append(f"<tr><td><a href='{html.escape(dd)}/{html.escape(report_name(m,dd))}'><b>{html.escape(m['name'])}</b></a>"
          f"<div class='hint'>{html.escape(dd)}</div></td><td class='n'>{m['when']}</td>"
          f"<td class='n'>{html.escape(fin)}</td><td class='n'>{html.escape(dur)}</td>"
          f"<td class='n'>{tot}</td><td class='n'>{a.get('rps_avg',0):.1f}</td>"
          f"<td class='n'>{a.get('p50',0):.0f}</td><td class='n b'>{a.get('p95',0):.0f}</td>"
          f"<td class='n {'bad' if e>1 else 'good'}'>{e:.1f}%</td></tr>")
    H=f"""<title>Прогоны — {html.escape(os.path.basename(base))}</title>{CSS}
<div class="wrap"><h1>Нагрузочные прогоны</h1>
<div class="meta">Стенд <code>{html.escape(os.path.basename(base))}</code> · прогонов: {len(runs)}</div>
<div class="scroll"><table><tr><th>Прогон</th><th>Старт</th><th>Завершён</th><th>Длительность</th><th>Запросов</th><th>rps</th><th>p50</th><th>p95</th><th>Ошибок</th></tr>
{''.join(runs)}</table></div></div>"""
    open(os.path.join(base,'index.html'),'w',encoding='utf-8').write(H)
