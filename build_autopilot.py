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


def main(log_path=None):
    import oasis_core as oc
    import minify

    # 学習データは races.jsonl（API から毎レース自動で取っている）が既定。
    # Discord ログは状態を取り違えていて、9月分は学習を悪化させていた（oasis_core.races_jsonl_frame 参照）。
    # 引数で logg を渡せば従来どおり Discord ログから学習する。
    if log_path is None:
        log_path = 'races.jsonl' if os.path.exists(os.path.join(HERE, 'races.jsonl')) else 'logg'
    jsonl = str(log_path).lower().endswith('.jsonl')
    step(1, f'モデルを学習して model.json を書き出す（{log_path}）')
    bundle = oc.train_model(os.path.join(HERE, log_path) if jsonl else log_path,
                            train_from=oc.JSONL_TRAIN_FROM if jsonl else oc.DEFAULT_TRAIN_FROM)
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
        print('   races.jsonl（または logg/）に十分なデータが入っているか確認してください。')
        return 1

    payload = oc.export_model_json(bundle)
    print(f'\n   → model.json  学習{n_races}レース / 係数{len(payload["coef"])}個'
          f' / σ単勝 {payload["race_sigma"]:.4f} / σ3連単 {payload["tri_sigma"]:.4f}')
    # 区間シミュレータ（2026/10/07〜 bot の予想はこちら。Ridge は比較用に記録だけ）
    if jsonl:
        import race_sim
        since = oc.JSONL_TRAIN_FROM.replace('/', '-')
        raw = [json.loads(l) for l in io.open(os.path.join(HERE, log_path), encoding='utf-8') if l.strip()]
        raw = [r for r in raw if str(r.get('race_date', '')) >= since and r.get('distance') in race_sim.NSEG]
        sim = race_sim.RaceSim(n_sim=8000).fit(raw)
        payload['sim'] = sim.export()
        print(f'   → シミュレータ  学習{len(raw)}レース / 消費のぶれ σ{sim.cost_sd:.3f}')
        # 見本は直近4レース＋区間効果（序盤/中盤/終盤・残り300m・残スタミナ・先頭・馬場）を持つ馬がいるレース
        want = race_sim.LOWST_LABELS | race_sim.LEAD_LABELS | {
            '終焉加速', '幻界終走', '時界超越', '末脚', '中盤加速', 'ロケットスタート', '二の脚', '芝啜り', '泥啜り'}
        seen, extra = set(), []
        for r in reversed(raw[:-4]):
            labs = {(it or {}).get('effect_label') for h in r['horses'] for it in (h.get('equipment'), h.get('charm'))
                    if isinstance(it, dict)} & (want - seen)
            if labs:
                seen |= labs
                extra.append(r)
        _write_sim_fixture(sim, raw[-4:] + extra[:12], race_sim)
    io.open(os.path.join(HERE, 'model.json'), 'w', encoding='utf-8').write(
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')))

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
    unverified = False
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

    # 金に直結する autopilot のブロック（オッズの取り損ね・CO・2・3着の順番補正）と、
    # 区間シミュレータの Python↔JS 一致を実際に動かす
    for tp in (os.path.join(HERE, 'test_autopilot.js'), os.path.join(HERE, 'test_sim.js')):
        if not os.path.exists(tp):
            continue
        try:
            r = subprocess.run(['node', tp], capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
            last = (r.stdout.strip().splitlines() or [''])[-1]
            if r.returncode != 0:
                print(f'   ❌ {os.path.basename(tp)} に失敗しました。ここで止めます。')
                print('   ' + (r.stderr or r.stdout).strip().replace('\n', '\n   ')[:800])
                return 1
            print('   ' + last)
        except FileNotFoundError:
            unverified = True
            break

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
            unverified = True
            break
    print('\n' + '=' * 64)
    if ok:
        print('✅ 完了。次の3つを GitHub に push してください:')
        print('   model.json / autopilot.bundle.js / oasis_autopilot_setup.html')
        if unverified:
            # node が無いと Python↔JS の一致も JS の構文も確かめていない。✅ だけ見て安心しないこと。
            print('   ⚠ ただし node が無いので、JS の構文と Python↔JS の一致は【未検証】です。'
                  ' https://nodejs.org から入れると自動で検証します。')
    return 0 if ok else 1


def _write_sim_fixture(sim, races, race_sim):
    """test_sim.js 用の見本: 直近数レースの Python 側の入力と勝率（4万回）。"""
    import oasis_core as oc
    out = []
    for r in races:
        hs = [h for h in r['horses'] if h.get('speed') is not None]
        r2 = dict(r, horses=hs)
        sim.n_sim = 40000
        win, _, _ = sim.predict(r2, seed=7)
        js_h = [{'name': h.get('name', ''), 'species': h.get('adult_key'),
                 'speed': h['speed'], 'power': h['power'], 'stamina': h['stamina'],
                 'passives': [oc.PASSIVE_CODE_MAP[c] for c in (h.get('passive_skill'), h.get('passive_skill_2'))
                              if c and c != 'none' and c in oc.PASSIVE_CODE_MAP],
                 'equipment': h.get('equipment'), 'charm': h.get('charm')} for h in hs]
        out.append({'sid': r.get('schedule_id'), 'dist': r['distance'], 'track': r.get('surface'),
                    'horses': js_h, 'inputs': sim.inputs(r2), 'win': [float(x) for x in win]})
    sim.n_sim = 8000
    io.open(os.path.join(HERE, '_parity_sim.json'), 'w', encoding='utf-8').write(
        json.dumps(out, ensure_ascii=False))


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
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
