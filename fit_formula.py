# -*- coding: utf-8 -*-
"""fit_formula.py — races.jsonl の timeline から内部式そのものを最小二乗で確定する。

    python fit_formula.py races.jsonl

timeline には各区間の **rating（ゲームが実際に使った値）** が入っている。
つまり「答え」が手元にあるので、距離×区間ごとの重みを推定ではなく実測で決められる。

  rating[距離][区間] = a·実効SP + b·実効PW + c·実効ST   （× コンディション倍率）

やること:
  ① 距離×区間ごとに (a,b,c) を最小二乗で推定し、現行定数と比較
  ② コンディション（好調/普通/不調）の倍率を推定
  ③ 確定した式で rating を再現できるか検証（残差）
  ④ 正しい rating で消費係数を測り直す
  ⑤ 新しい定数を oasis_core.py に貼れる形で出力
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


def eff_stats(h, dist, track, same):
    pas = tuple(x for x in (oc.passive_from_code(h.get('passive_skill')),
                            oc.passive_from_code(h.get('passive_skill_2'))) if x)
    e = oc.effective_stats(h['speed'], h['power'], h['stamina'], pas, dist, track,
                           SPEC, {'same_species': same})
    return np.array([e['speed'], e['power'], e['stamina']])


def collect(path):
    """[(距離, 区間, コンディション, 実効[SP,PW,ST], rating)] を集める。"""
    rows = []
    # ステータスを使うので、レース後日に採取した行は捨てる（値が「今」に化けている）
    for r in load_races(path, need_stats=True, quiet=True):
            hs = r['horses']
            same = oc.same_species_flags([h['name'] for h in hs],
                                         [h.get('adult_key') for h in hs])
            for i, h in enumerate(hs):
                v = eff_stats(h, r['distance'], r['surface'], same[i])
                seen = {}
                for t in (h.get('timeline') or []):
                    ph = PH_EN.get(t.get('phase'), t.get('phase'))
                    rt = t.get('rating')
                    if ph and rt and ph not in seen:
                        seen[ph] = float(rt)
                for ph, rt in seen.items():
                    rows.append((r['distance'], ph, h.get('condition') or '普通', v, rt))
    return rows


def diagnose(path):
    """式が合わない原因を切り分ける。

    (a) 1頭の残差比が区間をまたいで一定なら、それは**レースごとの乱数**であって
        モデル誤差ではない（除去できる）。
    (b) 区間限定パッシブ持ちを外すと残差が下がるなら、稼働率で薄めている扱いが原因。
    (c) 説明変数の相関が強いと最小二乗の係数は不安定になる（条件数で確認）。
    """
    import json as _j
    print('\n■ 診断')
    per_horse = defaultdict(dict)      # (race,horse) -> {phase: (vec, rating, passives, cond)}
    # ステータスを使うので、レース後日に採取した行は捨てる（値が「今」に化けている）
    for r in load_races(path, need_stats=True, quiet=True):
            hs = r['horses']
            same = oc.same_species_flags([h['name'] for h in hs],
                                         [h.get('adult_key') for h in hs])
            for i, h in enumerate(hs):
                v = eff_stats(h, r['distance'], r['surface'], same[i])
                pas = tuple(x for x in (oc.passive_from_code(h.get('passive_skill')),
                                        oc.passive_from_code(h.get('passive_skill_2'))) if x)
                seen = {}
                for t in (h.get('timeline') or []):
                    ph = PH_EN.get(t.get('phase'), t.get('phase'))
                    if ph and t.get('rating') and ph not in seen:
                        seen[ph] = float(t['rating'])
                for ph, rt in seen.items():
                    per_horse[(r['schedule_id'], h.get('pet_id'), r['distance'])][ph] = \
                        (v, rt, pas, h.get('condition') or '普通')

    # (a) 区間をまたいだ残差比の一貫性
    keys = [k for k, d in per_horse.items() if len(d) == 3]
    print(f'  (a) 3区間そろっている馬: {len(keys)}頭')
    W = {}
    for d in oc.DIST_LIST:
        for ph in PHASES:
            A, y = [], []
            for k in keys:
                if k[2] != d:
                    continue
                v, rt, *_ = per_horse[k][ph]
                A.append(v); y.append(rt)
            if len(A) >= 20:
                W[(d, ph)] = np.linalg.lstsq(np.array(A), np.array(y), rcond=None)[0]
    spreads = []
    for k in keys:
        rr = []
        for ph in PHASES:
            w = W.get((k[2], ph))
            if w is None:
                break
            v, rt, *_ = per_horse[k][ph]
            p = float(v @ w)
            if p > 0:
                rr.append(rt / p)
        if len(rr) == 3:
            spreads.append(max(rr) - min(rr))
    if spreads:
        sp = np.array(spreads)
        print(f'      1頭の中での残差比のばらつき: 中央値 {np.median(sp)*100:.2f}%'
              f' / 90%点 {np.percentile(sp,90)*100:.2f}%')
        print('      → 小さいほど「区間によらない per-race 乱数」＝モデル誤差ではない')

    # (b) 区間限定パッシブを持つ馬を外す
    def is_clean(pas):
        for p in pas:
            sc = (SPEC.get(p) or {}).get('scope', 'always')
            if sc not in ('always', 'aptitude', 'same_species', 'variance'):
                return False
        return True
    print('  (b) 区間限定・状況限定パッシブ持ちを除外した場合の残差')
    print(f'      {"距離":<6}{"区間":<5}{"全馬":>9}{"除外後":>9}{"頭数":>8}')
    for d in oc.DIST_LIST:
        for ph in PHASES:
            allr, cln, ncl = [], [], 0
            A0, y0, A1, y1 = [], [], [], []
            for k in keys:
                if k[2] != d or ph not in per_horse[k]:
                    continue
                v, rt, pas, cond = per_horse[k][ph]
                A0.append(v); y0.append(rt)
                if is_clean(pas):
                    A1.append(v); y1.append(rt); ncl += 1
            if len(A0) < 20 or len(A1) < 20:
                continue
            for A, y, box in ((A0, y0, allr), (A1, y1, cln)):
                A = np.array(A); y = np.array(y)
                w = np.linalg.lstsq(A, y, rcond=None)[0]
                box.append(float(np.median(np.abs(A @ w - y) / np.maximum(y, 1e-9))))
            print(f'      {d:<6}{ph:<5}{allr[0]*100:>8.2f}%{cln[0]*100:>8.2f}%{ncl:>8}')

    # (c) 共線性
    print('  (c) 説明変数の条件数（大きいと係数が不安定）')
    for d in oc.DIST_LIST:
        A = np.array([per_horse[k]['序盤'][0] for k in keys
                      if k[2] == d and '序盤' in per_horse[k]])
        if len(A) >= 20:
            print(f'      {d:<6} 条件数 {np.linalg.cond(A):>8.1f}   n={len(A)}')

    # (d) コンディションは rating に効いているか
    print('  (d) コンディション別の残差比（1.0からずれるなら効いている）')
    for cond in ('好調', '普通', '不調'):
        rr = []
        for k in keys:
            for ph in PHASES:
                w = W.get((k[2], ph))
                if w is None or ph not in per_horse[k]:
                    continue
                v, rt, pas, c = per_horse[k][ph]
                if c != cond:
                    continue
                p = float(v @ w)
                if p > 0:
                    rr.append(rt / p)
        if rr:
            rr = np.array(rr)
            print(f'      {cond:<3} 中央値 {np.median(rr):.4f}  '
                  f'四分位 {np.percentile(rr,25):.4f}〜{np.percentile(rr,75):.4f}  n={len(rr)}')


def main(path='races.jsonl'):
    rows = collect(path)
    if not rows:
        print(f'❌ {path} から timeline を読めません')
        return 1
    print(f'標本: {len(rows)} 件（頭×区間）')

    # ---------- ② コンディション倍率（先に推定して割り戻す）----------
    # 同一距離・区間の中で、素の重み付き和に対する rating の比を条件別に見る。
    print('\n■ ② コンディション倍率')
    base_w = {}
    for d in oc.DIST_LIST:
        for ph in PHASES:
            sub = [r for r in rows if r[0] == d and r[1] == ph]
            if len(sub) < 20:
                continue
            A = np.array([r[3] for r in sub]); y = np.array([r[4] for r in sub])
            base_w[(d, ph)] = np.linalg.lstsq(A, y, rcond=None)[0]
    ratios = defaultdict(list)
    for d, ph, cond, v, rt in rows:
        w = base_w.get((d, ph))
        if w is None:
            continue
        pred = float(v @ w)
        if pred > 0:
            ratios[cond].append(rt / pred)
    cond_mult = {}
    for c in ('好調', '普通', '不調'):
        if ratios.get(c):
            cond_mult[c] = float(np.median(ratios[c]))
            print(f'  {c:<3} 倍率 {cond_mult[c]:.4f}  (n={len(ratios[c])})')
    norm = cond_mult.get('普通', 1.0)
    if norm:
        for c in cond_mult:
            cond_mult[c] /= norm
        print('  普通=1.000 に正規化 → ' + ' / '.join(f'{c} {m:.3f}' for c, m in cond_mult.items()))

    # ---------- ① 距離×区間の重み ----------
    print('\n■ ① 距離×区間ごとの重み（コンディション補正後・最小二乗）')
    print(f'{"距離":<6}{"区間":<5}{"n":>5}{"SP":>9}{"PW":>9}{"ST":>9}{"残差%":>8}   現行の想定値')
    fitted = {}
    for d in oc.DIST_LIST:
        bal = oc.INTERNAL_DIST_BALANCE.get(d, [1, 1, 1])
        for ph in PHASES:
            sub = [r for r in rows if r[0] == d and r[1] == ph]
            if len(sub) < 20:
                continue
            A = np.array([r[3] for r in sub])
            y = np.array([r[4] / cond_mult.get(r[2], 1.0) for r in sub])
            w, *_ = np.linalg.lstsq(A, y, rcond=None)
            resid = np.abs(A @ w - y) / np.maximum(y, 1e-9)
            fitted[(d, ph)] = w
            cur = [oc.INTERNAL_PHASE_WEIGHTS[ph][k] * bal[k] for k in range(3)]
            print(f'{d:<6}{ph:<5}{len(sub):>5}{w[0]:>9.4f}{w[1]:>9.4f}{w[2]:>9.4f}'
                  f'{np.median(resid)*100:>7.2f}%   [{cur[0]:.2f},{cur[1]:.2f},{cur[2]:.2f}]')

    # ---------- ③ 距離バランスに分解 ----------
    print('\n■ ③ 区間重みと距離バランスへの分解（マイルを基準=1に正規化）')
    if all((d, ph) in fitted for d in oc.DIST_LIST for ph in PHASES):
        mile = np.array([fitted[('マイル', ph)] for ph in PHASES])
        print('  区間重み（マイル実測・合計1に正規化）:')
        for i, ph in enumerate(PHASES):
            s = mile[i] / mile[i].sum()
            print(f'    {ph}: [{s[0]:.3f}, {s[1]:.3f}, {s[2]:.3f}]'
                  f'   現行 {oc.INTERNAL_PHASE_WEIGHTS[ph]}')
        print('  距離バランス（マイル=1）:')
        for d in oc.DIST_LIST:
            got = np.array([fitted[(d, ph)] for ph in PHASES])
            bal = (got.sum(axis=0) / mile.sum(axis=0))
            print(f'    {d:<5} [{bal[0]:.3f}, {bal[1]:.3f}, {bal[2]:.3f}]'
                  f'   現行 {oc.INTERNAL_DIST_BALANCE[d]}')

    # ---------- ④ 正しい rating で消費係数を測り直す ----------
    print('\n■ ④ 確定した式での消費係数（消費 = 係数 × 序盤rating）')
    per = defaultdict(list)
    # ステータスを使うので、レース後日に採取した行は捨てる（値が「今」に化けている）
    for r in load_races(path, need_stats=True, quiet=True):
            for h in r['horses']:
                tl = h.get('timeline') or []
                cs = [t.get('stamina_cost') for t in tl if t.get('stamina_cost')]
                rt = next((t.get('rating') for t in tl if t.get('rating')), None)
                if cs and rt:
                    per[r['distance']].append((cs[0], float(rt), len(cs)))
    for d in oc.DIST_LIST:
        v = per.get(d)
        if not v:
            continue
        c = np.array([x[0] for x in v]); rt = np.array([x[1] for x in v])
        seg = int(np.median([x[2] for x in v]))
        lo, hi = c.min(), c.max()
        inner = (c > lo + 1e-9) & (c < hi - 1e-9)
        k = np.median(c[inner] / rt[inner]) if inner.any() else float('nan')
        print(f'  {d:<5} 区間{seg:>3}  消費 {lo:.3f}〜{hi:.3f}  係数 {k:.5f}'
              f'  → 必要ST {lo*seg:.1f}〜{hi*seg:.1f}')

    diagnose(path)

    # ---------- ⑤ 貼り付け用 ----------
    print('\n■ ⑤ oasis_core.py に貼る形（実測値）')
    if all((d, ph) in fitted for d in oc.DIST_LIST for ph in PHASES):
        print('INTERNAL_WEIGHTS_MEASURED = {   # 距離 → 区間 → [SP, PW, ST]')
        for d in oc.DIST_LIST:
            print(f"    '{d}': {{")
            for ph in PHASES:
                w = fitted[(d, ph)]
                print(f"        '{ph}': [{w[0]:.4f}, {w[1]:.4f}, {w[2]:.4f}],")
            print('    },')
        print('}')
        print('CONDITION_MULT_MEASURED = {' + ', '.join(
            f"'{c}': {m:.4f}" for c, m in cond_mult.items()) + '}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'races.jsonl'))
