# -*- coding: utf-8 -*-
"""fit_ridge_stamina.py — スタミナ不足を「Ridgeの特徴量」として足す価値があるかを測る。

    python fit_ridge_stamina.py races.jsonl

evaluate.py は内部式ベースの予測で測ったが、実際に使っているのは Ridge回帰。
Ridge は既にスタミナの線形項を持っているので、そこに不足分を足して**本当に効くか**は
別の話。ここでは oasis_core と同じレース単位クロスバリデーションで、

  ① 現行の特徴量だけ
  ② ＋ スタミナ不足（1列）
  ③ ＋ スタミナ不足（距離ごと4列）

を比べる。②③が①を上回らなければ、この改修は入れない。

不足量はレース前に計算できる量だけで作る:
    消費/100m = clamp(c[距離] × (実効ステ・序盤重み), 下限, 上限)
    不足      = max(0, 消費 × 区間数 − floor(実効スタミナ))
c / 下限 / 上限 / 区間数 は timeline から距離ごとに実測する。
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

SPEC = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))


def phase_vec(dist, phase='序盤'):
    b = oc.INTERNAL_DIST_BALANCE.get(dist, [1.0, 1.0, 1.0])
    p = oc.INTERNAL_PHASE_WEIGHTS[phase]
    return np.array([p[i] * b[i] for i in range(3)])



SIM_VERSION = 2          # 現行のスコア式。1 = 2026/07/27 以前の旧式
def _is_current(r):
    """このレースが現行のスコア式か。simulation_version が最優先、無ければ日付。"""
    vs = {h.get('simulation_version') for h in (r.get('horses') or [])}
    vs.discard(None)
    if vs:
        return vs == {SIM_VERSION}          # 版が混在する行は捨てる
    d = str(r.get('race_date') or '').replace('/', '-')
    return d >= '2026-07-28'


def load(path):
    """races.jsonl → 1頭1行の DataFrame（oasis_core の列名に合わせる）。

    旧スコア式（simulation_version=1）のレースは**捨てる**。混ぜると係数が汚れる。
    """
    rows = []
    n_old = 0
    vers = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            hs = r.get('horses') or []
            if len(hs) < 4 or r.get('distance') not in oc.DIST_LIST:
                continue
            if not _is_current(r):
                n_old += 1
                continue
            for h in hs:
                v = h.get('simulation_version')
                if v is not None:
                    vers[v] = vers.get(v, 0) + 1
            if not all(h.get('rank') and h.get('score') is not None for h in hs):
                continue
            same = oc.same_species_flags([h['name'] for h in hs],
                                         [h.get('adult_key') for h in hs])
            for i, h in enumerate(hs):
                tl = h.get('timeline') or []
                costs = [t.get('stamina_cost') for t in tl if t.get('stamina_cost')]
                rows.append(dict(
                    race_key=str(r['schedule_id']), name=h['name'],
                    speed=h['speed'], power=h['power'], stamina=h['stamina'],
                    condition=h.get('condition') or '普通',
                    passives=tuple(x for x in (oc.passive_from_code(h.get('passive_skill')),
                                               oc.passive_from_code(h.get('passive_skill_2'))) if x),
                    dist=r['distance'], track=r['surface'],
                    rank=h['rank'], score=float(h['score']),
                    same_species=bool(same[i]),
                    _cost=costs[0] if costs else np.nan, _nseg=len(costs) or np.nan))
    df = pd.DataFrame(rows).reset_index(drop=True)
    df.attrs['n_old'] = n_old
    df.attrs['versions'] = vers
    return df


def fit_cost_law(df):
    """距離ごとに 消費 = clamp(c × base, lo, hi) の c / lo / hi / 区間数 を実測。"""
    law = {}
    for d, g in df.groupby('dist'):
        g = g[g['_cost'].notna()]
        if len(g) < 20:
            continue
        base = np.array([float(np.array([e['speed'], e['power'], e['stamina']]) @ phase_vec(d))
                         for e in (oc.effective_stats(r.speed, r.power, r.stamina, r.passives,
                                                      d, r.track, SPEC,
                                                      {'same_species': r.same_species})
                                   for r in g.itertuples())])
        cost = g['_cost'].values
        lo, hi = float(cost.min()), float(cost.max())
        mid = (cost > lo + 1e-9) & (cost < hi - 1e-9)
        if mid.sum() < 5:
            continue
        law[d] = dict(c=float(np.median(cost[mid] / base[mid])), lo=lo, hi=hi,
                      n_seg=int(np.median(g['_nseg'].values)),
                      n=len(g),
                      n_lo=int((np.abs(cost - lo) < 1e-9).sum()),
                      n_hi=int((np.abs(cost - hi) < 1e-9).sum()))
    return law


def shortfall(df, law):
    """レース前に計算できる「必要スタミナ − 持ちスタミナ」の不足分。"""
    out = np.zeros(len(df))
    for i, r in enumerate(df.itertuples()):
        L = law.get(r.dist)
        if not L:
            continue
        e = oc.effective_stats(r.speed, r.power, r.stamina, r.passives, r.dist, r.track,
                               SPEC, {'same_species': r.same_species})
        base = float(np.array([e['speed'], e['power'], e['stamina']]) @ phase_vec(r.dist))
        cost = min(max(L['c'] * base, L['lo']), L['hi'])
        out[i] = max(0.0, cost * L['n_seg'] - np.floor(e['stamina']))
    return out


def _oof(X, y, groups, alpha, seed):
    """oc._oof_predictions と同じだが、分割の乱数種を変えられるようにしたもの。"""
    folds = oc._race_folds(groups, k=5, seed=seed)
    oof = np.zeros(len(y))
    for f in range(folds.max() + 1):
        te = folds == f
        if (~te).sum() < 10 or te.sum() == 0:
            continue
        m = oc.Ridge(alpha=alpha, fit_intercept=True).fit(X[~te], y[~te])
        oof[te] = m.predict(X[te])
    return oc._center_by_race(oof, groups)


def measure(X, y, df, label, seeds=range(8)):
    """レース単位CVを**分割の乱数種を変えて何度も**回す。

    レース数が少ないと、たまたまの分割で 0.01 くらい平気で動く。
    種を変えた時のバラつき（標準偏差）より差が小さければ、それは誤差。
    """
    groups = df['race_key'].values
    rhos, t1s = [], []
    for sd in seeds:
        best = (-np.inf, None)
        for a in [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]:
            o = _oof(X, y, groups, a, sd)
            r = oc._mean_race_spearman(o, df)
            if r > best[0]:
                best = (r, o)
        rhos.append(best[0])
        t1s.append(oc._top1_accuracy(best[1], df))
    rhos, t1s = np.array(rhos), np.array(t1s)
    print(f'  {label:<26} スピアマン {rhos.mean():.4f} ±{rhos.std():.4f}'
          f'   1着的中 {t1s.mean()*100:4.1f}% ±{t1s.std()*100:.1f}')
    return rhos


def main(path='races.jsonl'):
    if not os.path.exists(path):
        print(f'❌ {path} がありません')
        return 1
    df = load(path)
    if len(df) == 0:
        print('❌ 使えるレースがありません')
        return 1
    n_race = df['race_key'].nunique()
    print(f'レース {n_race} / {len(df)}頭   ' +
          ' / '.join(f'{d} {g["race_key"].nunique()}'
                     for d, g in df.groupby('dist')))
    n_old = df.attrs.get('n_old', 0)
    print(f'  除外: 旧スコア式（simulation_version≠{SIM_VERSION}）のレース {n_old}件')
    vers = df.attrs.get('versions') or {}
    if vers:
        print('  simulation_version: ' +
              ' / '.join(f'{k} {v}頭' for k, v in sorted(vers.items(), key=lambda x: -x[1])))
        if len(vers) > 1:
            print('  ⚠ 版が混ざっています。ここが割れているなら版でも切ってください。')

    law = fit_cost_law(df)
    print('\n■ 消費法則（timeline から実測）')
    for d in oc.DIST_LIST:
        L = law.get(d)
        if not L:
            print(f'  {d:<5} データ不足')
            continue
        # 下限・上限に**複数頭が同じ値で張り付いている**なら本物のクランプ。
        # 1頭しか居ないなら「たまたまの最小/最大」で、真の上下限はもっと外側かもしれない。
        mark = lambda k: '確定' if L[k] >= 3 else ('要検証' if L[k] >= 2 else '未確定')
        print(f'  {d:<5} 区間{L["n_seg"]:>3}  係数 {L["c"]:.5f}'
              f'  消費 {L["lo"]:.3f}〜{L["hi"]:.3f}'
              f'  → 必要ST {L["lo"]*L["n_seg"]:.1f}〜{L["hi"]*L["n_seg"]:.1f}')
        print(f'        下限に{L["n_lo"]:>3}頭({mark("n_lo")}) / 上限に{L["n_hi"]:>3}頭'
              f'({mark("n_hi")}) / 全{L["n"]}頭')

    sf = shortfall(df, law)
    n_short = int((sf > 0).sum())
    print(f'\n  スタミナ不足の馬 {n_short}/{len(df)} 頭 ({n_short/len(df)*100:.1f}%)'
          f'  不足量 中央値 {np.median(sf[sf > 0]) if n_short else 0:.2f}')
    if n_short < 30:
        print('  ⚠ 不足している馬が少なすぎます。効果は測れません。')

    y = oc._center_by_race(np.log(np.clip(df['score'].values, 1e-6, None)),
                           df['race_key'].values)
    X0 = oc.build_features(df, SPEC)
    s = (sf / 10.0).reshape(-1, 1)                 # 他の線形項と桁を揃える
    X1 = np.hstack([X0, s])
    X2 = np.hstack([X0] + [s * (df['dist'] == d).values.reshape(-1, 1).astype(float)
                           for d in oc.DIST_LIST])

    print('\n■ レース単位クロスバリデーション（out-of-fold）')
    r0 = measure(X0, y, df, '① 現行の特徴量')
    r1 = measure(X1, y, df, '② ＋不足（1列）')
    r2 = measure(X2, y, df, '③ ＋不足（距離ごと4列）')

    # 同じ分割どうしで比べる（対応のある差）。分割ごとのブレは相殺される。
    for lab, r in (('②', r1), ('③', r2)):
        d = r - r0
        print(f'\n  {lab} − ①  平均 {d.mean():+.4f} ±{d.std():.4f}'
              f'   改善した分割 {int((d > 0).sum())}/{len(d)}')
        if d.mean() >= 0.010 and (d > 0).all():
            print('    → 採用して良い（全分割で改善、かつ幅も十分）。')
        elif (d > 0).all():
            print('    → 方向は一貫しているが幅が小さい。データを増やしてから再判定。')
        else:
            print('    → 見送り。分割によって符号が変わる＝誤差。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'races.jsonl'))
