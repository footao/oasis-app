# -*- coding: utf-8 -*-
"""
oasis_core.py — Oasis 安定運用予測のコアロジック（UI非依存・テスト可能）
====================================================================
OASIS_predict_stable.py から純粋ロジックを抽出し、print/ipywidgets 依存を除いて
「データを返す」関数に再構成したもの。Streamlit アプリ(oasis_app.py)から利用する。

主要API:
  train_model(log_path, sigma_override=None, balance_patch_date=...) -> bundle(dict)
  analyze(raw_text, bundle, settings=dict) -> 解析結果(dict)
  BetLog(path) クラス: record / settle / report / undo / load  （ローカルCSVに永続化）
"""
import os
import re
import io
import math
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

# ===== 固定定数（ゲーム仕様・アルゴリズム）=====
CAT_COLS = ['dist', 'track', 'g_cond', 'condition', 'passive']   # 学習/予測で順序を揃える
RANK_MAP = {'🥇': 1, '🥈': 2, '🥉': 3}
DUP_MARK = '\u2009#'                     # 同名馬の内部マーカー（薄スペース+#）

STAKE_UNIT        = 10000    # 1口 = 10,000 rrc（ゲーム仕様で固定）
MAX_UNITS         = 20       # 1組あたり上限口数
MAX_TOTAL_UNITS   = 20       # 1レース合計口数の上限（ゲーム仕様）
N_SIM             = 200000   # モンテカルロ試行数
SIM_SEED          = 42       # 再現性のための固定シード
ODDS_FLOOR        = 1.5      # 初期値=未投票。これ以下は観客マネー≈0とみなす
MARKET_EDGE_RATIO = 1.3      # 単勝の割安/割高判定の比率しきい値（参考表示用）
MARKET_MIN_PROB   = 0.03     # 同上：この勝率未満は割安/割高判定の対象外

# ===== 安定運用（資金管理）パラメータの既定値 =====
# 実行時は settings で上書きする（アプリのサイドバーから変更）。
BANKROLL        = 1_200_000
KELLY_FRACTION  = 0.25
MAX_RISK_FRAC   = 0.10
EDGE_MIN        = 0.10

# ===== キャリーオーバー診断 =====
ASSUME_POOL_IS_PAYOUT = False
CARRYOVER_RRC         = None
CO_DETECT_LO          = 0.95
INV_SUM_SANE          = (0.5, 1.10)

DEFAULT_BALANCE_PATCH_DATE = '2026/03/15'

# 設定の既定（analyzeに渡すsettingsのテンプレ）
DEFAULT_SETTINGS = dict(
    dist='中距離', track='芝', ground='良', topn=15,
    bankroll=BANKROLL, kelly_fraction=KELLY_FRACTION,
    max_risk_frac=MAX_RISK_FRAC, edge_min=EDGE_MIN,
    carryover_rrc=None, assume_pool_is_payout=False,
    csv_path='',
    # 未成立スリーブ（任意・少額キャップ付き）
    unformed_sleeve=False,      # Trueで未成立組も少額で買う
    unformed_max_units=5,       # 未成立に賭ける合計口数の上限（3/4/5想定。各組1口）
    unformed_p_min=0.05,        # 未成立を採用するモデル的中率の下限
    unformed_edge_min=0.30,     # 未成立の実効エッジ下限（小プールでの過剰投資を防ぐ）
)


def unformed_sleeve_picks(combo_prob, disp, od_of, P_total,
                          p_min=0.05, edge_min=0.30, max_units=5,
                          remaining_budget=MAX_TOTAL_UNITS, stake_unit=STAKE_UNIT):
    """未成立組（市場オッズ無し）に各1口ずつ賭ける少額スリーブ。
    採用条件: モデル的中率 p ≥ p_min かつ 実効エッジ(p×実効od−1) ≥ edge_min。
    未成立の実効od=(P+1口)/1口（自分が唯一の購入者なら全プール総取り）。利益は賭け金に
    依らず一定なので各組1口が最適。p降順に max_units かつ残り予算まで採用。
    戻り値: [(combo_names, p, eff_od, 1), ...]。"""
    if P_total <= 0 or max_units <= 0 or remaining_budget <= 0:
        return []
    eff = (P_total + stake_unit) / stake_unit          # 未成立の実効od（全組共通）
    cand = []
    for idx, p in combo_prob.items():
        names = tuple(disp[i] for i in idx)
        if od_of(names) is not None:                   # 成立はスリーブ対象外
            continue
        if p < p_min:
            continue
        if (p * eff - 1) < edge_min:                   # プール連動のエッジ足切り
            continue
        cand.append((names, p))
    cand.sort(key=lambda x: x[1], reverse=True)
    cap = min(int(max_units), int(remaining_budget))
    return [(names, p, eff, 1) for names, p in cand[:cap]]


# --- 1. 個体識別と特徴量生成 ---
def horse_identity(name, owner, sp, st, pw):
    """同名別個体を区別するための識別キー。"""
    return (str(name).strip(), str(owner).strip(), int(sp), int(st), int(pw))


def _owner(s):
    return s.strip().rstrip('\r') if s else 'unknown'


def normalize_passive(s):
    """パッシブ名を絵文字非依存のキーに正規化する。
    bot とブックマークレットで先頭の絵文字が違っても（例 '🏃\u200d♂️ 中距離得意' と
    '🐎 中距離得意'）、日本語テキスト部分（'中距離得意'）で一致させる。
    英語コード（'middle_special' 等）は日本語を持たないためそのまま残り、未学習として
    警告される（＝ブックマークレット側でラベル変換が必要なサイン）。"""
    if s is None:
        return 'なし'
    s = str(s).strip()
    if s in ('', 'なし', 'None', 'nan'):
        return 'なし'
    m = re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', s)   # 最初の仮名/漢字の位置
    return s[m.start():].strip() if m else s


# ===== 攻略本（非公式・6/7時点）由来の内部スコア近似 =====
# 検証(実ログ5681行): 足切りクリア勢で theory と実score の相関 0.96、score≈theory。
# スタミナが必要最低値未満だとスコアが約0.6倍に落ちる（足切りペナルティ）。
# これらは明示的特徴量としてRFに渡す（未来レース予測の汎化が改善: 時系列R² 0.945→0.958）。
DIST_WEIGHTS = {  # 距離別 (speed係数, power係数)  ※攻略本 第3巻
    '短距離': (1.4, 0.8), 'マイル': (1.2, 1.2),
    '中距離': (0.9, 1.3), '長距離': (0.6, 1.0)}
STAMINA_CUTOFF = {'短距離': 30, 'マイル': 41, '中距離': 66, '長距離': 81}  # 第1巻
PASSIVE_TABLE = {  # パッシブ → (speed, power, stamina) 倍率  ※攻略本 第2巻
    'なし': (1, 1, 1),
    'スピードスター': (1.35, 1, 0.9), '脳筋': (0.9, 1.35, 1), 'マイペース': (1, 0.9, 1.35),
    'スピード大アップ': (1.25, 1, 1), 'パワー大アップ': (1, 1.25, 1), 'スタミナ大アップ': (1, 1, 1.25),
    'スピード小アップ': (1.1, 1, 1), 'パワー小アップ': (1, 1.1, 1), 'スタミナ小アップ': (1, 1, 1.1),
    '同族嫌悪': (1.2, 1.2, 1.2), '勝負師': (1.3, 1.3, 1.3), '器用貧乏': (1.05, 1.05, 1.05)}


def passive_multipliers(passive):
    """攻略本のパッシブ倍率を (speed, power, stamina) で返す。先頭絵文字は無視。
    既知の固有名→表引き、'○○得意'→距離得意1.15/馬場得意1.1、未知は等倍。"""
    p = normalize_passive(passive)
    if p in PASSIVE_TABLE:
        return PASSIVE_TABLE[p]
    if p.endswith('得意'):
        if '芝' in p or 'ダート' in p:
            return (1.1, 1.1, 1.1)        # 馬場得意
        return (1.15, 1.15, 1.15)          # 距離得意
    return (1.0, 1.0, 1.0)                  # 未知（別途 未学習警告で通知）


def add_features(df):
    df = df.copy()
    eps = 1e-5
    df['total_stats'] = df['speed'] + df['stamina'] + df['power']
    df['speed_stamina_ratio'] = df['speed'] / (df['stamina'] + eps)
    df['stamina_power_ratio'] = df['stamina'] / (df['power'] + eps)

    # 攻略本由来の特徴量: 理論スコア＋スタミナ足切りマージン
    pmul = df['passive'].map(passive_multipliers)
    p_s = pmul.map(lambda t: t[0]); p_p = pmul.map(lambda t: t[1]); p_st = pmul.map(lambda t: t[2])
    dwt = df['dist'].map(lambda d: DIST_WEIGHTS.get(d, (1.0, 1.0)))
    d_s = dwt.map(lambda t: t[0]); d_p = dwt.map(lambda t: t[1])
    df['theory_score'] = df['speed'] * d_s * p_s + df['power'] * d_p * p_p
    cutoff = df['dist'].map(lambda d: STAMINA_CUTOFF.get(d, 0))
    df['stam_margin'] = df['stamina'] * p_st - cutoff          # 実効スタミナ − 必要最低
    df['below_cutoff'] = (df['stam_margin'] < 0).astype(int)
    return df



# --- 2. ログ解析（セクション単位・高精度） ---
def parse_entries(text):
    """全ての『出走決定』をメッセージ境界に関係なく抽出する。"""
    entries = {}
    pat = re.compile(
        r'🏇[^\n]*?出走決定（(\d{1,2}:\d{2})）[^\n]*\n'
        r'([^\n｜]+)｜([^\n｜]+)｜([^\n\r]+)'                 # 距離｜馬場｜地面
        r'(.*?)(?=🏇[^\n]*?出走決定|🏁[^\n]*?レース 結果|\Z)',
        re.S)
    for m in pat.finditer(text):
        r_time = m.group(1).zfill(5)                          # 9:00 -> 09:00
        dist, track, g_cond = (g.strip().rstrip('\r') for g in m.group(2, 3, 4))
        body = m.group(5)
        pre = text[max(0, m.start() - 2000):m.start()]
        dm = re.findall(r'\[(\d{4}/\d{2}/\d{2}) \d{1,2}:\d{2}\]', pre)
        date = dm[-1] if dm else '????'
        r_key = f"{date} {r_time}"

        horses = []
        for blk in re.split(r'【枠番\s*\d+】', body)[1:]:
            n_m = re.search(r'🐣\s*([^\n\r]+)', blk)
            o_m = re.search(r'👤\s*(@[^\n\r]+)', blk)
            s_m = re.search(r'スピード\s*[:：]\s*(\d+)', blk)
            st_m = re.search(r'スタミナ\s*[:：]\s*(\d+)', blk)
            p_m = re.search(r'パワー\s*[:：]\s*(\d+)', blk)
            c_m = re.search(r'コンディション\s*[:：]\s*([^\s\r\n😄😐😞🙁]+)', blk)
            pa_m = re.search(r'✨\s*パッシブ\s*[:：]\s*([^\n\r]+)', blk)
            if n_m and s_m and st_m and p_m:
                horses.append({
                    'name': n_m.group(1).strip(),
                    'owner': _owner(o_m.group(1) if o_m else None),
                    'speed': int(s_m.group(1)),
                    'stamina': int(st_m.group(1)),
                    'power': int(p_m.group(1)),
                    'condition': c_m.group(1).strip() if c_m else '普通',
                    'passive': pa_m.group(1).strip() if pa_m else None,
                })
        entries[r_key] = {'dist': dist, 'track': track, 'g_cond': g_cond, 'horses': horses}
    return entries


def parse_results(text):
    """全ての『結果』を抽出（🥇🥈🥉 を着順1/2/3として認識）。
    着順マーカーで馬ごとのブロックに分割してから各要素を個別抽出する方式。
    """
    rows = []
    pat = re.compile(
        r'🏁[^\n]*?レース 結果\s*\n'
        r'🕘\s*(\d{1,2}:\d{2})｜([^\n｜]+)｜([^\n｜]+)｜([^\n\r]+)'
        r'(.*?)(?=🏇[^\n]*?出走決定|🏁[^\n]*?レース 結果|\Z)',
        re.S)
    for m in pat.finditer(text):
        r_time = m.group(1).zfill(5)
        dist, track, g_cond = (g.strip().rstrip('\r') for g in m.group(2, 3, 4))
        body = m.group(5)
        pre = text[max(0, m.start() - 2000):m.start()]
        dm = re.findall(r'\[(\d{4}/\d{2}/\d{2}) \d{1,2}:\d{2}\]', pre)
        date = dm[-1] if dm else '????'
        r_key = f"{date} {r_time}"

        for block in re.split(r'(?=🥇|🥈|🥉|\d+着)', body):
            if not block.strip():
                continue
            hm = re.search(r'(🥇|🥈|🥉|(\d+)着)\s+([^\n@]+?)\s*\n(?:(@[^\n\r]+)\s*\n)?', block)
            if not hm:
                continue
            s_m = re.search(r'スピード\s+(\d+)', block)
            st_m = re.search(r'スタミナ\s+(\d+)', block)
            p_m = re.search(r'パワー\s+(\d+)', block)
            sc_m = re.search(r'score\s+(\d+\.?\d*)', block)
            pa_m = re.search(r'✨\s*パッシブ\s*[:：]\s*([^\n\r]+)', block)
            if s_m and st_m and p_m and sc_m:
                rows.append({
                    'race_key': r_key,
                    'rank': RANK_MAP.get(hm.group(1)) or int(hm.group(2)),
                    'name': hm.group(3).strip(),
                    'owner': _owner(hm.group(4)),
                    'speed': int(s_m.group(1)), 'stamina': int(st_m.group(1)),
                    'power': int(p_m.group(1)), 'score': float(sc_m.group(1)),
                    'passive_res': pa_m.group(1).strip() if pa_m else None,
                    'dist': dist, 'track': track, 'g_cond': g_cond,
                })
    return rows


def parse_race_log(file_path):
    text = open(file_path, encoding='utf-8').read()
    entries = parse_entries(text)
    results = parse_results(text)

    out = []
    for r in results:
        passive = r['passive_res']
        condition = '不明'   # #10: 照合失敗時は '普通' に潰さず '不明' として残す
        ent = entries.get(r['race_key'])
        if ent:
            target = horse_identity(r['name'], r['owner'], r['speed'], r['stamina'], r['power'])
            for h in ent['horses']:
                if horse_identity(h['name'], h['owner'], h['speed'], h['stamina'], h['power']) == target:
                    condition = h['condition']
                    if not passive:
                        passive = h['passive']
                    break
        out.append({
            'date': r['race_key'].split(' ')[0],
            'dist': r['dist'], 'track': r['track'], 'g_cond': r['g_cond'],
            'speed': r['speed'], 'stamina': r['stamina'], 'power': r['power'],
            'condition': condition, 'passive': normalize_passive(passive),
            'rank': r['rank'], 'score': r['score'],
        })
    return pd.DataFrame(out)



# --- 4. 純粋計算ヘルパー（#13: UIから分離してテスト可能に） ---
def simulate_rankings(base, sigma, n_sim=N_SIM, seed=SIM_SEED):
    """予測スコア base に σ のガウスノイズを与え、n_sim 回ぶんの着順(argsort降順)を返す。
    seed 固定で再現性を確保する（#8）。"""
    rng = np.random.default_rng(seed)
    sim = base + rng.normal(0, sigma, (n_sim, len(base)))
    return np.argsort(-sim, axis=1)


def market_win_prob(odds):
    """単勝オッズ列から観客の勝率予想を推定。
    ・ライブ画面では未投票馬が初期値 ODDS_FLOOR(1.5) に張り付くため、そのまま 1/オッズ を
      使うと未投票馬を過大評価する。→ floor以下は観客マネー≈0 とみなして除外し、
      残りを正規化する（控除率ではなく単純正規化。暫定オッズは過剰ラウンドになり得る）。
    戻り値: 正規化済み確率配列 or None(市場情報なし)。
    """
    odds = np.asarray(odds, dtype=float)
    raw = np.where((odds > ODDS_FLOOR) & np.isfinite(odds), 1.0 / odds, 0.0)
    if raw.sum() <= 0:
        return None
    return raw / raw.sum()


def harville(p, idx):
    """勝率配列 p から、順序付き(idx[0]→idx[1]→idx[2])の3連単確率を近似。"""
    a, b, c = idx
    da = 1.0 - p[a]
    db = da - p[b]
    if da <= 1e-9 or db <= 1e-9:
        return 0.0
    return p[a] * (p[b] / da) * (p[c] / db)


def resolve_payout_pool(pool, odds_iter, manual_co=None, trust=True):
    """成立3連単オッズから Σ(1/od) を計算し、parimutuel が使う『払戻プール』を決める。
    Σ(1/od) = 実ベット総額 / 払戻プール（控除0%・全組合算の恒等式。未成立組はベット0で
    1/od=0 のため、成立分だけの合算でも比率は正確）。
    trust=False（全組がそろっている保証が無い＝CSVを間引いた等）の場合、CO検出しても
    自動補正はせず表示のみ（不完全なオッズ集合による偽CO検出→過剰投資を防ぐ）。
    戻り値: (payout_pool, info)。info に inv_sum / regime / carryover / bets を格納。"""
    vals = [float(o) for o in odds_iter if o and float(o) > 0 and math.isfinite(float(o))]
    info = {'inv_sum': None, 'regime': 'no_pool', 'carryover': 0.0, 'bets': float(pool), 'n': len(vals)}
    if pool <= 0 or not vals:
        return pool, info
    inv = sum(1.0 / o for o in vals)
    info['inv_sum'] = inv

    # 手動CO指定が最優先（pool=実ベット総額とみなして加算）
    if manual_co is not None:
        co = max(0.0, float(manual_co))
        info.update(regime='manual', carryover=co, bets=float(pool))
        return pool + co, info

    if inv > 1.05:                                  # 控除あり（house edge）
        info.update(regime='takeout')
        return pool, info                            # CO無しとして補正しない
    if inv >= CO_DETECT_LO:                          # 中立: 控除0%・CO無し
        info.update(regime='neutral')
        return pool, info
    # ここから inv < CO_DETECT_LO ＝ キャリーオーバー検出
    if not (INV_SUM_SANE[0] <= inv <= INV_SUM_SANE[1]):
        info.update(regime='carryover_unsure')       # 異常値（成立点が少ない等）は補正しない
        return pool, info
    payout = pool / inv
    if not trust:
        # 全組がそろっている保証が無い → 偽CO検出の恐れ。表示のみで補正しない（安全側）
        info.update(regime='carryover_untrusted', carryover=payout - pool, bets=float(pool))
        return pool, info
    if ASSUME_POOL_IS_PAYOUT:
        co = pool * (1.0 - inv)                       # poolに内包済みのCO（情報用）
        info.update(regime='carryover_in_pool', carryover=co, bets=pool - co)
        return pool, info                            # 計算は pool のまま（補正不要）
    # 既定: pool=実ベット総額とみなし、オッズから払戻プールを逆算してCOを加える
    info.update(regime='carryover_added', carryover=payout - pool, bets=float(pool))
    return payout, info


def optimal_units_ev(p, od, P_tot, stake_unit=STAKE_UNIT, max_units=MAX_UNITS):
    """パリミュチュエルで期待値を最大化する購入口数（#12: Kelly=対数効用ではなく EV最大化）。
    解析解  x = P_c * (√(p(o-1)/(1-p)) - 1),  k = x / stake_unit。
    自分が k 口買うと払戻オッズが下がる効果を織り込む（表示オッズより実効オッズは必ず下がる）。
    条件: p > 1/o のときのみ購入価値あり。
    戻り値: (k口, k口ぶんの実効EV合計, 実効オッズ)。
    """
    if p <= 0 or p >= 1 or od <= 1:
        return 0, 0.0, od
    if p <= 1.0 / od:                       # プール効果込みで損
        return 0, 0.0, od
    if P_tot <= 0:                          # プール不明：表示EVが正なら保守的に1口
        return 1, (p * od - 1) * stake_unit, od

    P_c = P_tot / od                        # この組み合わせの現在のプール額
    inner = p * (od - 1) / (1.0 - p)
    k_raw = (P_tot / (od * stake_unit)) * (math.sqrt(inner) - 1)
    if k_raw <= 0:
        return 0, 0.0, od
    if k_raw < 1:
        # 連続最適解 < 1口 → 1口でも実効EVが正か確認
        eff_od_1 = (P_tot + stake_unit) / (P_c + stake_unit)
        if p * eff_od_1 > 1:
            k = 1
        else:
            return 0, 0.0, od
    else:
        k = min(max_units, int(k_raw))      # 切り捨て（過剰投資を避ける）

    eff_od = (P_tot + k * stake_unit) / (P_c + k * stake_unit)
    eff_ev = (p * eff_od - 1) * stake_unit * k
    return k, eff_ev, eff_od


def allocate_units(cands, P_total, budget=MAX_TOTAL_UNITS,
                   stake_unit=STAKE_UNIT, max_per_combo=None):
    """合計 budget 口を、限界EV（追加1口あたりの期待値）が最大の買い目へ貪欲に配分する。
    parimutuelでは1口増やすごとに自分でオッズを下げる＝各買い目のEVが口数に対し凹なので、
    限界EVの大きい順に1口ずつ割り当てる貪欲法が総EV最大の整数配分になる。
    cands: [(combo, p, od), ...]。戻り値: {combo: (k, eff_ev_total, eff_od)}（k>0のみ）。
    ※ 簡略化のため、ある買い目への投入が他の買い目のプール(=オッズ)に与える影響は無視
      （影響は小さく、無視は実効EVを過小評価する保守側）。"""
    if max_per_combo is None:
        max_per_combo = budget
    pos = [(c, p, od) for (c, p, od) in cands
           if p > 0 and od > 1 and p > 1.0 / od]          # 購入価値のある候補のみ
    if not pos or budget <= 0:
        return {}

    if P_total <= 0:
        # プール不明: 希薄化を測れない → 表示EV順に1口ずつ（分散でブレ抑制）
        pos.sort(key=lambda t: t[1] * t[2] - 1, reverse=True)
        return {c: (1, (p * od - 1) * stake_unit, od) for (c, p, od) in pos[:budget]}

    def ev_c(p, od, k):                                   # k口投入時の合計EV
        if k <= 0:
            return 0.0
        Pc = P_total / od
        eff = (P_total + k * stake_unit) / (Pc + k * stake_unit)
        return (p * eff - 1) * stake_unit * k

    alloc = [0] * len(pos)
    used = 0
    while used < budget:
        best_i, best_m = -1, 1e-9                         # 正の限界EVのみ採用
        for i, (c, p, od) in enumerate(pos):
            if alloc[i] >= max_per_combo:
                continue
            m = ev_c(p, od, alloc[i] + 1) - ev_c(p, od, alloc[i])
            if m > best_m:
                best_m, best_i = m, i
        if best_i < 0:                                    # これ以上は限界EV≤0
            break
        alloc[best_i] += 1
        used += 1

    res = {}
    for i, (c, p, od) in enumerate(pos):
        k = alloc[i]
        if k > 0:
            Pc = P_total / od
            eff = (P_total + k * stake_unit) / (Pc + k * stake_unit)
            res[c] = (k, (p * eff - 1) * stake_unit * k, eff)
    return res


def allocate_units_stable(cands, P_total, bankroll=BANKROLL, kelly_frac=KELLY_FRACTION,
                          max_risk_frac=MAX_RISK_FRAC, edge_min=EDGE_MIN,
                          budget=MAX_TOTAL_UNITS, stake_unit=STAKE_UNIT, max_per_combo=None):
    """安定運用版の配分。EV最大化ではなく『資金を守りながら長期で安定成長』を狙う。
      ・成立組のみ（未成立=全プール総取りの高分散な賭けは除外）。
      ・実効エッジ p×eff_od(1口)−1 ≥ edge_min を満たす買い目だけ採用（薄い勝負は見送り）。
      ・各買い目の口数 = min(分数ケリー口数, EV最大口数, max_per_combo)。
        分数ケリーが1口未満でも、エッジが基準以上なら最低1口（資金比0.8%と十分小さい・分散効果優先）。
      ・1レースの総口数 = min(budget, 資金リスク上限=floor(max_risk_frac×bankroll/口))。
    戻り値: {combo: (k, eff_ev_total, eff_od)}。"""
    if max_per_combo is None:
        max_per_combo = budget
    if P_total <= 0 or bankroll <= 0:
        return {}
    risk_units = max(1, int((max_risk_frac * bankroll) // stake_unit))
    total_cap = min(budget, risk_units)

    def Pc_of(od):
        return P_total / od

    def ev_c(p, od, k):
        if k <= 0:
            return 0.0
        Pc = Pc_of(od)
        eff = (P_total + k * stake_unit) / (Pc + k * stake_unit)
        return (p * eff - 1) * stake_unit * k

    items = []   # (combo, p, od, cap)
    for (c, p, od) in cands:
        if not od or od <= 1 or not (0 < p < 1):
            continue                                       # 未成立/異常は除外
        Pc = Pc_of(od)
        eff1 = (P_total + stake_unit) / (Pc + stake_unit)  # 1口時の実効オッズ
        edge = p * eff1 - 1
        if edge < edge_min:
            continue                                       # 薄いエッジは見送り
        f = (p * eff1 - 1) / (eff1 - 1)                    # フルケリー分数
        k_kelly = int((kelly_frac * f * bankroll) // stake_unit)
        k_evmax, _, _ = optimal_units_ev(p, od, P_total, stake_unit, max_per_combo)
        cap = min(k_evmax if k_evmax > 0 else 0, max_per_combo)
        # 分数ケリー口数で上限。<1口でもエッジ基準を満たすなら最低1口は許容
        cap = min(cap, max(1, k_kelly))
        if cap >= 1:
            items.append((c, p, od, cap))
    if not items:
        return {}

    # 限界EV貪欲（per-combo cap と total_cap を遵守）
    alloc = {c: 0 for (c, p, od, cap) in items}
    used = 0
    while used < total_cap:
        best, best_m = None, 1e-9
        for (c, p, od, cap) in items:
            if alloc[c] >= cap:
                continue
            m = ev_c(p, od, alloc[c] + 1) - ev_c(p, od, alloc[c])
            if m > best_m:
                best_m, best = m, (c, p, od)
        if best is None:
            break
        alloc[best[0]] += 1
        used += 1

    res = {}
    for (c, p, od, cap) in items:
        k = alloc[c]
        if k > 0:
            Pc = Pc_of(od)
            eff = (P_total + k * stake_unit) / (Pc + k * stake_unit)
            res[c] = (k, (p * eff - 1) * stake_unit * k, eff)
    return res


# --- 5. 入力フォーマット解析 ---
def parse_betting_screen(text):
    """旧: 購入画面テキストを解析。
    1馬ブロックの想定:
        <名前>
        コンディション：好調
        159SPEED
        50POWER
        46STAM
        ⚡ スピードスター            ← パッシブ（ラベル無し・絵文字+名前）
        <パッシブ説明文>
        オッズ 1.5
        1口（10,000 rrc）            ← ゲーム画面の文言（1口=10,000 rrc に統一）
        購入                         ← ブロック区切り
    ※ パッシブはラベルが無いため KNOWN_PASSIVES と照合して特定する。
    """
    horses = []
    for block in re.split(r'購入', text):
        if not block.strip():
            continue
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 3:
            continue
        sp_m = re.search(r'(\d+)\s*SPEED', block, re.I)
        pw_m = re.search(r'(\d+)\s*POWER', block, re.I)
        st_m = re.search(r'(\d+)\s*STAM', block, re.I)
        if not (sp_m and pw_m and st_m):
            continue
        passive = 'なし'
        for pname in KNOWN_PASSIVES:
            if pname in block:
                passive = pname
                break
        c_m = re.search(r'(好調|普通|不調)', block)
        od_m = re.search(r'オッズ\s+([0-9.]+)', block)
        horses.append({
            'name': lines[0],
            'speed': int(sp_m.group(1)), 'power': int(pw_m.group(1)),
            'stamina': int(st_m.group(1)),
            'condition': c_m.group(1) if c_m else '普通',
            'passive': passive,
            'odds': float(od_m.group(1)) if od_m else float('nan'),
        })
    return horses


def disambiguate(names):
    """同名馬を A#1 / A#2 … と区別。重複が無い馬はそのまま。"""
    total = {}
    for n in names:
        total[n] = total.get(n, 0) + 1
    cnt, out = {}, []
    for n in names:
        if total[n] > 1:
            cnt[n] = cnt.get(n, 0) + 1
            out.append(f"{n}{DUP_MARK}{cnt[n]}")
        else:
            out.append(n)
    return out


def bare(name):
    """表示名から「こちらが付けた」重複マーカーのみ除去（ゲームの素の '#2' は保持）。
    CSVは素名のため照合に使用。"""
    return re.sub(re.escape(DUP_MARK) + r'\d+$', '', str(name))


def parse_trifecta_csv(path):
    """3連単オッズCSV(順位,1着,2着,3着,オッズ) → {(1着,2着,3着): オッズ}。
    『未成立』など数値でないオッズ行は除外。"""
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['_o'] = pd.to_numeric(df['オッズ'], errors='coerce')
    df = df[df['_o'].notna()]
    out = {}
    for _, r in df.iterrows():
        out[(str(r['1着']).strip(), str(r['2着']).strip(), str(r['3着']).strip())] = float(r['_o'])
    return out


def parse_unified(text):
    """統合フォーマット解析（ブックマークレット出力）。
    戻り値: (horses, trifecta_odds, auto_dist, auto_track, guild, schedule_id, pool, n_tri_total)
    n_tri_total = 3連単セクションの総行数（成立＋未成立）。完全性チェック（CO診断の信頼判定）用。
    """
    horses, odds = [], {}
    auto_dist, auto_track, guild, schedule_id, pool = None, None, None, None, None
    n_tri_total = 0

    gm = re.search(r'^guild=(\S+)',       text, re.M)
    sm = re.search(r'^schedule_id=(\S+)', text, re.M)
    pm = re.search(r'^pool=(\d+)',        text, re.M)
    if gm: guild       = gm.group(1).strip()
    if sm: schedule_id = sm.group(1).strip()
    if pm: pool        = int(pm.group(1))

    mh = re.search(r'===\s*出走馬一覧\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)
    mo = re.search(r'===\s*3連単オッズ\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)

    if mh:
        df = pd.read_csv(io.StringIO(mh.group(1).strip()))

        def _col_val(col):
            if col not in df.columns or len(df) == 0:
                return None
            v = str(df[col].iloc[0]).strip()
            return v if v not in ('', 'nan', 'None') else None

        auto_dist  = _col_val('レース距離')
        auto_track = _col_val('馬場')

        for _, r in df.iterrows():
            sp = pd.to_numeric(r.get('SPEED',   np.nan), errors='coerce')
            pw = pd.to_numeric(r.get('POWER',   np.nan), errors='coerce')
            st = pd.to_numeric(r.get('STAMINA', np.nan), errors='coerce')
            if pd.isna(sp) or pd.isna(pw) or pd.isna(st):
                continue

            ov = str(r.get('単勝オッズ', '')).strip()
            win_odds = pd.to_numeric(ov, errors='coerce')

            horses.append({
                'name':      str(r['馬名']).strip(),
                'speed':     int(sp), 'power': int(pw), 'stamina': int(st),
                'condition': str(r.get('コンディション', '普通')).strip(),
                'passive':   normalize_passive(r.get('パッシブスキル', 'なし')),
                'odds':      float(win_odds) if pd.notna(win_odds) else float('nan'),
            })

    if mo:
        dfo = pd.read_csv(io.StringIO(mo.group(1).strip()))
        n_tri_total = len(dfo)                    # 成立＋未成立の総数（完全性チェック用）
        dfo['_o'] = pd.to_numeric(dfo['オッズ'], errors='coerce')
        for _, r in dfo[dfo['_o'].notna()].iterrows():
            key = (str(r['1着']).strip(), str(r['2着']).strip(), str(r['3着']).strip())
            odds[key] = float(r['_o'])

    return horses, odds, auto_dist, auto_track, guild, schedule_id, pool, n_tri_total



# ====================================================================
#  学習: ログCSV/TXT からモデルを構築して bundle を返す
# ====================================================================
def train_model(log_path, sigma_override=None,
                balance_patch_date=DEFAULT_BALANCE_PATCH_DATE):
    """レースログを読み込み RandomForest を学習。
    戻り値 bundle(dict): model, X_columns, race_sigma, train_passives,
      train_conditions, known_passives, oob_r2, oob_resid_std,
      n_rows, n_drop, n_nodate, n_cond_unknown, messages(list)。
    log_path が無い/空なら ok=False。"""
    msgs = []
    if not log_path or not os.path.exists(log_path):
        return {'ok': False, 'messages': [f'ログが見つかりません: {log_path}'],
                'model': None}

    df_raw = parse_race_log(log_path)
    n_all = len(df_raw)
    if n_all == 0:
        return {'ok': False, 'messages': ['ログを解析できませんでした（中身を確認してください）。'],
                'model': None}

    df_raw['_date'] = pd.to_datetime(df_raw['date'], format='%Y/%m/%d', errors='coerce')
    patch_dt = pd.to_datetime(balance_patch_date, format='%Y/%m/%d')
    n_nodate = int(df_raw['_date'].isna().sum())
    df = (df_raw[df_raw['_date'] >= patch_dt]
          .drop(columns=['date', '_date']).reset_index(drop=True))
    n_drop = n_all - len(df)
    if len(df) == 0:
        return {'ok': False, 'model': None,
                'messages': [f'対象レースが0件（{balance_patch_date}以降のデータがありません）。']}

    known_passives = sorted(
        [p for p in df['passive'].unique() if p and p != 'なし'], key=len, reverse=True)
    train_passives = set(df['passive'].unique())
    train_conditions = set(df['condition'].unique())
    n_cond_unknown = int((df['condition'] == '不明').sum())

    df_fe = add_features(df)
    df_enc = pd.get_dummies(df_fe.drop(columns=['rank']), columns=CAT_COLS)
    X, y = df_enc.drop(columns=['score']), df_enc['score']
    model = RandomForestRegressor(
        n_estimators=200, oob_score=True, random_state=42, n_jobs=-1).fit(X, y)

    oob_resid_std = float(np.std(y - model.oob_prediction_))
    race_sigma = float(sigma_override) if sigma_override else max(oob_resid_std, 1e-6)
    r2 = float(model.oob_score_)

    msgs.append(f'学習完了  rows={len(X)}（対象外 {n_drop}行除外, うち日付不明 {n_nodate}行）  '
                f'OOB_R²={r2:.3f}  OOB残差std={oob_resid_std:.2f} → RACE_SIGMA={race_sigma:.2f}'
                + ('（手動上書き）' if sigma_override else ''))
    msgs.append('攻略本由来の特徴量（理論スコア・スタミナ足切り）を学習に使用。')
    if n_cond_unknown:
        msgs.append(f'コンディション不明 {n_cond_unknown}行は「不明」カテゴリとして学習。')
    if r2 >= 0.97:
        msgs.append('⚠ OOB_R²が非常に高い＝スコアはほぼ決定論的。勝率は極端化しがち。実測で校正を。')
    elif r2 < 0.4:
        msgs.append('⚠ OOB_R²が低い＝モデルがスコアを説明できていない。勝率の信頼度は低い。')

    return {'ok': True, 'model': model, 'X_columns': X.columns.tolist(),
            'race_sigma': race_sigma, 'train_passives': train_passives,
            'train_conditions': train_conditions, 'known_passives': known_passives,
            'oob_r2': r2, 'oob_resid_std': oob_resid_std, 'n_rows': int(len(X)),
            'n_drop': int(n_drop), 'n_nodate': int(n_nodate),
            'n_cond_unknown': int(n_cond_unknown), 'messages': msgs}


def _fetch_pool_api(guild, schedule_id, timeout=5):
    """api.oasis.red からプール総額を取得（失敗時 None, エラーメッセージ）。"""
    try:
        import requests
        r = requests.get(
            f'https://api.oasis.red/api/trifecta/pool?guild={guild}&schedule_id={schedule_id}',
            timeout=timeout)
        pool = r.json().get('pool', 0) or 0
        return int(pool), None
    except Exception as e:
        return None, f'プール取得失敗（口数計算スキップ）: {e}'


# ====================================================================
#  解析: 入力テキスト + bundle + settings → 構造化された結果(dict)
# ====================================================================
def analyze(raw_text, bundle, settings=None):
    s = dict(DEFAULT_SETTINGS)
    if settings:
        s.update(settings)
    if not bundle or not bundle.get('ok'):
        return {'ok': False, 'error': 'モデル未学習。先にログを読み込んでください。'}

    model = bundle['model']
    X_columns = bundle['X_columns']
    race_sigma = bundle['race_sigma']
    train_passives = bundle['train_passives']
    train_conditions = bundle['train_conditions']

    res = {'ok': True, 'error': None, 'messages': [], 'pool_msgs': []}
    P_total = 0

    # --- 入力解析 ---
    if '出走馬一覧' in raw_text:
        (horses, csv_odds, auto_dist, auto_track,
         guild, schedule_id, clip_pool, n_tri_total) = parse_unified(raw_text)
        if auto_dist:
            s['dist'] = auto_dist
            res['messages'].append(f'距離を自動設定: {auto_dist}')
        if auto_track:
            s['track'] = auto_track
            res['messages'].append(f'馬場を自動設定: {auto_track}')
        if clip_pool is not None:
            P_total = int(clip_pool)
            res['messages'].append(f'プール総額: {P_total:,} rrc（貼り付けデータから取得）')
        elif guild and schedule_id:
            pool, err = _fetch_pool_api(guild, schedule_id)
            if pool is not None:
                P_total = pool
                res['messages'].append(f'プール総額: {P_total:,} rrc（APIから取得）')
            elif err:
                res['messages'].append('⚠ ' + err)
    else:
        horses = parse_betting_screen(raw_text)
        csv_odds = None
        n_tri_total = 0

    if not horses:
        return {'ok': False, 'error': 'データ読み込み失敗（フォーマットを確認してください）。'}

    # 未学習パッシブ/コンディション警告
    unseen_pass = sorted({h['passive'] for h in horses if h['passive'] not in train_passives})
    unseen_cond = sorted({h['condition'] for h in horses if h['condition'] not in train_conditions})
    if unseen_pass:
        res['messages'].append('⚠ 未学習パッシブ（勝率に反映されずbaseline扱い）: ' + ', '.join(unseen_pass))
    if unseen_cond:
        res['messages'].append('⚠ 未学習コンディション（baseline扱い）: ' + ', '.join(unseen_cond))

    res['dist'], res['track'], res['ground'] = s['dist'], s['track'], s['ground']

    # --- 予測 + シミュレーション ---
    new_df = add_features(pd.DataFrame([{
        **h, 'dist': s['dist'], 'track': s['track'], 'g_cond': s['ground']} for h in horses]))
    new_enc = pd.get_dummies(new_df.drop(columns=['name']), columns=CAT_COLS) \
        .reindex(columns=X_columns, fill_value=0)
    base = model.predict(new_enc)
    ranks = simulate_rankings(base, race_sigma)
    disp = disambiguate([h['name'] for h in horses])
    n = len(base)

    win_p = (ranks[:, 0:1] == np.arange(n)).mean(axis=0)
    odds = np.array([h.get('odds', np.nan) for h in horses], dtype=float)
    mkt_p = market_win_prob(odds)

    # 単勝テーブル（参考）
    cutoff_map = STAMINA_CUTOFF
    order = np.argsort(-win_p)
    single = []
    for i in order:
        h = horses[i]
        stam_eff = h['stamina'] * passive_multipliers(h['passive'])[2]
        cut = cutoff_map.get(s['dist'], 0)
        row = {'name': disp[i], 'model_p': float(win_p[i]), 'market_p': None,
               'odds': None, 'tag': '',
               'below_cutoff': bool(stam_eff < cut),
               'stam_margin': float(stam_eff - cut)}
        if mkt_p is not None:
            row['market_p'] = float(mkt_p[i])
            od = odds[i]
            row['odds'] = float(od) if np.isfinite(od) else None
            if (not np.isfinite(od)) or od <= ODDS_FLOOR:
                row['tag'] = '（未投票）'
            elif win_p[i] > mkt_p[i] * MARKET_EDGE_RATIO and win_p[i] > MARKET_MIN_PROB:
                row['tag'] = '★割安'
            elif mkt_p[i] > win_p[i] * MARKET_EDGE_RATIO and mkt_p[i] > MARKET_MIN_PROB:
                row['tag'] = '割高(罠)'
        single.append(row)
    res['single_win'] = single
    res['model_pick'] = disp[order[0]]
    below = [r['name'] for r in single if r.get('below_cutoff')]
    if below:
        res['messages'].append(
            f'⚠ スタミナ足切り未満（{s["dist"]}は最低{STAMINA_CUTOFF.get(s["dist"], "?")}）'
            f': {", ".join(below)} → スコア大幅減。実質的に上位は厳しい。')
    res['has_market'] = mkt_p is not None

    # 3連単確率
    idx_counts = {}
    for r3 in ranks[:, :3]:
        key = (int(r3[0]), int(r3[1]), int(r3[2]))
        idx_counts[key] = idx_counts.get(key, 0) + 1
    combo_prob = {k: v / N_SIM for k, v in idx_counts.items()}

    exact_cp, name_cp = {}, {}
    for (i, j, k), p in combo_prob.items():
        ek = (disp[i], disp[j], disp[k])
        exact_cp[ek] = exact_cp.get(ek, 0.0) + p
        bk = (bare(disp[i]), bare(disp[j]), bare(disp[k]))
        name_cp[bk] = name_cp.get(bk, 0.0) + p
    screen_names = set(disp)

    def lookup_prob(combo):
        if combo in exact_cp:
            return exact_cp[combo], 'exact'
        bk = tuple(bare(x) for x in combo)
        if bk in name_cp:
            return name_cp[bk], 'bare'
        return 0.0, 'none'

    # CSVオッズ（統合に無ければCSVファイル）
    if csv_odds is None:
        path = (s.get('csv_path') or '').strip()
        if path and os.path.exists(path):
            try:
                csv_odds = parse_trifecta_csv(path)
            except Exception as e:
                res['messages'].append(f'⚠ CSV読み込み失敗: {e}')

    res['picks'] = []
    res['purchase_lines'] = []
    res['pool'] = P_total
    res['has_csv'] = bool(csv_odds)

    # 成立オッズ逆引き（disp名→od）。スリーブ判定・ランキング双方で使用。
    odds_exact, odds_bare = {}, {}
    if csv_odds:
        for (a, b, c), od in csv_odds.items():
            odds_exact[(a, b, c)] = od
            odds_bare[(bare(a), bare(b), bare(c))] = od

    def od_of(names):
        if names in odds_exact:
            return odds_exact[names]
        return odds_bare.get(tuple(bare(x) for x in names))

    if csv_odds:
        rows = []
        how_counts = {'exact': 0, 'bare': 0, 'none': 0}
        unmatched = set()
        for combo, od in csv_odds.items():
            p, how = lookup_prob(combo)
            how_counts[how] += 1
            if how == 'none':
                for nm in combo:
                    if bare(nm) not in {bare(x) for x in screen_names}:
                        unmatched.add(nm)
            rows.append((combo, p, od, STAKE_UNIT * (p * od - 1)))
        rows.sort(key=lambda x: x[3], reverse=True)
        mode = ('完全名一致' if how_counts['exact'] and not how_counts['bare']
                else '素名フォールバック' if how_counts['bare'] and not how_counts['exact']
                else '混在')
        res['mode'] = mode
        res['bare_used'] = bool(how_counts['bare'])
        res['unmatched_names'] = sorted(unmatched)

        # キャリーオーバー診断
        if P_total > 0:
            n_h = len(horses)
            expected = n_h * (n_h - 1) * (n_h - 2) if n_h >= 3 else 0
            co_trust = (n_tri_total > 0 and n_tri_total == expected)
            payout_pool, cinfo = resolve_payout_pool(
                P_total, csv_odds.values(),
                manual_co=s.get('carryover_rrc'), trust=co_trust)
            inv, reg = cinfo['inv_sum'], cinfo['regime']
            if reg == 'takeout':
                res['pool_msgs'].append(f'Σ(1/od)={inv:.3f} → 控除あり(約{(1-1/inv)*100:.1f}%)。補正なし。')
            elif reg == 'neutral':
                res['pool_msgs'].append(f'Σ(1/od)={inv:.3f} → 控除0%・CO無し。プール {P_total:,} rrc。')
            elif reg == 'carryover_added':
                res['pool_msgs'].append(
                    f'Σ(1/od)={inv:.3f} → キャリーオーバー検出（推定CO ≈ {cinfo["carryover"]:,.0f} rrc）。'
                    f'pool=実ベット総額とみなし加算 → 払戻プール {payout_pool:,.0f} rrc で計算。'
                    'ゲーム表示のCO額と一致するか確認を。')
                P_total = int(round(payout_pool))
            elif reg == 'carryover_untrusted':
                res['pool_msgs'].append(
                    f'Σ(1/od)={inv:.3f} → CO検出だが全{expected}組中{n_tri_total}組のみで不完全 → '
                    '自動補正せず。全組のオッズを貼れば補正します。')
            elif reg == 'carryover_in_pool':
                res['pool_msgs'].append(
                    f'Σ(1/od)={inv:.3f} → CO検出。poolは払戻総額(CO内包)とみなし補正なし。')
            elif reg == 'manual':
                res['pool_msgs'].append(
                    f'手動CO {cinfo["carryover"]:,.0f} rrc を加算 → 払戻プール {payout_pool:,.0f} rrc。')
                P_total = int(round(payout_pool))
            elif reg == 'carryover_unsure':
                res['pool_msgs'].append(f'Σ(1/od)={inv:.3f} は異常値 → 安全側で補正なし。')
        res['pool'] = P_total

        # 安定運用配分
        alloc = allocate_units_stable(
            [(c, p, od) for c, p, od, ev in rows], P_total,
            bankroll=s['bankroll'], kelly_frac=s['kelly_fraction'],
            max_risk_frac=s['max_risk_frac'], edge_min=s['edge_min'],
            budget=MAX_TOTAL_UNITS, max_per_combo=MAX_UNITS)

        alloc_rows, picks, purchase_lines, total_units = [], [], [], 0
        for combo, p, od, ev in rows:
            k, eff_ev, eff_od = alloc.get(combo, (0, 0.0, od))
            mark = '✅' if k > 0 else ('△' if ev > 0 else '')
            alloc_rows.append({
                'combo': ' → '.join(combo), 'model_p': p, 'disp_od': od,
                'theo_ev': ev, 'mark': mark, 'k': k, 'flag': '成',
                'eff_od': (eff_od if k > 0 else None),
                'eff_ev': (eff_ev if k > 0 else None)})
            if k > 0:
                picks.append((combo, p, eff_od, k))
                purchase_lines += [' ✅' + ' → '.join(combo)] * k
                total_units += k

        # 未成立スリーブ（任意・少額キャップ）。成立配分の残り予算内で各組1口。
        n_unformed = 0
        if s.get('unformed_sleeve') and P_total > 0:
            sleeve = unformed_sleeve_picks(
                combo_prob, disp, od_of, P_total,
                p_min=s.get('unformed_p_min', 0.05),
                edge_min=s.get('unformed_edge_min', 0.30),
                max_units=s.get('unformed_max_units', 5),
                remaining_budget=MAX_TOTAL_UNITS - total_units)
            for names, p, eff_od, k in sleeve:
                eff_ev = (p * eff_od - 1) * STAKE_UNIT * k
                alloc_rows.append({
                    'combo': ' → '.join(names), 'model_p': p, 'disp_od': None,
                    'theo_ev': None, 'mark': '✅', 'k': k, 'flag': '未',
                    'eff_od': eff_od, 'eff_ev': eff_ev})
                picks.append((names, p, eff_od, k))
                purchase_lines += [' ✅' + ' → '.join(names)] * k
                total_units += k
                n_unformed += k

        res['alloc_rows'] = alloc_rows
        res['picks'] = picks
        res['purchase_lines'] = purchase_lines

        risk_units = max(1, int((s['max_risk_frac'] * s['bankroll']) // STAKE_UNIT))
        invest = total_units * STAKE_UNIT
        tev = sum((r['eff_ev'] or 0) for r in alloc_rows if r['k'] > 0)
        hit = min(sum(p for _, p, _, _ in picks), 1.0)
        res['summary'] = {
            'n_points': len(picks), 'total_units': total_units, 'invest': invest,
            'invest_pct': (invest / s['bankroll'] * 100 if s['bankroll'] else 0),
            'tev': tev, 'hit': hit, 'miss': 1 - hit, 'max_loss': invest,
            'bankroll': s['bankroll'], 'kelly_pct': int(s['kelly_fraction'] * 100),
            'risk_pct': s['max_risk_frac'] * 100, 'risk_units': risk_units,
            'edge_pct': s['edge_min'] * 100, 'pool': P_total,
            'n_formed': total_units - n_unformed, 'n_unformed': n_unformed,
            'sleeve_on': bool(s.get('unformed_sleeve'))}
    else:
        # CSV未指定: 損益分岐表
        ranked = sorted(combo_prob.items(), key=lambda x: x[1], reverse=True)
        be = []
        for idx, p in ranked[:max(int(s['topn']), 1)]:
            be.append({'combo': ' → '.join(disp[i] for i in idx),
                       'model_p': p, 'need_od': (1 / p if p > 0 else float('inf'))})
        res['breakeven_rows'] = be
        res['mode'] = None
        res['summary'] = None
        res['alloc_rows'] = []
        res['bare_used'] = False
        res['unmatched_names'] = []

    # 的中確率ランキング（1口購入時の実効オッズ付き）
    ranked_p = sorted(combo_prob.items(), key=lambda x: x[1], reverse=True)
    shown = ranked_p[:max(int(s['topn']), 1)]
    ranking, cum = [], 0.0
    for rk, (idx, p) in enumerate(shown, 1):
        cum += p
        names = tuple(disp[i] for i in idx)
        od = od_of(names)
        flag = '成' if od else '未'
        if P_total > 0:
            P_c = (P_total / od) if od else 0.0
            eff = (P_total + STAKE_UNIT) / (P_c + STAKE_UNIT)
            ev1 = (p * eff - 1) * STAKE_UNIT
            ranking.append({'rank': rk, 'combo': ' → '.join(names), 'model_p': p,
                            'cum': cum, 'flag': flag, 'eff1_od': eff, 'ev1': ev1,
                            'plus_ev': p * eff > 1})
        else:
            ranking.append({'rank': rk, 'combo': ' → '.join(names), 'model_p': p,
                            'cum': cum, 'flag': flag, 'eff1_od': None, 'ev1': None,
                            'plus_ev': False})
    res['ranking'] = ranking
    res['ranking_pool_known'] = P_total > 0
    res['ranking_cover'] = min(cum, 1.0)
    res['horses_disp'] = list(disp)        # 結果入力(着順)ドロップダウン用
    return res


# ====================================================================
#  ベットログ（ローカルCSVに永続化）
# ====================================================================
LOG_COLUMNS = ['bet_id', 'time', 'race_id', 'combo', 'model_prob',
               'odds', 'stake', 'status', 'result', 'payout', 'pnl']


class BetLog:
    """賭けた✅と結果をローカルCSVに記録し、モデルの的中率を答え合わせする。"""

    def __init__(self, path, race_sigma=None, store=None):
        """store を渡すと外部ストレージ（Google Sheets 等）に読み書きする。
        store=None のときは従来どおりローカルCSV（self.path）を使う。
        store は read_df()->DataFrame と write_df(DataFrame) を持つオブジェクト。"""
        self.path = path
        self.race_sigma = race_sigma
        self.store = store

    def _coerce(self, df):
        """読み込んだ生データの型を整える（CSV/Sheets 共通）。"""
        for c in ['race_id', 'combo', 'status', 'result', 'time']:
            if c in df.columns:
                df[c] = df[c].fillna('').astype(str)
        for c in ['bet_id', 'model_prob', 'odds', 'stake', 'payout', 'pnl']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        return df

    def load(self):
        # 外部ストレージ（Google Sheets 等）
        if self.store is not None:
            try:
                df = self.store.read_df()
            except Exception:
                return pd.DataFrame(columns=LOG_COLUMNS)
            if df is None or len(df) == 0:
                return pd.DataFrame(columns=LOG_COLUMNS)
            return self._coerce(df)
        # ローカルCSV（従来動作）
        if os.path.exists(self.path):
            try:
                return self._coerce(pd.read_csv(self.path, encoding='utf-8-sig'))
            except Exception:
                pass
        return pd.DataFrame(columns=LOG_COLUMNS)

    def _save(self, df):
        if self.store is not None:
            self.store.write_df(df)
            return
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        df.to_csv(self.path, index=False, encoding='utf-8-sig')

    def race_horses(self, race_id):
        """指定レースで記録済みの買い目から、出走馬名（disp名）の一覧を復元。
        精算時の着順ドロップダウンを、直近の解析レースに依存せず正しく出すために使う。"""
        df = self.load()
        if len(df) == 0:
            return []
        sub = df[df['race_id'].astype(str) == str(race_id)]
        names = set()
        for combo in sub['combo'].astype(str):
            for nm in combo.split(' → '):
                nm = nm.strip()
                if nm:
                    names.add(nm)
        return sorted(names)

    def race_exists(self, race_id):
        df = self.load()
        return bool((df['race_id'].astype(str) == str(race_id)).any()) if len(df) else False

    def record(self, race_id, picks, unit=STAKE_UNIT):
        """picks: [(combo_tuple, model_prob, odds, units), ...] を pending として追記。"""
        df = self.load()
        base = int(df['bet_id'].max()) + 1 if len(df) else 1
        new = []
        for i, (combo, prob, od, units) in enumerate(picks):
            new.append({'bet_id': base + i,
                        'time': datetime.now().isoformat(timespec='seconds'),
                        'race_id': race_id, 'combo': ' → '.join(combo),
                        'model_prob': round(float(prob), 4), 'odds': float(od),
                        'stake': int(units) * int(unit), 'status': 'pending',
                        'result': '', 'payout': 0, 'pnl': 0})
        df = pd.concat([df, pd.DataFrame(new)], ignore_index=True)
        self._save(df)
        return len(new)

    def settle(self, race_id, order3):
        """order3=(1着,2着,3着) で当該レースの pending を精算。"""
        df = self.load()
        for col in ('payout', 'pnl', 'odds', 'stake'):       # int列へのfloat代入を防ぐ
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        actual = ' → '.join(order3)
        mask = (df['race_id'].astype(str) == str(race_id)) & (df['status'] == 'pending')
        cnt = 0
        for idx in df[mask].index:
            won = (df.at[idx, 'combo'] == actual)
            df.at[idx, 'status'] = 'won' if won else 'lost'
            df.at[idx, 'result'] = actual
            pay = float(df.at[idx, 'odds']) * float(df.at[idx, 'stake']) if won else 0.0
            df.at[idx, 'payout'] = pay
            df.at[idx, 'pnl'] = pay - float(df.at[idx, 'stake'])
            cnt += 1
        self._save(df)
        return cnt

    def undo_last(self):
        """直近 race_id のレコードを削除（取消）。戻り値: (race_id, 削除件数)。"""
        df = self.load()
        if len(df) == 0:
            return None, 0
        last_rid = df.iloc[-1]['race_id']
        keep = df[df['race_id'].astype(str) != str(last_rid)]
        self._save(keep)
        return last_rid, len(df) - len(keep)

    def report(self):
        """成績サマリを dict で返す（UIで描画）。"""
        df = self.load()
        if len(df) == 0:
            return {'empty': True, 'path': self.path}
        settled = df[df['status'].isin(['won', 'lost'])].copy()
        pending = int((df['status'] == 'pending').sum())
        out = {'empty': False, 'path': self.path, 'n_total': len(df),
               'n_settled': len(settled), 'n_pending': pending, 'buckets': [],
               'calib_hint': None, 'overall': None}
        if len(settled) == 0:
            return out
        stake = float(settled['stake'].sum())
        ret = float(settled['payout'].sum())
        pnl = ret - stake
        hits = int((settled['status'] == 'won').sum())
        out['overall'] = {
            'stake': stake, 'payout': ret, 'pnl': pnl,
            'roi': (pnl / stake * 100 if stake else 0),
            'hits': hits, 'n': len(settled),
            'hit_rate': hits / len(settled) * 100,
            'pred_rate': float(settled['model_prob'].mean()) * 100}
        bins = [0, 0.02, 0.05, 0.10, 0.20, 1.01]
        labels = ['0-2%', '2-5%', '5-10%', '10-20%', '20%+']
        settled['bucket'] = pd.cut(settled['model_prob'], bins=bins, labels=labels, right=False)
        for lab in labels:
            sub = settled[settled['bucket'] == lab]
            if len(sub):
                out['buckets'].append({
                    'label': lab, 'n': len(sub),
                    'pred': float(sub['model_prob'].mean()) * 100,
                    'real': float((sub['status'] == 'won').mean()) * 100,
                    'pnl': float(sub['payout'].sum() - sub['stake'].sum())})
        pred = float(settled['model_prob'].mean())
        real = float((settled['status'] == 'won').mean())
        rs = self.race_sigma
        if len(settled) >= 20:
            if real < pred * 0.7:
                out['calib_hint'] = (f'過信: 予測{pred*100:.2f}% > 実測{real*100:.2f}% → '
                                     + (f'SIGMA_OVERRIDE を大きく（例 {rs*1.3:.1f}）にして再学習。'
                                        if rs else 'σを大きくして再学習。'))
            elif real > pred * 1.3:
                out['calib_hint'] = (f'過小: 予測{pred*100:.2f}% < 実測{real*100:.2f}% → '
                                     + (f'SIGMA_OVERRIDE を小さく（例 {rs*0.7:.1f}）にして再学習。'
                                        if rs else 'σを小さくして再学習。'))
            else:
                out['calib_hint'] = (f'予測≈実測（{pred*100:.2f}% vs {real*100:.2f}%）→ '
                                     + (f'現在の RACE_SIGMA={rs:.2f} は妥当。' if rs else '現在のσは妥当。'))
        else:
            out['calib_hint'] = f'サンプル{len(settled)}件では不十分（最低20件目安）。'
        return out
