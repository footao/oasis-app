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
import re
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

    if regression_tests() != 0:
        return 1

    hr('✅ すべて完了')
    return 0


# =====================================================================
#  回帰テスト — 一度直したバグが戻っていないかを機械的に確認する
# =====================================================================
def _result_block(rank, nm, sp, st, pw, score, owner='@someone'):
    mark = {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, f'{rank}着')
    return '\n'.join([f'{mark} {nm}', owner, f'🏃 スピード {sp}', f'🫀 スタミナ {st}',
                      f'💥 パワー {pw}', f'📊 score {score}'])


def _discord_race(no, horses, date='2026/08/02', time='10:30'):
    """Discordエクスポート書式の1レース（同日同時刻・レース番号違いを作れる）。"""
    L = [f'[{date} {time}] bot', f'🏁 第{no}レース 結果', f'🕘 {time}｜中距離｜芝｜良']
    for rank, h in enumerate(horses, 1):
        L.append(_result_block(rank, h[0], h[1], h[2], h[3], 1000 - rank * 10))
    L.append('')
    return '\n'.join(L)


def _harvest_race(sid, horses, date='2026/08/02', time='0:00'):
    """harvest.js 書式（owner は常に @Unknown、レース番号は schedule_id）。"""
    L = [f'[{date} {time}] Oasis-API', '', f'🏁 第{sid}レース 結果',
         f'🕘 {time}｜中距離｜芝｜良']
    for rank, h in enumerate(horses, 1):
        L.append(_result_block(rank, h[0], h[1], h[2], h[3], 1000 - rank * 10,
                               owner='@Unknown'))
    L.append('')
    return '\n'.join(L)


def _bm_scrape(n_field, bet_units, order, par=20):
    """bookmarklets/src/bm.js の3連単オッズ取得ループを、そのまま Python に写したもの。

    node が入っていない環境でも回せるよう selftest 側に置いている
    （JS を直したらここも直すこと。見るのは打ち切り条件の算数だけなので短い）。
    返り値: (取得した組の集合, 何組取ったか)
    """
    SEED_, UNIT = oc.TRIFECTA_POOL_SEED, oc.STAKE_UNIT
    base = sum(bet_units.values()) * UNIT          # プール総額 − 初期プール金
    w = {i: 1.0 for i in range(n_field)}
    queue, seen_set = list(order), set()
    got_amt = 0.0
    while queue:
        batch, queue = queue[:par], queue[par:]
        hit = False
        for c in batch:
            seen_set.add(c)
            if c in bet_units:
                hit = True
                for h in c:
                    w[h] *= 3
        # 表示オッズは小数2桁なので 賭け金=base/od に最大 0.005/od の相対誤差が乗る。
        # 誤差ぶん残額を多めに見て、取り逃しが起きない側に倒す（bm.js と同じ式）。
        got_amt, err = 0.0, 0.0
        for c in seen_set:
            if c not in bet_units:
                continue
            od = base / (bet_units[c] * UNIT)      # サイトが表示するオッズ
            b = base / od
            err += b * 0.005 / od
            got_amt += b
        if base > 0 and base - got_amt + err < UNIT:
            break
        if hit:
            queue.sort(key=lambda c: -(w[c[0]] * w[c[1]] * w[c[2]]))
    return seen_set, len(seen_set)


def regression_tests():
    hr('6) 回帰テスト（過去に直したバグの再発防止）')
    A = [(f'馬A{i}', 100 + i, 90 + i, 80 + i) for i in range(4)]
    B = [(f'馬B{i}', 120 + i, 70 + i, 60 + i) for i in range(4)]
    discord = _discord_race(1, A) + '\n' + _discord_race(2, B)
    harvest = _harvest_race(9001, A) + '\n' + _harvest_race(9002, B)
    fails = []

    def check(label, cond, detail=''):
        print(f'  {"✅" if cond else "❌"} {label}' + (f'  {detail}' if detail else ''))
        if not cond:
            fails.append(label)

    # --- R3: 同日同時刻の別レースが1レースに合成されないこと ---
    d = oc.parse_race_log(texts=[discord])
    check('R3 同日同時刻の2レースが分かれている',
          d['race_key'].nunique() == 2 and set(d['n_field']) == {4},
          f'races={d["race_key"].nunique()} n_field={sorted(set(d["n_field"]))}')

    # --- R2: Discordログ + harvest採取ログの併用で二重カウントしないこと ---
    d2 = oc.parse_race_log(texts=[discord, harvest])
    check('R2 別ソースの同一レースを二重カウントしない',
          d2['race_key'].nunique() == 2 and len(d2) == 8 and set(d2['n_field']) == {4},
          f'races={d2["race_key"].nunique()} rows={len(d2)} '
          f'n_field={sorted(set(d2["n_field"]))}')
    check('R2 harvest単体でも別レースが潰れない',
          oc.parse_race_log(texts=[harvest])['race_key'].nunique() == 2)
    check('同じログを2回読んでも増えない',
          len(oc.parse_race_log(texts=[discord, discord])) == 8)

    # --- R1: オッズ 1.5 の「未投票」と「下限張り付きの大本命」を取り違えないこと ---
    fl = oc.diagnose_floor_odds([2.0, 4.0, 4.0, 1.5])          # Σ(1/od)=1.0
    check('R1 本当に未投票の馬を未投票と判定',
          fl['unbet'] == [False, False, False, True] and not fl['ambiguous'])

    shares = np.array([0.70, 0.15, 0.10, 0.05])
    od = np.round(1 / shares, 2)
    od[0] = oc.ODDS_FLOOR                                      # 下限に張り付いた本命
    fl = oc.diagnose_floor_odds(od)
    check('R1 下限張り付きの大本命を未投票と誤判定しない',
          not any(fl['unbet']) and abs(fl['odds_eff'][0] - 1 / 0.70) < 0.02,
          f'residual={fl["residual"]:.2f} 本当のod={fl["odds_eff"][0]:.2f}')

    fl = oc.diagnose_floor_odds([1.5, 1.5, 4.0, 10.0])         # 本命と未投票が混在
    check('R1 判別不能なら推奨から外す（安全側）',
          fl['ambiguous'] and np.isnan(fl['odds_eff'][0]) and np.isnan(fl['odds_eff'][1]))

    fl = oc.diagnose_floor_odds([1.5, 2.0, 4.0, 4.0], my_amounts=[3000, 0, 0, 0])
    check('R1 自分が買った1.5表示馬は未投票扱いにしない', fl['unbet'][0] is False)

    # 配分まで通して、ありえない実効オッズが出ないこと
    picks, _ = oc.win_bet_picks_pool(
        ['大本命', 'B', 'C', 'D'], [0.72, 0.14, 0.09, 0.05], od, 200_000,
        1_200_000, 0.25, 0.15, stake_unit=oc.WIN_STAKE_UNIT,
        unbet=oc.diagnose_floor_odds(od)['unbet'])
    worst = max([p['eff_od'] for p in picks], default=0)
    check('R1 大本命に非現実的な高配当を付けない', worst < 10,
          f'最大実効od={worst:.1f}')

    # --- M1: settings に None を渡しても既定値が生きること ---
    try:
        s = dict(oc.DEFAULT_SETTINGS)
        s.update({k: v for k, v in dict(model_weight=None).items() if v is not None})
        check('M1 None は既定値を上書きしない', s['model_weight'] is not None,
              f'model_weight={s["model_weight"]}')
    except Exception as e:                                     # pragma: no cover
        check('M1 None は既定値を上書きしない', False, str(e))

    # --- M3: プール取得APIで画面をブロックしないこと ---
    import time as _t
    t0 = _t.time()
    pool, _err = oc._fetch_pool_api('g', '1')
    check('M3 プールAPIでブロックしない', (_t.time() - t0) < 0.5 and pool is None,
          f'{(_t.time()-t0)*1000:.0f}ms / ENABLE_POOL_API={oc.ENABLE_POOL_API}')

    # --- M4: 精算時に最終オッズを反映できること ---
    import tempfile as _tf
    lp = os.path.join(_tf.gettempdir(), 'oasis_m4_regress.csv')
    if os.path.exists(lp):
        os.remove(lp)
    _bl = oc.BetLog(lp)
    _bl.record('X1', [(('A', 'B', 'C'), 0.1, 50.0, 1)], oc.STAKE_UNIT)
    _bl.record('X2', [(('A', 'B', 'C'), 0.1, 50.0, 1)], oc.STAKE_UNIT)
    _bl.settle('X1', ('A', 'B', 'C'))                              # 最終オッズなし
    _bl.settle('X2', ('A', 'B', 'C'), final_odds={'3連単': 32.0})   # 最終オッズあり
    _d = _bl.load()
    r1 = _d[_d['race_id'] == 'X1'].iloc[0]
    r2 = _d[_d['race_id'] == 'X2'].iloc[0]
    check('M4 最終オッズ未入力なら購入時オッズで概算',
          r1['payout'] == 500_000 and r1['payout_kind'] == '概算')
    check('M4 最終オッズを入れると払戻が実績になる',
          r2['payout'] == 320_000 and r2['payout_kind'] == '実績',
          f"od {50.0}→{r2['odds']:.1f} 払戻{r2['payout']:,.0f}")
    _rep = _bl.report()
    check('M4 レポートが概算払戻の件数を出す', _rep.get('n_payout_est') == 1,
          f"的中{_rep.get('n_won')}件 / 概算{_rep.get('n_payout_est')}件")
    _old = pd.read_csv(lp, encoding='utf-8-sig').drop(columns=['payout_kind'])
    _old.to_csv(lp, index=False, encoding='utf-8-sig')
    check('M4 payout_kind 列が無い旧ログも読める',
          'payout_kind' in oc.BetLog(lp).load().columns and len(oc.BetLog(lp).load()) == 2)
    os.remove(lp)

    # --- M2: 同σならMCを1回で済ませる（結果は変わらないこと）---
    _base = np.array([0.10, 0.05, 0.0, -0.03, -0.05, -0.08, -0.10, -0.12])
    _sig = np.full(len(_base), 0.05)
    _w_full, _c_full = oc.simulate_trifecta(_base, _sig, n_sim=40_000)
    _w_win, _c_win = oc.simulate_trifecta(_base, _sig, n_sim=40_000, need_combo=False)
    check('M2 need_combo=False でも勝率は完全一致',
          np.array_equal(_w_full, _w_win) and not _c_win,
          f'最大差={np.abs(_w_full - _w_win).max():.1e}')

    # --- 軽微1: bare() が2種類の重複マーカーを外すこと ---
    check('軽微1 bare() が \' #1\' と \'#1\' の両方を外す',
          oc.bare('ぴよ #1') == 'ぴよ' and oc.bare('ぴよ#1') == 'ぴよ'
          and oc.bare('ぴよ') == 'ぴよ',
          f"' #1'→{oc.bare('ぴよ #1')} / '#1'→{oc.bare('ぴよ#1')}")
    _disp = oc.disambiguate(['ぴよ#1', 'ぴよ#2', 'ほげ'])
    _cnt = {}
    for _d in _disp:
        _cnt[oc.bare(_d)] = _cnt.get(oc.bare(_d), 0) + 1
    check('軽微1 同名馬は素名フォールバックの対象外のまま（確率コピー防止）',
          _cnt.get('ぴよ') == 2, f'素名カウント={_cnt}')

    # --- P1: 3連単の初期プール金（サイト側のバグ）の補正 ---
    # 表示オッズは (プール総額 − 20万) で計算されているのに、払戻はプール総額から出る。
    # 補正を戻すと EV を過小評価して買い目を取りこぼすので、ここで固定する。
    _S = oc.TRIFECTA_POOL_SEED
    _bets = [3, 5, 1, 12]                       # 各組の口数（1口10,000rrc）
    _pool = sum(_bets) * oc.STAKE_UNIT + _S
    _disp_od = [(_pool - _S) / (b * oc.STAKE_UNIT) for b in _bets]   # サイトの表示値
    _true_od = [_pool / (b * oc.STAKE_UNIT) for b in _bets]          # 実際の払戻
    _got = [oc.true_trifecta_odds(o, _pool) for o in _disp_od]
    check('P1 初期プール金20万ぶんオッズを補正する',
          all(abs(g - t) < 1e-9 for g, t in zip(_got, _true_od)),
          f'プール{_pool:,} → 補正 x{_pool/(_pool-_S):.3f}')
    # 補正後のオッズから賭け金を逆算すると、必ず1口(10,000)の倍数に戻ること。
    # ここが崩れると実効オッズも口数配分も全部ずれる。
    _back = [_pool / o / oc.STAKE_UNIT for o in _got]
    check('P1 補正後のオッズから賭け金を逆算すると口数に戻る',
          all(abs(b - round(b)) < 1e-9 for b in _back), f'口数={[round(b) for b in _back]}')
    check('P1 プールが初期金以下なら補正しない（0除算・負値の防止）',
          oc.true_trifecta_odds(10.0, _S) == 10.0
          and oc.true_trifecta_odds(10.0, _S - 1) == 10.0
          and oc.true_trifecta_odds(10.0, 0) == 10.0)
    # キャリーオーバー判定は**補正前**の値で行うこと（補正後だと二重に足す）
    _pp_raw, _ci_raw = oc.resolve_payout_pool(_pool, _disp_od)
    _pp_cor, _ci_cor = oc.resolve_payout_pool(_pool, _got)
    check('P1 CO判定に補正後オッズを渡すと二重計上になる（＝補正前を渡すのが正）',
          abs(_pp_raw - _pool) < 1e-6 and _pp_cor > _pool * 1.05,
          f'補正前 {_pp_raw:,.0f}（Σ1/od={_ci_raw["inv_sum"]:.3f}） / '
          f'補正後 {_pp_cor:,.0f}（Σ1/od={_ci_cor["inv_sum"]:.3f}）')

    # --- P2: 市場の暗黙確率 q は「補正前オッズ」で正規化すること ---
    # 補正後で正規化すると Σ(1/od) が 1/_f に落ち、max(...,1.0) のクランプで
    # q が一律 1/_f 倍に縮む（＝λ混合の市場側が実質死ぬ）。
    _S2 = oc.TRIFECTA_POOL_SEED
    _P2 = 400_000
    _b2 = [80_000, 60_000, 30_000, 20_000, 10_000]      # 合計 = P − seed（完全な市場）
    check('P2 完全な市場なら補正前 Σ(1/od) は 1.00',
          abs(sum(b / (_P2 - _S2) for b in _b2) - 1.0) < 1e-9)
    _disp2 = [(_P2 - _S2) / b for b in _b2]
    _corr2 = [oc.true_trifecta_odds(o, _P2) for o in _disp2]
    check('P2 補正後だと Σ(1/od) が 1 を割る（クランプに当たる）',
          sum(1 / o for o in _corr2) < 0.99,
          f'補正後Σ={sum(1 / o for o in _corr2):.3f}')
    _fix2 = _P2 / (_P2 - _S2)
    _q2 = [(_fix2 / o) / max(sum(1 / x for x in _disp2), 1.0) for o in _corr2]
    check('P2 補正後オッズから市場シェアを正しく復元できる',
          all(abs(q - b / (_P2 - _S2)) < 1e-9 for q, b in zip(_q2, _b2)),
          f'q={[round(q, 4) for q in _q2]}')
    _q_bad = [(1 / o) / max(sum(1 / x for x in _corr2), 1.0) for o in _corr2]
    check('P2 補正後で正規化すると q が 1/_f 倍に縮む（これを踏まないこと）',
          all(abs(qb - q / _fix2) < 1e-9 for qb, q in zip(_q_bad, _q2)),
          f'縮み {_q_bad[0] / _q2[0]:.3f} 倍（= 1/{_fix2:.1f}）')

    # --- B1: 実効オッズの分子に「自分がそのレースで置いた全口数」が入ること ---
    _P3, _U3 = 400_000.0, oc.STAKE_UNIT
    _cd = [(('a', 'b', 'c'), 0.30, 20.0), (('a', 'c', 'b'), 0.20, 25.0),
           (('b', 'a', 'c'), 0.15, 30.0)]
    _al = oc.allocate_units_stable(_cd, _P3, bankroll=1_200_000, kelly_frac=0.25,
                                   max_risk_frac=0.10, edge_min=0.10)
    _tot = sum(v[0] for v in _al.values())
    _od_of = {c: od for c, _p, od in _cd}
    _ok_all = all(
        abs(eff - (_P3 + _tot * _U3) / (_P3 / _od_of[c] + k * _U3)) < 1e-9
        for c, (k, _ev, eff) in _al.items())
    check('B1 実効オッズの分子は全組の合計口数（その組だけではない）',
          _tot > 1 and _ok_all, f'{_tot}口 / eff={[round(v[2], 2) for v in _al.values()]}')
    _c0, (_k0, _e0, _eff0) = next(iter(_al.items()))
    check('B1 修正前の式より必ず高く出る（払戻の過小評価を解消）',
          _eff0 > (_P3 + _k0 * _U3) / (_P3 / _od_of[_c0] + _k0 * _U3) + 1e-9,
          f'{_eff0:.2f} > {(_P3 + _k0 * _U3) / (_P3 / _od_of[_c0] + _k0 * _U3):.2f}')

    # --- B4: ベットログの書き込みが原子的（途中で落ちても欠損しない）---
    import tempfile as _tf
    _d4 = _tf.mkdtemp()
    _bl4 = oc.BetLog(os.path.join(_d4, 'log.csv'))
    _bl4.record('r1', [(('a', 'b', 'c'), 0.3, 10.0, 2)], bet_type='3連単')
    _n4 = len(_bl4.load())
    _tmps = [f for f in os.listdir(_d4) if f.endswith('.tmp')]
    check('B4 保存後に一時ファイルが残らない', not _tmps, f'残骸={_tmps}')
    _orig = oc.pd.DataFrame.to_csv

    def _boom(self, path_or_buf=None, *a, **kw):          # 書き込み途中で落ちる想定
        _orig(self, path_or_buf, *a, **kw)
        raise OSError('disk full')
    oc.pd.DataFrame.to_csv = _boom
    try:
        _bl4.record('r2', [(('d', 'e', 'f'), 0.3, 10.0, 1)], bet_type='3連単')
    except Exception:
        pass
    finally:
        oc.pd.DataFrame.to_csv = _orig
    check('B4 書き込みが失敗しても既存のログが壊れない',
          len(_bl4.load()) == _n4, f'{_n4}行 → {len(_bl4.load())}行')

    # --- P3: bm.js の「残額が1口未満なら打ち切る」が金の乗った組を取り逃さないこと ---
    #   打ち切った組は「未成立」として出力され、unformed_sleeve_picks が
    #   全プール総取り(23倍)の買い目候補に入れる。取り逃すと架空の高EV買い目になる。
    import random as _rnd
    _worst = _saved = _total = 0
    for _n in (10, 12, 15, 16):
        _all = [c for c in itertools.permutations(range(_n), 3)]
        for _s in range(30):
            _r = _rnd.Random(_s * 7919 + _n)
            _order = _all[:]
            _r.shuffle(_order)                     # 初期順(SPEED)が当てにならない最悪ケース
            _pop = list(range(max(3, _n // 3)))    # 人気の数頭に金が集中する置き方
            _bet = {}
            while len(_bet) < 2 + _r.randrange(8):
                _c = tuple(_r.sample(_pop, 3)) if len(_pop) >= 3 else None
                if _c:
                    _bet[_c] = 1 + _r.randrange(3)
            _seen, _cut = _bm_scrape(_n, _bet, _order)
            _worst += len(set(_bet) - _seen)
            _saved += len(_all) - _cut
            _total += len(_all)
    check('P3 打ち切っても金の乗った組を取り逃さない',
          _worst == 0, f'取り逃し{_worst}組 / リクエスト削減 {100 * _saved // _total}%')

    # プールが取れない(=0)ときは打ち切らず全件取ること
    _allp = [c for c in itertools.permutations(range(10), 3)]
    _seen0, _cut0 = _bm_scrape(10, {}, _allp)
    check('P3 プール不明のときは全件取得（従来どおり）',
          _cut0 == len(_allp), f'{_cut0}/{len(_allp)}')

    # --- P4: 実測 duty が、貼り付けによる passive_spec.json の上書きで戻らないこと ---
    #   `spec_from_description` は説明文から duty を推定して source='game' で書き戻すので、
    #   実測値をJSONに入れただけだと、パッシブ効果を貼るたびに推定値に戻ってしまう。
    _t = ('1. 🌑 追い込み\n終盤開始時に順位が下位半分の場合、終盤のパワーが12%上昇する。\n'
          '2. 🚀 ロケットスタート\n序盤区間のみ、スピードが12%上昇する。')
    _d = oc.parse_passive_descriptions(_t)
    check('P4 貼り付けても実測dutyが推定値に戻らない',
          abs(_d['追い込み']['duty'] - oc.DUTY_MEASURED['追い込み']) < 1e-9
          and abs(_d['ロケットスタート']['duty'] - oc.DUTY_MEASURED['ロケットスタート']) < 1e-9,
          f"追い込み {_d['追い込み']['duty']:.3f} / ロケット {_d['ロケットスタート']['duty']:.3f}")
    _sp = oc.load_passive_spec(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            'passive_spec.json'))
    check('P4 独走態勢は実測どおり発動しない（duty=0）',
          _sp['独走態勢']['duty'] == 0.0, f"duty={_sp['独走態勢']['duty']}")
    # --- P5: 装備・お守りの「倍率」効果が反映されること ---
    #   購入ページ曰く「表示値＝個体値＋特訓＋装備品。倍率・条件スキルはレース中に適用」。
    #   つまり貼り付けの SPEED には**加算ぶんしか入っていない**ので、倍率は別に掛ける。
    #   実測（お守り「太陽のメダリオン」）: PW+5 は表示値に入る / スピード常時+4.4% は入らない。
    _hdr = ('レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,'
            'パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額,装備,装備効果,'
            'お守り,お守り効果')
    _rows = ('マイル,芝,,あ,a,100,50,50,普通,,,1.5,0,,,,\n'
             'マイル,芝,,い,b,100,50,50,普通,,,1.5,0,,,'
             '太陽のメダリオン,俊足の加護：スピードが常時4.4%上昇\n'
             'マイル,芝,,う,c,100,50,50,普通,,,1.5,0,,,'
             'お守りX,守り：他の出走馬が20m以内にいる間、パワーが6%上昇する。\n')
    _h, *_ = oc.parse_unified('=== 出走馬一覧 ===\n' + _hdr + '\n' + _rows)
    _by = {x['name']: x for x in _h}
    check('P5 常時倍率の装備効果を実効ステータスに掛ける',
          abs(_by['い']['speed'] - 104.4) < 1e-9 and _by['あ']['speed'] == 100,
          f"あ={_by['あ']['speed']} / い={_by['い']['speed']}")
    # 条件付きは**発動率で割り引いて**掛ける（パッシブと同じ扱い）。
    # 満額で掛けると条件付き装備を過大評価するので、そこが崩れていないかを見る。
    check('P5 条件付きの装備効果は発動率で割り引く（満額で掛けない）',
          50.0 < _by['う']['power'] < 50 * 1.06 - 1e-9,
          f"う PW={_by['う']['power']:.3f}（満額なら53.0）")
    check('P5 出走メンバー依存など判定できない効果は掛けずに警告に回す',
          not oc.item_effect_spec('同じ成体種が出場している場合、全ステータスが20%上昇する。',
                                  None, oc.default_spec()))
    check('P5 「常時」を挟んだ説明文をパースできる',
          (oc.spec_from_description('スピードが常時4.4%上昇') or {}).get('mult') == {'speed': 1.044},
          str((oc.spec_from_description('スピードが常時4.4%上昇') or {}).get('mult')))
    # --- P5c: 交易所で実際に出品されていた効果文が全部読めること ---
    #   同じテンプレでも個体ごとに数値も効果も違うので、文字列の型が想定どおりかを固定する。
    #   （2026/08/18 の交易所16件から、重複を除いた9パターン）
    _spec5c = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    _REAL = [
        ('序盤のスピードが2.3%上昇', 'gear_rocket_start', 'speed', True),
        ('中盤のパワーが2.1%上昇', 'gear_mid_acceleration', 'power', True),
        ('終盤のパワーが1.9%上昇', 'gear_final_kick', 'power', True),
        ('中盤開始後200mのスピードが1.8%上昇', 'gear_second_gear', 'speed', True),
        ('20m以内にライバルがいる間、パワーが1.9%上昇', 'gear_duel_spirit', 'power', True),
        ('スピードが常時1.7%上昇', 'charm_speed', 'speed', False),
        ('パワーが常時4.9%上昇', 'charm_power', 'power', False),
        ('スタミナ消費量が常時2.2%減少', None, 'stamina', False),
        ('全ステータスが常時1.3%上昇', None, 'speed', False),
    ]
    _bad = []
    for _d, _k, _stat, _discount in _REAL:
        _m = oc.item_effect_spec(_d, _k, _spec5c)
        _pct = oc._desc_pct(_d) if hasattr(oc, '_desc_pct') else None
        if not _m or _stat not in _m or _m[_stat] <= 1.0:
            _bad.append(_d)
            continue
        # 区間・条件限定は発動率で必ず割り引かれ、常時はそのまま乗ること
        _raw = 1 + float(re.search(r'(\d+(?:\.\d+)?)%', _d).group(1)) / 100
        if _discount and not (1.0 < _m[_stat] < _raw - 1e-9):
            _bad.append(_d + '（割り引かれていない）')
        if not _discount and abs(_m[_stat] - _raw) > 1e-9:
            _bad.append(_d + '（常時なのに割り引かれた）')
    check('P5c 交易所の効果文9パターンを正しく読める', not _bad, '; '.join(_bad) or '全件OK')
    check('P5c パッシブの実は装備効果として扱わない（通常のパッシブ経路へ）',
          oc.item_effect_spec('スタミナ不足による速度低下を20%軽減する。', None, _spec5c) is None)

    # --- P6: 貼り付けの balance= を読み、トークンは載っていないこと ---
    _txt = ('guild=1\nschedule_id=2\npool=400000\nbalance=3120000\n\n'
            '=== 出走馬一覧 ===\n'
            'レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,'
            'パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額\n'
            'マイル,芝,,あ,a,100,50,50,普通,,,1.5,0\n')
    _h6, *_ = oc.parse_unified(_txt)
    check('P6 貼り付けの所持金 balance= を読める',
          (_h6[0].get('_meta') or {}).get('balance') == 3120000.0,
          str((_h6[0].get('_meta') or {}).get('balance')))
    check('P6 token は出力に載せない設計（bm.js に token 出力が無い）',
          'token' not in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           'bookmarklets', 'src', 'bm.js'),
                              encoding='utf-8').read().split('const clip=[')[1],
          '出力組み立て部に token 参照なし')

    # --- P5b: 区間限定の装備効果は effect_key から実測 duty を引くこと ---
    #   実測装備 gear_second_gear「中盤開始後200mのスピードが2.4%上昇」。
    #   説明文だけだと「中盤 → 1/3」と読めて 1.008 になるが、二の脚の実測 duty は 0.128。
    _spec5 = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    _e1 = oc.item_effect_spec('中盤開始後200mのスピードが2.4%上昇', 'gear_second_gear', _spec5)
    _e2 = oc.item_effect_spec('中盤開始後200mのスピードが2.4%上昇', None, _spec5)
    check('P5b 区間限定の装備は effect_key の実測dutyで割り引く',
          abs(_e1['speed'] - (1 + 0.024 * _spec5['二の脚']['duty'])) < 1e-9
          and _e1['speed'] < _e2['speed'],
          f"key有り {_e1['speed']:.5f} / key無し {_e2['speed']:.5f}")
    check('P5b 常時の装備は割り引かない',
          oc.item_effect_spec('スピードが常時4.4%上昇', 'charm_speed',
                              _spec5) == {'speed': 1.044})

    check('P5 「残りスタミナが20%以下」を倍率と誤読しない',
          (oc.spec_from_description(
              '残りスタミナが20%以下になったとき、一度だけ最大スタミナの6%を回復する。')
           or {}).get('mult') == {'stamina': 1.06})

    check('P4 勝負師と常時発動系は触っていない',
          _sp['勝負師']['duty'] == 0.05 and _sp['省エネ走法']['duty'] == 1.0,
          f"勝負師 {_sp['勝負師']['duty']} / 省エネ走法 {_sp['省エネ走法']['duty']}")

    if fails:
        print('\n  ❌ 失敗:', ', '.join(fails))
        return 1
    print('\n  すべての回帰テストに合格')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'logg'))
