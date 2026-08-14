# -*- coding: utf-8 -*-
"""fit_stamina.py — 採取した races.jsonl から「スタミナ切れ」の法則を推定する。

    python fit_stamina.py races.jsonl

やること:
  ① レート式の定数 K を推定   rating = K × Σ(区間重み × 距離バランス × 実効ステータス)
  ② 距離ごとの区間数と、消費/100m の係数・上下限を推定
  ③ 「完走に必要なスタミナ」と実際の残量が合うか検証
  ④ スタミナ不足の罰則係数を距離ごとに推定（着順の順位相関が最大になる値）
  ⑤ 現行モデル（線形加算）と比較

実測1レースでは罰則6付近で順位相関1.000だったが、n=1では過剰適合と区別できない。
このスクリプトで**複数レースにわたって**同じ値が出るかを確かめるのが目的。
"""
import json
import sys
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

SPEC = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))
PH = oc.INTERNAL_PHASE_WEIGHTS
BAL = oc.INTERNAL_DIST_BALANCE


def eff(h, dist, track, same):
    pas = tuple(x for x in (oc.passive_from_code(h.get('passive_skill')),
                            oc.passive_from_code(h.get('passive_skill_2'))) if x)
    e = oc.effective_stats(h['speed'], h['power'], h['stamina'], pas, dist, track,
                           SPEC, {'same_species': same})
    return np.array([e['speed'], e['power'], e['stamina']]), pas


def phase_vec(dist, phase):
    b = BAL.get(dist, [1, 1, 1])
    p = PH[phase]
    return np.array([p[i] * b[i] for i in range(3)])


def load(path):
    races = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                races.append(json.loads(line))
            except Exception:
                pass
    return races


def spearman(a, b):
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if len(a) < 3:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def main(path='races.jsonl'):
    races = load(path)
    if not races:
        print(f'❌ {path} を読めません')
        return 1
    print(f'読み込み: {len(races)}レース / {sum(r["n_field"] for r in races)}頭')
    dists = {}
    for r in races:
        dists.setdefault(r['distance'], []).append(r)
    print('  距離別: ' + ' / '.join(f'{d} {len(v)}' for d, v in sorted(dists.items())))

    # ---------- ① レート定数 K ----------
    print('\n■ ① レート式の定数 K')
    ks = []
    for r in races:
        names = [h['name'] for h in r['horses']]
        same = oc.same_species_flags(names, [h.get('adult_key') for h in r['horses']])
        w = phase_vec(r['distance'], '序盤')
        for i, h in enumerate(r['horses']):
            tl = h.get('timeline') or []
            rt = next((t.get('rating') for t in tl if t.get('rating')), None)
            if not rt:
                continue
            v, _ = eff(h, r['distance'], r['surface'], same[i])
            base = float(v @ w)
            if base > 0:
                ks.append(rt / base)
    if ks:
        ks = np.array(ks)
        print(f'  K = {np.median(ks):.4f}  (平均 {ks.mean():.4f} / std {ks.std():.4f} / n={len(ks)})')
        K = float(np.median(ks))
    else:
        K = 1.1861
        print(f'  timeline に rating が無いため既定値 {K} を使用')

    # ---------- ② 消費/100m ----------
    print('\n■ ② 消費/100m と区間数')
    for d, rs in sorted(dists.items()):
        segs, ratios, costs = [], [], []
        for r in rs:
            for h in r['horses']:
                tl = h.get('timeline') or []
                cs = [t.get('stamina_cost') for t in tl if t.get('stamina_cost')]
                rt = next((t.get('rating') for t in tl if t.get('rating')), None)
                if not cs or not rt:
                    continue
                segs.append(len(cs))
                costs.append(cs[0])
                ratios.append(cs[0] / rt)
        if not costs:
            print(f'  {d}: timeline なし')
            continue
        costs = np.array(costs); ratios = np.array(ratios); segs = np.array(segs)
        lo, hi = costs.min(), costs.max()
        mid = ratios[(costs > lo + 1e-9) & (costs < hi - 1e-9)]
        n_seg = int(np.median(segs))
        print(f'  {d:<5} 区間 {n_seg:>2}  消費 {lo:.3f}〜{hi:.3f}'
              f'  係数 {np.median(mid) if len(mid) else float("nan"):.5f}'
              f'  → 必要ST {lo*n_seg:.1f}〜{hi*n_seg:.1f}')

    # ---------- ③ 必要量と実残量の整合 ----------
    print('\n■ ③ 「持ち − 必要」が API の stamina_after と一致するか')
    errs = []
    for r in races:
        for h in r['horses']:
            tl = h.get('timeline') or []
            cs = [t.get('stamina_cost') for t in tl if t.get('stamina_cost')]
            s0 = next((t.get('stamina') for t in tl), None)
            if not cs or s0 is None or h.get('stamina_after') is None:
                continue
            errs.append((s0 - cs[0] * len(cs)) - h['stamina_after'])
    if errs:
        e = np.array(errs)
        print(f'  誤差 中央値 {np.median(e):+.3f} / |誤差|<0.5 の割合 '
              f'{(np.abs(e) < 0.5).mean()*100:.1f}%  (n={len(e)})')

    # ---------- ④⑤ 罰則係数の推定 ----------
    print('\n■ ④ スタミナ不足の罰則係数（順位相関が最大になる値）')
    print(f'{"距離":<6}{"レース":>6}{"罰則0(現行)":>12}{"最良罰則":>10}{"その時の相関":>12}')
    for d, rs in sorted(dists.items()):
        cand = np.arange(0, 25.1, 0.5)
        scores = {c: [] for c in cand}
        for r in rs:
            names = [h['name'] for h in r['horses']]
            same = oc.same_species_flags(names, [h.get('adult_key') for h in r['horses']])
            w = phase_vec(r['distance'], '序盤')
            rows = []
            for i, h in enumerate(r['horses']):
                tl = h.get('timeline') or []
                cs = [t.get('stamina_cost') for t in tl if t.get('stamina_cost')]
                if not cs:
                    continue
                v, _ = eff(h, r['distance'], r['surface'], same[i])
                rating = K * float(v @ w)
                need = cs[0] * len(cs)
                short = max(0.0, need - np.floor(v[2]))
                rows.append((rating, short, h['rank']))
            if len(rows) < 4:
                continue
            rk = [x[2] for x in rows]
            for c in cand:
                sc = [-(x[0] - c * x[1]) for x in rows]
                s = spearman(sc, rk)
                if s == s:
                    scores[c].append(s)
        if not scores[0.0]:
            continue
        means = {c: float(np.mean(v)) for c, v in scores.items() if v}
        best = max(means, key=means.get)
        print(f'{d:<6}{len(rs):>6}{means[0.0]:>12.3f}{best:>10.1f}{means[best]:>12.3f}')
    print('\n  罰則0が現行モデル相当。差が大きいほど「スタミナ切れ」を入れる価値がある。')
    print('  距離ごとに同じような値に落ち着けば、過剰適合ではなく本物の効果。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'races.jsonl'))
