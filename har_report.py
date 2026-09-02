#!/usr/bin/env python3
"""HTML-отчёт по записи браузера, сведённой с Telescope.

  python3 har_report.py <файл.har> [tele.json] [--out отчёт.html]

Отвечает на три вопроса, ради которых запись и делалась:
  сколько запросов сессии реально доходит до бэка, а сколько снимает кэш;
  как темп запросов распределён по времени, а не в среднем по сессии;
  совпадает ли то, что ушло из браузера, с тем, что увидел сервер.

Данные считаются здесь, разметка собирается строками — внешних библиотек нет,
отчёт самодостаточен и открывается из файла.
"""
import json, os, sys, re, datetime, html, collections, statistics
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import importlib.util
_spec = importlib.util.spec_from_file_location('harmod', os.path.join(HERE, 'har.py'))
harmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harmod)

BARE = [False]       # --bare: без doctype/head, для публикации артефактом
BUCKET = 5           # секунд в столбце таймлайна
# Границы шага. K — во сколько раз простой должен превышать обычный ритм записи,
# чтобы считаться паузой человека; MIN/MAX не дают кратному выродиться на
# записях с очень быстрым или очень редким кликаньем.
STEP_K, STEP_MIN, STEP_MAX = 4.0, 1.0, 10.0
# Признаки фонового опроса: сколько раз должен встретиться, минимальный период,
# допустимый разброс периода и какую долю сессии должен покрывать.
POLL_MIN_N, POLL_MIN_PERIOD, POLL_MAX_SPREAD, POLL_MIN_COVER = 4, 15.0, 0.15, 0.5
CAP = 95             # потолок стенда по rps, замерен ранее (см. HANDOFF)
INTEGRATION = ('webhook', '/webhooks/')


# ---------------------------------------------------------------- данные
def collect(har_path, tele_path=None):
    entries, creators = harmod.load([har_path])
    t0 = harmod.started(entries[0])
    span = (harmod.started(entries[-1]) - t0).total_seconds()
    nb = int(span // BUCKET) + 1

    layers = collections.defaultdict(list)
    for e in entries:
        layers[harmod.classify(e)].append(e)

    def series(pred):
        v = [0] * nb
        for e in entries:
            if pred(e):
                v[min(nb - 1, int((harmod.started(e) - t0).total_seconds() // BUCKET))] += 1
        return v

    lay = lambda name: series(lambda e: harmod.classify(e) == name)
    data = {
        'file': os.path.basename(har_path),
        'browser': ', '.join(sorted(creators)),
        'start': t0, 'span': span, 'nb': nb,
        'total': len(entries),
        'counts': {k: len(v) for k, v in layers.items()},
        'ts_cache': lay('cache'),
        'ts_static': lay('static'),
        'ts_api': lay('api'),
        'ts_pre': lay('preflight'),
        'tele': None,
        '_entries': entries,
    }
    data['php'] = len(layers['api']) + len(layers['preflight'])
    # PHP-FPM сам ставит X-Powered-By. Сверяем деление с этим заголовком и
    # ПИШЕМ РЕЗУЛЬТАТ, а не утверждение: на записи, где статику отдаёт не nginx,
    # прежний текст соврал бы, и отчёт нельзя было бы запускать вслепую.
    marked = [e for e in entries if harmod.header(e, 'x-powered-by')]
    bad = [e for e in marked if harmod.classify(e) not in ('api', 'preflight', 'websocket')]
    bad += [e for e in entries if not harmod.header(e, 'x-powered-by')
            and harmod.classify(e) in ('api', 'preflight')]
    if not marked:
        data['phpcheck'] = ('Заголовка <span class="mono">X-Powered-By</span> в записи нет, '
                            'так что деление держится на путях запросов, а не на ответе сервера.')
    elif bad:
        data['phpcheck'] = ('<b>Осторожно:</b> заголовок <span class="mono">X-Powered-By</span> '
                            'расходится с делением на %d ответах — разбирать вручную.' % len(bad))
    else:
        data['phpcheck'] = ('Проверено по заголовку <span class="mono">X-Powered-By</span>, '
                            'который ставит сам сервер приложений: он стоит ровно на этих ответах '
                            'и ни на одном другом.')
    data['net'] = sum(len(layers[k]) for k in ('static', 'revalidate', 'preflight', 'api'))

    if tele_path and os.path.exists(tele_path):
        tele = harmod.load_telescope(tele_path)
        user, integ = [0] * nb, [0] * nb
        rows = []
        for t in tele:
            i = int((t['at'] - t0).total_seconds() // BUCKET)
            if not (0 <= i < nb):
                continue
            if any(m in t['uri'] for m in INTEGRATION):
                integ[i] += 1
            else:
                user[i] += 1
                rows.append(t)
        data['tele'] = {
            'all': len(tele), 'user': sum(user), 'integ': sum(integ),
            'ts_user': user, 'ts_integ': integ, 'rows': rows,
        }
    return data, entries


def find_polls(api, span):
    """Запросы, которые шлёт не человек, а таймер.

    Отличить их от действий можно только по ритму: опрос идёт с почти
    постоянным периодом и растянут на всю сессию, а действие человека —
    когда придётся и кучно. Меряем разброс периода устойчиво (медианное
    отклонение от медианы, а не среднеквадратичное): у опроса первый
    интервал часто короче остальных — это загрузка страницы, а не такт, —
    и одно такое значение раздувает обычный разброс втрое.

    Порог намеренно строгий. Ошибиться в эту сторону дешевле: не узнать
    опрос — значит получить лишние границы шагов, а принять действие за
    опрос — значит вырезать из сценария настоящий шаг человека.
    """
    by = collections.defaultdict(list)
    for e in api:
        by[(e['request']['method'],
            harmod.norm_path(urlparse(e['request']['url']).path))].append(harmod.started(e))
    out = {}
    for k, ts in by.items():
        if len(ts) < POLL_MIN_N:
            continue
        ts.sort()
        iv = [(ts[i] - ts[i - 1]).total_seconds() for i in range(1, len(ts))]
        m = statistics.median(iv)
        if m < POLL_MIN_PERIOD:
            continue
        mad = statistics.median([abs(x - m) for x in iv])
        rel = mad / m
        cov = (ts[-1] - ts[0]).total_seconds() / span if span else 0
        if rel <= POLL_MAX_SPREAD and cov >= POLL_MIN_COVER:
            out[k] = {'period': m, 'spread': rel, 'n': len(ts), 'cover': cov}
    return out


def step_threshold(gaps):
    """Порог паузы — от темпа самой записи, а не константой.

    Один человек кликает раз в полсекунды, другой раз в три — с общей
    константой у первого каждый клик станет отдельным шагом, а у второго
    полсценария слипнётся в один. Берём кратное медианному простою:
    медиана не тянется за длинными паузами между разделами, поэтому
    описывает именно ритм внутри пачки.

    Границы нужны, потому что кратное ломается на краях: на записи, где
    человек только листал карточки, медиана уходит к нулю и порогом станет
    доля секунды; на записи из трёх кликов — наоборот, к минутам.
    """
    live = [g for g in gaps if g > 0]
    if not live:
        return STEP_MIN
    return min(STEP_MAX, max(STEP_MIN, STEP_K * statistics.median(live)))


def scenario(entries):
    """Запись → шаги. Шаг — пачка запросов, между которыми человек не делал
    пауз. Веб-сокет из счёта убран: его `time` — это время жизни соединения,
    а не запроса, и он склеил бы всю сессию в один шаг."""
    api = [e for e in entries
           if harmod.classify(e) == 'api' and e['response']['status'] != 101
           and e.get('_resourceType') != 'websocket']
    api.sort(key=harmod.started)
    if not api:
        return [], {}, 0.0
    t0, t1 = harmod.started(api[0]), harmod.started(api[-1])
    polls = find_polls(api, (t1 - t0).total_seconds())

    key = lambda e: (e['request']['method'],
                     harmod.norm_path(urlparse(e['request']['url']).path))
    seq = [e for e in api if key(e) not in polls]
    bg = [e for e in api if key(e) in polls]

    gaps, prev = [], None
    for e in seq:
        st = harmod.started(e)
        end = st + datetime.timedelta(milliseconds=e['time'])
        if prev is not None:
            gaps.append(max(0.0, (st - prev).total_seconds()))
        prev = end if prev is None else max(prev, end)
    thr = step_threshold(gaps)

    steps, cur, prev, pause = [], [], None, 0.0
    for e in seq:
        st = harmod.started(e)
        end = st + datetime.timedelta(milliseconds=e['time'])
        if prev is not None and (st - prev).total_seconds() > thr:
            steps.append({'items': cur, 'pause': pause})
            pause = (st - prev).total_seconds()
            cur = []
        cur.append(e)
        prev = end if prev is None else max(prev, end)
    if cur:
        steps.append({'items': cur, 'pause': pause})

    out = []
    for st in steps:
        c = collections.Counter()
        for e in st['items']:
            c['%s %s' % (e['request']['method'],
                         harmod.norm_path(urlparse(e['request']['url']).path))] += 1
        out.append({'at': (harmod.started(st['items'][0]) - t0).total_seconds(),
                    'start': harmod.started(st['items'][0]),
                    'pause': st['pause'], 'n': len(st['items']), 'calls': c,
                    'bg': collections.Counter()})
    # Фоновые запросы приписываем шагу, во время которого (или в паузу после
    # которого) они прилетели: в сценарии они идут не «между» шагами, а поверх.
    for e in bg:
        st = harmod.started(e)
        i = 0
        for j, s in enumerate(out):
            if s['start'] <= st:
                i = j
            else:
                break
        out[i]['bg']['%s %s' % (e['request']['method'],
                                harmod.norm_path(urlparse(e['request']['url']).path))] += 1
    return out, polls, thr


# Разделы сайта для сводки «по разделам». Разложить запросы по смыслу может
# только тот, кто знает сайт, поэтому карта лежит в phases.json рядом с кодом
# и в репозиторий не попадает:
#
#   [["вход", ["/login", "/logout"]], ["каталог", ["/catalog", "/search"]]]
#
# Без неё раздел выводится из пути автоматически — по первому сегменту после
# префикса api и версии. Грубее, но работает на любом сайте и без настройки.
def load_phases():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'phases.json'), encoding='utf-8') as f:
            return [(name, tuple(keys)) for name, keys in json.load(f)]
    except Exception:
        return []


PHASES = load_phases()


def phase_of(path):
    for name, keys in PHASES:
        if any(k in path for k in keys):
            return name
    if PHASES:
        return 'прочее'
    segs = [x for x in re.sub(r'^/api(/v\d+)?', '', path).split('/') if x]
    return segs[0] if segs else 'прочее'


# ---------------------------------------------------------------- графика
def svg_stack(series, labels, colors, nb, bucket, height=170, title_fmt=None):
    """Столбики-стопки по таймлайну. Между сегментами оставляем зазор в 2px
    цветом подложки, иначе соседние заливки читаются как одна."""
    W, H = 1000, height
    pad_l, pad_b, pad_t = 44, 22, 10
    plot_w, plot_h = W - pad_l - 8, H - pad_b - pad_t
    tot = [sum(s[i] for s in series) for i in range(nb)]
    top = max(tot) or 1
    bw = plot_w / nb
    parts = []
    for i in range(nb):
        y = pad_t + plot_h
        for si, s in enumerate(series):
            if not s[i]:
                continue
            h = s[i] / top * plot_h
            y -= h
            tip = '%s, %s: %d' % (fmt_t(i * bucket), labels[si], s[i])
            parts.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
                'class="seg"><title>%s</title></rect>'
                % (pad_l + i * bw, y, max(1.0, bw - 1.5), max(0.8, h - 1.2),
                   colors[si], html.escape(tip)))
    # ось
    gl = []
    for f in (0, .5, 1):
        yy = pad_t + plot_h - f * plot_h
        gl.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (pad_l, yy, W - 8, yy))
        gl.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%d</text>'
                  % (pad_l - 6, yy + 4, round(top * f)))
    for i in range(0, nb, max(1, nb // 8)):
        gl.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                  % (pad_l + i * bw + bw / 2, H - 6, fmt_t(i * bucket)))
    return ('<svg viewBox="0 0 %d %d" role="img" preserveAspectRatio="xMidYMid meet">%s%s</svg>'
            % (W, H, ''.join(gl), ''.join(parts)))


def svg_lines(series, labels, colors, nb, bucket, height=190, unit=''):
    W, H = 1000, height
    pad_l, pad_b, pad_t = 44, 22, 12
    plot_w, plot_h = W - pad_l - 8, H - pad_b - pad_t
    top = max(max(s) for s in series) or 1
    step = plot_w / max(1, nb - 1)
    out = []
    for f in (0, .5, 1):
        yy = pad_t + plot_h - f * plot_h
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (pad_l, yy, W - 8, yy))
        out.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%.1f</text>'
                   % (pad_l - 6, yy + 4, top * f))
    for i in range(0, nb, max(1, nb // 8)):
        out.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                   % (pad_l + i * step, H - 6, fmt_t(i * bucket)))
    for si, s in enumerate(series):
        pts = ' '.join('%.1f,%.1f' % (pad_l + i * step, pad_t + plot_h - v / top * plot_h)
                       for i, v in enumerate(s))
        out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>' % (pts, colors[si]))
        # точка-хвост: по ней видно, чем серия кончилась
        out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="%s" class="endcap"/>'
                   % (pad_l + (nb - 1) * step,
                      pad_t + plot_h - s[-1] / top * plot_h, colors[si]))
    # прозрачные полосы для подсказок
    for i in range(nb):
        tip = fmt_t(i * bucket) + ' — ' + ', '.join(
            '%s %.2f%s' % (labels[si], s[i], unit) for si, s in enumerate(series))
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="transparent">'
                   '<title>%s</title></rect>'
                   % (pad_l + i * step - step / 2, pad_t, step, plot_h, html.escape(tip)))
    return ('<svg viewBox="0 0 %d %d" role="img" preserveAspectRatio="xMidYMid meet">%s</svg>'
            % (W, H, ''.join(out)))


def fmt_t(sec):
    return '%d:%02d' % (int(sec) // 60, int(sec) % 60)


def legend(labels, colors):
    return '<div class="lg">' + ''.join(
        '<span><i style="background:%s"></i>%s</span>' % (c, html.escape(l))
        for l, c in zip(labels, colors)) + '</div>'


# ---------------------------------------------------------------- разметка
CSS = """
:root{
  color-scheme: light;
  --bg:#f2f5f8; --surface:#ffffff; --sunk:#eef2f6;
  --ink:#0e1721; --ink2:#4b5b6c; --ink3:#7b8a99;
  --line:#dbe3ea; --line2:#c7d2dc;
  --accent:#1f4e79;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --good:#0f7b53; --warn:#b45309;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --bg:#0e1319; --surface:#151c25; --sunk:#111821;
    --ink:#e9eff5; --ink2:#a2b1c0; --ink3:#6f7e8d;
    --line:#232e3a; --line2:#31404e;
    --accent:#82b6ea;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --good:#3fae82; --warn:#d9932f;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --bg:#0e1319; --surface:#151c25; --sunk:#111821;
  --ink:#e9eff5; --ink2:#a2b1c0; --ink3:#6f7e8d;
  --line:#232e3a; --line2:#31404e;
  --accent:#82b6ea;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --good:#3fae82; --warn:#d9932f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Source Sans 3","Helvetica Neue",Arial,sans-serif;
  font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:44px 24px 80px}
h1,h2,h3{font-family:Archivo,"Helvetica Neue",Arial,sans-serif;
  font-weight:650;letter-spacing:-.015em;text-wrap:balance;margin:0}
h1{font-size:clamp(30px,4.6vw,44px);line-height:1.1}
h2{font-size:22px;margin:0 0 4px}
h3{font-size:16px;margin:0 0 6px}
.eyebrow{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--ink3)}
.sub{color:var(--ink2);max-width:66ch;margin:10px 0 0}
.mono,td.n,.num{font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-variant-numeric:tabular-nums}
header.top{border-bottom:2px solid var(--line2);padding-bottom:26px;margin-bottom:34px}
.meta{display:flex;flex-wrap:wrap;gap:6px 20px;margin-top:16px;
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12.5px;color:var(--ink3)}
.meta b{color:var(--ink2);font-weight:500}
section{margin:0 0 46px}
.shd{display:flex;align-items:baseline;gap:12px;border-bottom:1px solid var(--line);
  padding-bottom:8px;margin-bottom:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:14px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:14px 16px}
.tile .k{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;
  letter-spacing:.1em;text-transform:uppercase;color:var(--ink3)}
.tile .v{font-family:Archivo,sans-serif;font-size:30px;font-weight:660;
  line-height:1.15;margin-top:6px;font-variant-numeric:tabular-nums}
.tile .n{font-size:12.5px;color:var(--ink2);margin-top:2px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:3px;
  padding:18px 20px 12px}
.card + .card{margin-top:16px}
figure{margin:0}
figcaption{color:var(--ink2);font-size:13.5px;margin:2px 0 14px;max-width:74ch}
svg{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.ax{fill:var(--ink3);font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:10.5px}
.seg{shape-rendering:crispEdges}
.endcap{stroke:var(--surface);stroke-width:2}
.lg{display:flex;flex-wrap:wrap;gap:8px 18px;margin:12px 0 4px;font-size:13px;color:var(--ink2)}
.lg span{display:inline-flex;align-items:center;gap:7px}
.lg i{width:11px;height:11px;border-radius:2px;display:inline-block}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}
th{text-align:left;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ink3);font-weight:500;
  padding:0 10px 7px 0;border-bottom:1px solid var(--line2);white-space:nowrap}
td{padding:7px 10px 7px 0;border-bottom:1px solid var(--line);vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
.note{border-left:3px solid var(--accent);background:var(--sunk);
  padding:12px 16px;margin:16px 0 0;font-size:14.5px;color:var(--ink2)}
.note b{color:var(--ink)}
.pill{display:inline-block;font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11px;padding:1px 7px;border-radius:2px;border:1px solid var(--line2);color:var(--ink2)}
.pill.ok{color:var(--good);border-color:currentColor}
.pill.warn{color:var(--warn);border-color:currentColor}
details.tv{margin-top:10px}
details.tv summary{cursor:pointer;font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);
  padding:5px 0;list-style:none}
details.tv summary::before{content:"+ ";color:var(--accent)}
details.tv[open] summary::before{content:"− "}
details.tv summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
details.tv table{margin-top:6px;font-size:12.5px}
.steps{font-size:13.5px}
.steps td:first-child{color:var(--ink3);font-family:"IBM Plex Mono",monospace;white-space:nowrap}
.bgline{color:var(--ink3);font-size:12.5px;margin-top:3px;
  font-family:"IBM Plex Mono",ui-monospace,monospace}
.phase{font-family:Archivo,sans-serif;font-weight:640}
footer{border-top:1px solid var(--line);margin-top:52px;padding-top:18px;
  color:var(--ink3);font-size:13px}
@media (max-width:620px){.wrap{padding:28px 16px 60px}.tile .v{font-size:25px}}
"""


def table_view(labels, series, nb, every=1):
    """Таблица под графиком. Нужна не для красоты: часть цветов на светлом фоне
    не дотягивает до контраста 3:1, и правило требует, чтобы те же числа
    можно было прочитать без опоры на цвет."""
    head = ''.join('<th class="n">%s</th>' % html.escape(l) for l in labels)
    rows = []
    for i in range(0, nb, every):
        if not any(s[i] for s in series):
            continue
        rows.append('<tr><td>%s</td>%s</tr>'
                    % (fmt_t(i * BUCKET),
                       ''.join('<td class="n">%s</td>'
                               % ('%.2f' % s[i] if isinstance(s[i], float) else s[i])
                               for s in series)))
    return ('<details class="tv"><summary>Показать числа таблицей</summary>'
            '<div class="scroll"><table><thead><tr><th>время</th>%s</tr></thead>'
            '<tbody>%s</tbody></table></div></details>' % (head, ''.join(rows)))


def plural(n, one, few, many):
    """«61 вызов», а не «61 вызовов». Отчёт читают люди, и такие мелочи
    решают, доверяют ли остальным числам в нём."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def tile(k, v, n=''):
    return ('<div class="tile"><div class="k">%s</div><div class="v">%s</div>'
            '<div class="n">%s</div></div>' % (k, v, n))


def build(d, steps, polls, thr, out_path):
    c = d['counts']
    api, cache = c.get('api', 0), c.get('cache', 0)
    nb, span = d['nb'], d['span']
    rate_min = d['php'] / span * 60
    S = {'s1': 'var(--s1)', 's2': 'var(--s2)', 's3': 'var(--s3)', 's4': 'var(--s4)'}

    # --- график 1: слои по таймлайну
    lab1 = ['кэш браузера', 'статика по сети', 'API — работа бэка']
    col1 = [S['s3'], S['s4'], S['s1']]
    g1 = svg_stack([d['ts_cache'], d['ts_static'], d['ts_api']], lab1, col1, nb, BUCKET)

    # --- график 2: темп
    rps_all = [(d['ts_cache'][i] + d['ts_static'][i] + d['ts_api'][i] + d['ts_pre'][i]) / BUCKET
               for i in range(nb)]
    rps_php = [(d['ts_api'][i] + d['ts_pre'][i]) / BUCKET for i in range(nb)]
    lab2 = ['всего из браузера', 'дошло до приложения']
    col2 = [S['s3'], S['s1']]
    g2 = svg_lines([rps_all, rps_php], lab2, col2, nb, BUCKET, unit=' rps')

    # --- график 3: сверка
    g3 = tele_tbl = ''
    if d['tele']:
        t = d['tele']
        # Линии, а не стопка: браузер и Telescope считают ОДНИ И ТЕ ЖЕ запросы
        # с двух сторон, складывать их нельзя — стопка изобразила бы двойную
        # нагрузку там, где сверка как раз показывает совпадение.
        # Сравнивать надо сопоставимое: со стороны браузера берём и API,
        # и OPTIONS-предзапросы, потому что Telescope записывает и те и другие.
        # Без предзапросов синяя линия систематически ниже оранжевой, и
        # расхождение выглядело бы находкой, хотя это разный состав.
        br = [d['ts_api'][i] + d['ts_pre'][i] for i in range(nb)]
        lab3 = ['браузер → бэк', 'Telescope: пользователь', 'Telescope: интеграция']
        col3 = [S['s1'], S['s2'], S['s4']]
        g3 = svg_lines([br, t['ts_user'], t['ts_integ']], lab3, col3, nb, BUCKET,
                       height=200, unit='')
        g3 = ('<figure>' + g3 + '</figure>' + legend(lab3, col3)
              + table_view(lab3, [br, t['ts_user'], t['ts_integ']], nb))
        d['browser_php'] = sum(br)
        agg = collections.defaultdict(lambda: {'n': 0, 'ms': [], 'q': [], 'qms': []})
        for r in t['rows']:
            k = '%s %s' % (r['method'], harmod.norm_path(r['uri'].split('?')[0]))
            a = agg[k]
            a['n'] += 1
            a['ms'].append(r['duration'])
            a['q'].append(r['queries'] or 0)
            a['qms'].append(r.get('query_ms') or 0)
        rows = []
        for k, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]['ms']))[:18]:
            med = sorted(v['ms'])[len(v['ms']) // 2]
            qm = sorted(v['qms'])[len(v['qms']) // 2]
            rows.append('<tr><td>%s</td><td class="n">%d</td><td class="n">%.0f</td>'
                        '<td class="n">%.0f</td><td class="n">%.0f</td></tr>'
                        % (html.escape(k.replace('/api/v1/', '')), v['n'], med,
                           sum(v['q']) / v['n'], qm))
        tele_tbl = ('<div class="scroll"><table><thead><tr><th>запрос</th>'
                    '<th class="n">раз</th><th class="n">сервер, мс</th>'
                    '<th class="n">в БД</th><th class="n">время БД, мс</th></tr></thead>'
                    '<tbody>%s</tbody></table></div>' % ''.join(rows))

    # --- шаги сценария
    srows = []
    for i, s in enumerate(steps):
        calls = ', '.join(k.replace('/api/v1/', '') + ('×%d' % v if v > 1 else '')
                          for k, v in s['calls'].most_common())
        ph = phase_of(list(s['calls'])[0].split(' ', 1)[1]) if s['calls'] else '—'
        bg = ''
        if s['bg']:
            bg = ('<div class="bgline">фоном: %s</div>'
                  % html.escape(', '.join(k.split(' ', 1)[1].replace('/api/v1/', '')
                                          + ('×%d' % v if v > 1 else '')
                                          for k, v in s['bg'].most_common())))
        srows.append('<tr><td>%s</td><td class="n">%.0f с</td><td class="n">%d</td>'
                     '<td><span class="phase">%s</span><br>%s%s</td></tr>'
                     % (fmt_t(s['at']), s['pause'], s['n'], html.escape(ph),
                        html.escape(calls), bg))
    ph_agg = collections.Counter()
    for s in steps:
        for k, v in s['calls'].items():
            ph_agg[phase_of(k.split(' ', 1)[1])] += v
        # Фоновые опросы шагов не образуют, но нагрузку создают наравне
        # со всеми — из счёта по разделам их исключать нельзя.
        for k, v in s['bg'].items():
            ph_agg['фоновый опрос'] += v
    prows = ''.join('<tr><td>%s</td><td class="n">%d</td><td class="n">%.0f%%</td></tr>'
                    % (html.escape(k), v, 100.0 * v / sum(ph_agg.values()))
                    for k, v in ph_agg.most_common())

    tele_head = ''
    if d['tele']:
        t = d['tele']
        tele_head = tile('Записал бэк', '%d' % t['all'],
                         '%d от пользователя, %d интеграция' % (t['user'], t['integ']))

    doc = """<title>Анатомия сессии пользователя</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;650&family=Source+Sans+3:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>%(css)s</style>
<div class="wrap">
<header class="top">
  <div class="eyebrow">%(eyebrow)s</div>
  <h1>Анатомия сессии пользователя</h1>
  <p class="sub">Что один живой человек стоит серверу: сколько запросов снимает кэш,
  сколько доходит до приложения, во что каждый обходится в базе — и как это пересчитать
  в число людей, которые могут сидеть на сайте одновременно.</p>
  <div class="meta">
    <span><b>запись</b> %(file)s</span>
    <span><b>браузер</b> %(browser)s</span>
    <span><b>начало</b> %(start)s UTC</span>
    <span><b>длительность</b> %(dur)s</span>
  </div>
</header>

<section>
  <div class="shd"><h2>Из чего состоит сессия</h2></div>
  <div class="tiles">
    %(t1)s %(t2)s %(t3)s %(t4)s %(t5)s
  </div>
  <div class="note">Из <b>%(total)d</b> запросов страницы нагрузку на приложение
  создают <b>%(php)d</b>. Всё остальное — кэш браузера и статика, которую отдаёт
  веб-сервер, не будя приложение. %(phpcheck)s</div>
</section>

<section>
  <div class="shd"><h2>Кэш по таймлайну</h2><span class="eyebrow">столбец = %(bucket)d с</span></div>
  <figure>%(g1)s<figcaption>Высота столбца — все запросы браузера за пять секунд.
  Зелёное снял кэш и до сети оно не дошло; жёлтое ушло за статикой в nginx;
  синее — единственная часть, которую обрабатывает приложение.</figcaption></figure>
  %(lg1)s
  %(tv1)s
  <div class="note">Кэш работает неравномерно: он снимает нагрузку на переходах
  между уже виденными страницами и почти не помогает там, где человек открывает
  раздел впервые. Средняя доля по сессии — <b>%(cachepct).0f%%</b> — прячет это
  различие, поэтому смотреть надо именно на таймлайн.</div>
</section>

<section>
  <div class="shd"><h2>Темп запросов</h2><span class="eyebrow">rps, окно %(bucket)d с</span></div>
  <figure>%(g2)s<figcaption>Верхняя линия — всё, что уходит из браузера.
  Нижняя — то, что доходит до приложения. Расстояние между ними и есть работа кэша и веб-сервера.</figcaption></figure>
  %(lg2)s
  %(tv2)s
  <div class="note">Средний темп по сессии — <b>%(rate).1f запроса в минуту</b> на
  человека (%(rps).2f rps до приложения). Но пики втрое выше среднего: сессия — это
  не ровный поток, а пачки запросов при каждом переходе. Нагрузочный профиль,
  собранный по среднему, недооценит пики.</div>
</section>

%(sect3)s

<section>
  <div class="shd"><h2>Сколько людей помещается</h2></div>
  <div class="scroll"><table><thead><tr><th>если человек уходит офлайн на</th>
  <th class="n">сессия</th><th class="n">запросов в минуту</th>
  <th class="n">людей при %(cap)d rps</th></tr></thead><tbody>%(cap_rows)s</tbody></table></div>
  <div class="note">Число запросов за сессию не меняется — меняется только время,
  за которое человек их делает. Поэтому <b>%(base)d</b> — это жёсткая нижняя
  граница: режим, где никто не отвлекается ни на секунду. Реальные паузы —
  сделать домашнее задание офлайн, посмотреть запись трансляции — растягивают
  сессию и во столько же раз поднимают вместимость.</div>
</section>

<section>
  <div class="shd"><h2>Сценарий для нагрузочного теста</h2>
    <span class="eyebrow">%(nsteps)d %(stepw)s</span></div>
  <p class="sub">Шаг — пачка запросов, между которыми человек не делал пауз.
  Порог паузы не задан константой, а посчитан по самой записи:
  <b>%(thr).1f с</b> — вчетверо больше медианного простоя между запросами.
  Так шаги не разъезжаются от того, быстро или медленно человек кликал.</p>
  %(pollblock)s
  <div class="card"><h3>По разделам</h3>
  <div class="scroll"><table><thead><tr><th>раздел</th><th class="n">запросов</th>
  <th class="n">доля</th></tr></thead><tbody>%(prows)s</tbody></table></div></div>
  <div class="card"><h3>Шаги подряд</h3>
  <div class="scroll"><table class="steps"><thead><tr><th>время</th><th class="n">пауза</th>
  <th class="n">n</th><th>запросы</th></tr></thead><tbody>%(srows)s</tbody></table></div></div>
  <div class="note"><b>Чего в записи нет, а в жизни будет.</b> Длинные паузы
  надо вставить в профиль руками: там, где человек уходит от экрана — читает,
  делает что-то офлайн, смотрит видео с чужого хоста, — запись показывает ровно
  ноль запросов, потому что записывали её без этих пауз. Такие промежутки не
  грузят API вовсе, но растягивают сессию — а значит, снижают темп запросов
  на человека.</div>
</section>

<footer>Собрано из записи браузера%(tele_note)s. Столбец таймлайна — %(bucket)d с.
Серверное время берётся из Telescope и сетевой задержки не содержит.</footer>
</div>""" % {
        'eyebrow': ('Запись браузера · сверка с Telescope' if d['tele']
                    else 'Запись браузера'),
        'css': CSS, 'file': html.escape(d['file']), 'browser': html.escape(d['browser']),
        'start': d['start'].strftime('%Y-%m-%d %H:%M:%S'),
        'dur': '%d:%02d' % (int(span) // 60, int(span) % 60),
        'total': d['total'], 'php': d['php'], 'bucket': BUCKET,
        'phpcheck': d['phpcheck'],
        't1': tile('Запросов в записи', d['total'], 'всё, что делал браузер'),
        't2': tile('Снял кэш', cache, '%.0f%% запросов до сети не дошли'
                   % (100.0 * cache / d['total'])),
        't3': tile('Статика', c.get('static', 0), 'отдаёт веб-сервер, приложение не будит'),
        't4': tile('Дошло до приложения', d['php'],
                   '%d %s API' % (api, plural(api, 'вызов', 'вызова', 'вызовов'))),
        't5': tele_head or tile('Темп', '%.1f' % rate_min, 'запроса в минуту на человека'),
        'g1': g1, 'lg1': legend(lab1, col1),
        'tv1': table_view(lab1, [d['ts_cache'], d['ts_static'], d['ts_api']], nb),
        'g2': g2, 'lg2': legend(lab2, col2),
        'tv2': table_view(lab2, [[round(x, 2) for x in rps_all],
                                 [round(x, 2) for x in rps_php]], nb),
        'cachepct': 100.0 * cache / d['total'],
        'rate': rate_min, 'rps': d['php'] / span,
        'sect3': section3(d, g3, tele_tbl),
        'cap': CAP, 'cap_rows': cap_rows(d, span),
        'base': int(CAP / (d['php'] / span)),
        'thr': thr, 'pollblock': poll_block(polls),
        'nsteps': len(steps),
        'stepw': plural(len(steps), 'шаг', 'шага', 'шагов'),
        'prows': prows, 'srows': ''.join(srows),
        'tele_note': ' и записей Telescope' if d['tele'] else '',
    }
    if not BARE[0]:
        # Самостоятельный файл: без doctype и meta charset браузер откроет
        # кириллицу кракозябрами. В режиме --bare шапку добавляет издатель,
        # и своя тут была бы второй.
        doc = ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
               '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
               + doc.replace('</div>', '', 0) + '\n</body>\n</html>')
        doc = doc.replace('<div class="wrap">', '</head>\n<body>\n<div class="wrap">', 1)
    open(out_path, 'w', encoding='utf-8').write(doc)
    return out_path


def section3(d, g3, tbl):
    if not d['tele']:
        return ('<section><div class="shd"><h2>Сверка с бэком</h2></div>'
                '<p class="sub">Записей Telescope нет — раздел пуст. '
                'Запусти <span class="mono">telescope.py</span> за окно записи.</p></section>')
    t = d['tele']
    return """<section>
  <div class="shd"><h2>Браузер против бэка</h2>
    <span class="eyebrow">интеграции отделены</span></div>
  %(g3)s
  <p class="sub">Синее — то, что ушло из браузера к бэку (вызовы API вместе
  с OPTIONS-предзапросами). Оранжевое — то же самое глазами сервера.
  Линии идут вплотную: <b>%(browser)d</b> запросов из браузера против
  <b>%(user)d</b> записей Telescope. Жёлтое — <b>%(integ)d</b> вызовов интеграции,
  которых браузер не делал вовсе.</p>
  <div class="card" style="margin-top:14px"><h3>Расхождение: %(diff)+d запроса</h3>
  <div class="scroll"><table><thead><tr><th>метод</th><th class="n">браузер</th>
  <th class="n">Telescope</th><th class="n">разница</th></tr></thead>
  <tbody>%(mrows)s</tbody></table></div>
  <p class="sub" style="margin-top:10px">Расхождение меньше процента, и это не ошибка
  сверки, а свойство стенда: дев общий, окно выгрузки берётся с запасом в десять
  секунд с каждой стороны, и в него попадает соседний трафик. Важно другое —
  расхождение не систематическое: если бы фронт слал запросы, которых бэк не видит
  (или наоборот), разница шла бы в одну сторону и росла со временем.</p></div>
  <div class="note"><b>Интеграция идёт фоном и от числа людей не зависит.</b>
  За %(mins).1f минуты записи внешняя система прислала %(integ)d вебхуков —
  это %(per_min).1f в минуту, ровным потоком. Каждый дешёвый (медиана 10 мс,
  ноль запросов в БД), но каждый ставит фоновую задачу. В нагрузочный профиль
  их закладывать не нужно: они идут и без пользователей — но из замеров rps
  вычитать надо, иначе потолок стенда окажется занижен.</div>
  <div class="card" style="margin-top:16px"><h3>Во что обходится каждый вызов</h3>
  <p class="sub" style="margin:0 0 12px">Серверное время и работа в базе — из Telescope.
  Два вызова с одинаковым временем ответа могут отличаться числом запросов в БД в разы.</p>
  %(tbl)s</div>
</section>""" % {'g3': g3, 'user': t['user'], 'integ': t['integ'], 'tbl': tbl,
                 'mins': d['span'] / 60.0, 'per_min': t['integ'] / (d['span'] / 60.0),
                 'browser': d.get('browser_php', 0),
                 'diff': t['user'] - d.get('browser_php', 0),
                 'mrows': method_rows(d, t)}


def method_rows(d, t):
    """Сверка по методам: она показывает, ГДЕ разошлось, а не только на сколько."""
    br = collections.Counter()
    for e in d['_entries']:
        c = harmod.classify(e)
        if c in ('api', 'preflight'):
            br[e['request']['method']] += 1
    tl = collections.Counter(r['method'] for r in t['rows'])
    out = []
    for m in sorted(set(br) | set(tl), key=lambda x: -(br[x] + tl[x])):
        dd = tl[m] - br[m]
        cls = '' if dd == 0 else ' class="n"'
        out.append('<tr><td class="mono">%s</td><td class="n">%d</td><td class="n">%d</td>'
                   '<td class="n">%s</td></tr>'
                   % (m, br[m], tl[m], '—' if dd == 0 else '%+d' % dd))
    return ''.join(out)


def poll_block(polls):
    """Найденные фоновые опросы — отдельным блоком, с измеренным периодом.

    Пишем «похоже на опрос», а не «опрос»: различить таймер и человека,
    который случайно повторял действие в одном ритме, по записи нельзя.
    Период и разброс показаны, чтобы это можно было решить глазами.
    """
    if not polls:
        return ('<div class="note">Фоновых опросов в записи не нашлось: все запросы '
                'идут от действий человека. Если опрос в приложении есть, но редкий, '
                'он мог не набрать четырёх повторов за запись — тогда он попал в шаги '
                'как обычный вызов.</div>')
    rows = ''.join(
        '<tr><td class="mono">%s</td><td class="n">%.0f с</td><td class="n">±%.0f%%</td>'
        '<td class="n">%d</td><td class="n">%.0f%%</td></tr>'
        % (html.escape(k[1].replace('/api/v1/', '')), v['period'], v['spread'] * 100,
           v['n'], v['cover'] * 100)
        for k, v in sorted(polls.items(), key=lambda kv: -kv[1]['n']))
    return ("""<div class="card"><h3>Похоже на фоновый опрос</h3>
  <p class="sub" style="margin:0 0 12px">Эти запросы шлёт таймер, а не человек:
  период почти не плавает, и они растянуты на всю сессию. Границ шага они не
  образуют — иначе один опрос раз в минуту нарезал бы сценарий на куски там,
  где человек ничего не делал. В нагрузку они входят полностью.</p>
  <div class="scroll"><table><thead><tr><th>запрос</th><th class="n">период</th>
  <th class="n">разброс</th><th class="n">раз</th><th class="n">покрытие сессии</th>
  </tr></thead><tbody>%s</tbody></table></div>
  <p class="sub" style="margin-top:10px">В нагрузочном профиле это отдельный поток:
  он идёт всё время, пока вкладка открыта, и не зависит от того, чем занят человек.
  Человек, который час держит сайт открытым и ничего не делает, — это всё равно
  запрос в минуту.</p></div>""" % rows)


def cap_rows(d, span):
    out = []
    for extra in (0, 10, 20, 30):
        dur = span / 60.0 + extra
        rate = d['php'] / dur
        out.append('<tr><td>%s</td><td class="n">%.1f мин</td><td class="n">%.1f</td>'
                   '<td class="n">%d</td></tr>'
                   % ('ничего — как в записи' if not extra else '%d минут' % extra,
                      dur, rate, int(CAP / (rate / 60))))
    return ''.join(out)


def main():
    a = [x for x in sys.argv[1:]]
    out = 'har-report.html'
    if '--out' in a:
        i = a.index('--out'); out = a[i + 1]; del a[i:i + 2]
    if '--bare' in a:
        BARE[0] = True; a.remove('--bare')
    if not a:
        print(__doc__)
        return 1
    har = a[0]
    tele = a[1] if len(a) > 1 else None
    d, entries = collect(har, tele)
    steps, polls, thr = scenario(entries)
    p = build(d, steps, polls, thr, out)
    print('отчёт: %s (%d КБ)' % (p, os.path.getsize(p) // 1024))
    return 0


if __name__ == '__main__':
    sys.exit(main())
