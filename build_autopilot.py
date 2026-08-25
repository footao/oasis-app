# -*- coding: utf-8 -*-
"""build_autopilot.py — オートパイロット一式を実ログから作り直す。

    python build_autopilot.py            # 既定で ./logg を学習
    python build_autopilot.py <ログパス>

やること:
  1. logg/ を学習して model.json を書き出す
  2. model.js + model.json + autopilot.js を結合して autopilot.bundle.js を作る
     （モデルを埋め込むので、実行時に外部から取りに行かなくても動く）
  3. oasis_autopilot_setup.html（ローダー版の設置ページ）を作り直す
  4. 生成物の構文チェック（node があれば）

**再学習したら必ずこれを実行してください。**
実行しないと、画面のモデルとオートパイロットのモデルがズレたままになります。
"""
import io
import json
import os
import subprocess
import sys

# Windows のコンソールは既定 cp932。✅ や → を出すので UTF-8 に切り替える。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'bookmarklets', 'src')
sys.path.insert(0, HERE)
sys.path.insert(0, SRC)

MIN_RACES = 20          # これ未満だと autopilot.js 側が実行を拒否する


def step(n, msg):
    print(f'\n[{n}] {msg}')
    print('-' * 64)


def main(log_path='logg'):
    import oasis_core as oc
    import minify

    step(1, f'モデルを学習して model.json を書き出す（{log_path}）')
    bundle = oc.train_model(log_path)
    if not bundle.get('ok'):
        print('❌ 学習に失敗しました:')
        for m in bundle.get('messages', []):
            print('   ' + m)
        return 1
    for m in bundle['messages']:
        print('   ' + m)
    n_races = int(bundle.get('n_races', 0))
    if n_races < MIN_RACES:
        print(f'\n❌ 学習レースが {n_races} 件しかありません（{MIN_RACES}件以上必要）。')
        print('   logg/ に十分なログが入っているか確認してください。')
        return 1

    payload = oc.export_model_json(bundle, os.path.join(HERE, 'model.json'))
    print(f'\n   → model.json  学習{n_races}レース / 係数{len(payload["coef"])}個'
          f' / σ単勝 {payload["race_sigma"]:.4f} / σ3連単 {payload["tri_sigma"]:.4f}')

    step(2, 'autopilot.bundle.js を作る（モデル埋め込み）')
    model_js = minify.minify_js(io.open(os.path.join(SRC, 'model.js'), encoding='utf-8').read())
    auto_js = minify.minify_js(io.open(os.path.join(SRC, 'autopilot.js'), encoding='utf-8').read())
    mj = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    bundle_js = ('/* おあしすっち オートパイロット バンドル（モデル埋め込み・自動生成）*/\n'
                 f'/* 学習 {n_races}レース  {payload.get("date_min")}〜{payload.get("date_max")} */\n'
                 '(()=>{window.__OASIS_MODEL=' + mj + ';\n'
                 + model_js + '\n' + auto_js + '\n})();\n')
    io.open(os.path.join(HERE, 'autopilot.bundle.js'), 'w', encoding='utf-8').write(bundle_js)
    print(f'   → autopilot.bundle.js  {len(bundle_js.encode()):,} bytes')

    step(3, 'HTML を作り直す')
    combined = '(()=>{' + model_js + ' ' + auto_js + '})();'
    tmp = os.path.join(HERE, '_combined.js')
    io.open(tmp, 'w', encoding='utf-8').write(combined)
    try:
        sys.path.insert(0, os.path.join(HERE, 'bookmarklets'))
        import build_setup_page
        build_setup_page.build.__globals__['io'] = io
        # build_setup_page は /tmp/combined.js を見るので、ここでは自前で組み立てる
        _write_setup_page(combined, build_setup_page)
        print('   → oasis_autopilot_setup.html')
    except Exception as e:
        print(f'   ⚠ setup ページの生成に失敗: {e}')
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    step(4, 'Python↔JS の一致検証')
    ok = True
    if not os.path.exists(os.path.join(HERE, 'parity_test.py')):
        print('   ⚠ parity_test.py が無いので飛ばしました')
    else:
        r = subprocess.run([sys.executable, os.path.join(HERE, 'parity_test.py')],
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        for stream in (r.stdout, r.stderr):
            if stream.strip():
                print('   ' + stream.strip().replace('\n', '\n   '))
        if r.returncode != 0:
            print('   ❌ ここで止めます。上のエラー内容を確認してください。')
            print('      「不一致」と出ていれば model.js が oasis_core.py に追随していません。')
            print('      それ以外（Traceback など）は検証スクリプト側の問題です。')
            return 1

    step(5, '構文チェック')
    for f, label in [('autopilot.bundle.js', 'バンドル')]:
        p = os.path.join(HERE, f)
        try:
            r = subprocess.run(['node', '--check', p], capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
            if r.returncode == 0:
                print(f'   ✅ {label} 構文OK')
            else:
                print(f'   ❌ {label}: {r.stderr.strip()[:200]}')
                ok = False
        except FileNotFoundError:
            print('   ⚠ node が無いので構文チェックを飛ばしました')
            break
    print('\n' + '=' * 64)
    if ok:
        print('✅ 完了。次の3つを GitHub に push してください:')
        print('   model.json / autopilot.bundle.js / oasis_autopilot_setup.html')
    return 0 if ok else 1


def _write_setup_page(combined, mod):
    """build_setup_page の定数を使って setup ページを組み立てる。"""
    import html as H
    e = (lambda x: H.escape(x, quote=True))
    diag, loader = mod.DIAG, mod.LOADER
    page = io.open(os.path.join(HERE, 'oasis_autopilot_setup.html'),
                   encoding='utf-8').read()
    # 全部入りの textarea だけ差し替える（他の説明文はそのまま活かす）
    import re
    page, n = re.subn(r'(<textarea id="s3" readonly>javascript:)[\s\S]*?(</textarea>)',
                      lambda m: m.group(1) + H.escape(combined) + m.group(2),
                      page, count=1)
    if n != 1:
        raise RuntimeError('setup ページの textarea を差し替えられませんでした')
    # ローダー（href と s2 の textarea）も差し替える。jsDelivr 単独から
    # raw → raw.githack → jsDelivr の3段に変えたので、古いページに残っていると
    # push 直後に12時間ぶん古いバンドルを掴む（bm.js で実際に起きた）。
    page, n2 = re.subn(r'href="javascript:[^"]*autopilot\.bundle\.js[^"]*"',
                       lambda m: 'href="' + e(loader) + '"', page)
    page, n3 = re.subn(r'(<textarea id="s2" readonly>)[\s\S]*?(</textarea>)',
                       lambda m: m.group(1) + H.escape(loader) + m.group(2), page, count=1)
    if not (n2 and n3):
        raise RuntimeError('setup ページのローダーを差し替えられませんでした')
    io.open(os.path.join(HERE, 'oasis_autopilot_setup.html'), 'w',
            encoding='utf-8').write(page)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'logg'))
