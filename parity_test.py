# -*- coding: utf-8 -*-
"""parity_test.py — Python(oasis_core) と JS(model.js) が同じ予測を出すか検証する。

    python parity_test.py          # node が要る

特徴量を1つ足すたびに、Python 側だけ直して JS 側を忘れる事故が起きる。
それを機械的に止めるためのテスト。乱数の係数でモデルを作り、同じ馬を
両方に食わせて予測値が一致するかを見る。
"""
import json
import os
import subprocess
import sys

import numpy as np
from sklearn.linear_model import Ridge

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

TOL = 1e-9


def main():
    spec = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))
    names = oc.feature_names(spec)
    rng = np.random.default_rng(0)
    # 適当な係数のモデル（学習済みログが無くても検証できるようにする）
    X = rng.normal(size=(400, len(names)))
    m = Ridge(alpha=1.0).fit(X, rng.normal(size=400))
    m.coef_ = rng.normal(scale=0.3, size=len(names))
    bundle = {'ok': True, 'model': m, 'feature_names': names, 'spec': spec,
              'race_sigma': 0.2, 'tri_sigma': 0.2, 'n_races': 0}
    payload = oc.export_model_json(bundle, os.path.join(HERE, '_parity_model.json'))

    pas = [p for p in oc.PASSIVE_NAMES]
    cases = []
    for i in range(60):
        dist = oc.DIST_LIST[i % 4]
        track = oc.TRACK_LIST[i % 2]
        horses = []
        for j in range(6):
            k = rng.integers(0, len(pas), size=2)
            horses.append({
                'name': f'h{j}', 'species': f'sp{j}',
                'speed': int(rng.integers(30, 160)), 'power': int(rng.integers(30, 160)),
                'stamina': int(rng.integers(20, 120)),
                'condition': ['好調', '普通', '不調'][int(rng.integers(0, 3))],
                'passives': [pas[k[0]], pas[k[1]]],
            })
        base = oc.predict_base(bundle, [dict(h, passives=tuple(h['passives']))
                                        for h in horses], dist, track)
        cases.append({'dist': dist, 'track': track, 'horses': horses,
                      'base': [float(x) for x in base]})
    with open(os.path.join(HERE, '_parity_cases.json'), 'w', encoding='utf-8') as f:
        json.dump(cases, f, ensure_ascii=False)

    r = subprocess.run(['node', os.path.join(HERE, 'parity_test.js')],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    for f in ('_parity_model.json', '_parity_cases.json'):
        try:
            os.remove(os.path.join(HERE, f))
        except OSError:
            pass
    print(f'  特徴量 {len(names)}列 / スタミナ不足 '
          f'{"あり" if "スタミナ不足" in names else "★なし★"}')
    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
