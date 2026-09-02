#!/usr/bin/env python3
"""Текстовый вывод по прогону: до какого rps держится стабильность и где сломалось.

  python3 analyze.py <папка прогона>
  python3 analyze.py --last          последний прогон
  python3 analyze.py --all           по всем прогонам стенда

Отвечает на два вопроса заказчика:
  «до какого rps и какой загрузки CPU время ответа держится ровно»
  «какой максимум rps выжали и какой ценой»

Одиночные всплески намеренно игнорируются: в облаке всегда найдётся секунда,
где один запрос занял 12 секунд, и делать по ней вывод нельзя. Деградацией
считается только то, что держится подряд WINDOW секунд.
"""
import json, os, sys, glob, gzip, datetime, collections, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
ROOT = config.ROOT
STAND = config.STAND

SMOOTH = 5      # точек в скользящем окне (шаг серии 2 с → окно 10 с)
WINDOW = 8      # столько точек подряд должно быть плохо, чтобы счесть это деградацией
# Ищем НАЧАЛО роста, а не «уже всё плохо»: стабильность кончается там, где время
# ответа впервые уходит с полки, а не там, где оно выросло в полтора раза.
# Порог низкий (5%), поэтому от шума защищаемся длиной окна, а не высотой планки.
RISE = 1.05
MIN_RISE = 15   # ...и минимум на столько мс, иначе на быстрых запросах ловим шум
HOLD = 0.6      # доля оставшихся точек, которая должна остаться плохой
# Деградацию вообще признаём, только если к концу прогона стало ЗАМЕТНО хуже.
# Иначе рост p95 с 470 до 497 мс на минутном прогоне объявляется потерей
# стабильности, хотя это обычный шум.
SIGNIFICANT = 1.3
# Потолок загрузки процессора для «стабильного» режима. Формально сервер может
# отвечать ровно и на 98% CPU, но запаса там нет никакого: любой всплеск, соседний
# процесс или сборка мусора — и очередь встаёт. Поэтому стабильность кончается
# там, что наступит раньше: рост времени ответа ИЛИ упор в процессор.
# Договорённый потолок времени ответа. Пока p95 гуляет далеко под ним, рост
# «с 399 до 458 мс» — это не потеря стабильности, а обычное дыхание системы:
# по такому выводу человек начинает искать проблему там, где её нет.
LAT_LIMIT = 1000
LAT_NEAR = 0.8   # «подошли к порогу» — это 80% от него
LAT_BLOWUP = 3.0  # ...либо время ответа выросло втрое, даже если порог далеко
CPU_LIMIT = 85
CPU_HOLD = 3   # столько точек Beszel подряд выше потолка (минуты) — уже не случайность


def smooth(vals, win=SMOOTH):
    """Скользящая медиана: она, в отличие от среднего, не тянется за выбросом."""
    out = []
    for i in range(len(vals)):
        a = max(0, i - win // 2)
        out.append(statistics.median(vals[a:i + win // 2 + 1] or [vals[i]]))
    return out


def sustained(vals, win):
    """Уровень, который держался подряд win точек, — максимальный из таких.

    Мгновенный пик врёт в обе стороны: одна секунда со 113 rps не пропускная
    способность, а средняя за прогон занижает её вдвое, если профиль растущий
    (первую минуту сервер работал вхолостую). Нужен верх ПОЛКИ.
    """
    if not vals:
        return 0
    if len(vals) < win:
        return min(vals)
    return max(min(vals[i:i + win]) for i in range(len(vals) - win + 1))


def plateau(rps, vus, win):
    """Начало полки: с этой точки темп уже не растёт, хотя потоков прибавляется.

    Это и есть потолок пропускной способности. Ищем не «где максимум», а где
    кривая вышла на уровень и осталась на нём: одиночный пик в 113 rps — шум,
    а десять минут по сто — ответ на вопрос «сколько сервер тянет».
    """
    n = len(rps)
    if n < 20 or not vus or len(vus) != n:
        return None
    lvl = statistics.median(rps[-win:])           # уровень, на котором закончили
    if lvl <= 0:
        return None
    start = None
    # Полку признаём, только если она заняла последние полторы минуты и больше:
    # три точки на уровне — это ещё не «сервер больше не может».
    for i in range(n // 4, n - 3 * win):
        if all(v >= lvl * 0.88 for v in rps[i:]):
            start = i
            break
    if start is None:
        return None
    # Потоков после выхода на полку должно заметно прибавиться: иначе темп не
    # растёт просто потому, что нагрузку перестали наращивать. Ноль потоков в
    # начале полки любую проверку отношением проходит — его отсекаем отдельно.
    if vus[start] < 1 or vus[-1] < vus[start] * 1.2:
        return None
    return start


def band(v, step):
    """Значение → диапазон, кратный step: 43 → «40–45». Точность тут ложная,
    а заказчику нужен порядок величины, а не третий знак."""
    lo = int(v // step) * step
    return f"{lo}–{lo + step}"


def hhmm(tsec):
    return datetime.datetime.fromtimestamp(tsec).strftime('%H:%M')


def hhmmss(tsec):
    return datetime.datetime.fromtimestamp(tsec).strftime('%H:%M:%S')


def cpu_at(srv, tsec, tol=90):
    """CPU на момент времени. Beszel пишет раз в минуту, поэтому берём ближайшую
    точку, но только если она не дальше tol секунд — иначе честнее сказать «нет»."""
    if not srv:
        return None
    best = min(srv, key=lambda p: abs(p['tsec'] - tsec))
    return best['cpu'] if abs(best['tsec'] - tsec) <= tol else None


def cpu_range(srv, t0, t1):
    """Разброс CPU за интервал — как «70–80%» в формулировке заказчика.

    Вперёд не заглядываем: запас в минуту затягивал в «стабильную» зону точку,
    снятую уже ПОСЛЕ её конца, и стабильность получала чужие 93%.
    """
    v = [p['cpu'] for p in srv if t0 - 30 <= p['tsec'] <= t1]
    if not v:
        v = [p['cpu'] for p in srv if t0 - 90 <= p['tsec'] <= t1 + 30]
    return (min(v), max(v)) if v else None


def cpu_lerp(srv, tsec):
    """Загрузка в произвольный момент, линейно между соседними замерами.

    Между двумя точками Beszel лежит целая минута, за которую CPU успевает
    уйти с 46% на 93%. Брать «последнюю точку внутри окна» нельзя: на границе
    зоны это занижает загрузку вдвое и делает вывод неправдоподобным.
    """
    if not srv:
        return None
    if tsec <= srv[0]['tsec']:
        return srv[0]['cpu']
    if tsec >= srv[-1]['tsec']:
        return srv[-1]['cpu']
    for a, b in zip(srv, srv[1:]):
        if a['tsec'] <= tsec <= b['tsec']:
            d = b['tsec'] - a['tsec']
            k = (tsec - a['tsec']) / d if d else 0
            return a['cpu'] + (b['cpu'] - a['cpu']) * k
    return None


def cpu_cross(srv, limit):
    """Момент, когда загрузка пересекла потолок, с линейной интерполяцией.

    Beszel пишет раз в минуту: между 46% и 93% лежит целая минута, и без
    интерполяции граница стабильности уезжает на эту минуту в любую сторону.
    """
    for a, b in zip(srv, srv[1:]):
        if a['cpu'] <= limit < b['cpu']:
            d = b['cpu'] - a['cpu']
            k = (limit - a['cpu']) / d if d else 0
            return a['tsec'] + (b['tsec'] - a['tsec']) * k
    return srv[0]['tsec'] if srv and srv[0]['cpu'] > limit else None


def err_intervals(ser):
    """Промежутки с ошибками, слитые в непрерывные куски.

    Разрывы в одну точку сшиваем: иначе одна чистая секунда посреди сплошных
    отказов разрезает интервал надвое и в отчёте получается частокол.
    """
    bad = [i for i, r in enumerate(ser) if (r.get('e4', 0) + r.get('e5', 0)) > 0]
    if not bad:
        return []
    out = [[bad[0], bad[0]]]
    for i in bad[1:]:
        if i - out[-1][1] <= 2:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def errors_by_request(d, t0, t1):
    """Какие запросы падали в этом интервале — из сырого потока k6.

    В summary.json ошибки сведены за весь прогон, привязки ко времени там нет,
    поэтому за детализацией идём в raw.json.gz.
    """
    raw = os.path.join(d, 'raw.json.gz')
    if not os.path.exists(raw):
        return {}
    cnt = collections.Counter()
    with gzip.open(raw, 'rt') as f:
        for line in f:
            if '"http_req_duration"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get('type') != 'Point':
                continue
            dd = o['data']
            tags = dd.get('tags') or {}
            st = str(tags.get('status', '0'))
            if not (st.startswith('4') or st.startswith('5')):
                continue
            ts = dd.get('time', '')
            try:
                t = datetime.datetime.fromisoformat(
                    ts[:26].replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if t0 <= t <= t1:
                name = tags.get('name') or tags.get('url') or '—'
                cnt[f"{name} [{st}]"] += 1
    return dict(cnt.most_common(6))


def analyze(d):
    s = json.load(open(os.path.join(d, 'summary.json'), encoding='utf-8'))
    ser = s['agg']['series']
    meta = s['meta']
    srv = meta.get('server') or []
    rows = s['agg']['rows']
    if len(ser) < 5:
        return {'name': meta['name'], 'short': 'прогон слишком короткий для выводов'}

    # Пустые ответы (204: csrf-cookie, logout) в rps не идут: интересует темп
    # содержательных запросов, а не то, сколько раз сервер ответил «нечем».
    # Общий поток держим рядом — он нужен для скобок в выводе.
    fast_codes = s['agg'].get('fast_codes') or []
    fast_n = s['agg'].get('skipped_fast') or 0
    p95 = smooth([r['p95'] for r in ser])
    rps_all = smooth([r['rps'] for r in ser])
    rps = smooth([r['rps'] - sum(r.get('c' + c, 0) for c in fast_codes) for r in ser])

    # База — медиана первой четверти прогона: там нагрузка ещё низкая
    # и видно, как система отвечает, пока ей ничто не мешает.
    head = max(3, len(ser) // 4)
    base = statistics.median(p95[:head]) or 1
    thr = max(base * RISE, base + MIN_RISE)

    # Первая точка, после которой WINDOW подряд точек выше порога И система
    # уже не возвращается к норме. Без второго условия коленом объявляется
    # любой провал в середине — например 20 секунд пятисоток, после которых
    # всё восстановилось. Такие всплески идут отдельным списком, а не в вердикт.
    err5 = [(r.get('e5', 0) > 0) for r in ser]
    tail_q = p95[int(len(p95) * 0.75):] or p95
    worst = max(tail_q)
    # Деградацией считаем только то, что видно и по абсолютной величине:
    # либо время ответа подошло к порогу, либо выросло в разы.
    degraded = (worst >= base * SIGNIFICANT
                and (worst >= LAT_LIMIT * LAT_NEAR or worst >= base * LAT_BLOWUP))
    knee = None if not degraded else False
    if degraded:
        knee = None
        for i in range(head, len(p95) - WINDOW + 1):
            if not all(p95[j] > thr and not err5[j] for j in range(i, i + WINDOW)):
                continue
            tail = p95[i:]
            if sum(1 for v in tail if v > thr) / max(len(tail), 1) >= HOLD:
                knee = i
                break

    # Второе ограничение стабильной зоны — процессор.
    cpu_knee = None
    if srv and sum(1 for p in srv if p['cpu'] > CPU_LIMIT) >= CPU_HOLD:
        t_cross = cpu_cross(srv, CPU_LIMIT)
        if t_cross is not None:
            cpu_knee = min(range(len(ser)),
                           key=lambda i: abs(ser[i]['tsec'] - t_cross))

    limited_by = None
    if knee is not None and cpu_knee is not None:
        limited_by = 'cpu' if cpu_knee < knee else 'time'
        knee = min(knee, cpu_knee)
    elif cpu_knee is not None:
        limited_by, knee = 'cpu', cpu_knee
    elif knee is not None:
        limited_by = 'time'

    stable_end = knee if knee is not None else len(ser)
    st = ser[:stable_end] or ser
    st_rps = rps[:stable_end] or rps
    # «Держит до X rps» — это темп на последней точке ПЕРЕД деградацией.
    # Брать перцентиль по всей стабильной зоне нельзя: при разгоне она начинается
    # почти с нуля, и среднее занижает реально выдержанный темп.
    top_stable = st_rps[-1] if knee is not None else max(st_rps, default=0)
    st_all = rps_all[:stable_end] or rps_all
    top_stable_all = st_all[-1] if knee is not None else max(st_all, default=0)

    peak_i = max(range(len(rps)), key=lambda i: rps[i])
    peak_rps = rps[peak_i]
    avg_rps = (s['agg']['total'] - fast_n) / (s['agg'].get('duration') or 1)
    # «Максимум rps» — верх полки, а не средняя за прогон: на растущем профиле
    # средняя считает и первую минуту холостого хода, и выходит вдвое ниже того,
    # что сервер реально держал. Полкой считаем 30 секунд подряд.
    step = (ser[1]['tsec'] - ser[0]['tsec']) or 2
    win = max(5, int(30 / step))
    top_rps = sustained(rps, win)

    # Потолок пропускной способности: потоков больше, а темпа — нет.
    vus_ser = [r.get('vus', 0) for r in ser]
    sat_i = plateau(rps, vus_ser, win)
    # Если полка найдена — она и есть пропускная способность: верх шума над
    # полкой цифру только завышает, а к продолжительной работе отношения не имеет.
    if sat_i is not None:
        top_rps = statistics.median(rps[sat_i:])

    p50_stable = [r['p50'] for r in st]
    med = statistics.median(p50_stable) if p50_stable else 0
    med95 = statistics.median([r['p95'] for r in st]) if st else 0

    errs = sum(r['errors'] for r in rows)
    ivs = []
    for a, b in err_intervals(ser):
        t0, t1 = ser[a]['tsec'], ser[b]['tsec']
        n = sum(ser[i]['e4'] + ser[i]['e5'] for i in range(a, b + 1)) * 2
        ivs.append({'from': hhmmss(t0), 'to': hhmmss(t1), 'n': int(n),
                    'sec': int(t1 - t0) + 2, 'who': errors_by_request(d, t0, t1 + 2)})

    return {
        'name': meta['name'], 'when': meta.get('when', ''), 'dir': os.path.basename(d),
        'stable_all': knee is None, 'limited_by': limited_by,
        'cpu_limit': CPU_LIMIT,
        'top_stable': top_stable, 'peak_rps': peak_rps, 'avg_rps': avg_rps,
        'top_rps': top_rps, 'top_rps_all': sustained(rps_all, win),
        'top_stable_all': top_stable_all,
        'peak_rps_all': rps_all[peak_i],
        'fast_codes': fast_codes, 'fast_n': fast_n,
        'sat_rps': statistics.median(rps[sat_i:]) if sat_i is not None else None,
        'sat_t': hhmm(ser[sat_i]['tsec']) if sat_i is not None else None,
        'sat_cpu': cpu_lerp(srv, ser[sat_i]['tsec']) if sat_i is not None else None,
        'sat_vus': (vus_ser[sat_i], max(vus_ser)) if sat_i is not None else None,
        'knee_t': hhmm(ser[knee]['tsec']) if knee is not None else None,
        'knee_p95': p95[knee] if knee is not None else None,
        'base_p95': base, 'med_p50': med, 'med_p95': med95,
        'cpu_stable': cpu_range(srv, ser[0]['tsec'], ser[stable_end - 1]['tsec']),
        'cpu_knee': cpu_lerp(srv, ser[stable_end - 1]['tsec']),
        'cpu_peak': cpu_at(srv, ser[peak_i]['tsec']),
        'cpu_max': max((p['cpu'] for p in srv), default=None),
        'total': s['agg']['total'], 'errors': errs,
        'err_pct': 100 * errs / s['agg']['total'] if s['agg']['total'] else 0,
        'intervals': ivs, 'has_srv': bool(srv),
    }


def as_text(a):
    """Вывод в формулировках заказчика."""
    if a.get('short'):
        return f"{a['name']}: {a['short']}"
    L = []
    if a.get('limited_by') == 'cpu':
        # Границу зоны мы сами и провели по потолку, поэтому называем его,
        # а не случайное значение последнего замера.
        cpu_s = f" и до {a['cpu_limit']}% нагрузки CPU"
    elif a.get('cpu_knee') is not None and not a['stable_all']:
        # На границе зоны важна загрузка именно в этот момент, а не максимум
        # за весь стабильный участок, который начинался почти с холостого хода.
        cpu_s = f" и {band(a['cpu_knee'], 10)}% нагрузке CPU"
    elif a['cpu_stable']:
        cpu_s = f" и {band(a['cpu_stable'][1], 10)}% нагрузке CPU"
    else:
        cpu_s = ""
    if a['stable_all']:
        L.append(f"Стабильность работы наблюдается на протяжении всего теста — "
                 f"половина запросов укладывается в {band(a['med_p50'], 50)} мс, "
                 f"95% — в {band(a['med_p95'], 50)} мс{cpu_s}.")
    elif a.get('limited_by') == 'cpu':
        L.append(f"Стабильная работа наблюдается до {band(a['top_stable'], 5)} rps "
                 f"({a['knee_t']}){cpu_s}. Дальше сервер упирается в процессор: "
                 f"загрузка уходит выше {a['cpu_limit']}%, запаса не остаётся. "
                 f"Время ответа при этом ещё держится "
                 f"({a['base_p95']:.0f}→{a['knee_p95']:.0f} мс по p95), "
                 f"но режим уже предельный.")
    else:
        L.append(f"Стабильность во времени ответов наблюдается до "
                 f"{band(a['top_stable'], 5)} rps ({a['knee_t']}){cpu_s}. "
                 f"Дальше время ответа растёт: с {a['base_p95']:.0f} до "
                 f"{a['knee_p95']:.0f} мс по p95.")
    top_cpu = a['cpu_max'] if a['cpu_max'] is not None else a['cpu_peak']
    if top_cpu is not None:
        cp = (f" при нагрузке ≤ {band(top_cpu, 10)}% CPU"
              if top_cpu < 95 else " при нагрузке ≤ 100% CPU")
    else:
        cp = " (метрик CPU за окно прогона нет)"
    # Общий поток — в скобках: он объясняет, почему на графике линий больше,
    # но мерой нагрузки служат содержательные ответы.
    whole = (f" ({band(a['top_rps_all'], 5)} вместе с {', '.join(a['fast_codes'])})"
             if a.get('fast_n') else "")
    # В скобках — мгновенный пик: сначала содержательные ответы, следом весь
    # поток, если пустые коды вообще были, и загрузка ровно в этот момент.
    peak_s = f"{a['peak_rps']:.0f}"
    if a.get('fast_n'):
        peak_s += f" ({a['peak_rps_all']:.0f})"
    cpu_p = (f" при {a['cpu_peak']:.0f}% CPU") if a.get('cpu_peak') is not None else ""
    L.append(f"Максимум rps = {band(a['top_rps'], 5)}{whole}{cp} "
             f"(Максимум {peak_s} rps{cpu_p}).")
    if a.get('sat_rps') is not None:
        cs = f" при {a['sat_cpu']:.0f}% CPU" if a.get('sat_cpu') is not None else ""
        v0, v1 = a['sat_vus']
        L.append(f"Темп упёрся в потолок ≈{a['sat_rps']:.0f} rps с {a['sat_t']}{cs}: "
                 f"потоков дальше стало {v0:.0f}→{v1:.0f}, а запросов в секунду "
                 f"больше не стало.")

    if a['errors']:
        L.append(f"Ошибок: {a['errors']} из {a['total']} ({a['err_pct']:.1f}%).")
        for iv in a['intervals'][:4]:
            who = ', '.join(f"{k} ×{v}" for k, v in iv['who'].items()) or 'состав уточнить в отчёте'
            L.append(f"   {iv['from']}–{iv['to']} ({iv['sec']} с): {who}")
    else:
        L.append("Ошибок нет.")
    if not a['has_srv']:
        L.append("CPU сервера за окно прогона не получен — выводы только по времени ответа.")
    return '\n'.join(L)


def main():
    args = [x for x in sys.argv[1:]]
    base = os.path.join(config.RESULTS, STAND)
    if not args or args[0] == '--last':
        ds = sorted(glob.glob(os.path.join(base, '*', 'summary.json')),
                    key=os.path.getmtime, reverse=True)
        targets = [os.path.dirname(ds[0])] if ds else []
    elif args[0] == '--all':
        targets = sorted((os.path.dirname(p) for p in glob.glob(
            os.path.join(base, '*', 'summary.json'))), key=os.path.getmtime, reverse=True)
    else:
        targets = args
    if not targets:
        print('прогонов не найдено')
        sys.exit(1)
    for d in targets:
        a = analyze(d)
        print(f"\n=== {a['name']} · {a.get('when','')} ===")
        print(as_text(a))


if __name__ == '__main__':
    main()
