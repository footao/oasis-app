# -*- coding: utf-8 -*-
"""
selftest.py — Oasis 予測ツール v2 の動作確認
================================================
使い方:
    python selftest.py            # 既定で ./logg を学習
    python selftest.py <パス>     # ログのファイル/フォルダを指定

やること:
  1. ログの解析（何レース・何行読めたか、パッシブ2枠が取れているか）
  2. モデル学習（順位相関・1着的中・σ・校正チェック）
  3. パッシブ推定効果の表示
  4. 直近レースを使った擬似的な予測（貼り付けフォーマットの往復テスト）
  5. ベットログの記録・精算・レポート
"""
import itertools
import os
import sys
import tempfile
import warnings

warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oasis_core as oc


def hr(title):
    print('\n' + '=' * 68)
    print(title)
    print('=' * 68)


def main(log_path='logg'):
    hr('1) ログの解析')
    df = oc.parse_race_log(log_path)
    if len(df) == 0:
        print(f'❌ ログを読めませんでした: {log_path}')
        return 1
    print(f'  {len(df)}行 / {df["race_key"].nunique()}レース  '
          f'({df["date"].min()} 〜 {df["date"].max()})')
    n2 = int((df['passives'].map(len) >= 2).sum())
    print(f'  パッシブ2枠の行: {n2} / {len(df)}')
    print(f'  頭数の分布: {dict(sorted(df.groupby("race_key")["name"].size().value_counts().items()))}')
    new = df[df['_d'] >= pd.Timestamp(oc.SCORING_PATCH_DATE.replace("/", "-"))]
    print(f'  新スコア式({oc.SCORING_PATCH_DATE}以降): {new["race_key"].nunique()}レース / {len(new)}行')
    print('  スコアの距離別レンジ（新式）:')
    if len(new):
        print(new.groupby('dist')['score'].agg(['count', 'mean', 'min', 'max']).round(1).to_string())

    hr('2) モデル学習')
    bundle = oc.train_model(log_path)
    if not bundle.get('ok'):
        print('❌', bundle['messages'])
        return 1
    for m in bundle['messages']:
        print('  ' + m)
    for w in bundle.get('warnings', []):
        print('  ' + w)
    assert bundle['race_spearman'] > 0.4, '順位相関が低すぎます（計算式が変わった可能性）'

    hr('3) パッシブの効き目（中距離・芝）')
    pe = pd.DataFrame(oc.passive_effects(bundle, '中距離', '芝'))
    pe['効果'] = pe.apply(
        lambda r: (f'σ×{r["sigma_mult"]:.2f}' if r['source'] == 'variance'
                   else f'{r["pct"]:+.1f}%'), axis=1)
    print(pe[['passive', 'source', 'kind', '効果', 'n']].to_string(index=False))
    print('\n  出所の内訳:', dict(pe['source'].value_counts()))
    print('  🎮 game=ゲーム表記の確定値 / 🔎 inferred=推定 / 📊 learned=実測から学習')

    hr('3-b) パッシブ説明文の自動読み取り')
    sample = ('1. ⚡ スピードスター\nスピードが35%上昇する代わりに、スタミナが10%低下する。\n'
              '2. 🎯 安定感\nレース中の速度のばらつきが約半分になり、能力どおりに走りやすくなる。\n')
    got = oc.parse_passive_descriptions(sample)
    for k, v in got.items():
        print(f'  {k}: mult={v["mult"]} scope={v["scope"]} sigma×{v["sigma_mult"]}')
    assert 'スピードスター' in got and got['安定感']['sigma_mult'] == 0.5

    hr('4) 直近レースで予測（貼り付けフォーマットの往復テスト）')
    files = oc._iter_log_files(log_path)
    text = ''
    for f in files:
        text += open(f, encoding='utf-8', errors='replace').read()
    entries = oc.parse_entries(text)
    key = sorted(entries)[-1]
    e = entries[key]
    hs = e['horses']
    print(f'  対象: {key}  {e["dist"]}｜{e["track"]}｜{e["g_cond"]}  {len(hs)}頭')

    rng = np.random.default_rng(0)
    L = ['pool=1500000', '=== 出走馬一覧 ===',
         'レース距離,馬場,馬名,SPEED,POWER,STAMINA,コンディション,パッシブスキル,単勝オッズ']
    for h in hs:
        L.append(f'{e["dist"]},{e["track"]},{h["name"]},{h["speed"]},{h["power"]},'
                 f'{h["stamina"]},{h["condition"]},'
                 f'{" / ".join(h["passives"]) or "なし"},{round(float(rng.uniform(2, 40)), 2)}')
    disp = oc.disambiguate([h['name'] for h in hs])
    if len(hs) >= oc.MIN_FIELD_TRIFECTA:
        L += ['=== 3連単オッズ ===', '順位,1着,2着,3着,オッズ']
        for i, c in enumerate(itertools.permutations(disp, 3), 1):
            L.append(f'{i},{c[0]},{c[1]},{c[2]},{round(float(rng.uniform(50, 9000)), 1)}')
    r = oc.analyze('\n'.join(L), bundle,
                   dict(bankroll=1_200_000, edge_min=0.10, win_bets=True, topn=8))
    assert r['ok'], r.get('error')
    print(f'  読み込めた馬: {r["n_field"]}頭（元 {len(hs)}頭）'
          f'{"  ✅" if r["n_field"] == len(hs) else "  ❌"}')
    print('  モデルの◎:', r['model_pick'])
    print('\n  勝率上位:')
    for row in r['single_win'][:5]:
        c = row['contrib']
        print(f'    {row["name"]:10s} {row["model_p"]*100:5.1f}%  {row["passives"]:26s} '
              f'SP{c["speed"]:+.2f} PW{c["power"]:+.2f} ST{c["stamina"]:+.2f} '
              f'状態{c["condition"]:+.2f} パ{c["passive"]:+.3f}')
    print('\n  3連単ランキング:')
    for row in r['ranking'][:5]:
        print(f'    {row["rank"]:2d} {row["combo"]:36s} {row["model_p"]*100:6.2f}%  '
              f'累積{row["cum"]*100:5.1f}%  {row["flag"]}')
    if r.get('summary'):
        sm = r['summary']
        print(f'\n  推奨: {sm["n_points"]}点 / {sm["total_units"]}口 / '
              f'{sm["invest"]:,} rrc（いずれか的中 {sm["hit"]*100:.0f}%）')
    print('  ※ オッズはテスト用の乱数なので、EVの数値自体に意味はありません。')

    hr('5) ベットログ（記録 → 精算 → レポート）')
    path = os.path.join(tempfile.gettempdir(), 'oasis_selftest_log.csv')
    if os.path.exists(path):
        os.remove(path)
    bl = oc.BetLog(path, race_sigma=bundle['race_sigma'])
    picks = r.get('picks') or [((disp[0], disp[1], disp[2]), 0.1, 50.0, 1)]
    n = bl.record('SELFTEST', picks, oc.STAKE_UNIT)
    print(f'  記録 {n}点')
    cnt = bl.settle('SELFTEST', (disp[0], disp[1], disp[2]))
    print(f'  精算 {cnt}点  → 的中 {int((bl.load()["status"] == "won").sum())}点')
    rep = bl.report()
    print(f'  ROI {rep["overall"]["roi"]:+.1f}%   {rep["calib_hint"]}')
    os.remove(path)

    hr('✅ すべて完了')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'logg'))
