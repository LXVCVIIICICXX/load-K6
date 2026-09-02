#!/usr/bin/env python3
"""Инлайновые SVG-графики. Без CDN, тема через CSS-переменные."""
import html

PAL=['var(--c1)','var(--c3)','var(--c4)','var(--c5)','var(--c2)']
DASH=[None,'6 3',None,'2 3','9 4']   # вторая линия ещё и пунктиром — различима и в ч/б

def palette(n):
    """n различимых цветов. До пяти — фирменные, дальше ровный круг по тону.

    Ядер восемь, контейнеров под десяток: пяти цветов не хватает, а повтор
    сделал бы стопку нечитаемой — соседние полосы слились бы в одну.
    """
    if n<=len(PAL): return PAL[:n]
    return [f'hsl({int(20+i*360/n)%360} 58% {52 if i%2 else 62}%)' for i in range(n)]

def _pct(v,q):
    if not v: return 0.0
    v=sorted(v); k=(len(v)-1)*q/100; f=int(k)
    return v[f] if f+1>=len(v) else v[f]+(v[f+1]-v[f])*(k-f)

_UID=[0]

def _clip(o,W,pad,ih):
    """Прямоугольник поля графика. Точки за окном прогона рисуются за осью:
    линия должна входить в кадр сбоку, а не начинаться левее нуля."""
    _UID[0]+=1
    cid=f'cp{_UID[0]}'
    o.append(f'<clipPath id="{cid}"><rect x="{pad}" y="0" '
             f'width="{W-RPAD-pad}" height="{ih}"/></clipPath>')
    return cid

RPAD=30   # запас справа, иначе последняя подпись обрезается
TPAD=14   # запас сверху: без него верхняя подпись оси упирается в край SVG и срезается

def _axes(o,W,H,pad,ih,mx,unit,xlab,xmax,gridn=4):
    ph=ih-TPAD
    for i in range(gridn+1):
        f=i/gridn; y=ih-ph*f
        o.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{W-RPAD}" y2="{y:.1f}" stroke="var(--grid)"/>')
        o.append(f'<text x="{pad-5}" y="{y+4:.1f}" class="tick" text-anchor="end">{mx*f:.0f}{unit}</text>')
    if xlab:
        n=min(6,len(xlab))
        for i in range(n):
            idx=int(i*(len(xlab)-1)/max(n-1,1))
            x=pad+(W-pad-RPAD)*idx/max(xmax,1)
            anc='middle'
            if i==0: anc='start'
            elif i==n-1: anc='end'
            o.append(f'<text x="{x:.0f}" y="{H-5}" class="tick" text-anchor="{anc}">{xlab[idx]}</text>')

def _axes_t(o,W,H,pad,ih,mx,unit,xlab,xs,gridn=4):
    ph=ih-TPAD
    for i in range(gridn+1):
        f=i/gridn; y=ih-ph*f
        o.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{W-RPAD}" y2="{y:.1f}" stroke="var(--grid)"/>')
        o.append(f'<text x="{pad-5}" y="{y+4:.1f}" class="tick" text-anchor="end">{mx*f:.0f}{unit}</text>')
    if xlab:
        # Подписываем только точки внутри кадра. Метрики сервера снимаются с
        # запасом до и после прогона, и подпись такой точки уезжала за левый
        # край: от «17:08:46» оставалось «46».
        ins=[i for i,x in enumerate(xs) if -0.001<=x<=1.001] or list(range(len(xs)))
        n=min(6,len(ins))
        for i in range(n):
            idx=ins[int(i*(len(ins)-1)/max(n-1,1))]
            x=pad+(W-pad-RPAD)*xs[idx]
            anc='start' if i==0 else ('end' if i==n-1 else 'middle')
            o.append(f'<text x="{x:.0f}" y="{H-5}" class="tick" text-anchor="{anc}">{xlab[idx]}</text>')

def _mark_line(o,ih,x,lab='',W=None):
    """Красная вертикаль — момент, когда прогон упёрся в предел.

    Один и тот же момент на всех графиках: по нему видно, что скачок задержки
    и уход CPU в полку — одно событие, а не три разных.
    """
    o.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{ih}" '
             f'stroke="var(--mk)" stroke-width="1.6"/>')
    if lab:
        # Подпись уводим от края: у правого края текст иначе вылезает за кадр.
        right = W is not None and x > W*0.7
        o.append(f'<text x="{x+(-5 if right else 5):.1f}" y="10" class="tick" '
                 f'fill="var(--mk)" text-anchor="{"end" if right else "start"}">{html.escape(lab)}</text>')


def _thr_line(o,W,pad,y,lab):
    """Зелёная пунктирная черта порога плюс подпись у правого края.

    Черта — ориентир, а не показание: она обязана быть в кадре, иначе по
    графику не видно, есть ли запас до порога.
    """
    o.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{W-RPAD}" y2="{y:.1f}" '
             f'stroke="var(--ok)" stroke-width="1.5" stroke-dasharray="5 4"/>')
    if lab:
        o.append(f'<text x="{W-RPAD}" y="{y-4:.1f}" class="tick" '
                 f'text-anchor="end" fill="var(--ok)">{html.escape(lab)}</text>')


def lines(series,pts,xkey='t',unit=' мс',W=780,H=210,title='',fill=False,
          domain=None,robust=False,dots=False,thr=None,thr_lab='',
          mark=None,mark_lab='',step=None):
    """series: [(ключ, подпись)] ; pts: список словарей

    robust — считать верх шкалы по перцентилю, а не по максимуму. Один выброс p99
    в 12 секунд иначе прижимает все остальные линии к нулю, и график перестаёт
    что-либо показывать. Выбросы обрезаются по верху и пересчитываются в подпись.
    dots — рисовать маркеры точек: линия из одной точки невидима, а у Beszel,
    который пишет раз в минуту, на коротком прогоне точка вполне может быть одна.
    mark — момент (tsec) упора в предел: красная вертикаль.
    step — фиксированный шаг подписей оси. Без него шкала до 120% делится на
    четыре и даёт 30/60/90/120: круглой сотни на такой оси просто нет.
    """
    if not pts: return '<div class="empty">нет данных</div>'
    pad=44; ih=H-30
    o=[f'<svg viewBox="0 0 {W} {H}">']
    # Раскладка по АБСОЛЮТНОМУ времени на общем домене: иначе графики с разной
    # частотой точек растягиваются на одну ширину и одна позиция значит разное время.
    has_t = all('tsec' in p for p in pts)
    if has_t:
        d0,d1 = domain if domain else (pts[0]['tsec'], pts[-1]['tsec'])
        span = max(d1-d0, 1e-6)
        xs = [ (p['tsec']-d0)/span for p in pts ]
    else:
        xs = [ i/max(len(pts)-1,1) for i in range(len(pts)) ]

    # Шкалу считаем только по точкам, попавшим в окно: точки за границей всё равно
    # не видны, но растягивают ось и делают график плоским.
    vis=[i for i,x in enumerate(xs) if -0.02<=x<=1.02] or list(range(len(pts)))
    vals=[pts[i][k] for k,_ in series for i in vis]
    if robust and len(vals)>8:
        top=_pct(vals,97)
        mx=(top*1.30) or 1
        if mx<max(vals)*0.05: mx=max(vals)*1.15    # защита от вырожденного ряда
    else:
        mx=(max(vals,default=1) or 1)*1.15
    # Ради порога шкалу растягиваем и округляем: черта за пределами шкалы
    # бесполезна, а подписи оси должны остаться круглыми.
    if thr: mx=_nice(max(mx,thr*1.06))
    gridn=4
    if step:
        import math
        st=step
        while mx/st>8: st*=2      # больше восьми подписей ось не читается
        mx=math.ceil(mx/st-1e-9)*st
        gridn=int(round(mx/st))
    # Что не влезло — перечислим под графиком, чтобы пик не потерялся молча.
    over=[(pts[i],k) for k,_ in series for i in vis if pts[i][k]>mx]
    xlab=[str(p.get('ts') or p[xkey]) for p in pts]
    _axes_t(o,W,H,pad,ih,mx,unit,xlab,xs,gridn=gridn)
    if thr: _thr_line(o,W,pad,ih-(ih-TPAD)*min(thr,mx)/mx,thr_lab)
    if mark is not None and has_t:
        f=(mark-d0)/span
        if -0.001<=f<=1.001: _mark_line(o,ih,pad+(W-pad-RPAD)*f,mark_lab,W)
    o.append(f'<g clip-path="url(#{_clip(o,W,pad,ih)})">')
    for si,(k,lab) in enumerate(series):
        col=PAL[si%len(PAL)]; dash=DASH[si%len(DASH)]
        ph=ih-TPAD
        # Каждая линия — своя группа: по ней клик в легенде её и прячет.
        o.append(f'<g data-s="{si}">')
        d=' '.join(f'{"M" if i==0 else "L"}{pad+(W-pad-RPAD)*xs[i]:.1f},{ih-ph*min(p[k],mx)/mx:.1f}' for i,p in enumerate(pts))
        if fill:
            d2=d+f' L{pad+(W-pad-RPAD)*xs[-1]:.1f},{ih} L{pad+(W-pad-RPAD)*xs[0]:.1f},{ih} Z'
            o.append(f'<path d="{d2}" fill="{col}" opacity=".14"/>')
        da=f' stroke-dasharray="{dash}"' if dash else ''
        o.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"{da}/>')
        if dots:
            for i,p in enumerate(pts):
                if not (-0.02<=xs[i]<=1.02): continue
                cx=pad+(W-pad-RPAD)*xs[i]; cy=ih-ph*min(p[k],mx)/mx
                o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.6" fill="{col}"/>')
        o.append('</g>')
    o.append('</g>')
    o.append('</svg>')
    def sw(i):
        c=PAL[i%len(PAL)]; dsh=DASH[i%len(DASH)]
        return (f'<i style="background:{c}"></i>' if not dsh
                else f'<i style="background:repeating-linear-gradient(90deg,{c} 0 4px,transparent 4px 7px)"></i>')
    # Линий на графике бывает четыре и они идут почти вплотную; кликом по
    # подписи лишние убираются. Шкала при этом НЕ пересчитывается: иначе
    # соседние графики перестали бы читаться в одном масштабе.
    leg=' '.join(f'<span class="lg tog" data-s="{i}" title="скрыть или показать">{sw(i)}{html.escape(l)}</span>'
                 for i,(_,l) in enumerate(series))
    note=''
    if over:
        worst=max(over,key=lambda z:z[0][z[1]])
        when=worst[0].get('ts') or ''
        note=(f'<div class="hint" style="margin-top:5px">Шкала обрезана по {mx:.0f}{unit}, '
              f'чтобы редкие пики не прижимали остальные линии к нулю. '
              f'Выше шкалы точек: {len(over)}, максимум {worst[0][worst[1]]:.0f}{unit}'
              + (f' в {html.escape(str(when))}' if when else '') + '.</div>')
    return f'<div class="chart">{"".join(o)}<div class="legend">{leg}</div>{note}</div>'

def _nice(v,steps=4):
    """Верх шкалы, кратный круглому шагу: 579% превращается в 600%.

    Ось со ступенями 193/386/579 читается хуже, чем 150/300/450/600, —
    цифры на ней ни с чем не соотносятся.
    """
    import math
    if v<=0: return 1
    raw=v/steps
    mag=10**math.floor(math.log10(raw))
    for k in (1,1.2,1.5,2,2.5,3,4,5,6,8,10):
        if raw<=k*mag: return k*mag*steps
    return 10*mag*steps

def stackarea(series,pts,unit='%',W=780,H=210,note='',domain=None):
    """Стопка площадей: так же, как Beszel рисует ядра и контейнеры.

    Стопка, а не отдельные линии: восемь почти одинаковых кривых сливаются
    в кашу, а в стопке видно и вклад каждого слоя, и суммарную высоту.
    Ось — сумма по всем слоям, поэтому 8 ядер по 40% дают 320%.
    """
    if not pts or not series: return '<div class="empty">нет данных</div>'
    pad=44; ih=H-30; ph=ih-TPAD
    has_t=all('tsec' in p for p in pts)
    if has_t:
        d0,d1=domain if domain else (pts[0]['tsec'],pts[-1]['tsec'])
        span=max(d1-d0,1e-6)
        xs=[(p['tsec']-d0)/span for p in pts]
    else:
        xs=[i/max(len(pts)-1,1) for i in range(len(pts))]
    vis=[i for i,x in enumerate(xs) if -0.02<=x<=1.02] or list(range(len(pts)))
    tot=[sum(pts[i].get(k,0) or 0 for k,_ in series) for i in vis]
    mx=_nice((max(tot) or 1)*1.02)
    o=[f'<svg viewBox="0 0 {W} {H}">']
    _axes_t(o,W,H,pad,ih,mx,unit,[str(p.get('ts') or p.get('t')) for p in pts],xs)
    X=lambda i: pad+(W-pad-RPAD)*xs[i]
    Y=lambda v: ih-ph*min(v,mx)/mx
    cols=palette(len(series))
    o.append(f'<g clip-path="url(#{_clip(o,W,pad,ih)})">')
    base=[0.0]*len(pts)
    for si,(k,_) in enumerate(series):
        top=[base[i]+(p.get(k,0) or 0) for i,p in enumerate(pts)]
        up=' '.join(f'{"M" if i==0 else "L"}{X(i):.1f},{Y(top[i]):.1f}' for i in range(len(pts)))
        down=' '.join(f'L{X(i):.1f},{Y(base[i]):.1f}' for i in range(len(pts)-1,-1,-1))
        o.append(f'<path d="{up} {down} Z" fill="{cols[si]}" opacity=".72"/>')
        o.append(f'<path d="{up}" fill="none" stroke="{cols[si]}" stroke-width="1.2"/>')
        base=top
    o.append('</g>')
    o.append('</svg>')
    leg=' '.join(f'<span class="lg"><i style="background:{cols[i]}"></i>{html.escape(l)}</span>'
                 for i,(_,l) in enumerate(series))
    n=f'<div class="hint" style="margin-top:5px">{note}</div>' if note else ''
    return f'<div class="chart">{"".join(o)}<div class="legend">{leg}</div>{n}</div>'


def barsh(rows,key,lab_key,unit=' мс',W=780,rowh=21,thr=None,sub_key=None):
    if not rows: return '<div class="empty">нет данных</div>'
    mx=max(max(r[key] for r in rows),thr or 0)*1.14 or 1
    h=len(rows)*rowh+26; lw=290; bw=W-lw-64
    o=[f'<svg viewBox="0 0 {W} {h}">']
    if thr:
        x=lw+bw*thr/mx
        o.append(f'<rect x="{lw}" y="0" width="{x-lw:.0f}" height="{h-22}" fill="var(--ok-bg)"/>')
        o.append(f'<line x1="{x:.0f}" y1="0" x2="{x:.0f}" y2="{h-22}" stroke="var(--ok)" stroke-dasharray="4 3"/>')
    for i,r in enumerate(rows):
        y=i*rowh+3; v=r[key]; w=bw*v/mx
        o.append(f'<text x="0" y="{y+12}" class="lbl">{html.escape(str(r[lab_key])[:44])}</text>')
        o.append(f'<rect x="{lw}" y="{y+2}" width="{w:.1f}" height="12" rx="2" fill="var(--c2)"/>')
        if sub_key and r.get(sub_key) is not None:
            o.append(f'<rect x="{lw}" y="{y+2}" width="{bw*r[sub_key]/mx:.1f}" height="12" rx="2" fill="var(--c1)"/>')
        tx=lw+w+5
        if tx>W-34: o.append(f'<text x="{lw+w-5:.0f}" y="{y+12}" class="val" text-anchor="end">{v:.0f}</text>')
        else: o.append(f'<text x="{tx:.0f}" y="{y+12}" class="val">{v:.0f}</text>')
    o.append('</svg>')
    return f'<div class="chart">{"".join(o)}</div>'

def histogram(hist,W=780,H=180):
    if not hist: return '<div class="empty">нет данных</div>'
    mx=max(h['n'] for h in hist)*1.1 or 1
    pad=44; ih=H-30; n=len(hist); bw=(W-pad-RPAD)/n
    o=[f'<svg viewBox="0 0 {W} {H}">']
    _axes(o,W,H,pad,ih,mx,'',[f"{h['ms']}" for h in hist],n-1 or 1)
    ph=ih-TPAD
    for i,h in enumerate(hist):
        bh=ph*h['n']/mx
        o.append(f'<rect x="{pad+i*bw:.1f}" y="{ih-bh:.1f}" width="{max(bw-1,1):.1f}" height="{bh:.1f}" fill="var(--c1)" opacity=".85"/>')
    o.append('</svg>')
    return f'<div class="chart">{"".join(o)}<div class="legend"><span class="lg">по оси X — задержка, мс; по Y — число запросов</span></div></div>'

def curve(pts,W=780,H=190,thr=None,thr_lab=''):
    if not pts: return '<div class="empty">нет данных</div>'
    mx=max(p['ms'] for p in pts)*1.12 or 1
    if thr: mx=_nice(max(mx,thr*1.06))
    pad=44; ih=H-30; xmax=len(pts)-1 or 1
    o=[f'<svg viewBox="0 0 {W} {H}">']
    _axes(o,W,H,pad,ih,mx,' мс',[f"p{p['q']:g}" for p in pts],xmax)
    ph=ih-TPAD
    if thr: _thr_line(o,W,pad,ih-ph*min(thr,mx)/mx,thr_lab)
    d=' '.join(f'{"M" if i==0 else "L"}{pad+(W-pad-RPAD)*i/xmax:.1f},{ih-ph*p["ms"]/mx:.1f}' for i,p in enumerate(pts))
    o.append(f'<path d="{d}" fill="none" stroke="var(--c1)" stroke-width="2"/>')
    for i,p in enumerate(pts):
        o.append(f'<circle cx="{pad+(W-pad-RPAD)*i/xmax:.1f}" cy="{ih-ph*p["ms"]/mx:.1f}" r="3" fill="var(--c1)"/>')
    o.append('</svg>')
    return f'<div class="chart">{"".join(o)}</div>'

def scatter_rps_lat(pts,W=780,H=210,thr=None,thr_lab=''):
    """Задержка против фактического rps. Точка красится по худшему, что в ней было.

    Порядок важен: 5xx перекрывает 429, 429 перекрывает 4xx. Иначе окно с одной
    пятисоткой и сотней 429 покрасится как «просто лимит», а это разные диагнозы.
    """
    if not pts: return '<div class="empty">нет данных</div>'
    pts=sorted(pts,key=lambda p:p['rps'])
    mxx=max(p['rps'] for p in pts)*1.08 or 1
    mxy=max(p['p95'] for p in pts)*1.15 or 1
    if thr: mxy=_nice(max(mxy,thr*1.06))
    pad=44; ih=H-30; iw=W-pad-RPAD; ph=ih-TPAD
    X=lambda p: pad+iw*p['rps']/mxx
    Y=lambda p: ih-ph*min(p['p95'],mxy)/mxy

    def kind(p):
        if p.get('e5',0)>0:   return '5xx'
        if p.get('t429',0)>0: return '429'
        if p.get('e4',0)>0:   return '4xx'
        return 'OK'
    COL={'OK':'var(--st-ok)','4xx':'var(--st-4xx)','429':'var(--st-429)','5xx':'var(--st-5xx)'}
    RANK={'OK':0,'4xx':1,'429':2,'5xx':3}

    o=[f'<svg viewBox="0 0 {W} {H}">']
    for i in range(5):
        f=i/4; y=ih-ph*f
        o.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{W-RPAD}" y2="{y:.1f}" stroke="var(--grid)"/>')
        o.append(f'<text x="{pad-5}" y="{y+4:.1f}" class="tick" text-anchor="end">{mxy*f:.0f} мс</text>')
        x=pad+iw*f
        anc='start' if i==0 else ('end' if i==4 else 'middle')
        o.append(f'<text x="{x:.0f}" y="{H-5}" class="tick" text-anchor="{anc}">{mxx*f:.1f} rps</text>')
    if thr: _thr_line(o,W,pad,ih-ph*min(thr,mxy)/mxy,thr_lab)
    for a,b in zip(pts,pts[1:]):
        # Отрезок красим по худшему из двух концов.
        col=COL[max((kind(a),kind(b)),key=lambda k:RANK[k])]
        o.append(f'<line x1="{X(a):.1f}" y1="{Y(a):.1f}" x2="{X(b):.1f}" y2="{Y(b):.1f}" '
                 f'stroke="{col}" stroke-width="2" stroke-linecap="round"/>')
    for p in pts:
        o.append(f'<circle cx="{X(p):.1f}" cy="{Y(p):.1f}" r="3.2" fill="{COL[kind(p)]}"/>')
    o.append('</svg>')
    # Легенду показываем целиком: она объясняет шкалу цветов, а не перечисляет
    # случившееся. Встретившиеся категории выделяем, отсутствующие приглушаем.
    seen={kind(p) for p in pts} | {'OK'}
    leg=''.join(
        f'<span class="lg" style="opacity:{1 if k in seen else .45}">'
        f'<i style="background:{COL[k]}"></i>{k}</span>'
        for k in ('OK','4xx','429','5xx'))
    return f'<div class="chart">{"".join(o)}<div class="legend">{leg}</div></div>'


OVC=['var(--o1)','var(--o2)','var(--o3)','var(--o4)','var(--o5)','var(--o6)']
OVD=[None,'7 3',None,'2 3',None,'9 4']

def numfmt(v):
    """Число для подписи: без хвостовых нулей и без ложной точности.

    Одним форматом это не покрыть: на графике рядом стоят 108 rps, 0.48 с и
    3 потока. У «%.0f» секунды схлопнулись бы в ноль, у «%.2f» rps оброс бы
    мусором после запятой.
    """
    a=abs(v)
    s=f'{v:.0f}' if a>=100 else (f'{v:.1f}' if a>=10 else
                                 (f'{v:.2f}' if a>=1 else f'{v:.3f}'))
    return s.rstrip('0').rstrip('.') if '.' in s else s

def overlay(items,domain,W=780,H=290,mark=None,mark_lab=''):
    """Все ключевые линии прогона в одном кадре.

    Единицы разные — секунды, запросы, потоки, проценты, — общей оси у них быть
    не может. Поэтому у каждой линии своя шкала: низ кадра — ноль, верх кадра —
    максимум этой линии за прогон. Ступенчатый профиль из трёх ступеней ляжет
    на 33 / 66 / 100%, пик rps упрётся в верх, а сам потолок написан в легенде.

    Исключение — то, что само меряется в процентах (`abs100`): у CPU 100% кадра
    и есть 100% процессора. Нормируй его на максимум — и «процессор лёг под
    завязку» рисовалось бы ровно так же, как «процессор скучал на пяти
    процентах», то есть график врал бы ровно в том месте, ради которого он есть.

    Ось подписана в единицах линий, помеченных `axis` (это время ответа): доля
    от потолка сама по себе не читается, а «полсекунды» читается. Такие линии
    делят одну шкалу на всех — иначе подписи оси были бы верны для одной из них
    и врали бы для второй. Остальные линии остаются нормированными, их потолки
    написаны на кнопках.

    График отвечает не «сколько», а «что за чем»: где поднялись потоки, где
    следом лёг процессор, где после этого поехала задержка. «Сколько» —
    в подсказке при наведении, там значение показано в своих единицах.

    items: [{'id','lab','unit','pts':[(tsec,val)],'abs100':bool,'axis':bool}]
    """
    import datetime,json
    items=[it for it in items if it.get('pts')]
    if not items or not domain: return '<div class="empty">нет данных</div>',[]
    d0,d1=domain; span=max(d1-d0,1e-6)
    pad=44; ih=H-30; ph=ih-TPAD; iw=W-pad-RPAD
    _UID[0]+=1; uid=f'ov{_UID[0]}'
    ax=[it for it in items if it.get('axis')]
    ax_top=max((v for it in ax for _,v in it['pts']),default=0.0)
    ax_unit=ax[0].get('unit','') if ax else ''
    o=[f'<svg viewBox="0 0 {W} {H}">']
    for i in range(5):
        f=i/4; y=ih-ph*f
        o.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{W-RPAD}" y2="{y:.1f}" stroke="var(--grid)"/>')
        lab=f'{numfmt(ax_top*f)}{ax_unit}' if ax_top else f'{100*f:.0f}%'
        o.append(f'<text x="{pad-5}" y="{y+4:.1f}" class="tick" text-anchor="end">{lab}</text>')
    for i in range(6):
        f=i/5; x=pad+iw*f
        lab=datetime.datetime.fromtimestamp(d0+span*f).strftime('%H:%M:%S')
        anc='start' if i==0 else ('end' if i==5 else 'middle')
        o.append(f'<text x="{x:.0f}" y="{H-5}" class="tick" text-anchor="{anc}">{lab}</text>')
    if mark is not None:
        f=(mark-d0)/span
        if -0.001<=f<=1.001: _mark_line(o,ih,pad+iw*f,mark_lab,W)
    o.append(f'<g clip-path="url(#{_clip(o,W,pad,ih)})">')
    tops=[];meta=[]
    for si,it in enumerate(items):
        vals=[v for _,v in it['pts']]
        top=(100.0 if it.get('abs100') else
             (ax_top if (it.get('axis') and ax_top) else (max(vals) or 1)))
        if top<=0: top=1
        tops.append(top)
        col=OVC[si%len(OVC)]; dash=OVD[si%len(OVD)]
        d=' '.join(f'{"M" if i==0 else "L"}{pad+iw*(t-d0)/span:.1f},{ih-ph*min(v,top)/top:.1f}'
                   for i,(t,v) in enumerate(it['pts']))
        da=f' stroke-dasharray="{dash}"' if dash else ''
        o.append(f'<g data-ovline="{html.escape(it["id"])}">'
                 f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"{da} '
                 f'stroke-linejoin="round"/></g>')
        meta.append({'id':it['id'],'lab':it['lab'],'unit':it.get('unit',''),
                     'col':col,'top':top,
                     'p':[[round(t,1),round(v,4)] for t,v in it['pts']]})
    # Перекрестье и точка живут поверх линий и вне clip-группы: они рисуются
    # по краю кадра, и обрезка съедала бы их на первой и последней точке.
    o.append('</g>')
    o.append(f'<line class="ovcross" x1="0" y1="{TPAD}" x2="0" y2="{ih}" '
             f'stroke="var(--mut)" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>')
    o.append('<circle class="ovdot" r="3.5" fill="var(--card)" stroke-width="2" style="display:none"/>')
    o.append('</svg>')
    cfg=json.dumps({'W':W,'H':H,'pad':pad,'iw':iw,'ih':ih,'ph':ph,'d0':d0,'span':span,
                    's':meta},ensure_ascii=False,separators=(',',':'))
    return (f'<div class="ovwrap" id="{uid}">{"".join(o)}'
            f'<div class="ovtip"></div>'
            f'<script type="application/json">{cfg}</script>'
            f'<script>ovTip("{uid}")</script></div>'), tops

# Подсказка при наведении. Отчёт статический, данные линий уже лежат рядом в
# JSON — скрипту остаётся перевести курсор в координаты кадра и найти ближайшую
# точку. Ближайшую ищем по вертикали: линий на кадре шесть, они пересекаются,
# и «ближайшая по времени» показывала бы не ту, на которую человек смотрит.
# Переключение линий кликом по подписи в легенде. Один обработчик на документ:
# графиков в отчёте под десяток, вешать слушатель на каждый незачем.
LEG_TOGGLE_JS='''
document.addEventListener('click',function(e){
  var t=e.target.closest('.lg.tog'); if(!t) return;
  var chart=t.closest('.chart'); if(!chart) return;
  var g=chart.querySelector('svg [data-s="'+t.dataset.s+'"]'); if(!g) return;
  var off=t.classList.toggle('off');
  g.style.display=off?'none':'';
});
'''

OV_TIP_JS='''
function ovTip(uid){
  var root=document.getElementById(uid); if(!root) return;
  var svg=root.querySelector('svg'), tip=root.querySelector('.ovtip'),
      cross=svg.querySelector('.ovcross'), dot=svg.querySelector('.ovdot'),
      C=JSON.parse(root.querySelector('script[type="application/json"]').textContent);
  function fmt(v){var a=Math.abs(v);
    var s=a>=100?v.toFixed(0):(a>=10?v.toFixed(1):(a>=1?v.toFixed(2):v.toFixed(3)));
    return s.indexOf('.')<0?s:s.replace(/0+$/,'').replace(/\\.$/,'');}
  function hide(){tip.style.display='none';cross.style.display='none';dot.style.display='none';}
  svg.addEventListener('mouseleave',hide);
  svg.addEventListener('mousemove',function(ev){
    var r=svg.getBoundingClientRect(); if(!r.width||!r.height) return hide();
    var mx=(ev.clientX-r.left)*C.W/r.width, my=(ev.clientY-r.top)*C.H/r.height;
    if(mx<C.pad-4||mx>C.pad+C.iw+4) return hide();
    var t=C.d0+C.span*(mx-C.pad)/C.iw, best=null;
    C.s.forEach(function(s){
      var g=svg.querySelector('[data-ovline="'+s.id+'"]');
      if(g&&g.style.display==='none') return;          // линия убрана кнопкой
      var bi=0,bd=Infinity;
      for(var j=0;j<s.p.length;j++){var dd=Math.abs(s.p[j][0]-t); if(dd<bd){bd=dd;bi=j;}}
      var v=s.p[bi][1], y=C.ih-C.ph*Math.min(v,s.top)/s.top;
      if(!best||Math.abs(y-my)<Math.abs(best.y-my))
        best={s:s,v:v,y:y,x:C.pad+C.iw*(s.p[bi][0]-C.d0)/C.span,ts:s.p[bi][0]};
    });
    if(!best) return hide();
    var dt=new Date(best.ts*1000).toTimeString().slice(0,8);
    tip.innerHTML='<b style="color:'+best.s.col+'">'+best.s.lab+'</b>'
                 +'<span class="v">'+fmt(best.v)+best.s.unit+'</span>'
                 +'<span class="t">'+dt+'</span>';
    tip.style.display='block';
    // Держим подсказку в кадре: у правого края она разворачивается влево.
    var fx=best.x/C.W;
    tip.style.left=(fx*100)+'%';
    tip.style.top=(best.y/C.H*100)+'%';
    tip.style.transform='translate(' + (fx>0.72?'-104%':'4%') + ',-118%)';
    cross.setAttribute('x1',best.x); cross.setAttribute('x2',best.x);
    cross.style.display='';
    dot.setAttribute('cx',best.x); dot.setAttribute('cy',best.y);
    dot.setAttribute('stroke',best.s.col); dot.style.display='';
  });
}
'''

def ov_swatch(i):
    c=OVC[i%len(OVC)]; d=OVD[i%len(OVD)]
    return (f'<i style="background:{c}"></i>' if not d
            else f'<i style="background:repeating-linear-gradient(90deg,{c} 0 5px,transparent 5px 8px)"></i>')
