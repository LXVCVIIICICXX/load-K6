#!/usr/bin/env python3
"""Создаёт файлы, которые сценарии загружают на стенд: пара картинок и PDF.

Чужие файлы в репозиторий класть нельзя, а размер значение имеет — на
загрузке проверяется как раз он. Поэтому файлы синтетические и рождаются
при установке: setup.sh зовёт этот скрипт.

  python3 mocks/make_fixtures.py [--dir mocks] [--force]
"""
import base64, io, os, random, sys, zlib

HERE = os.path.dirname(os.path.abspath(__file__))

# Крошечный валидный JPEG 8×8. Нужен как запасной вариант: без Pillow
# нарисовать картинку нечем, а раздуть готовый файл до нужного размера можно
# комментарием — формат это разрешает, и декодеры такой файл читают.
TINY_JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a'
    'HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAAIAAgBAREA/8QAHwAAAQUBAQEB'
    'AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh'
    'ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ'
    'WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG'
    'x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+v//Z')


def jpeg(path, size, seed):
    """Картинка примерно заданного размера в байтах."""
    try:
        from PIL import Image                                    # noqa: PLC0415
        rnd = random.Random(seed)
        w, h = 1400, 1000
        img = Image.new('RGB', (w, h))
        px = img.load()
        # Градиент с шумом: ровная заливка сжалась бы в десяток килобайт,
        # а нам нужен файл, сопоставимый с фотографией с телефона.
        for y in range(h):
            for x in range(0, w, 2):
                v = (x * 255 // w + y * 255 // h) // 2
                c = (min(255, v + rnd.randint(0, 60)),
                     min(255, v + rnd.randint(0, 40)),
                     min(255, 255 - v + rnd.randint(0, 40)))
                px[x, y] = c
                px[x + 1, y] = c
        # Качество подбираем вниз, пока файл не влезет в целевой размер:
        # добить до него потом можно комментарием, а урезать уже нечем.
        data = b''
        for q in (95, 85, 75, 65, 55, 45, 35, 25):
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=q)
            data = buf.getvalue()
            if len(data) <= size:
                break
    except ImportError:
        data = TINY_JPEG
    data = pad_jpeg(data, size)
    open(path, 'wb').write(data)
    return len(data)


def pad_jpeg(data, size):
    """Дотягивает JPEG до нужного размера комментариями после SOI."""
    if len(data) >= size:
        return data
    need = size - len(data)
    chunks = []
    while need > 4:
        n = min(65533, need)
        chunks.append(b'\xff\xfe' + (n).to_bytes(2, 'big') + b'\x00' * (n - 2))
        need -= n + 2
    return data[:2] + b''.join(chunks) + data[2:]


def pdf(path, pages=2):
    """Минимальный валидный PDF — без внешних библиотек."""
    objs = []
    kids = ' '.join('%d 0 R' % (3 + i * 2) for i in range(pages))
    objs.append(b'<< /Type /Catalog /Pages 2 0 R >>')
    objs.append(('<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, pages)).encode())
    for i in range(pages):
        stream = ('BT /F1 24 Tf 72 700 Td (Load test fixture, page %d) Tj ET' % (i + 1)).encode()
        objs.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
                     '/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>'
                     % (3 + pages * 2, 4 + i * 2)).encode())
        objs.append(b'<< /Length %d >>stream\n' % len(stream) + stream + b'\nendstream')
    objs.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')

    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n' % (len(objs) + 1)
    out += b'0000000000 65535 f \n'
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (len(objs) + 1, xref))
    open(path, 'wb').write(bytes(out))
    return len(out)


def main():
    d = HERE
    if '--dir' in sys.argv:
        d = sys.argv[sys.argv.index('--dir') + 1]
    force = '--force' in sys.argv
    os.makedirs(d, exist_ok=True)
    plan = [('photo-1.jpg', lambda p: jpeg(p, 500_000, 1)),
            ('photo-2.jpg', lambda p: jpeg(p, 320_000, 2)),
            ('document.pdf', pdf)]
    for name, make in plan:
        p = os.path.join(d, name)
        if os.path.exists(p) and not force:
            print('• %s уже есть' % name)
            continue
        n = make(p)
        print('• %s — %s' % (name, '%d КБ' % (n // 1024) if n >= 1024 else '%d Б' % n))


if __name__ == '__main__':
    main()
