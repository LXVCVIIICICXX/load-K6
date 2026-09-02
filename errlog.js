// Логирование ТЕЛА неуспешного ответа. Код (422, 500) говорит что произошло,
// но не почему; без текста ошибку в отчёте не разобрать.
//
// Печатаем маркером ERR{...} в console.log — run.py вылавливает эти строки
// из потока k6, складывает в summary.json и показывает в отчёте отдельным
// раскрывающимся блоком.
//
// Тел много не нужно: 3578 одинаковых 422 читать невозможно, а отчёт
// группирует их по тексту. Поэтому на каждый (запрос+код) шлём не больше LIMIT
// штук — этого хватает, чтобы увидеть причину, и не раздувает лог.
const LIMIT = Number(__ENV.ERR_SAMPLES || 5);
const seen = {};

export function note(r, name) {
  if (!r || r.status >= 200 && r.status < 300) return r;
  const key = name + ':' + r.status;
  seen[key] = (seen[key] || 0) + 1;
  if (seen[key] > LIMIT) return r;

  let msg = '';
  try {
    const b = r.json();
    // Laravel кладёт текст в message, детали валидации — в errors.
    msg = b && b.message ? String(b.message) : '';
    if (b && b.errors) msg += ' | ' + JSON.stringify(b.errors);
  } catch (e) {
    msg = String(r.body || '').slice(0, 300);
  }
  if (!msg) msg = String(r.body || '').slice(0, 300);
  // Переводы строк сломали бы построчный разбор в run.py.
  msg = msg.replace(/\s+/g, ' ').trim().slice(0, 400);
  console.log('ERR' + JSON.stringify({ n: name, s: r.status, m: msg }));
  return r;
}

/**
 * Перехватывает все http-вызовы модуля и логирует тела неуспешных ответов.
 * Один вызов на тест вместо обёртки вокруг каждого запроса — так ни один
 * запрос не забудешь. Имя запроса берём из tags.name, которым тесты и так
 * помечают каждый вызов.
 */
export function hook(http) {
  for (const m of ['get', 'post', 'put', 'del', 'patch', 'request']) {
    const orig = http[m];
    if (typeof orig !== 'function') continue;
    http[m] = function (...args) {
      const r = orig.apply(http, args);
      let name = '';
      // noErrLog: код неуспешный, но ЖДАЛИ именно его (401 на probe до входа).
      // Такие в разбор не идут — иначе блок ошибок в отчёте состоит из них одних.
      let skip = false;
      for (const a of args) {
        if (a && a.tags && a.tags.name) name = a.tags.name;
        if (a && a.noErrLog) skip = true;
      }
      if (!skip) note(r, name || m.toUpperCase());
      return r;
    };
  }
}
