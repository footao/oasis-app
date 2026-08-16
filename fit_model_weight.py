# -*- coding: utf-8 -*-
"""fit_model_weight.py — モデル信頼度 λ（model_weight）を実測で決める。

    python fit_model_weight.py logg races.jsonl

`analyze()` は買う確率を  p_bet = λ×モデル + (1−λ)×市場  で混ぜている。
λ=1 はモデル全信頼、λ=0 は市場（オッズ）全信頼。既定は 0.7。

ここでは**単勝オッズ**を市場の予想とみなし、
「1着になったのは誰か」をどれだけ当てられたかで λ を選ぶ。
モデル側は out-of-fold 予測（自分のレースを学習に使っていない）なので公平。

⚠ 3連単のλを単勝で測っている。3連単のオッズは購入画面にしか無く、
   過去ぶんが手元に無いため。順番当ての難しさは違うので**目安**として読むこと。
"""
import os
import sys

import json

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

DEFAULT_LAMBDA = 0.7      # analyze() の既定（settings の model_weight）


def logloss(p):
    return -np.log(max(float(p), 1e-12))


def load_odds(jsonl):
    """races.jsonl から (開催日, 素名) → 単勝オッズ。

    logg 側にオッズが入っているレースは 103件中2件しか無いので、APIから補う。
    ステータスは使わないので、後日採取の行でも構わない（オッズは確定値）。
    """
    od = {}
    if not jsonl or not os.path.exists(jsonl):
        return od
    with open(jsonl, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            for h in r.get('horses') or []:
                if h.get('odds'):
                    od[(str(r.get('race_date')), oc.bare(h['name']))] = float(h['odds'])
    return od


def main(log_path='logg', jsonl='races.jsonl'):
    spec = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))
    df = oc.parse_race_log(log_path)
    df = df[(df['_d'] >= oc.SCORING_PATCH_DATE.replace('/', '-')) &
            (df['n_field'] >= 4)].reset_index(drop=True)
    y = oc._center_by_race(np.log(np.clip(df['score'].values, 1e-6, None)),
                           df['race_key'].values)
    X = oc.build_features(df, spec)
    g = df['race_key'].values
    oof = oc._oof_predictions(X, y, g, 8.0)
    sigma, _ = oc._calibrate_sigma(oof, df, float(np.std(y - oof)), top_k=1)
    print(f'レース {df["race_key"].nunique()} / σ={sigma:.4f}')

    ext = load_odds(jsonl)
    if ext:
        print(f'races.jsonl から単勝オッズ {len(ext)}件を補完')
    df['_ds'] = df['_d'].dt.strftime('%Y-%m-%d')

    rng = np.random.default_rng(0)
    races, bad = [], []
    for k, gg in df.groupby('race_key'):
        od = gg['win_odds'].values.astype(float)
        if not np.isfinite(od).all():
            od = np.array([ext.get((r['_ds'], oc.bare(r['name'])), np.nan)
                           for _, r in gg.iterrows()], float)
        # 市場として使えるオッズか。純パリミュチュエルなので Σ(1/od) は 1 付近になる。
        # by-id が返すオッズは大半が下限 1.5 の張りぼて（誰も賭けていない）で、
        # Σ が 6〜10 になる。これを市場とみなすと「モデルが市場に圧勝」という
        # **無意味な結論**が出るので、ここで弾く。
        if np.isfinite(od).all() and (od > 1).all():
            inv = float(np.sum(1.0 / od))
            if not (0.9 <= inv <= 1.15):
                bad.append(inv)
                continue
            if len(set(np.round(od, 3))) == 1:
                bad.append(inv)
                continue
        if len(gg) < 4 or not np.isfinite(od).all() or (od <= 1).any():
            continue
        b = oof[gg.index.values]
        b = b - b.mean()
        sim = b[None, :] + sigma * rng.standard_normal((20_000, len(b)))
        pm = np.bincount(np.argmax(sim, axis=1), minlength=len(b)) / 20_000
        mk = (1.0 / od) / np.sum(1.0 / od)          # 市場の含意確率
        win = int(np.argmin(gg['rank'].values))     # 実際の1着
        races.append((pm, mk, win))
    if bad:
        print(f'除外: Σ(1/オッズ) が 1 付近にならないレース {len(bad)}件'
              f'（中央 {np.median(bad):.2f}）。誰も賭けていない＝下限1.5が並ぶ状態で、'
              '市場の予想として使えません。')
    if len(races) < 20:
        print(f'❌ 市場として使えるオッズのレースが {len(races)} 件しかありません。'
              '\n   λ は測れません。購入画面から取った実オッズ（Σ(1/od)≈1）か、'
              '\n   決着済みのベットログが 20件以上必要です。')
        return 1
    print(f'オッズ付きで評価できたレース {len(races)} 件\n')

    print(f'{"λ":>6}{"対数損失":>12}{"1着的中":>10}')
    best = (9e9, None)
    curve = []
    for lam in np.round(np.arange(0, 1.001, 0.05), 2):
        ll, hit = [], []
        for pm, mk, w in races:
            p = lam * pm + (1 - lam) * mk
            p = p / p.sum()
            ll.append(logloss(p[w]))
            hit.append(int(np.argmax(p) == w))
        m = float(np.mean(ll))
        curve.append((float(lam), m, float(np.mean(hit))))
        if m < best[0]:
            best = (m, float(lam))
    for lam, m, h in curve:
        mark = ''
        if lam == best[1]:
            mark = '  ← 最良'
        elif abs(lam - DEFAULT_LAMBDA) < 1e-9:
            mark = '  ← 現行'
        if lam in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0) or mark:
            print(f'{lam:>6.2f}{m:>12.4f}{h*100:>9.0f}%{mark}')
    print(f'\n  最良 λ={best[1]:.2f}（対数損失 {best[0]:.4f}）'
          f' / 現行 λ={DEFAULT_LAMBDA}')
    only_m = [c for c in curve if c[0] == 1.0][0]
    only_k = [c for c in curve if c[0] == 0.0][0]
    print(f'  モデル単独 λ=1.00: 損失 {only_m[1]:.4f} / 的中 {only_m[2]*100:.0f}%')
    print(f'  市場単独   λ=0.00: 損失 {only_k[1]:.4f} / 的中 {only_k[2]*100:.0f}%')
    print('\n  損失が小さいほど良い。λ=1 が最良でも、混ぜた方が損失が下がるなら'
          '\n  市場はモデルの知らない情報を持っている（＝上げすぎは危険）。')
    return 0


if __name__ == '__main__':
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else 'logg', a[1] if len(a) > 1 else 'races.jsonl'))
