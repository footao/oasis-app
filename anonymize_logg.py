# -*- coding: utf-8 -*-
"""anonymize_logg.py — logg/ の Discord ハンドルを連番に置き換える。

    python anonymize_logg.py logg           # 置き換えて上書き（.bak を残す）
    python anonymize_logg.py logg --dry     # 何件変わるか見るだけ

`👤 @ハンドル`（出走表）と `@ハンドル`（結果）の両方を置き換える。
同じ人は毎回同じ番号（@u001 …）になるので、
「同じ馬主の馬」という情報は残る。学習は owner 列を使っていないので精度は変わらない。
`@Unknown` はそのまま（既に匿名）。
"""
import io
import os
import re
import sys

# 馬主の行は2種類ある。**両方置き換えないと壊れる**。
#   出走表: 「👤 @なまえ」
#   結果 : 「@なまえ」（👤 が付かない）
# 出走表と結果は (馬名, 馬主, ステータス) で突き合わせているので、片方だけ匿名化すると
# 照合が外れてコンディションが取れなくなる（実際 1113行が「普通」に化けた）。
# 名前は行末まで（空白や @ を含むハンドルがあるため）。
OWNER_RE = re.compile(r'^(\s*(?:👤\s*)?@)(.+?)(\s*)$', re.MULTILINE)
KEEP = {'Unknown'}


def anonymize(text, mapping):
    def sub(m):
        name = m.group(2)
        if name in KEEP:
            return m.group(0)
        if name not in mapping:
            mapping[name] = f'u{len(mapping) + 1:03d}'
        return f'{m.group(1)}{mapping[name]}{m.group(3)}'
    return OWNER_RE.sub(sub, text)


def main(path='logg', dry=False):
    files = [os.path.join(path, f) for f in sorted(os.listdir(path))
             if f.lower().endswith(('.txt', '.md'))] if os.path.isdir(path) else [path]
    if not files:
        print(f'❌ {path} に .txt がありません')
        return 1
    mapping, changed = {}, 0
    for f in files:
        src = io.open(f, encoding='utf-8', errors='replace').read()
        out = anonymize(src, mapping)
        n = sum(1 for a, b in zip(src.split('\n'), out.split('\n')) if a != b)
        changed += n
        print(f'  {os.path.basename(f)[:50]:<52} {n:>6}行')
        if not dry and n:
            # 2回目の実行で「匿名化済みの本文」を .bak に上書きすると原本が消える。
            if os.path.exists(f + '.bak'):
                print(f'    → {os.path.basename(f)}.bak が既にあるので中止'
                      '（原本を失わないため）。消すか退避してから再実行してください。')
                continue
            io.open(f + '.bak', 'w', encoding='utf-8').write(src)
            io.open(f, 'w', encoding='utf-8').write(out)
    print(f'\nハンドル {len(mapping)}人 / {changed}行')
    if dry:
        print('--dry なので書き換えていません。')
    else:
        print('元のファイルは .bak として同じ場所に残しました。')
        print('⚠ .bak は絶対に push しないこと（.gitignore に *.bak を入れてあります）。')
    return 0


def _selfcheck():
    """置換が期待どおりで、かつ他の行を壊さないこと。"""
    src = ('【枠番 1】🐣 おいら\n👤 @Unknown\n📉 コンディション：好調 😄\n'
           '【枠番 2】🐣 ゆゆ\n👤 @ぷち\n🏃 スピード：85\n'
           '【枠番 3】🐣 もも\n👤 @ぷち\n👤 @山田 太郎\n'
           '🥇 ゆゆ\n@ぷち\n🏃 スピード 85\n'          # 結果ブロック（👤 なし）
           '🥈 もも\n@断然猫派@同担拒否\n')
    m = {}
    out = anonymize(src, m)
    assert '@Unknown' in out, out
    assert '@ぷち' not in out and '@山田 太郎' not in out, out
    assert '@ぷち' not in out and '@断然猫派' not in out, out
    assert out.count('@u001') == 3, out          # 出走表2回＋結果1回、同じ人は同じ番号
    assert '@u002' in out, out
    assert '🏃 スピード：85' in out, out          # 他の行は無傷
    assert len(out.split('\n')) == len(src.split('\n'))
    print('selfcheck OK')


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == '--selfcheck':
        _selfcheck()
    else:
        sys.exit(main(a[0] if a else 'logg', '--dry' in a))
