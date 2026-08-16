# -*- coding: utf-8 -*-
"""check_pool_seed.py — 3連単プールの「初期金（シード）」がいくらかを実測する。

    python check_pool_seed.py 貼り付けたテキスト.txt

サイト側の疑い: プール総額の表示には初期プール金が含まれているのに、
オッズの計算には含まれていない。だとすると実際の払戻はオッズ表示より大きい。

**確かめ方**: 3連単の投票は 1口 10,000rrc 単位なので、各組に賭かっている金額は
必ず 10,000 の倍数になる。
    賭け金[組] = (プール総額 − シード) / オッズ[組]
シードを 0 から順に動かして、**全組が10,000の倍数に一番きれいに乗る値**を探す。
その値が実際のシード。0 なら疑いは外れ、200,000 付近なら当たり。
"""
import io
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

U = oc.STAKE_UNIT          # 10,000


def misfit(pool, odds, seed):
    """各組の賭け金が10,000の倍数からどれだけ外れているか（0が完全一致）。"""
    bets = (pool - seed) / np.asarray(odds, float)
    frac = np.abs(bets / U - np.round(bets / U))
    return float(np.mean(frac))


def scan(pool, odds, step=10_000, hi=None):
    hi = hi if hi is not None else min(pool * 0.9, 1_000_000)
    cand = np.arange(0, hi + 1, step)
    return [(float(s), misfit(pool, odds, s)) for s in cand]


def main(path):
    text = io.open(path, encoding='utf-8', errors='replace').read()
    horses, odds, dist, track, ground, guild, sid, pool, n_tri = oc.parse_unified(text)
    od = [v for v in odds.values() if v and v > 0]
    if not pool or not od:
        print('❌ pool= の行と「=== 3連単オッズ ===」の両方が要ります。'
              f'（pool={pool} / オッズ{len(od)}組）')
        return 1
    print(f'プール総額 {int(pool):,} rrc / オッズ {len(od)}組 / {dist}・{track}')
    inv = sum(1.0 / o for o in od)
    print(f'Σ(1/オッズ) = {inv:.4f}   （1.00 ちょうどなら控除なし）')

    curve = scan(pool, od)
    curve.sort(key=lambda t: t[1])
    best, best_err = curve[0]
    print(f'\n{"シード":>10}{"倍数からのズレ":>16}')
    for s, e in sorted(curve[:6]):
        mark = '  ← 最良' if s == best else ''
        print(f'{int(s):>10,}{e:>16.4f}{mark}')
    zero = dict(curve).get(0.0, float("nan"))
    print(f'\n  シード0（現行の前提）のズレ {zero:.4f}')
    print(f'  最良 {int(best):,} のズレ {best_err:.4f}')

    if best > 0 and best_err < zero * 0.5 and best_err < 0.02:
        print(f'\n✅ 初期プール金は約 {int(best):,} rrc。オッズはこれを含んでいません。')
        print(f'   実際の払戻はオッズ表示の {pool / (pool - best):.3f} 倍になります。')
        print('   → oasis_core.TRIFECTA_POOL_SEED をこの値にしてください。')
    elif zero < 0.02:
        print('\n❌ シード0が最もきれいに乗ります。疑いは外れです（オッズはプール総額と整合）。')
    else:
        print('\n△ どのシードでも倍数に乗りません。'
              'プール総額かオッズの取得値がずれている可能性があります。')
        print('  （1口の単位が10,000でない、オッズが丸められている、など）')
    return 0


def _selfcheck():
    """シード20万を仕込んだ架空データで、20万を復元できるか。"""
    seed, bets = 200_000, np.array([3, 5, 1, 12, 7, 2, 20, 4]) * U
    pool = float(bets.sum() + seed)
    odds = [(pool - seed) / b for b in bets]          # サイトの計算（シードを含めない）
    c = dict(scan(pool, odds))
    best = min(c, key=c.get)
    assert abs(best - seed) < 1e-9, (best, sorted(c.items(), key=lambda t: t[1])[:3])
    assert c[0.0] > 0.05, c[0.0]                      # シード0では明確に外れる
    # 逆に、シードが無いデータでは 0 が最良になること
    pool2 = float(bets.sum())
    odds2 = [pool2 / b for b in bets]
    c2 = dict(scan(pool2, odds2))
    assert min(c2, key=c2.get) == 0.0, sorted(c2.items(), key=lambda t: t[1])[:3]
    print(f'selfcheck OK  復元したシード {int(best):,} / シード0のズレ {c[0.0]:.4f}')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--selfcheck':
        _selfcheck()
    else:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'race.txt'))
