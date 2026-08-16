# -*- coding: utf-8 -*-
"""stamina_report.py — 「完走に何スタミナ要るのか」を実データから出す。

    python stamina_report.py logg races.jsonl

ステータスは logg（レース当時の値）、消費・区間数は races.jsonl の timeline を使う。
races.jsonl のステータスは当てにならないので混ぜない（harvest_results.py 冒頭の警告）。
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

SPEC = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))
W0 = oc.INTERNAL_PHASE_WEIGHTS['序盤']


def base_of(e, dist):
    """消費を決める「速さの指標」。序盤の区間重み × 距離バランス × 実効ステータス。"""
    b = oc.INTERNAL_DIST_BALANCE[dist]
    return e['speed'] * W0[0] * b[0] + e['power'] * W0[1] * b[1] + e['stamina'] * W0[2] * b[2]


def load(log_path, jsonl):
    tl = {}
    with open(jsonl, encoding='utf-8') as f:
        for line in f:
            r = json.loads(line)
            for h in r.get('horses') or []:
                seq = h.get('timeline') or []
                cs = [t.get('stamina_cost') for t in seq if t.get('stamina_cost')]
                if cs:
                    # 消費は区間ごとに微妙に違う（区間が変わるとレートが変わる）。
                    # cs[0]×区間数 で近似すると 4割の馬で 0.5 以上ずれるので、実測を合計する。
                    # ⚠ キーに時刻が要る。(日付, 馬名) だけだと1日6レースあるので
                    #   後のレースが前のレースを上書きする（実測 3,389頭中 1,025件が上書き、
                    #   うち距離まで一致していて素通りするものが 410件）。
                    #   (日付, 時刻, 生の馬名) なら衝突ゼロ。bare() で #1/#2 を潰さないこと。
                    tl[(r['race_date'], r.get('race_time'), h['name'])] = (
                        float(np.sum(cs)), len(cs), r['distance'], h.get('stamina_after'),
                        float(seq[0].get('stamina') or 0.0))
    d = oc.parse_race_log(log_path)
    d = d[d['n_field'] >= 4].reset_index(drop=True)
    ss = np.zeros(len(d), bool)
    for _, g in d.groupby('race_key'):
        for i, f in zip(g.index, oc.same_species_flags(g['name'].tolist())):
            ss[i] = f
    d['_ds'] = d['_d'].dt.strftime('%Y-%m-%d')
    # race_key は 'YYYY/MM/DD HH:MM 第NR' なので時刻を切り出す
    d['_tm'] = d['race_key'].astype(str).str.extract(r'(\d{1,2}:\d{2})')[0]
    rows = []
    for _, r in d.iterrows():
        k = (r['_ds'], r['_tm'], r['name'])
        if k not in tl:
            continue
        need, nseg, dist, after, st_api = tl[k]
        if dist != r['dist']:
            continue
        e = oc.effective_stats(r['speed'], r['power'], r['stamina'], r['passives'],
                               r['dist'], r['track'], SPEC, {'same_species': bool(ss[_])})
        rows.append(dict(dist=dist, base=base_of(e, dist), need=need, n_seg=nseg,
                         st0=np.floor(e['stamina']), st_api=st_api, after=after,
                         rank=r['rank'], race=r['race_key'], n_field=r['n_field']))
    return rows


def fit(rows):
    law = {}
    for d in oc.DIST_LIST:
        v = [x for x in rows if x['dist'] == d]
        if len(v) < 20:
            continue
        base = np.array([x['base'] for x in v])
        cost = np.array([x['need'] / x['n_seg'] for x in v])
        lo, hi = float(cost.min()), float(cost.max())
        mid = (cost > lo + 1e-9) & (cost < hi - 1e-9)
        law[d] = dict(c=float(np.median(cost[mid] / base[mid])), lo=lo, hi=hi,
                      n_seg=int(np.median([x['n_seg'] for x in v])), n=len(v),
                      n_lo=int((np.abs(cost - lo) < 1e-9).sum()),
                      n_hi=int((np.abs(cost - hi) < 1e-9).sum()))
    return law


def drop_by_shortfall(rows, d):
    """「指標の順位」と実着順の差を、スタミナが足りた馬／足りない馬で比べる。"""
    by = defaultdict(list)
    for x in rows:
        if x['dist'] == d:
            by[x['race']].append(x)
    ok, ng = [], []
    for g in by.values():
        if len(g) < 5:
            continue
        pr = np.argsort(np.argsort([-x['base'] for x in g])) + 1
        for p, x in zip(pr, g):
            (ng if x['st0'] - x['need'] < 0 else ok).append(x['rank'] - p)
    if not ok or not ng:
        return None, None, 0
    return float(np.mean(ok)), float(np.mean(ng)), len(ng)


def main(log_path='logg', jsonl='races.jsonl'):
    rows = load(log_path, jsonl)
    if not rows:
        print('❌ 突き合わせできる馬がいません')
        return 1
    print(f'突き合わせ {len(rows)}頭')
    law = fit(rows)

    print('\n■ 完走に必要なスタミナ')
    print(f'{"距離":<6}{"区間":>4}{"必要ST 最小":>12}{"最大":>8}{"消費/100m":>18}')
    for d in oc.DIST_LIST:
        L = law.get(d)
        if not L:
            print(f'  {d:<6} データ不足'); continue
        print(f'{d:<6}{L["n_seg"]:>4}{L["lo"]*L["n_seg"]:>12.1f}{L["hi"]*L["n_seg"]:>8.1f}'
              f'{L["lo"]:>12.3f}〜{L["hi"]:.3f}')

    print('\n■ 検算: 「持ち − 必要」が API の残スタミナと合うか')
    err = [(x['st0'] - x['need']) - x['after'] for x in rows if x['after'] is not None]
    err2 = [x['st0'] - x['st_api'] for x in rows]
    if err:
        e = np.array(err)
        print(f'  |誤差| < 0.5 の割合 {(np.abs(e) < 0.5).mean()*100:.1f}%'
              f'  中央値 {np.median(e):+.3f}  (n={len(e)})')
    e2 = np.array(err2)
    print(f'  初期スタミナ = floor(実効スタミナ) か: 一致 {(np.abs(e2) < 1e-6).mean()*100:.1f}%'
          f'  中央値 {np.median(e2):+.2f}')

    print('\n■ 実際に足りていたか')
    print(f'{"距離":<6}{"頭数":>6}{"不足あり":>9}{"不足の中央値":>13}{"余りの中央値":>13}')
    for d in oc.DIST_LIST:
        L = law.get(d)
        if not L:
            continue
        v = [x for x in rows if x['dist'] == d]
        gap = np.array([x['st0'] - x['need'] for x in v])
        short = gap < 0
        print(f'{d:<6}{len(v):>6}{short.sum():>6} ({short.mean()*100:>3.0f}%)'
              f'{-np.median(gap[short]) if short.any() else 0:>13.1f}'
              f'{np.median(gap[~short]) if (~short).any() else 0:>13.1f}')

    print('\n■ 速さと必要スタミナ（消費が上限に当たるライン）')
    for d in oc.DIST_LIST:
        L = law.get(d)
        if not L:
            continue
        b_lo, b_hi = L['lo'] / L['c'], L['hi'] / L['c']
        bal = oc.INTERNAL_DIST_BALANCE[d]
        # 実効SPだけを動かしたときの目安（PW/ST は中央値で固定）
        v = [x for x in rows if x['dist'] == d]
        print(f'  {d:<5} 指標 {b_lo:.0f} 以下 → 必要{L["lo"]*L["n_seg"]:.0f}（最小） / '
              f'{b_hi:.0f} 以上 → 必要{L["hi"]*L["n_seg"]:.0f}（最大・これ以上速くしても増えない）')
        print(f'         指標 = SP×{W0[0]*bal[0]:.2f} + PW×{W0[1]*bal[1]:.2f} + ST×{W0[2]*bal[2]:.2f}'
              f'  （実測の指標レンジ {min(x["base"] for x in v):.0f}〜{max(x["base"] for x in v):.0f}）')

    print('\n■ スタミナ切れは着順にいくら響くか')
    print('  同じレース内で「指標（速さ）の順位」と「実際の着順」を比べ、')
    print('  スタミナが足りた馬・足りなかった馬で落差を見る。')
    print(f'{"距離":<6}{"足りた馬":>12}{"足りない馬":>13}{"差":>8}')
    for d in oc.DIST_LIST:
        if d not in law:
            continue
        by = defaultdict(list)
        for x in rows:
            if x['dist'] == d:
                by[x['race']].append(x)
        okd, ngd = [], []
        for _, g in by.items():
            if len(g) < 5:
                continue
            pr = np.argsort(np.argsort([-x['base'] for x in g])) + 1   # 速さ順の予想着順
            for p, x in zip(pr, g):
                drop = x['rank'] - p                                   # ＋なら予想より負けた
                (ngd if x['st0'] - x['need'] < 0 else okd).append(drop)
        if okd and ngd:
            note = '' if len(ngd) >= 10 else f'  ※不足{len(ngd)}頭のみ・参考値'
            print(f'{d:<6}{np.mean(okd):>+12.2f}{np.mean(ngd):>+13.2f}'
                  f'{np.mean(ngd)-np.mean(okd):>+8.2f}{note}')
    print('\n  「差」が＋なら、スタミナ切れの馬は速さの割に負けている。')
    return 0


if __name__ == '__main__':
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else 'logg', a[1] if len(a) > 1 else 'races.jsonl'))
