# -*- coding: utf-8 -*-
"""evaluate.py — 「実測重み」と「スタミナ罰則」を採用すべきか、train/test で判定する。

    python evaluate.py races.jsonl

レースを訓練用と検証用に半分ずつ分け、**訓練側だけ**で重みと罰則を決めて、
**検証側**で着順の当たり具合を測る。同じデータで係数を決めて同じデータで
評価すると必ず良く見えるので、それを避ける。

比較するモデル:
  A. 現行定数          INTERNAL_PHASE_WEIGHTS × INTERNAL_DIST_BALANCE
  B. 実測重み          timeline の rating から最小二乗
  C. 実測重み ＋ スタミナ不足の罰則
指標: レース内スピアマン（順位相関）と 1着的中率。
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402
from harvest_results import load_races  # noqa: E402

SPEC = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))
PHASES = ['序盤', '中盤', '終盤']
PH_EN = {'early': '序盤', 'middle': '中盤', 'late': '終盤'}



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
    """旧スコア式のレースは捨てる。混ぜると係数が汚れる。"""
    races, n_old = [], 0
    # ステータスを使うので、レース後日に採取した行は捨てる（値が「今」に化けている）
    for r in load_races(path, need_stats=True):
        if not _is_current(r):
            n_old += 1
            continue
        hs = r['horses']
        same = oc.same_species_flags([h['name'] for h in hs],
                                     [h.get('adult_key') for h in hs])
        keep = []
        for i, h in enumerate(hs):
            pas = tuple(x for x in (oc.passive_from_code(h.get('passive_skill')),
                                    oc.passive_from_code(h.get('passive_skill_2'))) if x)
            e = oc.effective_stats(h['speed'], h['power'], h['stamina'], pas,
                                   r['distance'], r['surface'], SPEC,
                                   {'same_species': same[i]})
            v = np.array([e['speed'], e['power'], e['stamina']])
            tl = h.get('timeline') or []
            seen = {}
            for t in tl:
                ph = PH_EN.get(t.get('phase'), t.get('phase'))
                if ph and t.get('rating') and ph not in seen:
                    seen[ph] = float(t['rating'])
            costs = [t.get('stamina_cost') for t in tl if t.get('stamina_cost')]
            # 必要スタミナ。これが無いと score_race の罰則側が KeyError で落ちる。
            # 罰則を試すのは pen>0 のときだけなので、以前はレースが少なく
            # `len(rs) < 5` で全距離スキップされ、この経路に入っていなかった。
            need = oc.stamina_budget(e, r['distance'])[0]
            keep.append(dict(eff=v, rank=h.get('rank'), ratings=seen,
                             cost=costs[0] if costs else None, n_seg=len(costs),
                             need=need, stamina0=np.floor(v[2])))
        if len(keep) >= 5 and all(k['rank'] for k in keep):
            races.append(dict(sid=r['schedule_id'], dist=r['distance'], horses=keep))
    if n_old:
        print(f'除外: 旧スコア式（simulation_version≠{SIM_VERSION}）のレース {n_old}件')
    return races


def spearman(pred, rank):
    if len(pred) < 4:
        return np.nan
    a = np.argsort(np.argsort(-np.asarray(pred, float))).astype(float)
    b = np.argsort(np.argsort(np.asarray(rank, float))).astype(float)
    if a.std() == 0 or b.std() == 0:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


def current_w(dist, phase):
    bal = oc.INTERNAL_DIST_BALANCE.get(dist, [1, 1, 1])
    p = oc.INTERNAL_PHASE_WEIGHTS[phase]
    return np.array([p[i] * bal[i] for i in range(3)])


def fit_weights(races):
    W = {}
    for d in set(r['dist'] for r in races):
        for ph in PHASES:
            A, y = [], []
            for r in races:
                if r['dist'] != d:
                    continue
                for h in r['horses']:
                    if ph in h['ratings']:
                        A.append(h['eff']); y.append(h['ratings'][ph])
            if len(A) >= 30:
                W[(d, ph)] = np.linalg.lstsq(np.array(A), np.array(y), rcond=None)[0]
    return W


def score_race(r, W, penalty):
    """予測スコア = 3区間のレート合計 − 罰則 × スタミナ不足。"""
    out = []
    for h in r['horses']:
        tot = 0.0
        for ph in PHASES:
            w = W.get((r['dist'], ph))
            if w is None:
                return None
            tot += float(h['eff'] @ w)
        short = 0.0
        if penalty and h['need']:
            short = max(0.0, h['need'] - h['stamina0'])
        out.append(tot - penalty * short)
    return out


def main(path='races.jsonl', seed=0):
    races = load(path)
    if not races:
        print(f'❌ {path} を読めません')
        return 1
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(races))
    half = len(races) // 2
    train = [races[i] for i in idx[:half]]
    test = [races[i] for i in idx[half:]]
    print(f'レース {len(races)}  → 訓練 {len(train)} / 検証 {len(test)}')

    W_fit = fit_weights(train)
    W_cur = {(d, ph): current_w(d, ph) for d in oc.DIST_LIST for ph in PHASES}

    # 罰則は訓練側だけで決める
    print('\n■ 罰則係数を訓練側で決める（距離ごと）')
    best_pen = {}
    for d in oc.DIST_LIST:
        rs = [r for r in train if r['dist'] == d]
        if len(rs) < 5:
            continue
        best, bs = 0.0, -9
        for pen in np.arange(0, 12.01, 0.25):
            ss = [spearman(score_race(r, W_fit, pen), [h['rank'] for h in r['horses']])
                  for r in rs]
            ss = [s for s in ss if s == s]
            if ss and np.mean(ss) > bs:
                bs, best = float(np.mean(ss)), float(pen)
        best_pen[d] = best
        print(f'  {d:<6} 罰則 {best:>5.2f}  （訓練 {len(rs)}レース）')

    print('\n■ 検証側での成績（訓練に使っていないレース）')
    print(f'{"距離":<6}{"レース":>5}{"A 現行":>10}{"B 実測重み":>12}{"C B＋罰則":>12}{"1着的中 A→C":>14}')
    agg = defaultdict(list)
    for d in oc.DIST_LIST:
        rs = [r for r in test if r['dist'] == d]
        if not rs:
            continue
        res = {}
        for name, W, pen in (('A', W_cur, 0.0), ('B', W_fit, 0.0),
                             ('C', W_fit, best_pen.get(d, 0.0))):
            ss, t1 = [], []
            for r in rs:
                sc = score_race(r, W, pen)
                if sc is None:
                    continue
                rk = [h['rank'] for h in r['horses']]
                s = spearman(sc, rk)
                if s == s:
                    ss.append(s)
                t1.append(int(rk[int(np.argmax(sc))] == 1))
            res[name] = (float(np.mean(ss)) if ss else np.nan,
                         float(np.mean(t1)) if t1 else np.nan)
            agg[name].append((res[name][0], len(rs)))
        print(f'{d:<6}{len(rs):>5}{res["A"][0]:>10.3f}{res["B"][0]:>12.3f}{res["C"][0]:>12.3f}'
              f'{res["A"][1]*100:>8.0f}%→{res["C"][1]*100:.0f}%')
    print('\n  全体（レース数で加重）:')
    for name, label in (('A', '現行定数'), ('B', '実測重み'), ('C', '実測重み＋罰則')):
        v = [x for x in agg[name] if x[0] == x[0]]
        if v:
            m = sum(a * b for a, b in v) / sum(b for _, b in v)
            print(f'    {label:<16} スピアマン {m:.3f}')
    print('\n  C が A を明確に上回るなら採用、変わらないなら見送り。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'races.jsonl'))
