# -*- coding: utf-8 -*-
"""build_trainer.py — 育成プランナー（単体HTML）を作る。

    python build_trainer.py            # model.json + races.jsonl → trainer.html

やること:
  1. model.json（学習済み係数）と model.js（予測ロジック）を焼き込む
  2. races.jsonl から「基準となる相手10頭」を切り出して焼き込む
     ※ どの距離でも**同じ相手**にする。距離ごとに実在のレースを使うと、
       たまたま相手が弱かった距離が有利に見えて、距離の比較にならない。
  3. 単体HTMLとして書き出す（外部参照なし・オフラインで動く）

**再学習したらこれも回し直してください。** 係数が古いままになります。
"""
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GROWTH_MEAN = 3.0          # 1〜5 の一様乱数の期待値
N_TRAIN = 30               # 特訓の回数（ゲーム仕様）


def reference_field(path, since='2026-08-26', n=10):
    """最近の出走馬を強さ順に並べ、分位から n 頭とる。

    実在の1レースをそのまま使わないのは、頭数も相手の強さもレースごとに
    バラバラで、距離の比較が相手の運に左右されてしまうため。
    """
    import oasis_core as oc
    from harvest_results import load_races
    import numpy as np
    seen = {}
    for r in load_races(path, need_stats=True, quiet=True):
        d = str(r.get('race_date') or '').replace('/', '-')
        if d < since:
            continue
        for h in (r.get('horses') or []):
            key = (h['name'], h['speed'], h['power'], h['stamina'])
            seen[key] = dict(
                name=h['name'], speed=h['speed'], power=h['power'], stamina=h['stamina'],
                condition=h.get('condition') or '普通',
                passives=[x for x in (oc.passive_from_code(h.get('passive_skill')),
                                      oc.passive_from_code(h.get('passive_skill_2'))) if x])
    u = sorted(seen.values(), key=lambda h: h['speed'] + h['power'] + h['stamina'])
    if len(u) < n:
        return u
    idx = [int(round(q * (len(u) - 1))) for q in np.linspace(0.10, 0.95, n)]
    return [u[i] for i in idx]


def main():
    model = json.load(io.open(os.path.join(HERE, 'model.json'), encoding='utf-8'))
    model_js = io.open(os.path.join(HERE, 'bookmarklets', 'src', 'model.js'),
                       encoding='utf-8').read()
    field = reference_field(os.path.join(HERE, 'races.jsonl'))
    print(f'基準フィールド {len(field)}頭 / 学習 {model["n_races"]}レース'
          f' (core {model["core_version"]}, {model["trained_at"]})')

    tpl = io.open(os.path.join(HERE, 'trainer_template.html'), encoding='utf-8').read()
    html = (tpl
            .replace('/*__MODEL__*/', json.dumps(model, ensure_ascii=False))
            .replace('/*__FIELD__*/', json.dumps(field, ensure_ascii=False))
            .replace('/*__MODELJS__*/', model_js)
            .replace('__BUILT__', model['trained_at'])
            .replace('__NRACES__', str(model['n_races']))
            .replace('__GROWTH__', str(GROWTH_MEAN))
            .replace('__NTRAIN__', str(N_TRAIN)))
    out = os.path.join(HERE, 'trainer.html')
    io.open(out, 'w', encoding='utf-8').write(html)
    print(f'→ trainer.html  {len(html.encode("utf-8"))/1024:.0f} KB')
    print('   ⚠ モデルは焼き込みです。再学習したら回し直してください。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
