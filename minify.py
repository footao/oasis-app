"""ブックマークレット用の安全なミニファイ（行頭・行末の // コメントを除去して1行化）。"""
import re


def minify_js(src):
    lines = []
    for raw in src.split('\n'):
        line = raw.strip()
        if not line or line.startswith('//'):
            continue
        # 行末コメント（文字列の外にあるものだけ）を落とす
        m = re.search(r'\s+//\s.*$', line)
        if m:
            head = line[:m.start()]
            if '://' not in head[-8:] and head.count('"') % 2 == 0 \
                    and head.count("'") % 2 == 0 and head.count('`') % 2 == 0:
                line = head
        lines.append(line)
    return re.sub(r'\s+', ' ', ' '.join(lines)).strip()


def inject(html_path, js_path, marker):
    import html as H
    js = minify_js(open(js_path, encoding='utf-8').read())
    href = 'javascript:' + js
    page = open(html_path, encoding='utf-8').read()
    page = re.sub(r'href="javascript:.*?">' + re.escape(marker),
                  lambda m: 'href="' + H.escape(href, quote=True) + '">' + marker,
                  page, count=1, flags=re.S)
    open(html_path, 'w', encoding='utf-8').write(page)
    return len(js)


if __name__ == '__main__':
    import sys
    print(inject(sys.argv[1], sys.argv[2], sys.argv[3]))
