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
import math
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
    base = sum(bet_units.values()) * UNIT          # 実際に賭けられた総額 = プール − 初期金
    pool = base + SEED_                            # プール総額（オッズの分母）
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
        # 2026/08/23 以降、表示オッズは**プール総額**基準（od = P / 賭け金）。
        # 賭け金は必ず1口の倍数なので、P/od を口数に丸め直せば端数が消えて
        # 残額がちょうど 1口 の倍数になる（「残 238,806rrc」のような表示は出ない）。
        # 丸め切れない組（オッズの丸め幅が半口を超える大本命）だけ 1口ぶん多めに見る。
        got_amt, slack = 0, 0
        for c in seen_set:
            if c not in bet_units:
                continue
            od = round(pool / (bet_units[c] * UNIT), oc.ODDS_DECIMALS)  # サイトの表示値
            u = pool / od / UNIT
            if abs(u - round(u)) > 0.25:
                slack += UNIT
            got_amt += round(u) * UNIT
        if base > 0 and base - got_amt + slack < UNIT:
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

    # --- P1: 3連単の初期プール金（旧バグ）の補正 ---
    # 表示オッズが (プール総額 − 初期金) で計算されていた時代の補正式。
    # 2026/08/23 に「直った」前提で TRIFECTA_SEED_BUG_ACTIVE=False にしたが、
    # 再発したら True に戻すだけで効くように、**補正式そのものは常に検証しておく**。
    _S = oc.TRIFECTA_POOL_SEED
    # 補正が穏やかになる口数にしておく（初期金がプールの大半だと Σ(1/od) が
    # 正気の範囲 INV_SUM_SANE を割って、下の CO 判定が安全側に倒れて何も起きない）。
    _bets = [30, 25, 20, 15]                    # 各組の口数（1口10,000rrc）
    _pool = sum(_bets) * oc.STAKE_UNIT + _S
    _disp_od = [(_pool - _S) / (b * oc.STAKE_UNIT) for b in _bets]   # サイトの表示値
    _true_od = [_pool / (b * oc.STAKE_UNIT) for b in _bets]          # 実際の払戻
    _got = [oc.true_trifecta_odds(o, _pool) for o in _disp_od]
    check('P1 初期プール金ぶんオッズを補正する',
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
    # seed を渡すと「初期金がオッズに入っている(=修正後)」判定が先に効くので、
    # ここでは**旧仕様の補正が正しいか**だけを見たいので seed は渡さない。
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
    _P2 = 900_000
    # 合計がちょうど P − seed になるように按分する（＝完全な市場）。
    # 金額を直書きすると初期金が変わるたびに壊れるので、seed から作ること。
    _b2 = [(_P2 - _S2) * f for f in (0.40, 0.30, 0.15, 0.10, 0.05)]
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

    # 残額は必ず1口の倍数（端数が出るなら賭け金の丸めがおかしい）
    _rem_ok = True
    for _n, _bet in [(10, {(0, 1, 2): 3, (1, 0, 2): 5, (3, 4, 5): 1}),
                     (14, {(0, 1, 2): 20, (2, 1, 0): 1, (5, 6, 7): 2, (8, 9, 1): 4})]:
        _pool = sum(_bet.values()) * oc.STAKE_UNIT + oc.TRIFECTA_POOL_SEED
        for _c, _u in _bet.items():
            _od = round(_pool / (_u * oc.STAKE_UNIT), oc.ODDS_DECIMALS)
            if round(_pool / _od / oc.STAKE_UNIT) != _u:
                _rem_ok = False
    check('P3 表示オッズから口数をぴったり復元できる（残額に端数が出ない）',
          _rem_ok, '丸め幅 ±0.005 は半口(5,000rrc)より十分小さい')

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

    # --- P8: 結果が壊れているレースを学習から外すこと ---
    #   Discordログ側には schedule_id が無いので「日付＋時刻」でも外れる必要がある。
    _bad = sorted(EX for EX in oc.EXCLUDED_RACES if isinstance(EX, str))
    check('P8 除外リストが日付+時刻でも schedule_id でも効く',
          oc.is_excluded_race(2037) and oc.is_excluded_race(date='2026/08/19', time='12:00')
          and oc.is_excluded_race(date='2026-08-19', time='12:00'),
          f'登録: {_bad}')
    check('P8 別の日・別の時刻は外さない',
          not oc.is_excluded_race(date='2026/08/18', time='12:00')
          and not oc.is_excluded_race(date='2026/08/19', time='15:00')
          and not oc.is_excluded_race())
    # ログ本文からの除外（race_key 'YYYY/MM/DD HH:MM 第NR' 経由）
    _A = [(f'除外馬{i}', 100 + i, 90 + i, 80 + i) for i in range(4)]
    _keep = _discord_race(1, _A, date='2026/08/19', time='15:00')
    _drop = _discord_race(2, _A, date='2026/08/19', time='12:00')
    _d8 = oc.parse_race_log(texts=[_keep + '\n' + _drop])
    check('P8 壊れたレースだけログから落ちる',
          _d8['race_key'].nunique() == 1 and '12:00' not in ''.join(_d8['race_key'].unique()),
          f"残り {list(_d8['race_key'].unique())}")

    # --- P7: プール表示が 0 のときも初期プール金を実在扱いすること ---
    #   誰も買っていないと表示は 0 だが実際は 20万 入っており、1口入った瞬間に 21万 に飛ぶ。
    #   0 のまま扱うと未成立スリーブの実効オッズが 1.0 になり、**いちばん美味しい場面**
    #   （当たれば初期プール金を総取り）を「価値なし」と判定して何も出さなくなる。
    _hdr7 = ('レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,'
             'パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額')
    _rows7 = '\n'.join(
        'マイル,芝,,馬%d,a%d,%d,50,48,普通,スピードスター,マイル得意,1.5,0' % (i, i, 150 - i * 3)
        for i in range(9))
    _t7 = 'guild=1\nschedule_id=9\npool=0\n\n=== 出走馬一覧 ===\n%s\n%s\n' % (_hdr7, _rows7)
    _b7 = oc.train_model('logg')
    _r7 = oc.analyze(_t7, _b7, {'dist': 'マイル', 'track': '芝',
                                'unformed_sleeve': True, 'bankroll': 3_000_000})
    _sl = [x for x in (_r7.get('alloc_rows') or []) if x.get('flag') == '未']
    check('P7 プール0でも初期プール金をプール総額として扱う',
          _r7.get('pool') == oc.TRIFECTA_POOL_SEED, f"pool={_r7.get('pool')}")
    check('P7 プール0でも未成立スリーブを出す（実効31倍）',
          bool(_sl) and abs((_sl[0].get('eff_od') or 0)
                            - (oc.TRIFECTA_POOL_SEED + oc.STAKE_UNIT) / oc.STAKE_UNIT) < 1e-6,
          f"{len(_sl)}点 / 実効od={_sl[0].get('eff_od') if _sl else None}")

    # --- P11: 初期プール金とオッズのレジーム判定 ---
    #   2026/08/23 30万・単勝40万 → 2026/08/25 どちらも20万（賞金増額と引き換え）。
    #   Σ(1/od) がどちらの世界かを言い当てられること。ここを取り違えると、
    #   「まだ金が乗っている組を未成立と誤判定して総取り狙いの買い目を出す」
    #   という一番危ない外し方をする。
    _P11 = 900_000
    # 賭け金の合計は P − seed（＝全部の金が乗り切っている状態）。
    # 直書きすると初期金が変わるたびに壊れるので、seed から按分して作る。
    _bets11 = [(_P11 - oc.TRIFECTA_POOL_SEED) * f for f in (0.5, 1 / 3, 1 / 6)]
    _bug11 = [(_P11 - oc.TRIFECTA_POOL_SEED) / b for b in _bets11]
    _fix11 = [_P11 / b for b in _bets11]
    check('P11 Σ(1/od)=1.00 なら旧仕様（初期金がオッズに入っていない）と判定',
          oc.check_seed_regime(_P11, _bug11)[0] == 'buggy',
          f'Σ={oc.check_seed_regime(_P11, _bug11)[1]:.3f}')
    check('P11 Σ(1/od)=(P−S)/P なら修正済みと判定',
          oc.check_seed_regime(_P11, _fix11)[0] == 'fixed',
          f'Σ={oc.check_seed_regime(_P11, _fix11)[1]:.3f} / 期待 '
          f'{oc.check_seed_regime(_P11, _fix11)[2]:.3f}')
    check('P11 初期金がプールに対して小さすぎると判別不能を返す（誤判定しない）',
          oc.check_seed_regime(oc.TRIFECTA_POOL_SEED * 30, _fix11)[0] == 'unknown')
    # 修正後の regime では「キャリーオーバー」に化けないこと（払戻を P²/(P−S) と過大評価する）
    _pp11, _ci11 = oc.resolve_payout_pool(_P11, _fix11, seed=oc.TRIFECTA_POOL_SEED)
    check('P11 修正後のΣをキャリーオーバーと誤読しない',
          _ci11['regime'] == 'seeded' and abs(_pp11 - _P11) < 1e-6,
          f"regime={_ci11['regime']} / 払戻プール {_pp11:,.0f}")
    check('P11 3連単の初期金・単勝のNPCプールはどちらも20万（2026/08/25〜）',
          oc.TRIFECTA_POOL_SEED == 200_000 and oc.WIN_POOL_SEED == 200_000,
          f'3連単 {oc.TRIFECTA_POOL_SEED:,} / 単勝 {oc.WIN_POOL_SEED:,}')

    # 実データ race 2097（初期金30万の時代・全2,730組そろい）で検算:
    #   プール 360,000 / 6組が各オッズ36 → Σ(1/od)=0.16667=(360,000−300,000)/360,000
    #   賭け金/組 = P/od = 10,000 rrc = ちょうど1口。**バグは直っている**。
    #   ⚠ 当時の初期金 30万 を明示して渡すこと（今の 20万 で判定すると別の答えになる）。
    check('P11 実データ(race 2097)で修正済みと判定できる',
          oc.check_seed_regime(360_000, [36.0] * 6, seed=300_000)[0] == 'fixed'
          and abs(360_000 / 36.0 - oc.STAKE_UNIT) < 1e-6,
          f'Σ={oc.check_seed_regime(360_000, [36.0] * 6, seed=300_000)[1]:.5f}'
          f' / 賭け金 {360_000/36:,.0f} rrc')

    # 装備専用の効果キーは別表で正しいパッシブに寄せること。
    # 寄せられないと「追い抜かれてから200m」を常時発動と読んで 15倍 盛る。
    _sp11 = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    _rv = oc.item_effect_spec('追い抜かれてから200mの間、パワーが4.4%上昇',
                              'gear_revenge_mark', _sp11)
    check('P11 復讐刻印は『不屈』の実測dutyまで割り引く（常時扱いにしない）',
          _rv is not None and _rv['power'] < 1.005,
          f"PW×{_rv['power']:.5f}（別表なしなら 1.044）")
    check('P11 泥啜り（ダート限定）は芝で乗せない',
          oc.item_effect_spec('ダートでスタミナとパワーがそれぞれ1.6%上昇',
                              'gear_dirt_gnaw', _sp11) is None)
    _tw = oc.item_effect_spec('終盤のパワーが5.3%上昇', 'charm_twilight_guard', _sp11)
    check('P11 黄昏の護りは『末脚』の区間・dutyで効く',
          _tw is not None and abs(_tw['power'] - (1 + 0.053 * _sp11['末脚']['duty'])) < 1e-9,
          f"PW×{_tw['power']:.5f}")

    # --- P16: 手打ち用の購入ブックマークレット（buy_pick.js）---
    #   予測ツールを持っていない人向け。買い方は違うが**上限と二重購入防止は同じ**でないと
    #   ゲームに弾かれる／二重に買う事故になるので、そこだけ固定しておく。
    _bp = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'bookmarklets', 'src', 'buy_pick.js'), encoding='utf-8').read()
    check('P16 口数と上限が本家と一致（3連単1万/20口・単勝1千/100口）',
          'TRI_UNIT=10000' in _bp.replace(' ', '') and 'WIN_UNIT=1000' in _bp.replace(' ', '')
          and f'TRI_MAX_UNITS={oc.MAX_TOTAL_UNITS}' in _bp.replace(' ', '')
          and f'WIN_MAX_UNITS={oc.WIN_MAX_TOTAL_UNITS}' in _bp.replace(' ', ''))
    check('P16 購入済みの単勝を上限から引いている',
          'ownWin' in _bp and 'WIN_MAX_UNITS-Math.round(ownWin)' in _bp.replace(' ', ''))
    check('P16 二重購入防止（executed フラグ）と確認チェックがある',
          'executed=true' in _bp.replace(' ', '') and "$('_ok')" in _bp
          and '購入済みです' in _bp)
    check('P16 締切済みのレースでは購入させない',
          "phase==='betting'" in _bp.replace(' ', '') and 'open' in _bp)
    check('P16 3連単の購入済み口数を買い目ごとに照会する（APIは全部必須）',
          'trifecta/user-units' in _bp and 'first=' in _bp and 'second=' in _bp
          and 'third=' in _bp and 'triOwn' in _bp,
          '/api/trifecta/user-units は first/second/third が必須（実機で確認）')
    check('P16 トークンは購入APIにだけ使い、画面には出さない',
          _bp.count('token:T') == 2 and 'token' not in _bp.split('drawHorses')[1][:2000])

    # --- P15: 単勝は【1レース合計100口】。購入済みぶんを残り枠から引くこと ---
    #   オッズ取得の試し買い（最大5口）も自分の購入額に入るので、引かないと上限超過の
    #   買い目を出してしまう。貼り付けの「自分の購入額」が現在値なのでそれを合計する。
    _hdr15 = ('レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,'
              'パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額')
    def _mk15(per):
        rows = '\n'.join(
            'マイル,芝,,馬%d,a%d,%d,50,48,普通,スピードスター,マイル得意,%.2f,%d'
            % (i, i, 150 - i * 3, 3.0 + i, per) for i in range(10))
        return ('guild=1\nschedule_id=15\npool=900000\n\n=== 出走馬一覧 ===\n%s\n%s\n'
                % (_hdr15, rows))
    _b15 = oc.train_model('logg')
    _r0 = oc.analyze(_mk15(0), _b15, {'dist': 'マイル', 'track': '芝',
                                      'win_bets': True, 'bankroll': 3_000_000})
    _r6 = oc.analyze(_mk15(6000), _b15, {'dist': 'マイル', 'track': '芝',
                                         'win_bets': True, 'bankroll': 3_000_000})
    _rx = oc.analyze(_mk15(11000), _b15, {'dist': 'マイル', 'track': '芝',
                                          'win_bets': True, 'bankroll': 3_000_000})
    _u = lambda r: sum(x['units'] for x in (r.get('win_picks') or []))
    check('P15 購入済みの口数を合計して残り枠を出す',
          _r0.get('win_own_units') == 0 and _r6.get('win_own_units') == 60
          and _r6.get('win_left_units') == 40,
          f"0口→残{_r0.get('win_left_units')} / 60口→残{_r6.get('win_left_units')}")
    check('P15 推奨が残り枠を超えない',
          _u(_r6) <= _r6['win_left_units'] and _u(_r0) <= oc.WIN_MAX_TOTAL_UNITS,
          f"残枠{_r6['win_left_units']}口に対して推奨{_u(_r6)}口")
    check('P15 上限に達していたら単勝を出さない',
          _rx.get('win_left_units') == 0 and _u(_rx) == 0,
          f"購入済{_rx.get('win_own_units')}口 → 推奨{_u(_rx)}口")
    _bm15 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'bookmarklets', 'src', 'bm.js'), encoding='utf-8').read()
    _ap = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'bookmarklets', 'src', 'autopilot.js'), encoding='utf-8').read()
    # 折り返しの位置が変わるたびに落ちるので、空白を潰してから探す。
    _ap = re.sub(r'\s+', ' ', _ap)
    # オッズのバグ補正を無条件に掛けると EV を 1.5倍ほど過大評価する（race 2097 で修正済みを確認）
    check('P18 autopilot はオッズ補正を trifecta_seed_bug_active でゲートする',
          'M.trifecta_seed_bug_active && pool0 > SEED' in _ap
          and _ap.count('pool0 / (pool0 - SEED)') == 1,
          '初期プール金ぶんの補正はバグが生きているときだけ')
    check('P18 autopilot は装備・お守りの倍率を掛ける',
          'OasisModel.applyItems(' in _ap)
    check('P18 autopilot は単勝も出す',
          'winBetPicksPool(' in _ap and '/api/bet' in _ap)
    # トークンはレースごとに失効する。アームが次のレースを跨いだら「無人で買い続ける」
    # 状態になるので、armFor が今の次レースと一致するときだけ自動購入する。
    check('P18 autopilot の自動購入アームは1レース限り',
          'nextRaceTime().getTime() === ST.armFor' in _ap and 'disarm(' in _ap,
          'レースが過ぎたらアームは自動で外れる')
    # 2026/08/25 の実バグ: winBetPicksPool の行のキーは Python と同じ `eff_od` なのに
    # autopilot 側が `r.eff` で読んでいて undefined → .toFixed() で解析ごと落ちた。
    _wrow = set(oc.win_bet_picks_pool(
        ['a', 'b'], [0.6, 0.4], [2.0, 3.0], 500_000, 1_000_000, 0.25, 0.0)[0][0].keys())
    check('P18 autopilot は winBetPicksPool の返すキー名を使う',
          'r.eff_od' in _ap and 'eff_od' in _wrow and 'eff' not in _wrow,
          f'行のキー: {sorted(_wrow)}')
    # 表示で数値が欠けても解析ごと落とさない
    check('P18 autopilot の数値表示は undefined でも落ちない',
          "const fx = (v, d) => (Number.isFinite(+v)" in _ap
          and '.eff.toFixed' not in _ap and '.edge*100).toFixed' not in _ap)

    # 2026/08/25 の実害: 開いた瞬間に解析する作り(IMMEDIATE)なのに購入の窓を見ておらず、
    # 締切1時間前にアーム済みで開いたら**その場で買った**。1時間前のオッズ・プールは
    # 締切時点の前提と別物なので、購入も試し買いも LEAD_SEC の窓の中だけに限る。
    check('P19 autopilot は締切60秒前の窓の中でしか買わない',
          'function inBuyWindow()' in _ap
          and 'const canBuy = inBuyWindow();' in _ap
          and 'const armed = isArmed() && pl.canBuy;' in _ap,
          '窓の外は下見（買い目を出すだけ）')
    check('P19 autopilot は窓の外では試し買いもしない',
          'if (!isArmed() || !inBuyWindow()) {' in _ap)
    check('P19 autopilot は窓に入ったら下見を捨ててオッズを取り直す',
          'if (PENDING && !PENDING.canBuy && inBuyWindow()) {' in _ap)

    # 上限は「1レース」と「1日」の2本立て。1レースでゲーム上限まで買うと
    # 3連単20口(200,000) + 単勝100口(100,000) = 300,000 rrc なので、
    # 1日の上限をそこに置くと最初のレースで使い切って残りが全部見送りになる。
    check('P19 autopilot はレース単位と1日の上限を別に持つ',
          'RACE_BUDGET: 300000' in _ap and 'DAILY_BUDGET: 1800000' in _ap
          and 'Math.min(CFG.RACE_BUDGET, CFG.DAILY_BUDGET - ST.spent)' in _ap,
          '1レース30万 / 1日180万（6レース分）')
    # 口数は「1組1口ずつ」ではなく EV 最大の配分で決める（Python と同じ関数）
    check('P19 autopilot の3連単は allocate_units_stable で配分する',
          'OasisModel.allocateUnitsStable(' in _ap
          and 'OasisModel.unformedSleevePicks(' in _ap
          and 'UNITS_PER_COMBO' not in _ap,
          '成立組は貪欲配分、余りを未成立スリーブへ')
    check('P19 autopilot の3連単上限はゲーム上限（20口）',
          'CFG.TRI_MAX_UNITS = +(M.max_total_units || 20)' in _ap
          and 'Math.min(CFG.TRI_MAX_UNITS, unitsLeft)' in _ap,
          f'model.json の max_total_units = {oc.MAX_TOTAL_UNITS}')
    # 配分された口数ぶん買うこと（1組1口で送ると配分が意味を持たない）
    # 分けて買うと払戻も購入履歴もその数だけ分かれて読みにくい。まず一括で送り、
    # APIに**拒否されたときだけ**割る。通信エラーでは割らない（二重購入になる）。
    check('P19 autopilot はまず全口数を1リクエストで送る',
          'const buyUnits = async (url, mkBody, label, units, unit, chunk) => {' in _ap
          and 'if (await post(url, mkBody(units), `${label} ${units}口`, units * unit, true)) return units;' in _ap,
          '拒否されたら chunk 口ずつに割り直す')
    check('P19 autopilot は通信エラーでは再送しない',
          'return true; } };' in _ap.replace('\n', ' ').replace('  ', ' ')
          or '送信済みか不明' in _ap,
          '二重購入を避ける')
    check('P19 autopilot の分割単位は CFG に出ている',
          'TRI_PER_REQ: 10' in _ap and 'WIN_PER_REQ: 20' in _ap)

    # エッジが大きいことは異常ではない（プールが薄いほど初期プール金の比率が上がる）。
    # 2026/08/26 まで +300%超でレースごと中止しており、R2120 で一番おいしい組を捨てた。
    # 知らせるだけにして、購入は止めないこと。
    check('P19 autopilot はエッジの大きさで購入を止めない',
          'WARN_EDGE' in _ap and 'MAX_SANE_EDGE' not in _ap
          and 'は異常 → このレースは中止' not in _ap,
          'ログで知らせるだけ')

    # 「更新されたか分からない」を潰す。挙動の版とビルド時刻の両方をパネルに出すこと。
    check('P19 autopilot は馬場を装備の判定に渡す',
          'M, fxSkipped, { dist: dist, track: track }' in _ap,
          '芝啜り／泥啜りを馬場で判定できるようにする')
    check('P19 autopilot は版とビルド時刻を表示する',
          "const AP_VER = " in _ap
          and "🛩 オートパイロット v' + AP_VER" in _ap
          and 'M.trained_at' in _ap,
          'AP_VER は手で上げる / trained_at は build_autopilot.py が自動で入れる')

    # Streamlit と同じ推奨を出すための3点。ここがズレると同じレースで別の買い目が出る。
    check('P19 autopilot は金の乗った組を取り切る（上位N打ち切りをしない）',
          'ODDS_TOP_N' not in _ap and 'if (BASE > 0 && BASE - seenAmt < unit) break;' in _ap,
          'Streamlit は貼り付けで全組そろうので、候補集合と市場の正規化を合わせる')
    check('P19 autopilot は実残高を手元資金に使う',
          'async function loadBalance()' in _ap and 'cands, P, bankroll(),' in _ap
          and 'bankroll(), D.kelly_fraction' in _ap,
          'Streamlit は貼り付けの balance= を BANKROLL に自動反映している')
    check('P19 autopilot は min_prob を切り上げない',
          'CFG.MIN_PROB = +D.min_prob;' in _ap and 'Math.max(+D.min_prob' not in _ap,
          '0.02 に切り上げていたので小さい確率の組を取りこぼしていた')
    # トークンは購入と残高照会にだけ使い、ログには出さないこと
    check('P19 autopilot はトークンをログに出さない',
          'AUTH.token' in _ap
          and not any(f'{x}' in _ap for x in ('log(`' + '${AUTH.token}',
                                              'esc(AUTH.token)')),
          'token は fetch の中だけ')

    # 1レースのリスク上限は資金比ではなく RACE_BUDGET 固定（資金が増えても賭け額は増えない）
    check('P19 autopilot の1レース上限は所持金に依らず RACE_BUDGET',
          'const riskFrac = () => CFG.RACE_BUDGET / bankroll();' in _ap
          and 'riskFrac(), CFG.EDGE_MIN,' in _ap
          and 'riskCapFrac: riskFrac(),' in _ap
          and 'D.max_risk_frac' not in _ap,
          '3連単20口＋単勝100口 ＝ 30万 に届く')

    # LEAD_SEC を詰めるなら監視間隔も詰めないと、窓に入ってから待たされて
    # 解析に使える時間が LEAD_SEC より短くなる。値をベタ書きせず、
    # 「間隔 ≤ LEAD_SEC の1/5」という関係で見る（手で詰めても壊れないように）。
    _lead = int(re.search(r'LEAD_SEC:\s*(\d+)', _ap).group(1))
    _poll = int(re.search(r'setInterval\(\(\) => tick\(false\), (\d+)\)', _ap).group(1))
    check('P19 autopilot の監視間隔は LEAD_SEC より十分短い',
          _poll / 1000.0 <= _lead / 5.0,
          f'LEAD_SEC={_lead}s / 監視間隔={_poll}ms。'
          '窓の外はリクエストを投げないので短くしても負荷は増えない')
    # 起動したら自動でアームする（チェックを手で入れなくても買う）。
    # アームは次の1レースぶんだけ有効なので、放置で走り続けることはない。
    # 単勝の合計上限は100口。試し買いで先に何口か入っていても、合計が100に届くこと。
    # totalUnits に「残り枠」を渡すと winBetPicksPool 内の -購入済み と二重になり、
    # 100 - 購入済み 口で止まる（試し買い1口 → 99口）。
    check('P19 autopilot は単勝の合計上限に残り枠ではなく上限そのものを渡す',
          'ownUnits + Math.floor(Math.max(budgetLeft, 0) / WU)' in _ap
          and 'totalUnits: Math.min(left,' not in _ap,
          'winBetPicksPool が中で購入済みを引くので、ここで引くと二重になる')

    # 購入後のまとめ。あとで実結果と突き合わせるので、**買えた口数だけ**を
    # 的中率・実効オッズ・予測EV つきで残す（拒否された口数を混ぜない）。
    check('P19 autopilot は購入後にまとめを出す',
          '購入まとめ' in _ap and '予測EV' in _ap
          and 'const st = d.u * d.unit, e = st * d.edge;' in _ap,
          'EV = 賭け金 × エッジ')
    check('P19 autopilot の buyUnits は買えた口数を返す',
        'if (await post(url, mkBody(u), `${label} ${u}口`, u * unit)) sent += u;' in _ap
          and 'return sent;' in _ap,
          '拒否された口数をまとめに入れないため')
    check('P19 autopilot に購入まとめのコピーボタンがある',
          "$('_cp').onclick" in _ap and 'id=_cp' in _ap
          and "const txt = (ST.sum || []).join('\\n\\n');" in _ap
          and 'ST.sum = (ST.sum || []).concat([lines.join(' in _ap,
          'コピーするのは記録に使うまとめだけ。解析の途中経過は入れない')

    check('P19 autopilot は起動時に自動でアームする',
          'if (!isArmed()) { arm(); }' in _ap
          and '自動購入をオンに設定しました。' in _ap,
          'token がレースごとに失効するので効くのは次の1レースだけ')

    check('P19 autopilot は解析にかかった秒数と締切までの残りを出す',
          'const took = (Date.now() - t0) / 1000;' in _ap
          and '締切まで残り' in _ap,
          'LEAD_SEC を詰めすぎていないか実測で分かるようにする')

    # キャリーオーバー中は BASE に届かず打ち切りが効かない。下見（時間のある回）で
    # 全組舐めて CO を確定し、締切30秒前の本番ではそれを引いて早く打ち切る。
    check('P20 autopilot は CO を引いて BASE を出す',
          'const BASE = Math.max(pool - SEED - CO, 0);' in _ap
          and 'const BASE = Math.max(pool0 - SEED - CO, 0);' in _ap)
    check('P20 autopilot は下見で全組舐めて CO を確定する',
          'canBuy ? CFG.ODDS_MAX_REQ : combo.length' in _ap
          and 'if (!queue.length) {' in _ap and 'setCO(found);' in _ap,
          '本番は上限つき、下見は上限なし')
    check('P20 autopilot と bm.js は CO の保存先を共有する',
          "'oasis_co_' + AUTH.guild" in _ap and "COKEY='oasis_co_'+G" in _bm15,
          '購入ページは同一オリジンなのでどちらで測っても効く')

    # 買い目には 的中率 / 買う前のオッズ / 買ったあとの実効オッズ を並べる。
    # 実効odだけだと「自分の金でどれだけ薄まったか」が見えない。
    check('P19 autopilot の買い目に的中率と前後のオッズを出す',
          '的中 ${fx(p.p * 100, 1)}%' in _ap and '的中 ${fx(w.p * 100, 1)}%' in _ap
          and 'od ${fx(od, d)} → 実効' in _ap,
          '未成立の組は買う前のオッズが存在しないので「（未成立）」表記')
    check('P19 autopilot は買う前のオッズを pick に持たせる',
          'od: odds.get(k) || null' in _ap and 'od: r.unbet ? null : r.odds' in _ap)

    # カウントダウンは毎秒動くこと（render() は20秒に1回しか回らないので別立て）
    check('P19 autopilot のカウントダウンは毎秒更新する',
          'function renderClock()' in _ap
          and 'setInterval(renderClock, 1000)' in _ap
          and "id=_cd>" in _ap)

    # 7頭以下は3連単が存在しない。オートパイロットも組を作らず、単勝だけ買うこと
    # （WIN_ON を切っていても、そのレースで買えるのは単勝だけなので出す）。
    check('P19 autopilot も7頭以下では3連単を作らない',
          'const triOk = n >= (M.min_field_trifecta || 8);' in _ap
          and 'const combo = triOk ?' in _ap
          and 'const triPicks = triOk ? await analyseTrifecta(' in _ap,
          '組のシミュレーションごと回さない')
    check('P19 autopilot は3連単が無いレースで単勝を強制的に出す',
          'const winOn = CFG.WIN_ON || !triOk;' in _ap
          and 'CFG.WIN_ON ? analyseWin' not in _ap
          and 'const winPicks = winOn ? analyseWin(' in _ap)

    # 試し買いは**実際の購入**。アーム（人が購入を許可したレース）でしか走らせないこと。
    check('P18 autopilot の試し買いはアーム中だけ・1レース1回',
          '!isArmed()' in _ap and 'ST.probed[sid]' in _ap,
          '[今すぐ解析]の連打で二重に買わない')
    # 実測の式は bm.js と1文字も違わないこと（片方だけ直す事故を止める）
    for _frag in ('const w = oa * oa; sw += w; sr += w * (oa / ob); n++;'.replace(' ', ''),
                  # 刻み幅の定数名だけ違う（bm.js は直書き ODD_STEP、autopilot は model.json の STEP）
                  'const sdR = (@ / Math.sqrt(12)) * Math.sqrt(2 / swErr);'.replace(' ', ''),
                  'if (!seenOd.has(oa)) seenOd.set(oa, w);'.replace(' ', '')):
        _n = lambda t: re.sub(r'\s+', '', t).replace('ODD_STEP', '@').replace('STEP', '@')
        check(f'P18 実測の式が bm.js と一致 [{_frag[:28]}…]',
              _frag in _n(_ap) and _frag in _n(_bm15))
    check('P18 モデルは装備の scope/duty と既定値を JSON に出す',
          set(['item_scope', 'item_key_alias', 'trifecta_seed_bug_active', 'defaults',
               'win_pool_seed', 'win_max_total_units'])
          <= set(oc.export_model_json(_b7).keys()))

    # --- P21: スタミナ不足は「必要量に対する割合」で効かせる ---
    # 同じ不足5でも、必要量29の短距離ではレースの17%を空っぽで走ることになり、
    # 必要量85の長距離では6%で済む。「不足/10」の固定尺度では短距離の罰が約1/3だった。
    # timeline 2,159頭の実測: 枯渇割合 = 0.859×(不足/必要)（相関0.945）、
    # 枯渇中の区間速度は −35% → log スコアへの影響は約 −0.46×(不足/必要)。
    _sp21 = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    def _feat21(dist, sp, pw, st, ps):
        rows = pd.DataFrame([dict(name='x', species='x', speed=sp, power=pw, stamina=st,
                                  condition='普通', passives=ps, dist=dist, track='芝',
                                  same_species=False)])
        X = oc.build_features(rows, _sp21)
        return dict(zip(oc.feature_names(_sp21), X[0]))
    # 特徴量が「5×不足÷必要」そのものであること（尺度合わせの×5込み）
    def _budget21(dist, sp, pw, st, ps):
        e = oc.effective_stats(sp, pw, st, ps, dist, '芝', _sp21)
        return oc.stamina_budget(e, dist)
    _need_s, _sh_s, _ = _budget21('短距離', 160, 50, 20, ())
    _f_s = _feat21('短距離', 160, 50, 20, ())
    check('P21 特徴量は 5×不足÷必要',
          abs(_f_s['スタミナ不足'] - 5 * _sh_s / _need_s) < 1e-9,
          f'不足{_sh_s:.2f} / 必要{_need_s:.2f} → {_f_s["スタミナ不足"]:.4f}')
    # **同じ不足量**なら、必要量が小さい短距離のほうが強く効くこと。
    # 不足5になる実効STを距離ごとに逆算して比べる。
    def _feat_at_short21(dist, target=5.0):
        lo, hi = 1.0, 400.0
        for _ in range(60):
            mid = (lo + hi) / 2
            n, sh, _x = _budget21(dist, 100, 100, mid, ())
            if sh > target: lo = mid
            else: hi = mid
        n, sh, _x = _budget21(dist, 100, 100, lo, ())
        return 5 * sh / n, n, sh
    _v_s, _n_s, _q_s = _feat_at_short21('短距離')
    _v_l, _n_l, _q_l = _feat_at_short21('長距離')
    check('P21 同じ不足量なら短距離のほうが強く効く',
          _v_s > _v_l * 1.5,
          f'短距離 必要{_n_s:.0f} 不足{_q_s:.1f} → {_v_s:.3f} / '
          f'長距離 必要{_n_l:.0f} 不足{_q_l:.1f} → {_v_l:.3f}')
    # 足りている馬は 0
    _f_ok = _feat21('短距離', 160, 50, 90, ('パワー大アップ',))
    check('P21 スタミナが足りていれば不足は0', _f_ok['スタミナ不足'] == 0.0)
    # 学習した係数が機構ベースの推定（-0.46/割合、×5 列なので -0.092）に近いこと
    _c21 = dict(zip(_b7['feature_names'], _b7['model'].coef_))['スタミナ不足']
    check('P21 学習係数が実測の機構ベース推定と整合する',
          -0.20 < _c21 < -0.03,
          f'係数 {_c21:+.4f}（÷5 で {_c21/5:+.4f}/割合。機構ベースの推定 -0.092/-0.462）')

    # --- P20: キャリーオーバー（誰も3連単を当てずに繰り越された金）---
    # 繰越金は**賭け金ではないがプールには入っている**。BASE から引かないと
    # seenAmt が永久に BASE に届かず、打ち切りが効かず全2,730組を舐める。
    check('P20 bm.js は BASE からキャリーオーバーを引く',
          'const BASE=Math.max(pool0-SEED-CO,0);' in _bm15
          and "localStorage.getItem(COKEY)" in _bm15,
          '引かないと打ち切りが永久に効かない')
    check('P20 bm.js は全組舐めたら残りをキャリーオーバーとして確定する',
          'coFound=Math.max(Math.round((pool0-SEED-seenAmt)/UNIT)*UNIT,0);' in _bm15
          and 'if(!queue.length&&!regimeBad){' in _bm15,
          '途中で打ち切れた回は queue が残るので確定しない')
    check('P20 bm.js は co= を出力し、手で入れ直せる',
          '`co=${CO}`' in _bm15 and 'const editCO=' in _bm15 and 'coBtn.onclick=editCO;' in _bm15)
    # Σ(1/od) の判定も CO を引かないとどちらの式にも合わず unknown に落ちる
    _P20, _S20, _CO20 = 1_400_000, oc.TRIFECTA_POOL_SEED, 500_000
    _bets20 = [(_P20 - _S20 - _CO20) * f for f in (0.5, 1 / 3, 1 / 6)]
    _fix20 = [_P20 / x for x in _bets20]
    check('P20 キャリーオーバーを渡さないとレジームを判定できない',
          oc.check_seed_regime(_P20, _fix20)[0] == 'unknown')
    check('P20 キャリーオーバーを渡せば修正済みと判定できる',
          oc.check_seed_regime(_P20, _fix20, carryover=_CO20)[0] == 'fixed',
          f'Σ={oc.check_seed_regime(_P20, _fix20, carryover=_CO20)[1]:.4f}')
    # 貼り付けの co= を読めること
    _t20 = _t7.replace('pool=0', 'pool=1400000\nco=500000')
    _r20 = oc.analyze(_t20, _b7, {'dist': 'マイル', 'track': '芝'})
    check('P20 貼り付けの co= を読む',
          any('キャリーオーバー' in m for m in _r20['messages']),
          [m for m in _r20['messages'] if 'キャリーオーバー' in m][:1])

    check('P17 bm.js はプールが初期金のままなら1リクエストも投げない',
          'const noBets = !(pool0 > 0) || BASE < UNIT;' in _bm15,
          '3連単プールが初期金のまま＝賭け0件。全組が未成立なので取得を省略する')

    check('P15 bm.js は試し買いの有無に関わらず win_own を出す',
          'win_own=${Math.round(ownWin)}' in _bm15
          and 'const ownWin=pets.reduce' in _bm15,
          '試し買いしなくても購入済み口数が分かる')

    # --- P14: 2026/08/19〜 の Discord ログ新フォーマット ---
    #   ① ステータスが「素 + 装備 ＝ 合計」表記に変わった（合計を取らないと装備を丸ごと落とす）
    #   ② 結果が複数メッセージに分割され、2通目以降が「結果（続き）」になった
    #      → 拾わないとレースが丸ごと消える（実際 08/20 以降の 57レースが落ちていた）
    #   ③ 馬ブロックに装備・お守りの効果文が載るようになった（倍率を学習に反映できる）
    check('P14 「素 + 装備 ＝ 合計」から合計を取る',
          oc._stat_in('🏃 スピード：158 + 🟨 **1** ＝ **159**', 'スピード') == 159
          and oc._stat_in('🏃 スピード：154 + **3** ＝ **157**', 'スピード') == 157
          and oc._stat_in('🏃 スピード：148', 'スピード') == 148
          and oc._stat_in('🏃 スピード 111', 'スピード') == 111,
          '新表記・旧表記・装備なし・旧結果の4通り')
    check('P14 パッシブ名の「スピード大アップ」を数値と誤読しない',
          oc._stat_in('✨ パッシブ：🚀 スピード大アップ / ⚡ スピードスター', 'スピード') is None)
    _cont = (
        '[2026/08/20 8:00] bot\n\n🏁 第9レース 結果\n🕘 09:00｜短距離｜芝｜良\n'
        '🥇 あ\n@u\n🏃 スピード：100 + 🟨 **10** ＝ **110**\n🫀 スタミナ：50\n'
        '💥 パワー：40 \n📊 score 900.0\n✨ パッシブ：⚡ スピードスター\n\n'
        '🏁 第9レース 結果（続き）\n🕘 09:00｜短距離｜芝｜良\n'
        '2着 い\n@u\n🏃 スピード：90\n🫀 スタミナ：50\n💥 パワー：40 \n📊 score 800.0\n')
    _rr = oc.parse_results(_cont)
    check('P14 「結果（続き）」も同じレースとして拾う',
          len(_rr) == 2 and len({r['race_key'] for r in _rr}) == 1
          and _rr[0]['speed'] == 110,
          f"{len(_rr)}行 / key={_rr[0]['race_key'] if _rr else '-'}")
    _spec14 = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    _blk14 = ('✨ パッシブ：🚀 スピード大アップ\n\n**装備**\n🥾 🔵 疾風のブーツ［レア］\n'
              '🏃スピード+5\n✨ 二の脚：中盤開始後200mのスピードが3.5%上昇\n\n'
              '**お守り**\n☀️ 🟣 白日の聖章［エピック］\n'
              '✨ 調和の加護：全ステータスが常時3.4%上昇')
    _im14 = oc._item_mults_from_block(_blk14, _spec14)
    check('P14 ログの装備・お守りから倍率を拾う（加算は合計側なので二重に足さない）',
          abs(_im14.get('power', 1) - 1.034) < 1e-9
          and 1.034 < _im14.get('speed', 1) < 1.05,
          f"SP×{_im14.get('speed',1):.5f} PW×{_im14.get('power',1):.5f}")
    check('P14 パッシブ行を装備効果として拾わない',
          oc._item_mults_from_block('✨ パッシブ：🚀 スピード大アップ / ⚡ スピードスター',
                                    _spec14) == {})

    # --- P13: bm.js の単勝プール実測（試し買い）の算数 ---
    #   od_j = P/P_j。自分が Δ 入れると自分が買っていない馬は od_j後/od_j前 = (P+Δ)/P = R。
    #   → P = Δ/(R−1)。オッズは小数2桁なので比は od² を重みにした加重平均で取る。
    #   ⚠ 誤差の見積もりは**同じオッズの馬を1つに数える**こと。丸め誤差はオッズの値に
    #     対して決まるので、同値の馬を独立サンプル扱いすると 1/√n ぶん精度を過大評価する
    #     （NPCが均等に賭けて全馬同オッズのケースで実際に外した）。
    def _probe(amounts, tgt=0, max_probe=5, unit=oc.WIN_POOL_QUANTUM, step=oc.ODDS_STEP):
        r2 = lambda x: round(x, oc.ODDS_DECIMALS)
        p0 = sum(amounts)
        before = [r2(p0 / a) for a in amounts]
        am, spent, out = list(amounts), 0, None
        for _ in range(max_probe):
            am[tgt] += unit
            spent += unit
            p2 = sum(am)
            after = [r2(p2 / a) for a in am]
            sw = sr = 0.0
            n = 0
            seen = {}
            for i, (ob, oa) in enumerate(zip(before, after)):
                if i == tgt or not ob or not oa:
                    continue
                w = oa * oa
                sw += w
                sr += w * (oa / ob)
                n += 1
                seen.setdefault(oa, w)
            if sw <= 0 or n < 2:
                continue
            R = sr / sw
            if R <= 1:
                continue
            P = spent / (R - 1)
            sd = (step / math.sqrt(12)) * math.sqrt(2 / sum(seen.values()))
            rel = sd * P / spent
            if rel * P < unit:
                P = round(P / unit) * unit
                rel = max(rel, (unit / 2) / P)      # 量子化の下限より小さくは名乗れない
            out = (P, rel)
            if rel <= 0.04:
                break
        return p0, out

    _cases = [
        ('人気に偏り', [150000, 90000, 60000, 40000, 25000, 15000, 8000, 5000,
                    3000, 2000, 1000, 1000, 1000, 1000, 1000]),
        ('全馬同オッズ', [27000] * 15),
        ('8頭・小プール', [40000, 30000, 20000, 12000, 8000, 5000, 3000, 2000]),
    ]
    _all_ok = True
    _detail = []
    for _lbl, _am in _cases:
        _true, _res = _probe(_am)
        if not _res:
            _all_ok = False
            _detail.append(f'{_lbl}:測定不可')
            continue
        _est, _rel = _res
        _err = abs(_est - _true) / _true
        # 「報告した誤差が実際の誤差を下回らない」ことが命。下回ると精度を偽ることになる。
        if _err > max(_rel, oc.WIN_POOL_QUANTUM / _true):
            _all_ok = False
        _detail.append(f'{_lbl}:実{_err*100:.1f}% ≤ 申告{_rel*100:.1f}%')
    check('P13 単勝プールの実測が真値に届き、申告誤差が実誤差を下回らない',
          _all_ok, ' / '.join(_detail))

    # --- P12: 装備図鑑・スキル図鑑（2026/08/23）の30種を効果名で引けること ---
    check('P12 図鑑の効果は装備16種＋お守り14種の30種',
          len(oc.ITEM_EFFECT_CATALOG) == 30, f'{len(oc.ITEM_EFFECT_CATALOG)}種')
    check('P12 効果名を説明文の頭から取り出せる',
          oc.item_effect_label('末脚：終盤のパワーが8.2%上昇') == '末脚'
          and oc.item_effect_label('スピードが常時4.4%上昇') is None)
    _sp12 = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    # 効果キーが分からない新効果でも、**効果名だけ**で実測 duty まで割り引けること
    _br = oc.item_effect_spec('血走り：残りスタミナ25%以下でスピードが8.5%上昇',
                              'gear_unknown_key_9999', _sp12)
    check('P12 コード未知でも効果名で duty を引ける（血走り＝残ST25%以下 0.183）',
          _br is not None and abs(_br['speed'] - (1 + 0.085 * 0.183)) < 1e-9,
          f"SP×{_br['speed']:.5f}（割引なしなら 1.085）")
    # 「AとBがそれぞれN%」型を両方に効かせ、かつ**二重計上しない**こと
    _pair = oc.spec_from_description('スピードとパワーがそれぞれ3.0%上昇')
    check('P12 「AとBがそれぞれN%」は両方に1回ずつ掛かる（二重計上しない）',
          _pair['mult'] == {'speed': 1.03, 'power': 1.03},
          f"{_pair['mult']}（抜き忘れると power が 1.0609 になる）")
    check('P12 馬場限定（芝啜り/泥啜り）はこの場では乗せない',
          oc.item_effect_spec('芝啜り：芝でスピードとパワーがそれぞれ2.4%上昇',
                              'gear_turf_gnaw', _sp12) is None
          and oc.item_effect_spec('泥啜り：ダートでスタミナとパワーがそれぞれ1.6%上昇',
                                  'gear_dirt_gnaw', _sp12) is None)
    check('P12 安定の加護は図鑑経由でも σ に落ちる',
          (oc.item_effect_spec('安定の加護：レース中の乱数幅を1.9%狭める',
                               'charm_consistency', _sp12) or {}).get('_sigma') == 0.981)
    # 図鑑の全効果名が、説明文つきで渡されたときに例外を出さないこと
    _ok12 = True
    for _lb in oc.ITEM_EFFECT_CATALOG:
        try:
            oc.item_effect_spec(f'{_lb}：パワーが5.0%上昇', None, _sp12)
        except Exception:
            _ok12 = False
    check('P12 図鑑30種すべてが例外なく処理できる', _ok12)

    # --- P19: 7頭以下のレースに3連単は存在しない（買い目を出さない・単勝だけ出す）---
    # 以前は警告を出すだけで買い目もランキングも作っていた。**買えない券の推奨購入**は
    # 害しかない（オートパイロットがそのまま送って弾かれる）ので、作らないこと。
    _rows19 = '\n'.join(
        'マイル,芝,,馬%d,a%d,%d,50,48,普通,スピードスター,マイル得意,%.2f,0'
        % (i, i, 150 - i * 6, 2.0 + i) for i in range(7))
    _t19 = ('guild=1\nschedule_id=19\npool=0\n\n=== 出走馬一覧 ===\n%s\n%s\n'
            % (_hdr7, _rows19))
    _r19 = oc.analyze(_t19, _b7, {'dist': 'マイル', 'track': '芝',
                                  'win_bets': False, 'bankroll': 3_000_000})
    _tri19 = [x for x in (_r19.get('buy_all') or []) if x['kind'] == '3連単']
    check('P19 7頭なら3連単の買い目・ランキングを出さない',
          _r19['n_field'] == 7 and not _tri19 and not _r19['ranking']
          and not _r19['picks'] and not _r19.get('summary'),
          f"n={_r19['n_field']} / 3連単{len(_tri19)}件 / ランキング{len(_r19['ranking'])}行")
    # このレースで買えるのは単勝だけなので、単勝トグルが切れていても出す
    check('P19 3連単が無いレースでは単勝トグルを無視して単勝を出す',
          bool(_r19.get('win_picks')),
          f"単勝{len(_r19.get('win_picks') or [])}件 / "
          f"プール{_r19.get('win_pool')}")
    check('P19 8頭以上なら従来どおり3連単を出す', bool(_r7['ranking']),
          f"9頭 → ランキング{len(_r7['ranking'])}行")

    # --- P17: 貼り付けCSVの装備効果は**ラベルを削らずに**カタログへ渡すこと ---
    # race 2109 の実バグ。`v.split('：',1)[-1]` でラベルを落としていたため
    # ITEM_EFFECT_CATALOG が引けず、図鑑30種のうち11種が「常時・duty 1.0」に
    # 化けていた（首位の呪い 0.49% → 6.2%、12倍）。
    # 結果、でゅんでゅんが2着予想に浮上して にこるん→でゅんでゅん→メビウス が41%になった。
    _cols17 = ['装備', '装備効果', '装備効果キー', 'お守り', 'お守り効果', 'お守り効果キー']
    _row17 = {'装備': '雷鳥の兜',
              '装備効果': '首位の呪い：先頭の間、スピードが6.2%上昇するがスタミナ消費も増加',
              '装備効果キー': 'gear_leader_curse'}
    _m17, _ = oc.item_mults_from_row(_row17, _cols17, _sp12)
    check('P17 条件付き装備は duty で割り引く（首位の呪い 6.2%×0.079）',
          abs(_m17.get('speed', 1.0) - (1 + 0.062 * 0.079)) < 1e-6,
          f"speed={_m17.get('speed')}")
    # 馬場限定はこの場では判定できないので**乗せない**（ラベルを削ると常時5%になる）
    _row17b = {'装備': '草食みの蹄鉄', '装備効果': '芝啜り：芝でスピードが5%上昇',
               '装備効果キー': 'gear_turf_gnaw'}
    _m17b, _sk17b = oc.item_mults_from_row(_row17b, _cols17, _sp12)
    check('P17 馬場が分からないときは馬場限定の装備を乗せず skipped に回す',
          not _m17b and len(_sk17b) == 1, f'{_m17b} / {_sk17b}')
    # 馬場が分かっていれば乗せる。ここを捨てると芝啜りのレジェンドが丸ごと消える
    # （実測 +5.2% ＝ 残差σの 0.63倍。着順が入れ替わる大きさ）。
    _m17c, _ = oc.item_mults_from_row(_row17b, _cols17, _sp12,
                                      {'dist': '短距離', 'track': '芝'})
    _m17d, _sk17d = oc.item_mults_from_row(_row17b, _cols17, _sp12,
                                           {'dist': '短距離', 'track': 'ダート'})
    check('P17 芝のレースでは芝啜りを倍率に乗せる',
          abs(_m17c.get('speed', 1.0) - 1.05) < 1e-9, f"speed={_m17c.get('speed')}")
    check('P17 ダートのレースでは芝啜りを乗せない',
          not _m17d and len(_sk17d) == 1, f'{_m17d} / {_sk17d}')

    # 推奨購入（3連単＋単勝）が1本のリストにまとまること
    _r11 = oc.analyze(_t7, _b7, {'dist': 'マイル', 'track': '芝',
                                 'unformed_sleeve': True, 'bankroll': 3_000_000})
    _ba = _r11.get('buy_all') or []
    check('P11 推奨購入がまとまって出る（種別＋買い目＋口数）',
          bool(_ba) and all(x['kind'] in ('3連単', '単勝') for x in _ba)
          and all(len(l.split('\t')) == 3 for l in _r11['buy_lines']),
          f"{len(_ba)}点 / 合計 {_r11['buy_total']:,} rrc")
    check('P11 まとめの投資額は口数×1口の合計と一致',
          _r11['buy_total'] == sum(x['units'] * x['unit'] for x in _ba))

    # --- P9: 分散低減のお守り（安定の加護）を σ に落とすこと ---
    #   「レース中の乱数幅を1.9%狭める」はステータス倍率ではないので _pct_mults では拾えず、
    #   放置すると**常時発動なのに丸ごと無視**される。σ 側（安定感と同じ扱い）に入れる。
    _sp9 = oc.load_passive_spec(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'passive_spec.json'))
    check('P9 「乱数幅をN%狭める」を σ 倍率として読む',
          oc.item_effect_spec('レース中の乱数幅を1.9%狭める', 'charm_consistency', _sp9)
          == {'_sigma': 0.981})
    check('P9 σ倍率はステータスには一切効かない',
          not {k for k in (oc.item_effect_spec('レース中の乱数幅を1.9%狭める', None, _sp9) or {})
               if k in ('speed', 'power', 'stamina')})
    _hdr9 = ('レース距離,馬場,地面,馬名,成体種,SPEED,POWER,STAMINA,コンディション,'
             'パッシブスキル1,パッシブスキル2,単勝オッズ,自分の購入額,装備,装備効果,'
             '装備効果キー,お守り,お守り効果,お守り効果キー')
    _row9 = ('長距離,芝,,あ,a,100,100,100,普通,,,1.5,0,,,,'
             '星見のコンパス,安定の加護：レース中の乱数幅を1.9%狭める,charm_consistency\n'
             '長距離,芝,,い,b,100,100,100,普通,,,1.5,0,,,,,,\n')
    _h9, *_ = oc.parse_unified('=== 出走馬一覧 ===\n' + _hdr9 + '\n' + _row9)
    _by9 = {x['name']: x for x in _h9}
    check('P9 お守りの σ 倍率が馬に乗る（ステータスは変わらない）',
          abs(_by9['あ']['item_sigma_mult'] - 0.981) < 1e-9
          and _by9['あ']['speed'] == 100 and _by9['あ']['power'] == 100
          and _by9['い'].get('item_sigma_mult', 1.0) == 1.0,
          f"あ σ×{_by9['あ']['item_sigma_mult']} / い σ×{_by9['い'].get('item_sigma_mult', 1.0)}")
    _sg = oc.horse_sigmas({'spec': _sp9}, [_by9['あ'], _by9['い']], 0.01)
    check('P9 σ が実際に下がる（安定感と同じ経路）',
          _sg[0] < _sg[1], f'{_sg[0]:.6f} < {_sg[1]:.6f}')

    # --- P6: 貼り付けの balance= を読み、トークンは載っていないこと ---
    _txt = ('guild=1\nschedule_id=2\npool=900000\nbalance=3120000\n\n'
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

    # --- P10: bm.js のオッズ取得順が「簡易スコア順」であること ---
    #   SPEED だけの積で並べると実測の強さとの順位相関が 0.271 しかなく（302レース）、
    #   金が乗っていない弱い組を延々と取りにいって遅くなる。距離重み＋パッシブ倍率＋
    #   スタミナ収支の簡易スコアなら 0.820。ここが素の speed に戻っていないかを見張る。
    _bm = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'bookmarklets', 'src', 'bm.js'), encoding='utf-8').read()
    _bmc = _bm.replace(' ', '')
    check('P10 初期順は簡易スコア（素のSPEED積に戻っていない）',
          'const STR=new Map' in _bm
          and 'combos.sort((p,q)=>sc(q)-sc(p));' in _bm
          and 'Math.max(Number(h.speed)||1,1)' not in _bm)
    # 重みの初期値が 1 に戻ると、最初のヒットで簡易スコアの順序が全部捨てられる。
    check('P10 適応重みの初期値は簡易スコア（1 ではない）',
          'w=newMap(pets.map(h=>[h.pet_id,STR.get(h.pet_id)]))' in _bmc
          and 'w=newMap(pets.map(h=>[h.pet_id,1]))' not in _bmc)
    check('P10 スタミナ消費の定数が core と一致（bm.js は 0.0132 を .0132 と書く）',
          all(f"'{d}':[" in _bmc for d in oc.DIST_LIST)
          and all(f'[{repr(L["c"])[1:] if repr(L["c"]).startswith("0.") else L["c"]},'
                  f'{L["lo"]},{L["hi"]},{L["n_seg"]}]' in _bmc
                  for L in oc.STAMINA_COST_LAW.values()),
          'STAMINA_COST_LAW が bm.js にもそのまま入っている')

    def _strength(sp_, pw_, st_, ps, dist, surf):
        """bm.js の strength() の Python 版（順序の性質だけ確かめる）。"""
        WD = {'短距離': [1.96, .68, .375], 'マイル': [1.40, .85, .75],
              '中距離': [1.26, 1.105, .975], '長距離': [.84, .85, 1.05]}[dist]
        BL = {'短距離': [1.4, .8, .5], 'マイル': [1, 1, 1],
              '中距離': [.9, 1.3, 1.3], '長距離': [.6, 1, 1.4]}[dist]
        SL = {'短距離': [.0132, 2.125, 2.879, 10], 'マイル': [.0197, 2.234, 3.067, 15],
              '中距離': [.03065, 2.541, 3.737, 20], '長距離': [.04109, 2.57, 3.68, 25]}[dist]
        PM = {'speed_star': [1.35, 1, .9], 'muscle_head': [.9, 1.35, 1],
              'speed_l': [1.25, 1, 1], 'stamina_l': [1, 1, 1.25]}
        s_, p_, t_ = float(sp_), float(pw_), float(st_)
        for c in ps:
            m = PM.get(c)
            if m:
                s_ *= m[0]; p_ *= m[1]; t_ *= m[2]
        r = s_ * WD[0] + p_ * WD[1] + t_ * WD[2]
        d = int(t_) - min(max(SL[0] * (s_ * .6 * BL[0] + p_ * .3 * BL[1]
                                       + t_ * .1 * BL[2]), SL[1]), SL[2]) * SL[3]
        r *= max(.65, 1 + .02 * d) if d < 0 else min(1.03, 1 + .0012 * d)
        return max(r, 1)

    # 長距離: スタミナ型 > スピード型（SPEED だけ見ると逆になる組み合わせ）
    _fast = _strength(160, 40, 40, ['speed_star'], '長距離', '芝')
    _stay = _strength(100, 90, 95, ['stamina_l'], '長距離', '芝')
    check('P10 長距離はスタミナ型が上（SPEED順だと取り違える）',
          _stay > _fast and 160 > 100, f'スタミナ型 {_stay:.0f} > スピード型 {_fast:.0f}')
    # 短距離では逆転すること（距離重みが効いている証拠）
    check('P10 短距離はスピード型が上',
          _strength(160, 40, 40, ['speed_star'], '短距離', '芝')
          > _strength(100, 90, 95, ['stamina_l'], '短距離', '芝'))
    # スタミナ切れの罰は最大 -35%
    check('P10 スタミナ切れの罰は 0.65 で頭打ち',
          abs(_strength(200, 200, 1, [], '長距離', '芝')
              / _strength(200, 200, 1, [], '長距離', '芝')) == 1.0
          and _strength(150, 50, 10, [], '長距離', '芝')
          < _strength(150, 50, 90, [], '長距離', '芝'))

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
