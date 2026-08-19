# -*- coding: utf-8 -*-
"""
oasis_core.py — Oasis（おあしすっち）レース予測コア  v2（2026/07/27 大型アプデ対応）
=====================================================================================
旧版からの主な変更点
--------------------
1. **パッシブ2枠対応**（2026/07/27 大型アプデ）
   ログ・貼り付けデータともに「A / B」形式の2スキルを解析し、35種すべてを特徴量化。
2. **スコア計算式の変更に対応**（2026/07/28「スコアはタイムによって計算」）
   score の絶対値・スケールが距離ごとに激変したため、学習ターゲットを
   **レース内で中心化した log スコア（相対値）** に変更。距離/馬場/地面といった
   レース単位の水準差が自動的に消え、将来また式が変わっても壊れにくい。
3. **モデルを RandomForest → 構造化リッジ回帰へ**
   実測データ（旧式2026/03/15-07/26 と 新式07/28以降）での検証:
     旧方式(RF・旧式データ学習)   レース内スピアマン 0.55 / 1着的中 19%
     新方式(リッジ・新式データ)    レース内スピアマン 0.84 / 1着的中 69%
   log(スピード/パワー/スタミナ)×距離 の交互作用を明示的に持たせ、少ないサンプルでも
   壊れない。α（正則化）はレース単位のクロスバリデーションで自動選択。
4. **σ（着順のブレ幅）を自動校正**
   OOF予測に対し「実際の着順が出る尤度」を最大化する σ を探索。旧版の残差std直用は
   過大（予測1着的中32% vs 実測69%）だった。
5. **攻略本のハードコード（距離係数・スタミナ足切り・パッシブ倍率）を全廃**
   すべて実データから学習。パッシブの効き目は学習後に一覧表示できる（数値の見える化）。
6. **16頭立て / 3連単は8頭以上のみ** など現行ルールを反映。単勝の推奨も追加。
7. ログは**フォルダ指定で複数ファイルをまとめて学習**できる。

主要API:
  train_model(log_path, ...) -> bundle(dict)
  analyze(raw_text, bundle, settings) -> 解析結果(dict)
  BetLog(path)  … ローカルCSVに賭けと結果を永続化
"""
from __future__ import annotations

import os
import re
import io
import glob
import math
import json
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

# oasis_app.py との組み合わせ検査に使う版番号。
# 機能を足したら上げること（app 側の REQUIRED_CORE と一致している必要がある）。
CORE_VERSION = '3.8.3'

# =====================================================================
#  0. ゲーム仕様の定数
# =====================================================================
STAKE_UNIT          = 10_000   # 3連単 1口 = 10,000 rrc
# 3連単プールの初期金。**サイト側のバグ**（2026/08/15 開発者に確認）:
#   プール総額の表示にはこの20万が含まれているが、購入画面のオッズは
#   (プール総額 − 20万) を分子にして計算されている。払戻はプール総額から出るので、
#   実際の払戻は表示オッズの P/(P−20万) 倍になる。プールが小さいほど差が大きい
#   （プール30万なら3.0倍、100万なら1.25倍、500万なら1.04倍）。
TRIFECTA_POOL_SEED  = 200_000
MAX_UNITS           = 20       # 1組あたり上限口数
MAX_TOTAL_UNITS     = 20       # 1レース合計口数の上限（2026/04/17 で 10→20）
WIN_MAX_UNITS       = 100      # 単勝の1頭あたり上限口数
WIN_MAX_TOTAL_UNITS = 100      # 単勝は【1レース合計】100口まで（全頭の合算）
WIN_STAKE_UNIT      = 1_000    # 単勝は 1口 = 1,000 rrc（購入画面の表記）
WIN_POOL_QUANTUM    = 1_000    # 単勝プール総額は 1,000 rrc 単位で決まる（全ベットが1口=1000rrcの倍数のため）
MIN_FIELD_TRIFECTA  = 8        # 2026/06/17: 7頭以下は3連単なし
MAX_FIELD           = 16       # 2026/04/20: 当選 8→16頭

N_SIM               = 400_000  # モンテカルロ試行数（16頭×3連単の裾を安定させるため増量）
SIM_CHUNK           = 50_000   # メモリ節約のための分割サイズ
SIM_SEED            = 42

ODDS_FLOOR          = 1.5      # 2026/02/25: 最低オッズ 1.1 → 1.5（＝未投票の初期値）
MARKET_EDGE_RATIO   = 1.3
MARKET_MIN_PROB     = 0.03

# --- ゲームのアップデート日（学習ウィンドウの決定に使う）---
PASSIVE_PATCH_DATE  = '2026/07/27'   # パッシブ2枠化・新スキル17種
SCORING_PATCH_DATE  = '2026/07/28'   # スコアがタイム基準に変更
BALANCE_PATCH_DATE  = '2026/03/15'   # 得意系パッシブの倍率下方修正
DEFAULT_TRAIN_FROM  = SCORING_PATCH_DATE
MIN_RACES_FOR_ERA   = 12             # これ未満なら旧式データも併用（時間減衰つき）

DUP_MARK = ' #'                 # 同名馬の内部マーカー
RANK_MAP = {'🥇': 1, '🥈': 2, '🥉': 3}

# =====================================================================
#  1. パッシブスキル・カタログ（2026/07/27 時点・全35種）
# =====================================================================
# kind: 'stat'=ステータス倍率系 / 'aptitude'=適性（距離・馬場が一致した時だけ有効）
#       'phase'=レース展開系（序盤/中盤/終盤など・2026/07/27 追加分）
PASSIVE_CATALOG = {
    # --- ステータス系（12） ---
    'スピードスター':   'stat', '脳筋':           'stat', 'マイペース':     'stat',
    '勝負師':           'stat', '器用貧乏':       'stat', '同族嫌悪':       'stat',
    'スピード大アップ': 'stat', 'パワー大アップ': 'stat', 'スタミナ大アップ': 'stat',
    'スピード小アップ': 'stat', 'パワー小アップ': 'stat', 'スタミナ小アップ': 'stat',
    # --- 適性系（6） ---
    '芝得意': 'aptitude', 'ダート得意': 'aptitude',
    '短距離得意': 'aptitude', 'マイル得意': 'aptitude',
    '中距離得意': 'aptitude', '長距離得意': 'aptitude',
    # --- 展開系（17・2026/07/27 追加） ---
    'ロケットスタート': 'phase', '二の脚':       'phase', '独走態勢':   'phase',
    '不屈':             'phase', 'ロングスパート': 'phase', '追い込み':   'phase',
    '中盤加速':         'phase', '競り合い':     'phase', '省エネ走法': 'phase',
    '差しの構え':       'phase', '緊急回復':     'phase', '安定感':     'phase',
    '粘り腰':           'phase', 'セカンドウインド': 'phase', '逃げの心得': 'phase',
    'ペース配分':       'phase', '末脚':         'phase',
}
PASSIVE_NAMES = list(PASSIVE_CATALOG.keys())

# 表記ゆれ（メモ・非公式資料など）→ 正式名
PASSIVE_ALIASES = {
    '省エネ走行': '省エネ走法', 'セカンドウィンド': 'セカンドウインド',
    'ロケットスタート ': 'ロケットスタート',
}

# =====================================================================
#  1-b. パッシブの実数値スペック（2026/07/27「パッシブスキルの数値を明記」対応）
# =====================================================================
# ゲームの購入画面に表示される説明文の数値をそのまま持つ。
#   mult      : ステータス倍率 {'speed':1.35, 'stamina':0.90} など
#   scope     : always / aptitude（距離・馬場一致時のみ）/ phase（区間限定）
#               / conditional（状況限定）/ variance（速度のばらつきに作用）
#   scope_arg : aptitude なら 'マイル' '芝' など、phase なら '序盤' など
#   duty      : 実質発動率（区間限定なら 1/3 など）。倍率は 1+(m-1)*duty で効かせる
#   sigma_mult: 着順のブレへの倍率（安定感 = 0.5）
#   source    : 'game'     … ゲーム内表記から取得（確定）
#               'inferred' … 対称性・実測から推定（実ログで最適値であることを検証済み）
# 貼り付けデータに説明文が含まれていれば自動で読み取り、passive_spec.json に貯めます。
PASSIVE_SPEC_SEED = {
    # ---- ステータス系（常時）----
    'スピードスター':   dict(mult={'speed': 1.35, 'stamina': 0.90}, scope='always', code='speed_star',
                       desc='スピードが35%上昇する代わりに、スタミナが10%低下する。'),
    '脳筋':             dict(mult={'power': 1.35, 'speed': 0.90}, scope='always', code='muscle_head',
                       desc='パワーが35%上昇する代わりに、スピードが10%低下する。'),
    'マイペース':       dict(mult={'stamina': 1.35, 'power': 0.90}, scope='always', code='steady_runner',
                       desc='スタミナが35%上昇する代わりに、パワーが10%低下する。'),
    '器用貧乏':         dict(mult={'speed': 1.05, 'power': 1.05, 'stamina': 1.05},
                       scope='always', code='jack_of_all', desc='全ステータスが5%上昇する。'),
    'スピード大アップ': dict(mult={'speed': 1.25}, scope='always', code='speed_l',
                       desc='スピードが25%上昇する。'),
    'パワー大アップ':   dict(mult={'power': 1.25}, scope='always', code='power_l',
                       desc='パワーが25%上昇する。'),
    'スタミナ大アップ': dict(mult={'stamina': 1.25}, scope='always', code='stamina_l',
                       desc='スタミナが25%上昇する。'),
    'スピード小アップ': dict(mult={'speed': 1.15}, scope='always', code='speed_s',
                       desc='スピードが15%上昇する。'),
    'パワー小アップ':   dict(mult={'power': 1.15}, scope='always', code='power_s',
                       desc='パワーが15%上昇する。'),
    'スタミナ小アップ': dict(mult={'stamina': 1.15}, scope='always', code='stamina_s',
                       desc='スタミナが15%上昇する。'),
    '勝負師':           dict(mult={'speed': 1.25, 'power': 1.25, 'stamina': 1.25},
                       scope='conditional', duty=0.05, code='gambler',
                       desc='レース開始時に5%の確率で発動し、全ステータスが25%上昇する。'),
    '同族嫌悪':         dict(mult={'speed': 1.20, 'power': 1.20, 'stamina': 1.20},
                       scope='same_species', code='same_kind_boost',
                       desc='同じ成体種のおあしすっちが出場している場合、全ステータスが20%上昇する。'),
    # ---- 適性系（距離・馬場が一致した時のみ）----
    '芝得意':           dict(mult={'speed': 1.10, 'power': 1.10, 'stamina': 1.10},
                       scope='aptitude', scope_arg='芝', code='turf_specialist',
                       desc='芝レースでは、全ステータスが10%上昇する。'),
    'ダート得意':       dict(mult={'speed': 1.10, 'power': 1.10, 'stamina': 1.10},
                       scope='aptitude', scope_arg='ダート', code='dirt_specialist',
                       desc='ダートレースでは、全ステータスが10%上昇する。'),
    '短距離得意':       dict(mult={'speed': 1.15, 'power': 1.15, 'stamina': 1.15},
                       scope='aptitude', scope_arg='短距離', code='short_special',
                       desc='短距離レースでは、全ステータスが15%上昇する。'),
    'マイル得意':       dict(mult={'speed': 1.15, 'power': 1.15, 'stamina': 1.15},
                       scope='aptitude', scope_arg='マイル', code='mile_special',
                       desc='マイルレースでは、全ステータスが15%上昇する。'),
    '中距離得意':       dict(mult={'speed': 1.15, 'power': 1.15, 'stamina': 1.15},
                       scope='aptitude', scope_arg='中距離', code='middle_special',
                       desc='中距離レースでは、全ステータスが15%上昇する。'),
    '長距離得意':       dict(mult={'speed': 1.15, 'power': 1.15, 'stamina': 1.15},
                       scope='aptitude', scope_arg='長距離', code='long_special',
                       desc='長距離レースでは、全ステータスが15%上昇する。'),
    # ---- 展開系（区間・状況限定。duty＝実質的な発動割合）----
    'ロケットスタート': dict(mult={'speed': 1.12}, scope='phase', scope_arg='序盤', duty=1 / 3,
                       code='rocket_start', desc='序盤区間のみ、スピードが12%上昇する。'),
    '中盤加速':         dict(mult={'power': 1.10}, scope='phase', scope_arg='中盤', duty=1 / 3,
                       code='mid_acceleration', desc='中盤区間のみ、パワーが10%上昇する。'),
    '末脚':             dict(mult={'power': 1.12}, scope='phase', scope_arg='終盤', duty=1 / 3,
                       code='final_kick', desc='終盤区間のみ、パワーが12%上昇する。'),
    '二の脚':           dict(mult={'speed': 1.08}, scope='phase', scope_arg='中盤', duty=0.15,
                       code='second_gear', desc='中盤開始後の200mのみ、スピードが8%上昇する。'),
    'ロングスパート':   dict(mult={'power': 1.07, 'stamina': 0.92}, scope='phase',
                       scope_arg='終盤', duty=0.50, code='long_spurt',
                       desc='中盤後半からパワーが7%上昇するが、スタミナ消費量が8%増加する。'),
    'ペース配分':       dict(mult={'speed': 0.95, 'stamina': 1.15}, scope='phase',
                       scope_arg='序盤', duty=1 / 3, code='pace_control',
                       desc='序盤のスピードが5%低下する代わりに、序盤のスタミナ消費量が15%減少する。'),
    '逃げの心得':       dict(mult={'speed': 1.08, 'power': 1.08, 'stamina': 1.08 * 0.88},
                       scope='phase', scope_arg='序盤', duty=1 / 3, code='front_runner',
                       desc='序盤の走行能力が8%上昇するが、序盤のスタミナ消費量が12%増加する。'),
    '差しの構え':       dict(mult={'power': 1.08}, scope='conditional', scope_arg='中盤',
                       duty=1 / 6, code='closer_stance',
                       desc='中盤開始時に順位が下位半分の場合、中盤のパワーが8%上昇する。'),
    '追い込み':         dict(mult={'power': 1.12}, scope='conditional', scope_arg='終盤',
                       duty=1 / 6, code='deep_closer',
                       desc='終盤開始時に順位が下位半分の場合、終盤のパワーが12%上昇する。'),
    '競り合い':         dict(mult={'power': 1.06}, scope='conditional', duty=0.50,
                       code='duel_spirit', desc='他の出走馬が20m以内にいる間、パワーが6%上昇する。'),
    '独走態勢':         dict(mult={'stamina': 1.06}, scope='conditional', duty=0.20,
                       code='solo_lead',
                       desc='先頭で2位と50m以上離れている間、スタミナ消費量が6%減少する。'),
    '不屈':             dict(mult={'power': 1.08}, scope='conditional', duty=0.10,
                       code='indomitable',
                       desc='他の出走馬に追い抜かれた直後の100mのみ、パワーが8%上昇する。'),
    '緊急回復':         dict(mult={'stamina': 1.06}, scope='conditional', duty=0.50,
                       code='emergency_recovery',
                       desc='残りスタミナが20%以下になったとき、一度だけ最大スタミナの6%を回復する。'),
    'セカンドウインド': dict(mult={'stamina': 1.08}, scope='always', code='second_wind',
                       desc='中盤開始時に、最大スタミナの8%を回復する。'),
    '省エネ走法':       dict(mult={'stamina': 1.08}, scope='always', code='energy_saver',
                       desc='レース中のスタミナ消費量が8%減少する。'),
    '安定感':           dict(mult={}, scope='variance', sigma_mult=0.5, code='consistency',
                       desc='レース中の速度のばらつきが約半分になり、能力どおりに走りやすくなる。'),
    # 粘り腰は「スタミナ不足による速度低下を20%軽減」でステータス倍率に落ちないため、
    # 数値は入れず実ログから効果を学習する。
    '粘り腰':           dict(mult={}, scope='learned', code='tenacious',
                       desc='スタミナ不足による速度低下を20%軽減する。'),
}
for _v in PASSIVE_SPEC_SEED.values():
    _v.setdefault('source', 'game')      # 全35種ともゲーム内表記から取得

# API のコード（passive_skill / passive_skill_2）→ 日本語名
PASSIVE_CODE_MAP = {v['code']: k for k, v in PASSIVE_SPEC_SEED.items() if v.get('code')}

# レース中のブレのうち「ゲーム側のランダム性」が占める割合。残りはモデルの推定誤差。
# 安定感のような分散低減スキルは、この割合の部分にだけ効かせる（安全側）。
#
# 経緯: 2026/08 のプチ修正で乱数幅 1.5% → 3% に拡大（game_var ×4 → 割合 4/5 = 0.8）。
#       2026/08/17 に開発者告知で **3% → 1.5% に戻された**ので 0.5 に戻す。
#         game_var ≒ model_var → 割合 = 1/2 = 0.5
# これに連動して「安定感」など分散低減スキルの価値が元に戻る。
# （σそのものは自動校正に任せる。手動での水増しはロングショット過大評価につながり逆効果。）
VARIANCE_SHARE = 0.5

# レース中、各馬の実効ステータスにレースごとに掛かる乱数幅（±割合）。
# 表示・資料用の定数（σは自動校正なので計算には直接使わない）。
# 1.5%（〜2026/08）→ 3%（2026/08のプチ修正）→ **1.5%（2026/08/17 に戻された）**。
STAT_RNG_WIDTH = 0.015
STAT_RNG_WIDTH_PREV = 0.03

SPEC_FILE = 'passive_spec.json'      # 学習した数値を貯めるファイル（アプリと同じ場所）

DIST_LIST  = ['短距離', 'マイル', '中距離', '長距離']
TRACK_LIST = ['芝', 'ダート']
COND_LIST  = ['好調', '普通', '不調']

# ---------------------------------------------------------------------
# ゲーム内部の着順スコア式（閲覧サイトの result API から逆解析）
#   rating ＝ 定数 × Σ_区間 Σ_stat( 区間重み[区間][stat] × 距離バランス[距離][stat]
#                                     × 実効ステータス ) × 疲労補正
#   ・stat の順序は [スピード, パワー, スタミナ]
#   ・疲労補正は 1.0 近辺の小さな係数（スタミナ切れの馬だけ下がる）
# 予測モデルはこの式を「丸写し」にせず参考にとどめる（リスケール・未知の相互作用に強く
# するため）。表示用にここへ確定値として置いておく。
INTERNAL_PHASE_WEIGHTS = {           # 区間重み[区間] = [SP, PW, ST]
    '序盤': [0.60, 0.30, 0.10],
    '中盤': [0.45, 0.20, 0.35],
    '終盤': [0.35, 0.35, 0.30],
}
INTERNAL_DIST_BALANCE = {            # 距離バランス[距離] = [SP, PW, ST]
    '短距離': [1.4, 0.8, 0.5],
    'マイル': [1.0, 1.0, 1.0],
    '中距離': [0.9, 1.3, 1.3],
    '長距離': [0.6, 1.0, 1.4],
}


# --- スタミナ収支（result API の timeline から実測 / 999頭）---
# 残スタミナ = 初期スタミナ − Σ(100mごとの消費)。実測 98.3パーセントがこの式どおり。
#   消費/100m = clamp(c[距離] × Σ(序盤重み × 距離バランス × 実効ステータス), lo, hi)
#   必要スタミナ = 消費 × 区間数
# c が距離ごとに違うのは INTERNAL_DIST_BALANCE の絶対倍率のズレを吸収しているため。
# 同一レースの全馬が同じ距離なのでレース内順位には影響しない。
# lo/hi は観測された最小・最大。外挿すると偽のスタミナ切れを量産するので外挿しない。
# 2026/08/16 再測定: stamina_report.py の突き合わせキーが (日付, 馬名) で、1日6レース
# あるため後のレースが前を上書きしていた（3,389頭中1,025件）。キーに時刻を足して
# 衝突ゼロにしたうえで測り直した値。突き合わせ 999→1225頭、残スタミナの検算 79.9→86.7%。
# 変化は小さく（c は1〜2%、境界は短距離のみ）、予測への影響はほぼ無い。
STAMINA_COST_LAW = {
    '短距離': {'c': 0.0132, 'lo': 2.125, 'hi': 2.879, 'n_seg': 10},
    'マイル': {'c': 0.0197, 'lo': 2.234, 'hi': 3.067, 'n_seg': 15},
    '中距離': {'c': 0.03065, 'lo': 2.541, 'hi': 3.737, 'n_seg': 20},
    '長距離': {'c': 0.04109, 'lo': 2.57, 'hi': 3.68, 'n_seg': 25},
}


def stamina_budget(eff, dist):
    """(必要スタミナ, 不足, 余り) を返す。eff は effective_stats() の戻り値。

    「余り」は疲労補正を最大 +3% まで上げる（1.030 で頭打ち）。
    「不足」は最後に減速する量で、こちらは −35% まで落ちる（下限 0.650）。
    **効き方が左右で非対称**なので、1列にまとめると表現できない。**両方を特徴量にするのが肝**で、片方だけだと
    「必要量のところでスタミナの価値が折れる」形を表現できず、かえって精度が落ちる。
    """
    L = STAMINA_COST_LAW.get(dist)
    if not L:
        return 0.0, 0.0, 0.0
    w = INTERNAL_PHASE_WEIGHTS['序盤']
    b = INTERNAL_DIST_BALANCE.get(dist, [1.0, 1.0, 1.0])
    base = (eff['speed'] * w[0] * b[0] + eff['power'] * w[1] * b[1]
            + eff['stamina'] * w[2] * b[2])
    need = min(max(L['c'] * base, L['lo']), L['hi']) * L['n_seg']
    have = math.floor(eff['stamina'])
    return need, max(0.0, need - have), max(0.0, have - need)


def internal_stat_weights(dist):
    """距離ごとの「実効重み」= 距離バランス × 区間重みの合計（区間の長さは等しいと近似）。
    -> {'SP':.., 'PW':.., 'ST':..} と、SP=1 に正規化した比。"""
    phase_sum = [sum(INTERNAL_PHASE_WEIGHTS[p][k] for p in INTERNAL_PHASE_WEIGHTS)
                 for k in range(3)]                      # [1.40, 0.85, 0.75]
    bal = INTERNAL_DIST_BALANCE.get(dist, [1.0, 1.0, 1.0])
    w = [bal[k] * phase_sum[k] for k in range(3)]
    base = w[0] if w[0] else 1.0
    return {'SP': w[0], 'PW': w[1], 'ST': w[2],
            'norm': [round(x / base, 3) for x in w]}

# 適性スキル → (照合する列, 照合する値)
APTITUDE_MATCH = {
    '芝得意': ('track', '芝'), 'ダート得意': ('track', 'ダート'),
    '短距離得意': ('dist', '短距離'), 'マイル得意': ('dist', 'マイル'),
    '中距離得意': ('dist', '中距離'), '長距離得意': ('dist', '長距離'),
}

# 交互作用（パッシブ×距離）の実効ペナルティを主効果より強くするための縮小係数。
# 小さいほど「距離ごとの効き目の違い」を学習しにくくなる＝サンプルが少ない間は安全側。
INTERACTION_SHRINK = 0.5


# =====================================================================
#  2. パッシブ名の正規化
# =====================================================================
_JP_RE = re.compile(r'[぀-ヿ一-鿿]')


def _strip_emoji(s: str) -> str:
    """先頭の絵文字・記号を落として日本語部分から始まる文字列にする。"""
    s = str(s).strip()
    m = _JP_RE.search(s)
    return s[m.start():].strip() if m else s


def canonical_passive(s):
    """1つのパッシブ表記 → カタログ上の正式名 or None（なし）。
    未知の表記はそのまま返す（呼び出し側で「未学習」として警告）。"""
    if s is None:
        return None
    t = _strip_emoji(s)
    t = re.sub(r'[\s　]+', '', t)
    if t in ('', 'なし', 'None', 'nan', '-', '—'):
        return None
    if t in PASSIVE_ALIASES:
        return PASSIVE_ALIASES[t]
    if t in PASSIVE_CATALOG:
        return t
    # 部分一致（説明文が混ざっている等）。長い名前から順に照合。
    for name in sorted(PASSIVE_NAMES, key=len, reverse=True):
        if name in t:
            return name
    return t          # 未知（新スキル追加時など）


def parse_passives(s):
    """'💪 脳筋 / 💣 パワー大アップ' → ('脳筋', 'パワー大アップ')。
    区切りは / ・ 、 , ＋ + / 全角スラッシュに対応。最大2つ（ゲーム仕様）。"""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ()
    text = str(s).strip()
    if not text or text in ('なし', 'nan', 'None'):
        return ()
    parts = re.split(r'[／/・、,＋+｜|]+', text)
    out = []
    for p in parts:
        c = canonical_passive(p)
        if c and c not in out:
            out.append(c)
    return tuple(out[:2])


def normalize_passive(s):
    """後方互換: 表示用に 'A / B' の正規化文字列を返す。"""
    ps = parse_passives(s)
    return ' / '.join(ps) if ps else 'なし'


# =====================================================================
#  2-b. パッシブ説明文 → 数値スペックの自動抽出
# =====================================================================
_STAT_JA = {'スピード': 'speed', 'パワー': 'power', 'スタミナ': 'stamina', '全ステータス': 'all'}
# 装備の効果文は「スピードが**常時**4.4%上昇」のように、が と数字の間に語が入る。
# 埋め草は 。、％ と数字を含まない6文字までに限る（別の文や別ステータスをまたがない）。
# 方向語（上昇/低下/…）を必須にしているので「残りスタミナが20%以下に」は拾わない。
_PCT_RE = re.compile(r'(スピード|パワー|スタミナ|全ステータス)が[^。、%％\d]{0,6}'
                     r'(\d+(?:\.\d+)?)[%％](上昇|低下|アップ|ダウン)')


_ABILITY_RE = re.compile(r'走行能力が(\d+(?:\.\d+)?)[%％](上昇|低下)')
_PROB_RE = re.compile(r'(\d+(?:\.\d+)?)[%％]の確率で発動')
_CONSUME_RE = re.compile(r'スタミナ消費量[^。]*?(\d+(?:\.\d+)?)[%％](増加|減少)')
_RECOVER_RE = re.compile(r'(?:最大)?スタミナの(\d+(?:\.\d+)?)[%％]を?回復')


def _pct_mults(d):
    """説明文中の『○○が N% 上昇/低下』をすべて倍率にする。
    スタミナ消費量の増減・最大スタミナの回復も、実効スタミナの倍率に換算する
    （消費8%減 = 走れる距離が増える = スタミナ×1.08 と同じ扱い）。"""
    mult = {}
    for pct, dirn in _CONSUME_RE.findall(d):
        v = 1 + float(pct) / 100 if dirn == '減少' else 1 - float(pct) / 100
        mult['stamina'] = mult.get('stamina', 1.0) * v
    for pct in _RECOVER_RE.findall(d):
        mult['stamina'] = mult.get('stamina', 1.0) * (1 + float(pct) / 100)
    for pct, dirn in _ABILITY_RE.findall(d):          # 「走行能力が8%上昇」＝全ステータス
        v = 1 + float(pct) / 100 if dirn == '上昇' else 1 - float(pct) / 100
        for k in ('speed', 'power', 'stamina'):
            mult[k] = mult.get(k, 1.0) * v
    for st, pct, dirn in _PCT_RE.findall(d):
        v = 1 + float(pct) / 100 if dirn in ('上昇', 'アップ') else 1 - float(pct) / 100
        keys = ['speed', 'power', 'stamina'] if _STAT_JA[st] == 'all' else [_STAT_JA[st]]
        for k in keys:
            mult[k] = mult.get(k, 1.0) * v
    return mult


def spec_from_description(desc):
    """『スピードが35%上昇する代わりに、スタミナが10%低下する。』のような説明文から
    倍率・適用範囲・発動率を読み取る。読めなければ None。"""
    if not desc:
        return None
    d = str(desc).strip()

    # --- ばらつき低減系（パッシブ『安定感』、お守り『安定の加護』など）---
    # 実物: 「レース中の乱数幅を1.9%狭める」（charm_consistency・常時発動）。
    # ステータス倍率ではないので _pct_mults では拾えず、ここで σ 側に落とす。
    if re.search(r'ばらつき|ブレ|乱数幅', d) and not _PCT_RE.search(d):
        m = re.search(r'約?(半分|\d+(?:\.\d+)?[%％])', d)
        sg = 0.5
        if m and m.group(1) not in ('半分',):
            sg = 1.0 - float(re.sub(r'[%％]', '', m.group(1))) / 100.0
        return dict(mult={}, scope='variance', scope_arg=None, duty=1.0,
                    sigma_mult=max(0.05, sg), source='game', desc=d)

    mult = _pct_mults(d)
    if not mult:
        return None

    scope, scope_arg, duty = 'always', None, 1.0
    pm = _PROB_RE.search(d)                            # 「5%の確率で発動」
    if pm:
        return dict(mult=_pct_mults(d), scope='conditional', scope_arg=None,
                    duty=float(pm.group(1)) / 100, sigma_mult=1.0, source='game', desc=d)
    if re.search(r'回復', d) and not re.search(r'場合|以下になったとき', d):
        return dict(mult=_pct_mults(d), scope='always', scope_arg=None, duty=1.0,
                    sigma_mult=1.0, source='game', desc=d)
    if re.search(r'同じ(?:成体種|おあしすっち|馬|種類|キャラ)', d) or re.search(r'同族', d):
        return dict(mult=_pct_mults(d), scope='same_species', scope_arg=None,
                    duty=1.0, sigma_mult=1.0, source='game', desc=d)
    m = re.search(r'(短距離|マイル|中距離|長距離|芝|ダート)(?:レース|コース|馬場)?では', d)
    if m:
        scope, scope_arg = 'aptitude', m.group(1)
    ph = re.search(r'(序盤|中盤|終盤)', d)
    # 「中盤開始時に」は発動タイミングであって条件ではないので除外する
    _d2 = re.sub(r'(序盤|中盤|終盤)開始時に', '', d)
    cond = bool(re.search(r'場合|[いてっ]る間|直後|以内|になったとき|た場合', _d2))
    if ph and scope == 'always':
        scope, scope_arg, duty = 'phase', ph.group(1), 1.0 / 3
    if cond and scope in ('always', 'phase'):
        if re.search(r'直後の\s*\d+\s*m', d):
            duty = 0.10
        elif re.search(r'[いてっ]る間|以内', d):
            duty = 0.50
        elif re.search(r'一度だけ', d):
            duty = 0.50
        elif re.search(r'下位半分|上位半分', d):
            duty = (1.0 / 3) * 0.5
        else:
            duty = 0.30
        scope = 'conditional'
    return dict(mult=mult, scope=scope, scope_arg=scope_arg, duty=duty,
                sigma_mult=1.0, source='game', desc=d)


_DESC_BLOCK_RE = re.compile(
    r'^\s*\d+\s*[.．、]\s*([^\n]+)\n\s*([^\n]*?(?:上昇|低下|アップ|ダウン|ばらつき)[^\n]*)', re.M)


def parse_passive_descriptions(text):
    """購入画面テキストから『1. ⚡ スピードスター / 説明文』の組を拾ってスペック化。
    -> {パッシブ名: spec}"""
    out = {}
    for m in _DESC_BLOCK_RE.finditer(str(text)):
        name = canonical_passive(m.group(1))
        spec = spec_from_description(m.group(2))
        if name and spec and name not in out:
            out[name] = apply_measured_duty(name, spec)
    return out


# timeline の activated_passives から実測した発動割合（2026/08/17・v2の24,110区間）。
# 説明文からの推定（下位半分なら 1/3×0.5、など）は当て推量で、実測と最大13倍ずれていた。
# **説明文パースより優先する**。貼り付けで passive_spec.json が上書きされても戻らないよう、
# spec を組み立てる3経路すべてでここを最後に適用する。
#   ⚠ 勝負師は「レース開始時に発動」で timeline に出ない（initial 側で 3/38 = 7.9%、
#     spec の 0.05 と矛盾しないのでそのまま）。常時発動系も timeline に出ないので載せない。
DUTY_MEASURED = {
    '緊急回復': 0.037,        # 推定 0.500（「一度だけ」を 0.5 と見ていた。実際は1区間だけ）
    '独走態勢': 0.000,        # 推定 0.200（36頭・全区間で一度も発動せず＝実質無価値）
    '差しの構え': 0.271,      # 推定 0.167
    '追い込み': 0.254,        # 推定 0.167
    '中盤加速': 0.400,        # 推定 0.333
    'ロケットスタート': 0.287,  # 推定 0.333（序盤は区間数の1/3よりやや短い）
    '逃げの心得': 0.287,      # 推定 0.333
    'ペース配分': 0.290,      # 推定 0.333
    '末脚': 0.315,           # 推定 0.333
    '不屈': 0.065,           # 推定 0.100
    '二の脚': 0.128,         # 推定 0.150
    '競り合い': 0.514,        # 推定 0.500
    'ロングスパート': 0.488,   # 推定 0.500
}


def apply_measured_duty(name, sp):
    """実測した発動割合があれば duty を差し替える。無ければそのまま。"""
    d = DUTY_MEASURED.get(name)
    if d is not None and isinstance(sp, dict):
        sp = dict(sp)
        sp['duty'] = float(d)
    return sp


def _norm_spec(sp):
    """欠けている項目を既定値で埋める。"""
    d = dict(mult={}, scope='always', scope_arg=None, duty=1.0,
             sigma_mult=1.0, source='inferred', desc='')
    d.update({k: v for k, v in dict(sp).items() if v is not None})
    d['mult'] = {k: float(v) for k, v in (d.get('mult') or {}).items()}
    d['duty'] = float(d.get('duty', 1.0))
    d['sigma_mult'] = float(d.get('sigma_mult', 1.0))
    return d


def default_spec():
    return {k: apply_measured_duty(k, _norm_spec(v)) for k, v in PASSIVE_SPEC_SEED.items()}


def load_passive_spec(path=None):
    """既定スペック＋保存済みJSONをマージして返す。JSON側（ゲーム表記）が優先。"""
    spec = default_spec()
    if not path:
        return spec
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
            for k, v in (saved or {}).items():
                v = apply_measured_duty(k, _norm_spec(v))
                # ゲーム表記は推定値を上書きする。推定同士なら既定を優先。
                if v['source'] == 'game' or k not in spec:
                    spec[k] = v
    except Exception:
        pass
    return spec


def save_passive_spec(spec, path):
    """ゲーム表記から取れたものだけ保存する（推定値は毎回コードから読む）。"""
    try:
        keep = {k: v for k, v in spec.items() if v.get('source') == 'game'}
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(keep, f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def merge_passive_spec(spec, learned, path=None):
    """新しく読み取ったスペックを取り込む。 -> (新spec, 追加/更新された名前のリスト)"""
    spec = dict(spec)
    changed = []
    for name, sp in (learned or {}).items():
        sp = _norm_spec(sp)
        cur = spec.get(name)
        if cur is None or cur.get('source') != 'game' or cur.get('mult') != sp['mult'] \
                or cur.get('sigma_mult') != sp['sigma_mult']:
            spec[name] = sp
            changed.append(name)
    if path and (changed or learned):
        save_passive_spec(spec, path)      # 変化が無くても保存し、次回以降も残るようにする
    return spec, changed


# =====================================================================
#  2-c. スペックを使った実効ステータス
# =====================================================================
def effective_stats(speed, power, stamina, passives, dist, track, spec, ctx=None):
    """パッシブの倍率を反映した実効ステータスを返す。
    区間限定・状況限定のスキルは 1+(倍率-1)×発動率 として部分的に効かせる。
    ctx: {'same_species': bool} … レースの顔ぶれに依存するスキル（同族嫌悪）の発動条件。"""
    ctx = ctx or {}
    v = {'speed': float(speed), 'power': float(power), 'stamina': float(stamina)}
    for p in (passives or ()):
        sp = spec.get(p)
        if not sp or not sp.get('mult'):
            continue
        if sp['scope'] == 'aptitude' and sp.get('scope_arg') not in (dist, track):
            continue
        if sp['scope'] == 'same_species' and not ctx.get('same_species'):
            continue
        duty = min(max(sp.get('duty', 1.0), 0.0), 1.0)
        for k, m in sp['mult'].items():
            if k in v:
                v[k] *= (1.0 + (float(m) - 1.0) * duty)
    for k in v:
        v[k] = max(v[k], 1.0)
    return v


def sigma_multiplier(passives, spec, variance_share=VARIANCE_SHARE, extra_mult=1.0):
    """安定感のような分散低減スキルによる σ の倍率。
    σ全体のうち variance_share だけが『ゲーム側のランダム性』とみなし、そこにだけ効かせる
    （残りはモデルの推定誤差なのでスキルでは減らない）。

    extra_mult: パッシブ以外の分散低減（お守り『安定の加護』など）の倍率。
    """
    m = float(extra_mult or 1.0)
    for p in (passives or ()):
        sp = spec.get(p)
        if sp:
            m *= float(sp.get('sigma_mult', 1.0))
    if m == 1.0:
        return 1.0
    f = min(max(variance_share, 0.0), 1.0)
    return math.sqrt(f * m * m + (1.0 - f))


# =====================================================================
#  3. Discordログの解析
# =====================================================================
_ENTRY_PAT = re.compile(
    r'🏇[^\n]*?(?:第(\d+)レース\s*)?出走決定（(\d{1,2}:\d{2})）[^\n]*\n'
    r'([^\n｜]+)｜([^\n｜]+)｜([^\n\r]+)'
    r'(.*?)(?=🏇[^\n]*?出走決定|🏁[^\n]*?レース\s*結果|\Z)', re.S)

_RESULT_PAT = re.compile(
    r'🏁[^\n]*?(?:第(\d+)レース\s*)?結果\s*\n'
    r'🕘\s*(\d{1,2}:\d{2})｜([^\n｜]+)｜([^\n｜]+)｜([^\n\r]+)'
    r'(.*?)(?=🏇[^\n]*?出走決定|🏁[^\n]*?レース\s*結果|\Z)', re.S)

_DATE_PAT = re.compile(r'\[(\d{4}/\d{2}/\d{2}) \d{1,2}:\d{2}\]')


def _owner(s):
    return s.strip().rstrip('\r') if s else 'unknown'


def _date_before(text, pos):
    dm = _DATE_PAT.findall(text[max(0, pos - 2500):pos])
    return dm[-1] if dm else '????'


def _race_key(date, r_time, race_no):
    """レースを識別するキー。

    従来は「日付 時刻」だけだったため、同日同時刻の別レース（レース番号違い・
    harvest の time フォールバック等）が **1レースに合成** されてしまった。
    レース番号が取れている場合はキーに含めて衝突を防ぐ。
    先頭2トークン（日付 時刻）は「ベースキー」として、ソース間の突合に使う。"""
    key = f"{date} {r_time}"
    if race_no:
        key += f" 第{race_no}R"
    return key


def _base_key(race_key):
    """race_key からレース番号を除いた「日付 時刻」部分。
    Discordログと harvest ログでレース番号の表記が違っても、同一レースなら
    ベースキーは一致するので、ソースをまたいだ重複除去に使う。"""
    return ' '.join(str(race_key).split(' ')[:2])


def horse_identity(name, owner, sp, st, pw):
    return (str(name).strip(), str(owner).strip(), int(sp), int(st), int(pw))


def parse_entries(text):
    """『出走決定』セクションを全部抜き出す。 -> {race_key: {...}}"""
    entries = {}
    for m in _ENTRY_PAT.finditer(text):
        race_no = m.group(1)
        r_time = m.group(2).zfill(5)
        dist, track, g_cond = (g.strip().rstrip('\r') for g in m.group(3, 4, 5))
        body = m.group(6)
        date = _date_before(text, m.start())
        r_key = _race_key(date, r_time, race_no)

        fm = re.search(r'出走頭数[:：]\s*(\d+)\s*/\s*(\d+)', body)
        field_n = int(fm.group(1)) if fm else None

        horses = []
        for blk in re.split(r'【枠番\s*\d+】', body)[1:]:
            n_m = re.search(r'🐣\s*([^\n\r]+)', blk)
            o_m = re.search(r'👤\s*(@[^\n\r]+)', blk)
            s_m = re.search(r'スピード\s*[:：]\s*(\d+)', blk)
            st_m = re.search(r'スタミナ\s*[:：]\s*(\d+)', blk)
            p_m = re.search(r'パワー\s*[:：]\s*(\d+)', blk)
            c_m = re.search(r'コンディション\s*[:：]\s*([^\s\r\n😄😐😞🙁😰]+)', blk)
            pa_m = re.search(r'✨\s*パッシブ\s*[:：]\s*([^\n\r]+)', blk)
            if n_m and s_m and st_m and p_m:
                horses.append({
                    'name': n_m.group(1).strip(),
                    'owner': _owner(o_m.group(1) if o_m else None),
                    'speed': int(s_m.group(1)),
                    'stamina': int(st_m.group(1)),
                    'power': int(p_m.group(1)),
                    'condition': c_m.group(1).strip() if c_m else '普通',
                    'passives': parse_passives(pa_m.group(1)) if pa_m else (),
                })
        entries[r_key] = {'dist': dist, 'track': track, 'g_cond': g_cond,
                          'race_no': race_no, 'field_n': field_n, 'horses': horses}
    return entries


def parse_results(text):
    """『レース 結果』セクションを全部抜き出す。 -> [row, ...]"""
    rows = []
    for m in _RESULT_PAT.finditer(text):
        race_no = m.group(1)
        r_time = m.group(2).zfill(5)
        dist, track, g_cond = (g.strip().rstrip('\r') for g in m.group(3, 4, 5))
        body = m.group(6)
        date = _date_before(text, m.start())
        r_key = _race_key(date, r_time, race_no)

        for block in re.split(r'(?=🥇|🥈|🥉|(?<!\d)\d{1,2}着)', body):
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
            od_m = re.search(r'(?:最終)?オッズ\s*[:：]?\s*([0-9.]+)', block)
            # 結果ブロックに直接コンディションが書かれている場合（API採取ログ等）はそれを使う。
            cd_m = re.search(r'コンディション\s*[:：]\s*([^\s\r\n😄😐😞🙁😰]+)', block)
            if s_m and st_m and p_m and sc_m:
                rows.append({
                    'race_key': r_key, 'race_no': race_no,
                    'rank': RANK_MAP.get(hm.group(1)) or int(hm.group(2)),
                    'name': hm.group(3).strip(), 'owner': _owner(hm.group(4)),
                    'speed': int(s_m.group(1)), 'stamina': int(st_m.group(1)),
                    'power': int(p_m.group(1)), 'score': float(sc_m.group(1)),
                    'passives': parse_passives(pa_m.group(1)) if pa_m else (),
                    'win_odds': float(od_m.group(1)) if od_m else np.nan,
                    'condition': cd_m.group(1).strip() if cd_m else None,
                    'dist': dist, 'track': track, 'g_cond': g_cond,
                })
    return rows


def _iter_log_files(log_path):
    """ファイル / フォルダ / グロブ を受け取り、対象テキストファイルの一覧を返す。"""
    p = os.path.expanduser(str(log_path or '').strip())
    if not p:
        return []
    if os.path.isdir(p):
        files = sorted(glob.glob(os.path.join(p, '*.txt')) + glob.glob(os.path.join(p, '*.md')))
        return files
    if any(ch in p for ch in '*?['):
        return sorted(glob.glob(p))
    return [p] if os.path.exists(p) else []


# 結果が壊れているレースは学習にも解析にも使わない（運営告知など）。
# `schedule_id`（int）でも '日付 時刻'（'2026-08-19 12:00'）でも書ける。
# Discordログ側には schedule_id が無いので、両方書いておくと確実に外れる。
EXCLUDED_RACES = {
    2037, '2026-08-19 12:00',   # 中距離13頭。結果がバグと運営告知（2026/08/19）
}


def _race_time_key(date, time):
    """'2026/08/19' + '12:00' → '2026-08-19 12:00'。片方でも無ければ None。"""
    if not date or not time:
        return None
    d = str(date).strip().replace('/', '-')
    t = str(time).strip()[:5]
    if len(t) == 4:                      # '9:00' → '09:00'
        t = '0' + t
    return f'{d} {t}'


def is_excluded_race(schedule_id=None, date=None, time=None):
    """このレースを学習・解析から外すか。"""
    if schedule_id is not None:
        try:
            if int(schedule_id) in EXCLUDED_RACES:
                return True
        except (TypeError, ValueError):
            pass
    k = _race_time_key(date, time)
    return bool(k and k in EXCLUDED_RACES)


def parse_race_log(log_path=None, texts=None):
    """ログ（ファイル/フォルダ/グロブ、または文字列のリスト）→ 1行1頭の DataFrame。"""
    all_rows, all_entries = [], {}
    chunks = list(texts) if texts else []
    for f in _iter_log_files(log_path):
        try:
            chunks.append(open(f, encoding='utf-8').read())
        except UnicodeDecodeError:
            chunks.append(open(f, encoding='utf-8', errors='replace').read())
    for text in chunks:
        all_entries.update(parse_entries(text))
        all_rows.extend(parse_results(text))

    # レース番号の表記ゆれ（結果側に番号が無い等）に備えたベースキーの照合表。
    # 同じ「日付 時刻」に複数エントリがある場合は曖昧なのでフォールバックしない。
    base_entries = {}
    for k, e in all_entries.items():
        base_entries.setdefault(_base_key(k), []).append(e)

    out = []
    for r in all_rows:
        passives = r['passives']
        condition = r.get('condition') or '不明'
        ent = all_entries.get(r['race_key'])
        if ent is None:
            cand = base_entries.get(_base_key(r['race_key']))
            if cand and len(cand) == 1:
                ent = cand[0]
        if ent:
            target = horse_identity(r['name'], r['owner'], r['speed'], r['stamina'], r['power'])
            for h in ent['horses']:
                if horse_identity(h['name'], h['owner'], h['speed'],
                                  h['stamina'], h['power']) == target:
                    if not r.get('condition'):
                        condition = h['condition']
                    if not passives:
                        passives = h['passives']
                    break
        out.append({
            'race_key': r['race_key'], 'date': r['race_key'].split(' ')[0],
            'dist': r['dist'], 'track': r['track'], 'g_cond': r['g_cond'],
            'name': r['name'], 'owner': r['owner'],
            'speed': r['speed'], 'stamina': r['stamina'], 'power': r['power'],
            'condition': condition, 'passives': passives,
            'passive': ' / '.join(passives) if passives else 'なし',
            'rank': r['rank'], 'score': r['score'], 'win_odds': r['win_odds'],
        })
    df = pd.DataFrame(out)
    if len(df):
        before = len(df)
        # --- (1) 行レベル: 同じログを2回読んだ場合の完全重複 ---
        # 重要: キーに owner・name を使ってはいけない。harvest 採取ログは owner を
        # 常に '@Unknown' で出力し、同名馬の表記（'名前' vs '名前#1'）もソース間で
        # ずれるため、それらを含めると重複除去が一切効かなくなる。
        df = df.drop_duplicates(
            subset=['race_key', 'rank', 'speed', 'stamina', 'power'],
            keep='first').reset_index(drop=True)

        # --- (2) レースレベル: ソースをまたいだ同一レース ---
        # Discordログと harvest 採取ログでは race_key の作られ方が違う
        # （レース番号 vs schedule_id、時刻が取れず 0:00 になる等）ので、
        # キー文字列の一致では重複を検出できない。
        # 「距離・馬場＋(着順, SP, ST, PW) の並び」= レースの中身そのものを指紋にして
        # 突き合わせる。8頭立てでこれが偶然一致することは実質ありえない。
        def _sig(g):
            return (g['dist'].iloc[0], g['track'].iloc[0],
                    tuple(sorted(zip(g['rank'], g['speed'], g['stamina'], g['power']))))

        seen_sig, drop_keys = {}, []
        for k, g in df.groupby('race_key', sort=False):
            s = _sig(g)
            if s in seen_sig:
                drop_keys.append(k)
            else:
                seen_sig[s] = k
        df.attrs['n_dup_races'] = len(drop_keys)
        if drop_keys:
            df = df[~df['race_key'].isin(drop_keys)].reset_index(drop=True)

        df.attrs['n_duplicates'] = before - len(df)

        # --- (3) 結果が壊れているレースを外す ---
        # race_key は 'YYYY/MM/DD HH:MM 第NR' 形式。時刻が取れない行はそのまま残す。
        _rk = df['race_key'].astype(str).str.extract(r'^(\d{4}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})')
        _ex = pd.Series(
            [is_excluded_race(date=d, time=t) for d, t in zip(_rk[0], _rk[1])], index=df.index)
        if _ex.any():
            df.attrs['n_excluded_races'] = int(df.loc[_ex, 'race_key'].nunique())
            df.attrs['excluded_races'] = sorted(df.loc[_ex, 'race_key'].unique().tolist())
            df = df[~_ex].reset_index(drop=True)

        df['n_field'] = df.groupby('race_key')['score'].transform('size')
        df['_d'] = pd.to_datetime(df['date'], format='%Y/%m/%d', errors='coerce')
        # 日付が拾えなかった行（'????'）は学習フィルタで無言で消えるので数えておく
        df.attrs['n_bad_date'] = int(df['_d'].isna().sum())
    return df


# =====================================================================
#  4. 特徴量
# =====================================================================
def feature_names(spec):
    """スペックが分かっているパッシブは実効ステータスに畳み込むので、ダミー列は作らない。
    スペック未知のパッシブだけ、ダミー＋距離交互作用で学習する。"""
    names = []
    for d in DIST_LIST:
        # log項（頑健・逓減）＋ 内部式由来の線形項（加法的・スタミナの重みを正しく捉える）。
        # ゲームの実結果APIから、内部速度が Σ(フェーズ重み×距離係数×実効ステ) の
        # 加法形であることを確認済み。log単独だと中〜長距離でスタミナを過小評価するため、
        # 線形項を併用する（新式34レースの検証で held-out スピアマン 0.898→0.926）。
        names += [f'{d}:切片', f'{d}:log(SP)', f'{d}:log(PW)', f'{d}:log(ST)',
                  f'{d}:lin(SP)', f'{d}:lin(PW)', f'{d}:lin(ST)']
    # スタミナ収支（2列）。「余り＝無駄」と「不足＝減速」を別々の列にすることで、
    # 必要量のところでスタミナの価値が折れる形を表現できる。
    # 実測103レースで 1着的中 88.7→93.3、3着セット的中 60.7→70.5（ともに8/8分割で改善）。
    # レース内スピアマンは -0.006 と僅かに落ちるが、3連単で効くのは上位3頭の当たり方。
    # ⚠ 片方だけ足すと逆効果（余りだけ -6.2pt / 不足だけ +1.3pt で不安定）。必ず2列セットで。
    names += ['スタミナ余り', 'スタミナ不足']
    names += ['好調', '不調']
    for p in unspecced_passives(spec):
        names.append(p)
        if PASSIVE_CATALOG.get(p) != 'aptitude':
            for d in DIST_LIST:
                names.append(f'{p}×{d}')
    return names


def unspecced_passives(spec):
    """数値スペックが無い＝データから効果を学ぶしかないパッシブ。"""
    return [p for p in PASSIVE_NAMES if not (spec.get(p) or {}).get('mult')
            and (spec.get(p) or {}).get('scope') != 'variance']


def passive_from_code(code):
    """API の passive_skill コード（speed_star 等）→ 日本語名。"""
    if code is None:
        return None
    c = str(code).strip()
    if not c or c in ('none', 'null', 'None', 'nan'):
        return None
    return PASSIVE_CODE_MAP.get(c) or canonical_passive(c)


def _row_features(speed, power, stamina, condition, passives, dist, track, spec, ctx=None):
    e = effective_stats(speed, power, stamina, passives, dist, track, spec, ctx)
    sp = math.log(max(e['speed'], 1.0))
    pw = math.log(max(e['power'], 1.0))
    st = math.log(max(e['stamina'], 1.0))
    # 線形項は 1/100 スケール（log項と桁を揃え、正則化の効きを均す）
    lsp, lpw, lst = e['speed'] / 100.0, e['power'] / 100.0, e['stamina'] / 100.0
    f = []
    for d in DIST_LIST:
        m = 1.0 if dist == d else 0.0
        f += [m, m * sp, m * pw, m * st, m * lsp, m * lpw, m * lst]
    _, _short, _sur = stamina_budget(e, dist)
    f += [_sur / 10.0, _short / 10.0]           # 他の線形項と桁を揃える
    f += [1.0 if condition == '好調' else 0.0, 1.0 if condition == '不調' else 0.0]
    pset = set(passives or ())
    for p in unspecced_passives(spec):
        has = 1.0 if p in pset else 0.0
        if PASSIVE_CATALOG.get(p) == 'aptitude':
            col, val = APTITUDE_MATCH[p]
            ok = (track == val) if col == 'track' else (dist == val)
            f.append(has if ok else 0.0)
        else:
            f.append(has)
            for d in DIST_LIST:
                f.append(INTERACTION_SHRINK * has * (1.0 if dist == d else 0.0))
    return f


def build_features(df, spec):
    cols = len(feature_names(spec))
    X = np.empty((len(df), cols), dtype=float)
    if 'same_species' in df.columns:
        same = df['same_species'].fillna(False).astype(bool).tolist()
    elif 'race_key' in df.columns and 'name' in df.columns:
        same = np.zeros(len(df), dtype=bool)
        idx = np.arange(len(df))
        for _, g in df.groupby('race_key'):
            pos = idx[df.index.get_indexer(g.index)]
            for j, f in zip(pos, same_species_flags(g['name'].tolist())):
                same[j] = f
        same = same.tolist()
    elif 'name' in df.columns:
        same = same_species_flags(df['name'].tolist())
    else:
        same = [False] * len(df)
    for i, (_, r) in enumerate(df.iterrows()):
        X[i] = _row_features(r['speed'], r['power'], r['stamina'],
                             r.get('condition', '普通'), r.get('passives', ()),
                             r['dist'], r['track'], spec, {'same_species': bool(same[i])})
    return X


# =====================================================================
#  5. 学習
# =====================================================================
def _center_by_race(values, race_keys):
    s = pd.Series(values)
    return (s - s.groupby(pd.Series(list(race_keys))).transform('mean')).values


def _race_folds(race_keys, k=5, seed=0):
    uniq = sorted(set(race_keys))
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    fold_of = {r: i % k for i, r in enumerate(uniq)}
    return np.array([fold_of[r] for r in race_keys])


def _oof_predictions(X, y, groups, alpha, w=None, k=5):
    folds = _race_folds(groups, k=k)
    oof = np.zeros(len(y))
    for f in range(folds.max() + 1):
        te = folds == f
        tr = ~te
        if tr.sum() < 10 or te.sum() == 0:
            continue
        m = Ridge(alpha=alpha, fit_intercept=True)
        m.fit(X[tr], y[tr], sample_weight=(None if w is None else w[tr]))
        oof[te] = m.predict(X[te])
    # レース内で中心化（相対値なので水準は無意味）
    return _center_by_race(oof, groups)


def _truth_top(g, k=3):
    """「実際の上位k着」の位置インデックス。rank 列（ゲームの公式着順）を優先する。
    スコアで argsort すると同点レース（実測55/886件）で並びが不定になる。"""
    if 'rank' in g.columns and g['rank'].notna().all():
        order = np.argsort(g['rank'].values, kind='stable')
    else:
        order = np.argsort(-g['score'].values, kind='stable')
    return tuple(int(x) for x in order[:min(k, len(g))])


def _order_loglik(base, truth, sigma, z):
    """base(予測相対スコア) + sigma*z のモンテカルロで、実際の上位着順が出る確率の log。"""
    k = len(truth)
    sim = base[None, :] + sigma * z
    order = np.argsort(-sim, axis=1)[:, :k]
    hit = np.ones(len(sim), dtype=bool)
    for j in range(k):
        hit &= (order[:, j] == truth[j])
    p = hit.mean()
    return math.log(max(p, 1.0 / (len(sim) * 5.0)))


MAX_CALIB_RACES = 80          # σ校正に使う直近レース数の上限（計算時間の頭打ち）


def _recent_races(df, limit=MAX_CALIB_RACES, min_field=4):
    """直近 limit レースだけを (race_key, DataFrame) のリストで返す。"""
    races = [(k, g) for k, g in df.groupby('race_key') if len(g) >= min_field]
    races.sort(key=lambda kv: (kv[1]['_d'].iloc[0], kv[0]))
    return races[-limit:]


def _calibrate_sigma(oof, df, resid_std, n_draw=40_000, seed=7, min_field=4, top_k=3):
    """OOF予測に対して、実際の上位 top_k 着順の尤度が最大になる σ を選ぶ。

    top_k は**そのσを何に使うか**に合わせること。
      ・単勝用 → top_k=1（1着が誰かだけ当てればよい）
      ・3連単用 → top_k=3, min_field=8（順番まで当てる必要がある）
    2026/08/15 まで単勝用も top_k=3 で校正しており、σが大きく出て
    本命の勝率を過小評価していた（実測 予測72% vs 実測94%）。
    """
    races = _recent_races(df, min_field=min_field)
    if len(races) < 6:
        return max(resid_std * 0.6, 1e-4), []
    # 乱数を全レース分まとめて持つと 500MB 超になるので、レースごとに使い捨てる。
    # σの比較で同じ乱数を使いたいので、レースごとに固定シードで作り直す。
    rng_seeds = {k: seed * 1000003 + i for i, (k, g) in enumerate(races)}
    def score(s):
        tot = 0.0
        for k, g in races:
            z = np.random.default_rng(rng_seeds[k]).standard_normal((n_draw, len(g)))
            tot += _order_loglik(oof[g.index.values], _truth_top(g, top_k), s, z)
        return tot / len(races)

    # 粗く対数グリッドで当たりを付けてから、その周りだけ細かく見る。
    # 全域を細かく舐めると σ1つあたり数十万回のシミュレーションが要り重すぎる。
    # 旧実装は下限を resid_std の 0.15倍で切っていたため、モデルが強いときに
    # 必要な小さいσ（0.006 など）へ**構造的に到達できなかった**。
    coarse = np.unique(np.round(np.geomspace(0.002, max(resid_std * 2.0, 0.05), 15), 5))
    curve = [(float(s), score(s)) for s in coarse]
    i = int(np.argmax([c[1] for c in curve]))
    lo = curve[max(i - 1, 0)][0]
    hi = curve[min(i + 1, len(curve) - 1)][0]
    if hi > lo:
        for s in np.round(np.linspace(lo, hi, 9)[1:-1], 5):
            if all(abs(s - c[0]) > 1e-9 for c in curve):
                curve.append((float(s), score(float(s))))
    curve.sort()
    best = max(curve, key=lambda t: t[1])[0]
    return max(best, 1e-4), curve


# σに掛ける係数。既定 1.0。
# 注意: かつて 1.25（弱気側）を既定にしていたが、これは誤った安全策だった。
# σを膨らませると確率分布が平坦になり、ロングショットの確率を過大評価する。
# 市場が正しい確率どおりに張っている状況をシミュレートすると、σ×1.25 では
# 「偽の+EV」の検出が 87件 → 666件（平均オッズ1164倍に集中）に増えた。
# 資金を守る安全弁は σ ではなく、分数ケリー・model_weight(λ)・min_prob が担う。
SIGMA_SAFETY = 1.0


def train_model(log_path=None, sigma_override=None, train_from=DEFAULT_TRAIN_FROM,
                min_field=4, half_life_days=21.0, alpha_grid=None, spec=None,
                spec_path=None, sigma_safety=SIGMA_SAFETY, texts=None):
    """レースログを学習して bundle を返す。

    train_from: この日付以降のレースのみを学習に使う（既定 = スコア式変更日）。
                対象レースが少なすぎる場合は旧式データも時間減衰つきで併用する。
    """
    msgs, warns = [], []
    if spec is None:
        spec = load_passive_spec(spec_path)
    files = _iter_log_files(log_path)
    if not files and not texts:
        return {'ok': False, 'model': None, 'warnings': [],
                'messages': [f'ログが見つかりません: {log_path}']}

    df_all = parse_race_log(log_path, texts=texts)
    if len(df_all) == 0:
        return {'ok': False, 'model': None, 'warnings': [],
                'messages': ['ログを解析できませんでした（中身を確認してください）。'
                             'Discordエクスポートの .txt をそのまま指定してください。']}

    n_ex = int(df_all.attrs.get('n_excluded_races', 0) or 0)
    if n_ex:
        msgs.append(f'ℹ 結果が壊れているレース {n_ex}件 を学習から除外しました: '
                    + ', '.join(map(str, df_all.attrs.get('excluded_races', [])[:4])))
    n_dup_races = int(df_all.attrs.get('n_dup_races', 0) or 0)
    if n_dup_races:
        msgs.append(
            f'ℹ 別ソースに同じレースが {n_dup_races}件 あったので1件ずつに集約しました'
            '（Discordログと harvest 採取ログの併用など）。')
    n_bad_date = int(df_all.attrs.get('n_bad_date', 0) or 0)
    if n_bad_date:
        warns.append(
            f'⚠ 日付を特定できない行が {n_bad_date}行 あり、学習から除外しました'
            '（ログの [YYYY/MM/DD HH:MM] 行が遠すぎる・欠けている可能性。'
            'エクスポートをそのまま使っているか確認してください）。')

    df_all = df_all[df_all['n_field'] >= min_field].copy()
    cut = pd.to_datetime(train_from, format='%Y/%m/%d')
    newest = df_all['_d'].max()

    df = df_all[df_all['_d'] >= cut].copy()
    n_races_new = df['race_key'].nunique()
    mode = 'new_only'
    if n_races_new < MIN_RACES_FOR_ERA:
        bal = pd.to_datetime(BALANCE_PATCH_DATE, format='%Y/%m/%d')
        df = df_all[df_all['_d'] >= bal].copy()
        mode = 'blended'
        warns.append(
            f'⚠ 新スコア式（{train_from}以降）のレースが {n_races_new} 件しかないため、'
            f'{BALANCE_PATCH_DATE} 以降の旧データも時間減衰つきで併用しています。'
            'ログが貯まったら再学習してください（新式だけで学習するのが最も正確）。')
    if len(df) == 0:
        return {'ok': False, 'model': None, 'warnings': [],
                'messages': [f'対象レースが0件（{train_from} 以降のデータがありません）。']}
    df = df.reset_index(drop=True)

    # --- ターゲット: レース内で中心化した log スコア（スケール不変） ---
    df['lsc'] = np.log(np.clip(df['score'].values, 1e-6, None))
    y = _center_by_race(df['lsc'].values, df['race_key'].values)
    X = build_features(df, spec)
    groups = df['race_key'].values

    # --- 時間減衰の重み ---
    age = (newest - df['_d']).dt.days.fillna(9999).values.astype(float)
    if mode == 'blended':
        w = 0.5 ** (age / max(half_life_days, 1.0))
        w = np.clip(w, 1e-3, None)
    else:
        w = np.ones(len(df))

    # --- α をレース単位CVで選択 ---
    if alpha_grid is None:
        alpha_grid = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
    best_alpha, best_score, cv_rows = None, -np.inf, []
    for a in alpha_grid:
        oof = _oof_predictions(X, y, groups, a, w)
        sc = -np.mean((y - oof) ** 2)
        # レース内スピアマンでも評価（順位が本命なので）
        rho = _mean_race_spearman(oof, df)
        cv_rows.append({'alpha': a, 'mse': -sc, 'spearman': rho})
        obj = rho                                     # 順位精度を最優先
        if obj > best_score:
            best_score, best_alpha = obj, a
    oof = _oof_predictions(X, y, groups, best_alpha, w)
    resid_std = float(np.std(y - oof))
    race_rho = _mean_race_spearman(oof, df)
    top1 = _top1_accuracy(oof, df)

    model = Ridge(alpha=best_alpha, fit_intercept=True).fit(X, y, sample_weight=w)

    # --- σ 校正 ---
    if sigma_override:
        race_sigma, sigma_curve = float(sigma_override), []
        sigma_mle = race_sigma
        sigma_note = '（手動上書き）'
    else:
        # 単勝は「1着が誰か」だけ当てればよいので top_k=1 で校正する
        sigma_mle, sigma_curve = _calibrate_sigma(oof, df, resid_std, top_k=1)
        race_sigma = sigma_mle * max(float(sigma_safety), 0.1)
        sigma_note = (f'（着順尤度の最適値 {sigma_mle:.4f} × 係数 {sigma_safety:g}。'
                      '※σを膨らませても安全にはなりません。資金の保険は'
                      '分数ケリーとモデル信頼度λが担います）')

    # --- 3連単専用 σ ---
    # 3連単は 8頭以上でしか成立せず、2・3着の「順番」に敏感。全頭数で校正した σ は
    # 小頭数レース（順番が当てやすい）に引っ張られて小さめになり、実際の 8頭以上レースでは
    # 本命1点を過信する（検証: 予測44% vs 実測37%）。そこで 8頭以上のレースだけで
    # 順番的中に合わせた tri_sigma を別に持ち、3連単のシミュレーションに使う。単勝は race_sigma のまま。
    if sigma_override:
        tri_sigma = race_sigma
        tri_note = '（単勝と同じ・手動上書き）'
    else:
        tri_mle, _ = _calibrate_sigma(oof, df, resid_std,
                                      min_field=MIN_FIELD_TRIFECTA, top_k=3)
        n_tri_races = len(_recent_races(df, min_field=MIN_FIELD_TRIFECTA))
        if n_tri_races >= 8 and tri_mle > 0:
            tri_sigma = tri_mle * max(float(sigma_safety), 0.1)
            _cmp = ('より大きめ＝3連単の順番のブレを正しく反映'
                    if tri_sigma > race_sigma * 1.001 else
                    ('とほぼ同じ' if tri_sigma > race_sigma * 0.999 else 'より小さめ'))
            tri_note = (f'（8頭以上{n_tri_races}レースの順番的中で校正した最適値 {tri_mle:.4f}。'
                        f'単勝用 {race_sigma:.4f} {_cmp}）')
        else:
            tri_sigma = race_sigma
            tri_note = f'（8頭以上のレースが{n_tri_races}件と少ないため単勝と同じ {race_sigma:.4f} を使用）'

    # --- 校正チェック: OOF予測の「予測確率 vs 実測」 ---
    # 単勝は race_sigma、3連単は tri_sigma（8頭以上のみ）で別々に見る。
    calib = _calibration_check(oof, df, race_sigma)
    calib_tri = _calibration_check(oof, df, tri_sigma, min_field=MIN_FIELD_TRIFECTA)

    # --- 学習に現れたパッシブ ---
    seen = {}
    for ps in df['passives']:
        for p in ps:
            seen[p] = seen.get(p, 0) + 1
    unknown_in_log = sorted({p for p in seen if p not in PASSIVE_CATALOG})
    未出現 = [p for p in PASSIVE_NAMES if seen.get(p, 0) == 0]
    thin = [p for p in PASSIVE_NAMES if 0 < seen.get(p, 0) < 8]

    n_dup = int(df_all.attrs.get('n_duplicates', 0) or 0)
    msgs.append(
        f'学習完了  {len(df)}行 / {df["race_key"].nunique()}レース  '
        f'（{df["date"].min()}〜{df["date"].max()}, mode={mode}）'
        + (f'  ※重複 {n_dup}行を除外' if n_dup else ''))
    msgs.append(
        f'精度(OOF)  レース内スピアマン={race_rho:.3f}  1着的中={top1*100:.0f}%  '
        f'残差std={resid_std:.4f}  α={best_alpha}')
    n_num = sum(1 for k, v in spec.items()
                if k in PASSIVE_CATALOG and (v.get('mult') or v.get('scope') == 'variance'))
    n_game = sum(1 for k, v in spec.items()
                 if k in PASSIVE_CATALOG and v.get('source') == 'game')
    n_none = len(unspecced_passives(spec))
    msgs.append(f'パッシブ {len(PASSIVE_NAMES)}種中: 数値を計算に使用 {n_num}種 / '
                f'実測から学習 {n_none}種（ゲーム内表記から取得済み {n_game}種）')
    msgs.append(f'RACE_SIGMA（単勝）={race_sigma:.4f} {sigma_note}')
    msgs.append(f'TRI_SIGMA（3連単）={tri_sigma:.4f} {tri_note}')
    if calib:
        msgs.append(
            f'校正チェック 単勝(OOF {calib["n_races"]}レース)  '
            f'1着: 予測{calib["p_top1"]*100:.0f}% vs 実測{calib["a_top1"]*100:.0f}%')
        if calib['a_top1'] > calib['p_top1'] * 1.35:
            warns.append('△ モデルは実測よりやや弱気（σが大きめ）。実績ログが貯まったら σ を'
                         '少し下げると期待値が上がる可能性があります。')
        elif calib['p_top1'] > calib['a_top1'] * 1.35:
            warns.append('⚠ モデルが実測より強気（σが小さめ）。σを上げないと過剰投資になります。')
    if calib_tri:
        msgs.append(
            f'校正チェック 3連単(8頭以上 OOF {calib_tri["n_races"]}レース)  '
            f'本命1点: 予測{calib_tri["p_tri"]*100:.0f}% vs 実測{calib_tri["a_tri"]*100:.0f}%')
    if 未出現:
        warns.append('⚠ 学習データに出てこないパッシブ（効果0として扱う）: ' + ', '.join(未出現))
    if thin:
        warns.append('△ サンプルが少ないパッシブ（効果の推定が粗い）: '
                     + ', '.join(f'{p}({seen[p]})' for p in thin))
    if unknown_in_log:
        warns.append('⚠ カタログ外のパッシブがログにあります（新スキル？ カタログ更新を検討）: '
                     + ', '.join(unknown_in_log))
    if race_rho < 0.5:
        warns.append('⚠ 順位相関が低いです。ゲーム側の計算式がまた変わった可能性があります。')

    return {
        'ok': True, 'model': model, 'alpha': best_alpha, 'spec': spec,
        'feature_names': feature_names(spec), 'race_sigma': race_sigma,
        'tri_sigma': float(tri_sigma), 'calibration_tri': calib_tri,
        'sigma_curve': sigma_curve, 'resid_std': resid_std,
        'sigma_mle': float(sigma_mle), 'sigma_safety': float(sigma_safety),
        'race_spearman': race_rho, 'top1_acc': top1,
        'n_rows': int(len(df)), 'n_races': int(df['race_key'].nunique()),
        'mode': mode, 'train_from': train_from,
        'date_min': str(df['date'].min()), 'date_max': str(df['date'].max()),
        'passive_counts': seen, 'passives_unseen': 未出現,
        'passives_thin': thin, 'passives_unknown': unknown_in_log,
        'train_conditions': set(df['condition'].unique()),
        'cv_rows': cv_rows, 'files': files or ['(アップロード/Driveのデータ)'],
        'calibration': calib,
        'messages': msgs, 'warnings': warns,
    }


def _calibration_check(oof, df, sigma, n_draw=60_000, seed=11, min_field=4):
    """OOF予測 + σ で作った確率が、実測とどれくらい合っているかを見る。
    min_field=8 にすると 3連単が実在する頭数のレースだけで確認できる。"""
    races = _recent_races(df, min_field=min_field)
    if len(races) < 5:
        return None
    rng = np.random.default_rng(seed)
    p1, a1, pt, at = [], [], [], []
    for k, g in races:
        b = oof[g.index.values]
        b = b - b.mean()
        sim = b[None, :] + sigma * rng.standard_normal((n_draw, len(b)))
        order = np.argsort(-sim, axis=1)[:, :3]
        win = np.bincount(order[:, 0], minlength=len(b)) / n_draw
        truth = _truth_top(g)
        p1.append(win.max())
        a1.append(int(np.argmax(win) == truth[0]))
        # 予測最有力3連単の確率と、それが的中したか
        key = order[:, 0] * len(b) ** 2 + order[:, 1] * len(b) + order[:, 2]
        u, c = np.unique(key, return_counts=True)
        best = u[np.argmax(c)]
        pt.append(c.max() / n_draw)
        at.append(int(best == truth[0] * len(b) ** 2 + truth[1] * len(b) + truth[2]))
    return {'n_races': len(races), 'p_top1': float(np.mean(p1)),
            'a_top1': float(np.mean(a1)), 'p_tri': float(np.mean(pt)),
            'a_tri': float(np.mean(at))}


def _spearman(a, b):
    """順位相関（scipy非依存）。"""
    ra = pd.Series(a).rank().values
    rb = pd.Series(b).rank().values
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    den = math.sqrt(float((ra ** 2).sum()) * float((rb ** 2).sum()))
    return float((ra * rb).sum() / den) if den > 0 else float('nan')


def _mean_race_spearman(pred, df):
    vals = []
    for _, g in df.groupby('race_key'):
        if len(g) < 4:
            continue
        actual = (-g['rank'].values) if 'rank' in g.columns else g['score'].values
        r = _spearman(pred[g.index.values], actual)
        if r == r:
            vals.append(r)
    return float(np.mean(vals)) if vals else float('nan')


def _top1_accuracy(pred, df):
    hits = []
    for _, g in df.groupby('race_key'):
        if len(g) < 4:
            continue
        hits.append(int(np.argmax(pred[g.index.values]) == _truth_top(g, 1)[0]))
    return float(np.mean(hits)) if hits else float('nan')


def passive_effects(bundle, dist=None, track=None, same_species=True):
    """パッシブごとの効き目を一覧化する。
    数値スペックがあるものは「その距離・馬場での実効的な効果」を理論値から計算し、
    スペックが無いものは学習した係数をそのまま出す。"""
    if not bundle or not bundle.get('ok'):
        return []
    coef = dict(zip(bundle['feature_names'], bundle['model'].coef_))
    spec = bundle.get('spec') or default_spec()
    cnt = bundle.get('passive_counts', {})
    d = dist or '中距離'
    t = track or '芝'
    b_sp = coef.get(f'{d}:log(SP)', 0.0)
    b_pw = coef.get(f'{d}:log(PW)', 0.0)
    b_st = coef.get(f'{d}:log(ST)', 0.0)
    l_sp = coef.get(f'{d}:lin(SP)', 0.0)
    l_pw = coef.get(f'{d}:lin(PW)', 0.0)
    l_st = coef.get(f'{d}:lin(ST)', 0.0)
    ref = (100.0, 100.0, 100.0)
    out = []
    for p in PASSIVE_NAMES:
        sp_ = spec.get(p) or {}
        src = sp_.get('source')
        if sp_.get('mult'):
            e = effective_stats(*ref, (p,), d, t, spec, {'same_species': same_species})
            eff = (b_sp * math.log(e['speed'] / ref[0])
                   + b_pw * math.log(e['power'] / ref[1])
                   + b_st * math.log(e['stamina'] / ref[2])
                   + l_sp * (e['speed'] - ref[0]) / 100.0
                   + l_pw * (e['power'] - ref[1]) / 100.0
                   + l_st * (e['stamina'] - ref[2]) / 100.0)
            kind = 'game' if src == 'game' else 'inferred'
        elif sp_.get('scope') == 'variance':
            eff = 0.0
            kind = 'variance'
        else:
            eff = coef.get(p, 0.0)
            if PASSIVE_CATALOG.get(p) != 'aptitude':
                eff += INTERACTION_SHRINK * coef.get(f'{p}×{d}', 0.0)
            kind = 'learned'
        cond = {'aptitude': f'{sp_.get("scope_arg", "")}のみ',
                'same_species': '同族が同レースにいる時のみ',
                'phase': f'{sp_.get("scope_arg", "")}区間のみ',
                'conditional': '状況限定',
                'variance': 'ブレを低減'}.get(sp_.get('scope'), '')
        out.append({'passive': p, 'kind': PASSIVE_CATALOG.get(p, 'phase'),
                    'source': kind, 'effect': float(eff), 'condition': cond,
                    'pct': float(math.exp(eff) - 1) * 100,
                    'sigma_mult': float(sp_.get('sigma_mult', 1.0)),
                    'desc': sp_.get('desc', ''), 'n': int(cnt.get(p, 0))})
    out.sort(key=lambda r: -r['effect'])
    return out


def model_formula(bundle):
    """学習済みモデルの「予測スコア式」を表形式で返す。

    予測値（レース内で中心化した相対log）:
      pred = 距離ごとに [ 切片 + b_log·log(実効stat) + b_lin·(実効stat/100) ] を合算
             ＋ 好調/不調の係数 ＋ スペック未知パッシブの係数
    実効stat にはスペック済みパッシブの倍率が畳み込まれている。
    """
    coef = dict(zip(bundle['feature_names'], bundle['model'].coef_))
    rows = []
    for d in DIST_LIST:
        w = internal_stat_weights(d)
        rows.append({
            'dist': d,
            'intercept': float(coef.get(f'{d}:切片', 0.0)),
            'log_SP': float(coef.get(f'{d}:log(SP)', 0.0)),
            'log_PW': float(coef.get(f'{d}:log(PW)', 0.0)),
            'log_ST': float(coef.get(f'{d}:log(ST)', 0.0)),
            'lin_SP': float(coef.get(f'{d}:lin(SP)', 0.0)),
            'lin_PW': float(coef.get(f'{d}:lin(PW)', 0.0)),
            'lin_ST': float(coef.get(f'{d}:lin(ST)', 0.0)),
            'internal_norm': w['norm'],       # 参考: 内部式のSP=1正規化重み
        })
    cond = {'好調': float(coef.get('好調', 0.0)), '不調': float(coef.get('不調', 0.0))}
    intc = getattr(bundle.get('model'), 'intercept_', 0.0)
    try:
        intc = float(intc)
    except (TypeError, ValueError):
        intc = 0.0
    return {'per_dist': rows, 'condition': cond, 'intercept0': intc}


def export_model_json(bundle, path=None):
    """学習済みモデルをブラウザ（autobet.js）で使えるJSONに書き出す。

    予測は「線形結合＋正規乱数のモンテカルロ」だけなので、係数さえ渡せば
    ブラウザ側で完全に同じ計算ができる。**特徴量は名前で対応付ける**こと
    （位置で対応させると、パッシブが増減した瞬間に静かにズレる）。
    """
    if not bundle or not bundle.get('ok'):
        raise ValueError('学習済み bundle が必要です')
    names = list(bundle['feature_names'])
    coef = [float(x) for x in bundle['model'].coef_]
    if len(names) != len(coef):
        raise ValueError(f'特徴量名 {len(names)} と係数 {len(coef)} の数が違います')
    spec = bundle.get('spec') or default_spec()
    payload = {
        'core_version': CORE_VERSION,
        'trained_at': datetime.now().isoformat(timespec='seconds'),
        'n_races': int(bundle.get('n_races', 0)),
        'date_min': bundle.get('date_min'), 'date_max': bundle.get('date_max'),
        'race_spearman': float(bundle.get('race_spearman') or 0),
        # 名前→係数。JS側は名前で引くので順序に依存しない。
        'coef': {n: round(c, 10) for n, c in zip(names, coef)},
        'intercept': float(getattr(bundle['model'], 'intercept_', 0.0)),
        'race_sigma': float(bundle['race_sigma']),
        'tri_sigma': float(bundle.get('tri_sigma') or bundle['race_sigma']),
        'spec': {k: {'mult': v.get('mult', {}), 'scope': v.get('scope', 'always'),
                     'scope_arg': v.get('scope_arg'), 'duty': float(v.get('duty', 1.0)),
                     'sigma_mult': float(v.get('sigma_mult', 1.0))}
                 for k, v in spec.items() if k in PASSIVE_CATALOG},
        'unspecced': unspecced_passives(spec),
        'catalog': dict(PASSIVE_CATALOG),
        'code_map': dict(PASSIVE_CODE_MAP),
        'aptitude_match': {k: list(v) for k, v in APTITUDE_MATCH.items()},
        'dist_list': list(DIST_LIST), 'track_list': list(TRACK_LIST),
        'variance_share': VARIANCE_SHARE, 'interaction_shrink': INTERACTION_SHRINK,
        # スタミナ収支の特徴量を JS 側でも作るための定数一式
        'stamina_cost_law': {d: dict(v) for d, v in STAMINA_COST_LAW.items()},
        'phase_early': list(INTERNAL_PHASE_WEIGHTS['序盤']),
        'dist_balance': {d: list(v) for d, v in INTERNAL_DIST_BALANCE.items()},
        'odds_floor': ODDS_FLOOR, 'stake_unit': STAKE_UNIT,
        'trifecta_pool_seed': TRIFECTA_POOL_SEED,
        'max_total_units': MAX_TOTAL_UNITS, 'min_field_trifecta': MIN_FIELD_TRIFECTA,
    }
    if path:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    return payload


def passive_coef_table(spec=None):
    """パッシブの「係数（倍率）」を表形式で返す。スペック済み＝ゲーム/推定の倍率、
    未スペック＝実測学習（倍率なし）。"""
    spec = spec or default_spec()
    rows = []
    for p in PASSIVE_NAMES:
        sp_ = spec.get(p) or {}
        mult = sp_.get('mult') or {}
        rows.append({
            'passive': p,
            'kind': PASSIVE_CATALOG.get(p, 'phase'),
            'SP': mult.get('speed'),
            'PW': mult.get('power'),
            'ST': mult.get('stamina'),
            'scope': sp_.get('scope', ''),
            'scope_arg': sp_.get('scope_arg', ''),
            'duty': sp_.get('duty'),
            'sigma_mult': sp_.get('sigma_mult', 1.0),
            'source': sp_.get('source', ''),
            'desc': sp_.get('desc', ''),
        })
    return rows


# =====================================================================
#  6. 予測とモンテカルロ
# =====================================================================
def predict_base(bundle, horses, dist, track):
    """出走馬リスト → レース内で中心化した予測スコア（相対log）。"""
    spec = bundle.get('spec') or default_spec()
    same = same_species_flags([h.get('name', '') for h in horses],
                              [h.get('species') for h in horses])
    rows = pd.DataFrame([{
        'name': h.get('name', ''), 'speed': h['speed'], 'power': h['power'],
        'stamina': h['stamina'], 'condition': h.get('condition', '普通'),
        'passives': h.get('passives', ()), 'dist': dist, 'track': track,
        'same_species': same[i]} for i, h in enumerate(horses)])
    X = build_features(rows, spec)
    p = bundle['model'].predict(X)
    return p - p.mean()


def horse_sigmas(bundle, horses, sigma):
    """馬ごとの σ（安定感などの分散低減スキルを反映）。"""
    spec = bundle.get('spec') or default_spec()
    return np.array([sigma * sigma_multiplier(h.get('passives', ()), spec,
                                              extra_mult=h.get('item_sigma_mult', 1.0))
                     for h in horses], dtype=float)


def simulate_trifecta(base, sigma, n_sim=N_SIM, seed=SIM_SEED, chunk=SIM_CHUNK,
                      need_combo=True):
    """メモリを抑えたチャンク実行。 -> (win_prob[n], {(i,j,k): prob})

    need_combo=False にすると3連単の組の集計（np.unique + dict更新）を省く。
    単勝の勝率だけが欲しい呼び出しではこれが処理時間の大半なので、明確に効く。"""
    n = len(base)
    sig = np.broadcast_to(np.asarray(sigma, dtype=float), (n,))
    rng = np.random.default_rng(seed)
    win = np.zeros(n)
    counts = {}
    want_combo = need_combo and n >= 3
    done = 0
    while done < n_sim:
        m = min(chunk, n_sim - done)
        sim = base[None, :] + rng.standard_normal((m, n)) * sig[None, :]
        if want_combo:
            order = np.argsort(-sim, axis=1)[:, :3]
            np.add.at(win, order[:, 0], 1)
            key = (order[:, 0] * n * n + order[:, 1] * n + order[:, 2])
            u, c = np.unique(key, return_counts=True)
            for kk, cc in zip(u.tolist(), c.tolist()):
                counts[kk] = counts.get(kk, 0) + cc
        else:
            # 1着だけ分かればよいので argsort（O(n log n)）ではなく argmax（O(n)）
            np.add.at(win, np.argmax(sim, axis=1), 1)
        done += m
    win /= n_sim
    combo = {(k // (n * n), (k // n) % n, k % n): c / n_sim for k, c in counts.items()}
    return win, combo


# 後方互換
def simulate_rankings(base, sigma, n_sim=N_SIM, seed=SIM_SEED):
    rng = np.random.default_rng(seed)
    sim = base + rng.normal(0, sigma, (n_sim, len(base)))
    return np.argsort(-sim, axis=1)


def market_win_prob(odds, floor=ODDS_FLOOR):
    """表示オッズ → 市場の暗黙勝率。

    floor 以下のオッズは「まだ誰も賭けていない（プレースホルダ）」とみなして除外する。
    下限に張り付いた本命の**本当の**オッズ（1/シェア < 1.5）を渡すときは
    floor=1.0 にして除外されないようにすること（diagnose_floor_odds を参照）。"""
    odds = np.asarray(odds, dtype=float)
    raw = np.where((odds > floor) & np.isfinite(odds), 1.0 / odds, 0.0)
    if raw.sum() <= 0:
        return None
    return raw / raw.sum()


# 下限表示（1.5）の馬が抱えているシェアの判定しきい値。
# 丸め誤差（オッズは小数2桁）で Σ(1/od) は ±0.01 程度ぶれるので、余裕をとる。
FLOOR_RESIDUAL_UNBET = 0.02    # これ以下 → 下限表示の馬は本当に未投票
FLOOR_RESIDUAL_REAL = 0.10     # これ以上 → 下限表示の馬に実際のお金が入っている


def diagnose_floor_odds(odds, my_amounts=None):
    """表示オッズ 1.5 の馬が「未投票」なのか「本命すぎて下限に張り付いた」のかを判定する。

    **なぜ必要か**: 1.5 は未投票馬のプレースホルダであると同時に、ゲームの
    最低オッズ（下限）でもある。シェアが 2/3 を超える大本命は、実際にお金が
    入っていても 1.50 と表示される。これを「投入額0」と誤認すると、
    実効オッズを (P+u)/u と桁違いに過大評価して、**大本命に超高配当の買い推奨**を
    出してしまう（小さいプールほど起きやすい）。

    **判定の原理**: 単勝は控除0%の純パリミュチュエルなので、お金が入っている馬だけで
        Σ(1/od) = 1
    が成り立つ。下限表示の馬を除いた合計 S = Σ_{od>1.5}(1/od) が 1 を大きく割るなら、
    不足分 residual = 1 - S は下限表示の馬が抱えているシェアである。

    **一意性**: 下限に張り付くのは P_j >= P/1.5（シェア 2/3 以上）の時だけなので、
    張り付ける馬は1レースに高々1頭（2頭なら合計 4/3 > 1 で矛盾）。
    よって下限表示が1頭だけなら、その馬のシェア = residual と確定できる。

    -> dict(unbet=[bool], odds_eff=[float], ambiguous=bool, residual=float|None,
            messages=[str])
       odds_eff は下限に張り付いた馬の表示オッズを「本当のオッズ」1/シェアに
       置き換えたもの（判定できない馬は NaN にして推奨から外す）。
    """
    od = np.asarray(odds, dtype=float)
    n = len(od)
    mine = (np.zeros(n) if my_amounts is None
            else np.nan_to_num(np.asarray(my_amounts, dtype=float), nan=0.0))
    at_floor = np.isfinite(od) & (np.abs(od - ODDS_FLOOR) < 1e-9)
    priced = np.isfinite(od) & (od > ODDS_FLOOR)
    out = {'unbet': [False] * n, 'odds_eff': od.astype(float).copy(),
           'ambiguous': False, 'residual': None, 'messages': []}
    if not at_floor.any():
        return out

    # 自分で買った馬は、下限表示でも「お金が入っている」ことが確定している
    known_bet = at_floor & (mine > 0)
    cand = at_floor & ~known_bet          # 未投票かもしれない馬

    if not priced.any():
        # 全馬が下限表示。誰も賭けていない（プール空）か、1頭が総取りしている状態。
        # 自分の購入額があるならプールは空ではない。
        if mine.sum() > 0:
            out['ambiguous'] = True
            out['odds_eff'][at_floor] = np.nan
            out['messages'].append(
                '⚠ 全馬のオッズが下限 1.5 のままですが、自分の購入額があるためプールは'
                '空ではありません。どの馬にお金が入っているか判別できないため、'
                '単勝の推奨は出しません。')
        else:
            for i in np.flatnonzero(cand):
                out['unbet'][i] = True
        return out

    S = float(np.sum(1.0 / od[priced]))
    residual = 1.0 - S
    out['residual'] = residual

    if residual <= FLOOR_RESIDUAL_UNBET:
        # Σ(1/od) がほぼ 1 → 下限表示の馬にはお金が入っていない
        for i in np.flatnonzero(cand):
            out['unbet'][i] = True
        if known_bet.any():
            out['odds_eff'][known_bet] = np.nan
        return out

    if residual < FLOOR_RESIDUAL_REAL:
        # どちらとも言い切れない中間帯（丸め誤差・データの取得ずれなど）。安全側に倒す。
        out['ambiguous'] = True
        out['odds_eff'][at_floor] = np.nan
        out['messages'].append(
            f'△ 単勝オッズの合計 Σ(1/od)={S:.3f} がわずかに 1 を割っています'
            f'（残り {residual*100:.1f}%）。オッズ 1.5 表示の馬が未投票か'
            '本命かを判別できないため、その馬は単勝の推奨から外します。')
        return out

    # residual が大きい＝下限表示の馬に実際のお金が入っている
    idx = np.flatnonzero(cand)
    if len(idx) == 1 and not known_bet.any():
        i = int(idx[0])
        out['odds_eff'][i] = 1.0 / residual     # 下限で隠れていた本当のオッズ
        out['messages'].append(
            f'⚠ オッズ 1.5 表示の馬は「未投票」ではなく、プールの約 {residual*100:.0f}% を'
            f'集めた大本命です（Σ(1/od)={S:.3f}）。表示は下限に張り付いているだけなので、'
            f'本当のオッズ ≒ {1.0/residual:.2f} 倍として計算します。')
    else:
        out['ambiguous'] = True
        out['odds_eff'][at_floor] = np.nan
        out['messages'].append(
            f'⚠ オッズ 1.5 表示の馬が {int(at_floor.sum())}頭 あり、そのうち1頭が'
            f'プールの約 {residual*100:.0f}% を集めた大本命です（Σ(1/od)={S:.3f}）。'
            'どの馬かはオッズだけでは判別できないため、1.5 表示の馬は'
            '単勝の推奨から外します（ゲーム画面の投票額を確認してください）。')
    return out


# =====================================================================
#  7. 貼り付けデータの解析
# =====================================================================
def disambiguate(names):
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


_DUP_RE = re.compile(r'\s*#\s*\d+\s*$')


def bare(name):
    """表示名から重複マーカーを外す。

    マーカーには2種類ある:
      - ' #1' … このツールの `disambiguate()` が付ける（DUP_MARK）
      - '#1'  … ゲーム／ブックマークレットが同名馬に付ける（スペース無し）
    以前は前者しか外せず、ブックマークレット由来の '名前#1' に対して
    素名フォールバックが機能していなかった。両方に対応する。
    """
    return _DUP_RE.sub('', str(name)).strip()


_SPECIES_RE = _DUP_RE          # 後方互換（同じ正規表現）


def species_name(name):
    """おあしすっちの種類名。同名馬に付く '#2' 等のマーカーを外す。"""
    return bare(name)


def same_species_flags(names, species=None):
    """同じレースに同じ成体種のおあしすっちがいるか（同族嫌悪の発動条件）。
    species（ブックマークレットが出す adult_key）があればそれを使い、無ければ名前で代用。"""
    if species and all(x for x in species):
        sp = [str(x).strip() for x in species]
    else:
        sp = [species_name(n) for n in names]
    cnt = {}
    for x in sp:
        cnt[x] = cnt.get(x, 0) + 1
    return [cnt[x] >= 2 for x in sp]


_PASSIVE_COLS = ['パッシブスキル', 'パッシブ', 'スキル', 'passive', 'passives']
_PASSIVE_COLS2 = [('パッシブスキル1', 'パッシブスキル2'), ('パッシブ1', 'パッシブ2'),
                  ('スキル1', 'スキル2'), ('passive_skill', 'passive_skill_2')]


_ITEM_EFFECT_COLS = [('装備効果', '装備', '装備効果キー'), ('お守り効果', 'お守り', 'お守り効果キー')]


def item_effect_spec(desc, effect_key=None, spec=None):
    """装備・お守りの効果 → 発動率まで織り込んだ倍率。読めなければ None。

    倍率の**大きさ**は説明文から、**発動する範囲（scope / duty）は effect_key から**取る。
    実測した装備:
      charm_speed       「スピードが常時4.4%上昇」          → 常時
      gear_second_gear  「中盤開始後200mのスピードが2.4%上昇」→ パッシブ『二の脚』と同じ範囲
    説明文だけだと後者は「中盤 = 1/3」と読めてしまうが、二の脚の実測 duty は 0.128。
    `gear_` / `charm_` を外したものがパッシブのコードなら、そちらの scope と duty を使う。
    """
    sp = spec_from_description(desc or '')
    if not sp:
        return None
    # 分散低減（お守り『安定の加護』など）はステータス倍率ではなく σ に効く。
    # `_sigma` キーで返し、呼び出し側が σ の計算にだけ使う。
    if sp.get('scope') == 'variance':
        sg = float(sp.get('sigma_mult', 1.0))
        return {'_sigma': sg} if sg != 1.0 else None
    if not sp.get('mult'):
        return None
    duty, scope = float(sp.get('duty', 1.0)), sp.get('scope', 'always')
    key = str(effect_key or '').strip()
    if key:
        code = re.sub(r'^(?:gear|charm|item)_', '', key)
        base = (spec or {}).get(passive_from_code(code) or '')
        if base:
            scope, duty = base.get('scope', scope), float(base.get('duty', duty))
    # 出走メンバーや距離に依存する範囲は、この場では判定できないので採用しない
    if scope in ('aptitude', 'same_species', 'variance'):
        return None
    return {k: 1.0 + (float(m) - 1.0) * duty for k, m in sp['mult'].items()}


def item_mults_from_row(r, cols, spec=None):
    """貼り付け1行の装備・お守り → (倍率, 反映できなかった説明)。

    購入ページが「表示値＝個体値＋特訓＋装備品。**倍率・条件スキルはレース中に適用**」と
    書いているとおり、貼り付けの SPEED/POWER/STAMINA には**加算ぶんしか入っていない**。
    倍率はここで別に掛ける（実測: お守り「太陽のメダリオン」は PW+5 が表示値に入る一方、
    効果「スピードが常時4.4%上昇」は入っていない）。
    """
    mult, skipped = {}, []
    mult['_sigma'] = 1.0
    for col, slot, keycol in _ITEM_EFFECT_COLS:
        if col not in cols:
            continue
        v = str(r.get(col, '') or '').strip()
        if not v or v in ('nan', 'None'):
            continue
        kv = str(r.get(keycol, '') or '').strip() if keycol in cols else ''
        m = item_effect_spec(v.split('：', 1)[-1], kv, spec)
        if m:
            for k, x in m.items():
                mult[k] = mult.get(k, 1.0) * float(x)
        else:
            skipped.append(f'{slot}「{v}」')
    if mult.get('_sigma', 1.0) == 1.0:
        mult.pop('_sigma', None)
    return mult, skipped


def _passives_from_row(r, cols):
    got = []
    for a, b in _PASSIVE_COLS2:
        if a in cols:
            for c in (a, b):
                if c not in cols:
                    continue
                v = r.get(c)
                code = passive_from_code(v) if (isinstance(v, str) and re.fullmatch(
                    r'[a-z_0-9]+', v.strip())) else None
                got += ([code] if code else list(parse_passives(v)))
            break
    if not got:
        for c in _PASSIVE_COLS:
            if c in cols:
                got = list(parse_passives(r.get(c)))
                break
    out = []
    for g in got:
        if g and g not in out:
            out.append(g)
    return tuple(out[:2])


def parse_passive_effect_section(text):
    """ブックマークレットが出す『=== パッシブ効果 ===』（パッシブ,コード,説明）を読む。"""
    m = re.search(r'===\s*パッシブ効果\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)
    if not m:
        return {}
    out = {}
    try:
        df = pd.read_csv(io.StringIO(m.group(1).strip()))
        df.columns = [str(c).strip() for c in df.columns]
    except Exception:
        return {}
    for _, r in df.iterrows():
        name = canonical_passive(r.get('パッシブ') or r.get('名前') or '')
        code = str(r.get('コード', '') or '').strip()
        if code and code in PASSIVE_CODE_MAP:
            name = PASSIVE_CODE_MAP[code]
        sp = spec_from_description(r.get('説明') or r.get('効果') or '')
        if name and sp:
            sp['code'] = code or None
            out[name] = sp
    return out


def parse_unified(text, spec=None):
    """統合フォーマット（ブックマークレット出力）を解析。
    -> (horses, trifecta_odds, dist, track, ground, guild, schedule_id, pool, n_tri_total)

    spec は装備効果の発動率（duty）を引くのに使う。省略時は既定スペック。
    """
    spec = spec or default_spec()
    horses, odds = [], {}
    dist = track = ground = guild = schedule_id = None
    pool = None
    n_tri_total = 0
    meta = {}

    for key, setter in [('guild', 'guild'), ('schedule_id', 'schedule_id')]:
        m = re.search(rf'^{key}=(\S+)', text, re.M)
        if m:
            if key == 'guild':
                guild = m.group(1).strip()
            else:
                schedule_id = m.group(1).strip()
    pm = re.search(r'^pool=(\d+)', text, re.M)
    if pm:
        pool = int(pm.group(1))
    for key in ('balance', 'win_pool', 'win_pool_before', 'win_pool_delta', 'win_pool_n',
                'win_pool_spread', 'win_pool_err', 'win_own', 'win_pool_min',
                'win_pool_exact'):
        m = re.search(rf'^{key}=([0-9.]+)', text, re.M)
        if m:
            meta[key] = float(m.group(1))
    m = re.search(r'^win_market=(\w+)', text, re.M)
    if m:
        meta['win_market'] = m.group(1)

    mh = re.search(r'===\s*出走馬一覧\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)
    mo = re.search(r'===\s*3連単オッズ\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)

    skipped, item_skipped = [], []
    if mh:
        df = pd.read_csv(io.StringIO(mh.group(1).strip()))
        df.columns = [str(c).strip() for c in df.columns]
        cols = set(df.columns)

        def _first(col_names):
            for c in col_names:
                if c in cols and len(df):
                    v = str(df[c].iloc[0]).strip()
                    if v not in ('', 'nan', 'None'):
                        return v
            return None

        dist = _first(['レース距離', '距離'])
        track = _first(['馬場', 'コース'])
        ground = _first(['地面', '馬場状態', '馬場コンディション'])

        for _, r in df.iterrows():
            sp = pd.to_numeric(r.get('SPEED', r.get('スピード', np.nan)), errors='coerce')
            pw = pd.to_numeric(r.get('POWER', r.get('パワー', np.nan)), errors='coerce')
            st = pd.to_numeric(r.get('STAMINA', r.get('スタミナ', np.nan)), errors='coerce')
            if pd.isna(sp) or pd.isna(pw) or pd.isna(st):
                skipped.append(str(r.get('馬名', r.get('名前', '?'))).strip())
                continue
            win_odds = pd.to_numeric(str(r.get('単勝オッズ', '')).strip(), errors='coerce')
            spc = str(r.get('成体種', r.get('adult_key', '')) or '').strip()
            mine = pd.to_numeric(r.get('自分の購入額', r.get('my_amount', np.nan)),
                                 errors='coerce')
            _im, _isk = item_mults_from_row(r, cols, spec)
            item_skipped += _isk
            horses.append({
                'my_amount': (float(mine) if pd.notna(mine) else None),
                'name': str(r.get('馬名', r.get('名前', ''))).strip(),
                'species': spc or None,
                # 装備の**倍率**はここで掛ける（加算ぶんは貼り付けの値に既に入っている）
                'speed': int(sp) * _im.get('speed', 1.0),
                'power': int(pw) * _im.get('power', 1.0),
                'stamina': int(st) * _im.get('stamina', 1.0),
                'item_sigma_mult': _im.get('_sigma', 1.0),
                'item_mult': ({k: v for k, v in _im.items() if k != '_sigma'} or None),
                'condition': str(r.get('コンディション', '普通')).strip(),
                'passives': _passives_from_row(r, cols),
                'odds': float(win_odds) if pd.notna(win_odds) else float('nan'),
            })

    if mo:
        dfo = pd.read_csv(io.StringIO(mo.group(1).strip()))
        dfo.columns = [str(c).strip() for c in dfo.columns]
        n_tri_total = len(dfo)
        dfo['_o'] = pd.to_numeric(dfo['オッズ'], errors='coerce')
        for _, r in dfo[dfo['_o'].notna()].iterrows():
            key = (str(r['1着']).strip(), str(r['2着']).strip(), str(r['3着']).strip())
            odds[key] = float(r['_o'])

    if skipped:
        meta['skipped_rows'] = skipped
    if item_skipped:
        meta['item_effects_skipped'] = item_skipped
    if meta:
        for h in horses:
            h.setdefault('_meta', meta)
    return horses, odds, dist, track, ground, guild, schedule_id, pool, n_tri_total


def parse_betting_screen(text):
    """購入画面をそのままコピーした場合のフォールバック解析（パッシブ2枠対応）。"""
    horses = []
    for block in re.split(r'購入', text):
        if not block.strip():
            continue
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 3:
            continue
        sp_m = re.search(r'(\d+)\s*SPEED', block, re.I)
        # 馬名は「コンディション：」の直前の行。先頭ブロックにはページのヘッダが
        # 混ざるため、単純に lines[0] を使うと馬名を取り違える。
        name = lines[0]
        for li, l in enumerate(lines):
            if re.match(r'コンディション\s*[:：]', l) and li >= 1:
                name = lines[li - 1]
                break
        pw_m = re.search(r'(\d+)\s*POWER', block, re.I)
        st_m = re.search(r'(\d+)\s*STAM', block, re.I)
        if not (sp_m and pw_m and st_m):
            continue
        found = []
        for pname in sorted(PASSIVE_NAMES, key=len, reverse=True):
            if pname in block and pname not in found:
                if any(pname in f and pname != f for f in found):
                    continue
                found.append(pname)
        # 長い名前優先で拾ったので、他スキル名の部分文字列は除去
        found = [p for p in found if not any(p != q and p in q for q in found)]
        c_m = re.search(r'(好調|普通|不調)', block)
        od_m = re.search(r'オッズ\s*[:：]?\s*([0-9.]+)', block)
        horses.append({
            'name': name,
            'speed': int(sp_m.group(1)), 'power': int(pw_m.group(1)),
            'stamina': int(st_m.group(1)),
            'condition': c_m.group(1) if c_m else '普通',
            'passives': tuple(found[:2]),
            'odds': float(od_m.group(1)) if od_m else float('nan'),
        })
    return horses


def detect_stake_unit(text, default=WIN_STAKE_UNIT):
    """購入画面の『1口（1,000 rrc）』から1口いくらかを読み取る。"""
    m = re.search(r'1\s*口\s*[（(]\s*([0-9,]+)\s*rrc', str(text))
    if m:
        try:
            v = int(m.group(1).replace(',', ''))
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def parse_trifecta_csv(path):
    df = pd.read_csv(path, encoding='utf-8-sig')
    df['_o'] = pd.to_numeric(df['オッズ'], errors='coerce')
    df = df[df['_o'].notna()]
    return {(str(r['1着']).strip(), str(r['2着']).strip(), str(r['3着']).strip()): float(r['_o'])
            for _, r in df.iterrows()}


# =====================================================================
#  8. プール／キャリーオーバー
# =====================================================================
CO_DETECT_LO = 0.95
INV_SUM_SANE = (0.5, 1.10)
ASSUME_POOL_IS_PAYOUT = False


# 開催者がこのバグを直したら **False にするだけ**で補正が止まる。
# 直ったあとも補正を掛け続けると払戻を過大評価して、エッジの無いところに張ってしまう
# （逆に、直っていないのに False にすると買い控えるだけなので、迷ったら False が安全側）。
# 「総取り」のほうは仕様なので、こちらとは無関係に有効なまま。
TRIFECTA_SEED_BUG_ACTIVE = True


def true_trifecta_odds(od, pool, seed=TRIFECTA_POOL_SEED):
    """表示オッズ → 実際に払い戻されるオッズ。

    サイトは od = (pool − seed) / その組の賭け金 で表示しているが、
    払戻は pool から出るので実際は pool / その組の賭け金。
    よって実オッズ = 表示オッズ × pool / (pool − seed)。
    pool が seed 以下（ほぼ誰も賭けていない）ときは補正しない。
    """
    if not od or od <= 0 or pool is None or pool <= seed:
        return od
    return float(od) * float(pool) / (float(pool) - float(seed))


def resolve_payout_pool(pool, odds_iter, manual_co=None, trust=True):
    vals = [float(o) for o in odds_iter if o and float(o) > 0 and math.isfinite(float(o))]
    info = {'inv_sum': None, 'regime': 'no_pool', 'carryover': 0.0,
            'bets': float(pool), 'n': len(vals)}
    if pool <= 0 or not vals:
        return pool, info
    inv = sum(1.0 / o for o in vals)
    info['inv_sum'] = inv
    if manual_co is not None:
        co = max(0.0, float(manual_co))
        info.update(regime='manual', carryover=co, bets=float(pool))
        return pool + co, info
    if inv > 1.05:
        info.update(regime='takeout')
        return pool, info
    if inv >= CO_DETECT_LO:
        info.update(regime='neutral')
        return pool, info
    if not (INV_SUM_SANE[0] <= inv <= INV_SUM_SANE[1]):
        info.update(regime='carryover_unsure')
        return pool, info
    payout = pool / inv
    if not trust:
        info.update(regime='carryover_untrusted', carryover=payout - pool, bets=float(pool))
        return pool, info
    if ASSUME_POOL_IS_PAYOUT:
        co = pool * (1.0 - inv)
        info.update(regime='carryover_in_pool', carryover=co, bets=pool - co)
        return pool, info
    info.update(regime='carryover_added', carryover=payout - pool, bets=float(pool))
    return payout, info


# 3連単プールをサーバから直接APIで取りに行くか。**既定は False**。
# デプロイ先（Streamlit Community Cloud など）からは api.oasis.red に到達できない
# （403 / 無応答）ため、有効にすると「必ず失敗する通信」の完了を待つあいだ画面が固まる。
# Streamlit は1セッション内で処理が直列なので、待ち時間はそのまま操作不能時間になる。
# ブックマークレット（bm.js / probe.js）は常に `pool=` 行を出力するので、
# 通常の運用ではこの経路自体が不要。ローカル等で到達できる環境なら True にしてよい。
ENABLE_POOL_API = False
POOL_API_TIMEOUT = 2.0        # 有効にする場合でも短く。待ち時間＝画面が固まる時間。


def _fetch_pool_api(guild, schedule_id, timeout=POOL_API_TIMEOUT):
    if not ENABLE_POOL_API:
        return None, ('貼り付けデータに `pool=` の行がないため、3連単プール総額が不明です'
                      '（口数計算はスキップ）。ブックマークレットで取り直すと自動で入ります。'
                      'サーバからAPIを直接叩くことはできないので、取りに行きません。')
    try:
        import requests
        r = requests.get(
            f'https://api.oasis.red/api/trifecta/pool?guild={guild}&schedule_id={schedule_id}',
            timeout=timeout)
        return int(r.json().get('pool', 0) or 0), None
    except Exception as e:
        return None, f'プール取得失敗（口数計算スキップ）: {e}'


# =====================================================================
#  9. 資金配分
# =====================================================================
def optimal_units_ev(p, od, P_tot, stake_unit=STAKE_UNIT, max_units=MAX_UNITS):
    if p <= 0 or p >= 1 or not od or not math.isfinite(od) or od <= 1:
        return 0, 0.0, od
    if p <= 1.0 / od:
        return 0, 0.0, od
    if P_tot <= 0:
        return 1, (p * od - 1) * stake_unit, od
    P_c = P_tot / od
    inner = p * (od - 1) / (1.0 - p)
    k_raw = (P_tot / (od * stake_unit)) * (math.sqrt(inner) - 1)
    if k_raw <= 0:
        return 0, 0.0, od
    if k_raw < 1:
        eff1 = (P_tot + stake_unit) / (P_c + stake_unit)
        if p * eff1 > 1:
            k = 1
        else:
            return 0, 0.0, od
    else:
        # 連続解の floor と floor+1 を比べる（floor だけだとEVを取りこぼす）
        def _ev(kk):
            if kk <= 0:
                return -1.0
            e = (P_tot + kk * stake_unit) / (P_c + kk * stake_unit)
            return (p * e - 1) * stake_unit * kk
        k_lo = min(max_units, int(k_raw))
        k_hi = min(max_units, k_lo + 1)
        k = k_hi if _ev(k_hi) > _ev(k_lo) else k_lo
    eff_od = (P_tot + k * stake_unit) / (P_c + k * stake_unit)
    return k, (p * eff_od - 1) * stake_unit * k, eff_od


def allocate_units_stable(cands, P_total, bankroll, kelly_frac, max_risk_frac,
                          edge_min, budget=MAX_TOTAL_UNITS,
                          stake_unit=STAKE_UNIT, max_per_combo=None):
    """安定運用配分（成立組のみ・分数ケリー・1レースの総リスク上限）。"""
    if max_per_combo is None:
        max_per_combo = budget
    if P_total <= 0 or bankroll <= 0:
        return {}
    risk_units = max(1, int((max_risk_frac * bankroll) // stake_unit))
    total_cap = min(budget, risk_units)

    # 実効オッズ = (プール総額 + **自分がこのレースで置いた全口数**) / (その組の額 + その組の口数)。
    # 分子に「その組の口数」しか足していなかったので、払戻を過小評価していた
    # （実測: 3組7口で本命 eff 6.14 → 正しくは 6.71、合計EVで −23%）。
    # 単勝側（win_bet_picks_pool の P_new）は最初から全頭の合計を足しており、そちらが正しい。
    # 他の組に置いた口数もプールに入る＝全組の払戻を押し上げるので、
    # 1口足すかどうかは**ポートフォリオ全体のEV**で判断する。
    def total_ev(alloc):
        tot_u = sum(alloc.values())
        s = 0.0
        for (c_, p_, od_, _cap) in items:
            k_ = alloc.get(c_, 0)
            if k_ <= 0:
                continue
            Pc_ = P_total / od_
            eff_ = (P_total + tot_u * stake_unit) / (Pc_ + k_ * stake_unit)
            s += (p_ * eff_ - 1) * stake_unit * k_
        return s

    items = []
    for (c, p, od) in cands:
        # inf/nan のオッズが通ると int(nan // stake) で ValueError になり画面が落ちる
        if not od or not math.isfinite(od) or od <= 1 or not (0 < p < 1):
            continue
        Pc = P_total / od
        eff1 = (P_total + stake_unit) / (Pc + stake_unit)
        edge = p * eff1 - 1
        if edge < edge_min:
            continue
        f = edge / (eff1 - 1)
        k_kelly = int((kelly_frac * f * bankroll) // stake_unit)
        k_evmax, _, _ = optimal_units_ev(p, od, P_total, stake_unit, max_per_combo)
        cap = min(k_evmax if k_evmax > 0 else 0, max_per_combo)
        cap = min(cap, max(1, k_kelly))
        if cap >= 1:
            items.append((c, p, od, cap))
    if not items:
        return {}

    alloc = {c: 0 for (c, p, od, cap) in items}
    used = 0
    base_ev = 0.0
    while used < total_cap:
        best, best_m = None, 1e-9
        for (c, p, od, cap) in items:
            if alloc[c] >= cap:
                continue
            alloc[c] += 1
            m = total_ev(alloc) - base_ev
            alloc[c] -= 1
            if m > best_m:
                best_m, best = m, c
        if best is None:
            break
        alloc[best] += 1
        used += 1
        base_ev += best_m

    res = {}
    for (c, p, od, cap) in items:
        k = alloc[c]
        if k > 0:
            Pc = P_total / od
            eff = (P_total + used * stake_unit) / (Pc + k * stake_unit)
            res[c] = (k, (p * eff - 1) * stake_unit * k, eff)
    return res


def unformed_sleeve_picks(combo_prob, disp, od_of, P_total, p_min=0.05, edge_min=0.30,
                          max_units=5, remaining_budget=MAX_TOTAL_UNITS,
                          stake_unit=STAKE_UNIT, p_scale=1.0):
    """未成立の組に各1口だけ置く。**的中時は全プール総取り**（2026/08/16 オーナーが確認）。

    実効オッズ = (プール総額 + 1口) / 1口。プールに初期金20万が含まれるので、
    誰も買っていない薄いプールほど跳ねる（プール22万なら23倍）。
    ⚠ 高EVだが**全部外れる確率も高い**。既定は OFF（画面の「未成立組も少額で買う」）。
    """
    if P_total <= 0 or max_units <= 0 or remaining_budget <= 0:
        return []
    eff = (P_total + stake_unit) / stake_unit
    cand = []
    for idx, p in combo_prob.items():
        p = p * p_scale                     # 市場情報が無い組は λ でモデル確率を割り引く
        names = tuple(disp[i] for i in idx)
        if od_of(names) is not None:
            continue
        if p < p_min or (p * eff - 1) < edge_min:
            continue
        cand.append((names, p))
    cand.sort(key=lambda x: x[1], reverse=True)
    cap = min(int(max_units), int(remaining_budget))
    return [(names, p, eff, 1) for names, p in cand[:cap]]


UNBET_ODDS = 1.5           # まだ誰も賭けていない馬に表示される初期値（実測で確認）
ODDS_DECIMALS = 2          # サイトが単勝オッズを丸めている桁数（実ログで確認）
ODDS_STEP = 10 ** -ODDS_DECIMALS


def estimate_win_pool(before, after, floor=None):
    """単勝プール総額を「試し買いの前後のオッズ変化」から推定する。

    単勝は控除0%の純パリミュチュエル（実ログ379レースで Σ(1/最終オッズ)=1.000、std 0.001）。
    そのため **オッズ自体はシェアしか表さず、プール規模の情報を一切含まない**。
    自分で少額を入れて、その前後の動きから逆算するしかない。

    原理:
      od_j = P / P_j（P=プール総額, P_j=その馬への投入額）
      自分が合計 Δ を入れると P → P+Δ。自分が **買っていない** 馬 j は P_j が変わらないので
          od_j後 / od_j前 = (P+Δ) / P  ＝ 全馬共通の比 R
      よって  P = Δ / (R − 1)

    推定の要点: オッズは小数2桁に丸められているため、丸め誤差は od に反比例する
    （高オッズの馬ほど相対誤差が小さい）。そこで比 R を **重み od² の加重平均**で求める。
    これが分散最小で、丸め誤差だけを考えたときの理論精度も同時に計算できる。
    """
    # 同名馬がいると名前キーの辞書では1頭消えるので、まず「名前+ステータス」で対応付け、
    # 駄目なら出走順（位置）で対応付ける。
    def _key(h):
        return (str(h.get('name', '')).strip(), h.get('speed'), h.get('power'), h.get('stamina'))

    pair = []
    kb = {}
    for h in before:
        kb.setdefault(_key(h), []).append(h)
    used = {}
    for h in after:
        k = _key(h)
        i = used.get(k, 0)
        cand = kb.get(k, [])
        if i < len(cand):
            pair.append((cand[i], h))
            used[k] = i + 1
    if len(pair) < len(after) and len(before) == len(after):
        pair = list(zip(before, after))          # 位置でのフォールバック
    msgs = []

    deltas = {}
    total_delta = 0.0
    for hb, ha in pair:
        mb, ma = hb.get('my_amount'), ha.get('my_amount')
        if mb is None or ma is None:
            continue
        d = float(ma) - float(mb)
        if d > 0:
            deltas[id(ha)] = d
            total_delta += d
    if total_delta <= 0:
        return {'ok': False, 'pool': None, 'messages': [
            '試し買いの増分が見つかりません。①の後に実際に単勝を買ってから②を取得してください。'
            '（「自分の購入額」が両方のデータに入っている必要があります）']}

    sw = sr = 0.0
    detail, singles = [], []
    for hb, ha in pair:
        n = str(ha.get('name', ''))
        ob, oa = hb.get('odds'), ha.get('odds')
        d_i = deltas.get(id(ha), 0.0)
        note = '試し買いした馬' if d_i > 0 else ''
        if ob is None or oa is None or not (np.isfinite(ob) and np.isfinite(oa)) \
                or ob <= 0 or oa <= 0:
            detail.append({'name': n, 'est': None, 'note': note or 'オッズ不明'})
            continue
        if d_i > 0:                       # 買った馬は P_j が動くので比の推定には使わない
            detail.append({'name': n, 'est': None, 'od_before': float(ob),
                           'od_after': float(oa), 'note': note})
            continue
        ratio = oa / ob
        if abs(ratio - 1.0) < 1e-12:
            detail.append({'name': n, 'est': None, 'od_before': float(ob),
                           'od_after': float(oa), 'note': '動かず'})
            continue
        w = oa * oa
        sw += w
        sr += w * ratio
        est = total_delta / (ratio - 1.0)
        singles.append(est)
        detail.append({'name': n, 'est': float(est), 'od_before': float(ob),
                       'od_after': float(oa), 'note': note})

    if sw <= 0:
        return {'ok': False, 'pool': None, 'per_horse': detail, 'delta': total_delta,
                'messages': ['オッズが動いておらず推定できません。'
                             '試し買いの口数を増やすか、市場が動いてから試してください。']}
    R = sr / sw
    if R <= 1:
        return {'ok': False, 'pool': None, 'per_horse': detail, 'delta': total_delta,
                'messages': ['オッズが想定と逆に動いています（他の人の投票が大きく入った可能性）。'
                             '測り直してください。']}
    pool = total_delta / (R - 1.0)
    sd_R = (ODDS_STEP / math.sqrt(12)) * math.sqrt(2.0 / sw)
    rel_err = float(sd_R * pool / total_delta)          # 1σ の相対誤差
    sd_abs = rel_err * pool                             # 1σ の絶対誤差（rrc）
    pos = sorted(x for x in singles if x > 0)
    spread = float((pos[-1] - pos[0]) / pool) if len(pos) > 1 else 0.0

    # 単勝プール総額は 1,000 rrc 単位で決まる。連続推定が十分精密なら、その最も近い
    # グリッド点にスナップすると値が“確定”する（丸め誤差より格子間隔が広いとき有効）。
    q = WIN_POOL_QUANTUM
    snapped = exact = False
    if sd_abs < q:                                     # 1σ が1格子未満 → 丸めが期待誤差を減らす
        snap_before = round(pool / q) * q
        if snap_before > 0:
            pool = float(snap_before)
            snapped = True
            if sd_abs < q / 4:                         # 2σ が半格子未満 → 95%で正しい格子
                exact = True
                rel_err = float(q / 4) / pool          # 実質“確定”（残差は半格子未満）
            else:
                rel_err = float(sd_abs / pool)         # スナップしても誤差は正直に残す

    if exact:
        msgs.append(f'✅ プール総額を 1,000 rrc 単位に確定：{pool + total_delta:,.0f} rrc。')
    elif rel_err > 0.05:
        msgs.append(f'△ 推定精度は ±{rel_err*200:.0f}%（95%目安）。オッズが小数2桁までしか'
                    '出ないため、プールが大きいと1口の影響が小さく精度が出ません。'
                    'もう一度試し買いすると累積で精度が上がります。')
    if spread > 0.5 and spread > rel_err * 6:
        msgs.append('⚠ 馬ごとの推定のばらつきが、丸め誤差だけでは説明できないほど大きいです。'
                    '試し買いの前後で他の人も投票した可能性があります。')
    if len(pos) < 3:
        msgs.append('△ 推定に使えた馬が少ないため精度は粗いです。')
    if not exact:
        msgs.append(f'自分の投入 {total_delta:,.0f} rrc の前後で、{len(pos)}頭のオッズ変化から'
                    f'推定しました（推定精度 ±{rel_err*200:.0f}%'
                    + ('・1000rrc単位にスナップ済み' if snapped else '') + '）。')
    # pool は試し買い"前"の総額。②のオッズは"後"なので、そちらに合わせた値も返す。
    return {'ok': True, 'pool': float(pool + total_delta), 'pool_before': float(pool),
            'per_horse': detail, 'n_used': len(pos), 'rel_err': rel_err,
            'spread': spread, 'delta': total_delta, 'snapped': snapped,
            'exact': exact, 'messages': msgs}


def win_bet_picks_pool(names, win_p, odds, pool, bankroll, kelly_frac, edge_min,
                       stake_unit=WIN_STAKE_UNIT, total_units=WIN_MAX_TOTAL_UNITS,
                       max_units=WIN_MAX_UNITS, risk_cap_frac=0.10, my_units=None,
                       unbet=None):
    """プール総額が分かっている場合の単勝配分（希薄化を織り込む）。

    パリミュチュエルなので、自分が k口 入れると
        実効オッズ = (P + Σk·u) / (P_i + k_i·u)
    と必ず下がる。表示オッズのまま計算すると期待値を過大評価するので、
    合計EVが最大になる配分を「限界EVが一番大きい馬へ1口ずつ」の貪欲法で求める。
    プールが小さいほど希薄化は激しく、口数は自然に絞られる。
    """
    n = len(names)
    od = np.asarray(odds, dtype=float)
    p = np.asarray(win_p, dtype=float)
    unb = np.zeros(n, dtype=bool) if unbet is None else np.asarray(unbet, dtype=bool)
    ok = np.isfinite(od) & (p > 0) & ((od > 1.0) | unb)
    if pool is None or pool <= 0 or not ok.any():
        return [], None
    # 未投票の馬（オッズが初期値のまま）は「その馬への投入額 0」。
    # 表示オッズ 1.5 をそのまま使うと実際とかけ離れるので 0 として扱う。
    P_i = np.where(ok & ~unb, pool / np.where(od > 0, od, 1.0), 0.0)
    k = np.zeros(n, dtype=int)
    # k0 = すでに買った分。pool と P_i は「現在の値」＝ k0 を含んでいるので、
    # プールの再計算では k0 を足してはいけない（足すと二重計上になり希薄化を過小評価する）。
    k0 = np.zeros(n, dtype=int) if my_units is None else np.asarray(my_units, dtype=int)

    risk_units = max(1, int((risk_cap_frac * bankroll) // stake_unit))
    budget = min(int(total_units), risk_units) - int(k0.sum())
    if budget <= 0:
        return [], {'note': '既に上限まで購入済み'}

    def total_ev(kv):
        """これから追加する kv 口ぶんの期待値。既存の k0 は pool / P_i に織り込み済み。"""
        P_new = pool + kv.sum() * stake_unit
        Pi_new = P_i + kv * stake_unit
        with np.errstate(divide='ignore', invalid='ignore'):
            eff = np.where(Pi_new > 0, P_new / Pi_new, 0.0)
        return float(np.sum(np.where(kv > 0, kv * stake_unit * (p * eff - 1.0), 0.0)))

    # ケリー上限（1口時の実効オッズで計算）
    caps = np.zeros(n, dtype=int)
    for i in range(n):
        if not ok[i]:
            continue
        eff1 = (pool + stake_unit) / (P_i[i] + stake_unit)
        edge = p[i] * eff1 - 1
        if edge < edge_min or eff1 <= 1:
            continue
        f = edge / (eff1 - 1)
        caps[i] = max(0, min(int((kelly_frac * f * bankroll) // stake_unit), int(max_units)))

    base = total_ev(k)
    used = 0
    while used < budget:
        best_i, best_gain = -1, 1e-9
        for i in range(n):
            if not ok[i] or k[i] + k0[i] >= caps[i]:
                continue
            trial = k.copy()
            trial[i] += 1
            g = total_ev(trial) - base
            if g > best_gain:
                best_gain, best_i = g, i
        if best_i < 0:
            break
        k[best_i] += 1
        base = total_ev(k)
        used += 1

    P_new = pool + k.sum() * stake_unit
    out = []
    for i in range(n):
        if k[i] <= 0:
            continue
        eff = P_new / (P_i[i] + k[i] * stake_unit)
        out.append({'name': names[i], 'p': float(p[i]),
                    'odds': (None if unb[i] else float(od[i])), 'unbet': bool(unb[i]),
                    'eff_od': float(eff), 'edge': float(p[i] * eff - 1),
                    'units': int(k[i]), 'stake': int(k[i] * stake_unit),
                    'ev': float(k[i] * stake_unit * (p[i] * eff - 1))})
    out.sort(key=lambda r: -r['ev'])
    summary = {'units': int(k.sum()), 'invest': int(k.sum() * stake_unit),
               'ev': float(base), 'pool_before': float(pool), 'pool_after': float(P_new),
               'hit': float(sum(r['p'] for r in out)), 'unit': stake_unit,
               'max_units': int(total_units), 'already': int(k0.sum())}
    return out, summary


def win_bet_picks(names, win_p, odds, bankroll, kelly_frac, edge_min,
                  max_units=WIN_MAX_UNITS, stake_unit=WIN_STAKE_UNIT,
                  risk_cap_frac=0.10, total_units=WIN_MAX_TOTAL_UNITS):
    """単勝の推奨（参考）。

    ゲーム仕様: 1レースの単勝は **合計** total_units 口まで（全頭の合算）。
    1頭あたりも max_units 口までだが、実際には合計側が先に効くことが多い。

    配分は「1口あたりの期待値（＝エッジ）が大きい買い目から、分数ケリーの口数まで
    埋めていく」貪欲法。単勝は自分の購入による希薄化を織り込めない（プール額が
    分からない）ので、EVは口数に対して線形とみなしている。

    プール額が不明な以上ここは参考値。控えめに使うこと。
    """
    cand = []
    for i, nm in enumerate(names):
        od = odds[i]
        p = win_p[i]
        if not np.isfinite(od) or od <= ODDS_FLOOR or p <= 0:
            continue
        edge = p * od - 1
        if edge < edge_min:
            continue
        f = edge / (od - 1)
        k_kelly = int((kelly_frac * f * bankroll) // stake_unit)
        cap = max(0, min(k_kelly, int(max_units)))
        if cap >= 1:
            cand.append({'name': nm, 'p': float(p), 'odds': float(od),
                         'edge': float(edge), 'cap': cap})

    # 合計上限 = min(ゲーム仕様の合計口数, 資金リスク上限)
    risk_units = max(1, int((risk_cap_frac * bankroll) // stake_unit))
    budget = min(int(total_units), risk_units)

    cand.sort(key=lambda r: -r['edge'])        # 1口あたりEVが大きい順
    out, used = [], 0
    for c in cand:
        if used >= budget:
            break
        k = min(c['cap'], budget - used)
        if k < 1:
            continue
        used += k
        out.append({'name': c['name'], 'p': c['p'], 'odds': c['odds'],
                    'edge': c['edge'], 'units': int(k),
                    'stake': int(k * stake_unit),
                    'ev': float(c['edge'] * k * stake_unit)})
    out.sort(key=lambda r: -r['ev'])
    return out


# =====================================================================
#  10. 解析
# =====================================================================
DEFAULT_SETTINGS = dict(
    dist='中距離', track='芝', ground='良', topn=20,
    bankroll=1_200_000, kelly_fraction=0.25, max_risk_frac=0.10, edge_min=0.10,
    carryover_rrc=None, csv_path='',
    unformed_sleeve=False, unformed_max_units=5,
    unformed_p_min=0.05, unformed_edge_min=0.30,
    win_bets=False, win_edge_min=0.15,
    n_sim=N_SIM, spec_path=None,
    # モデル確率をどこまで信じるか。EV計算では p_bet = λ×モデル + (1−λ)×市場 を使う。
    # モデルと市場が食い違うとき、食い違いの一部は必ずモデル側の誤差なので、
    # λ=1（モデル全信頼）はそのまま「モデルの誤差に賭ける」ことになる。
    model_weight=0.7,
    # モデル的中率がこれ未満の組は買わない。モンテカルロの試行数に対して確率が小さすぎる
    # 組は推定ノイズが支配的で、「偽の+EV」のほぼ全てがこの領域から出る。
    min_prob=0.003,
)


def analyze(raw_text, bundle, settings=None):
    s = dict(DEFAULT_SETTINGS)
    if settings:
        # None は「指定なし」＝既定値を使う、の意味。
        # 以前は「既知キーなら None でも上書き」になっており、model_weight=None を
        # 渡すと後段の float(None) で TypeError になっていた。
        s.update({k: v for k, v in settings.items() if v is not None})
    if not bundle or not bundle.get('ok'):
        return {'ok': False, 'error': 'モデル未学習。先にログを読み込んで学習してください。'}

    res = {'ok': True, 'error': None, 'messages': [], 'pool_msgs': []}
    P_total = 0
    csv_odds = None
    n_tri_total = 0

    _spec = (bundle or {}).get('spec') or default_spec()

    if '出走馬一覧' in raw_text:
        (horses, csv_odds, a_dist, a_track, a_ground,
         guild, schedule_id, clip_pool, n_tri_total) = parse_unified(raw_text, _spec)
        res['auto_race_info'] = bool(a_dist and a_track)
        # ベットログのレースIDに使う。schedule_id にしておくと、精算時に
        # 結果APIから着順と最終オッズを自動で引ける（settle_bets.py）。
        res['schedule_id'] = schedule_id
        res['guild'] = guild
        for key, val, label in [('dist', a_dist, '距離'), ('track', a_track, '馬場'),
                                ('ground', a_ground, '地面')]:
            if val:
                s[key] = val
                res['messages'].append(f'{label}を自動設定: {val}')
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
        # 誰も買っていないと表示は 0 だが、実際には初期プール金が入っている。
        # 1口入った瞬間に 0 → 21万 に飛ぶ（＝0 のときも 20万 は存在している）。
        # ここを 0 のままにすると未成立スリーブの実効オッズが (0+1口)/1口 = 1.0 になり、
        # **本当は21倍で最も美味しい場面を「価値なし」と判定**してしまう。
        if P_total == 0 and TRIFECTA_SEED_BUG_ACTIVE:
            P_total = TRIFECTA_POOL_SEED
            res['pool_msgs'].append(
                f'プール表示が 0 なので初期プール金 {TRIFECTA_POOL_SEED:,} rrc を'
                'プール総額として扱います（表示は1口入るまで 0 のまま、'
                f'入った瞬間に {TRIFECTA_POOL_SEED + STAKE_UNIT:,} に飛ぶため）。'
                f'未成立に1口置いた場合の実効オッズは '
                f'{(TRIFECTA_POOL_SEED + STAKE_UNIT) / STAKE_UNIT:.0f}倍です。')
    else:
        horses = parse_betting_screen(raw_text)
        res['auto_race_info'] = False
        res['messages'].append(
            f'⚠ 購入画面の貼り付けにはレース条件が含まれていません。'
            f'サイドバーの設定（**{s["dist"]}・{s["track"]}・{s["ground"]}**）で計算します。'
            '距離が違うと結果は大きく変わるので、必ず合わせてください。')

    if not horses:
        return {'ok': False, 'error': 'データ読み込み失敗（フォーマットを確認してください）。'}

    # 貼り付けデータにパッシブの説明文があれば数値スペックとして取り込む
    res['spec_learned'] = []
    learned = dict(parse_passive_descriptions(raw_text))
    learned.update(parse_passive_effect_section(raw_text))
    if learned:
        cur = bundle.get('spec') or default_spec()
        _, changed = merge_passive_spec(cur, learned, s.get('spec_path'))
        res['spec_learned'] = changed
        if changed:
            res['messages'].append(
                '📘 パッシブの数値を貼り付けから取り込みました（' + ', '.join(changed) +
                '）。**再学習すると予測に反映されます。**')
        else:
            res['messages'].append(
                f'📘 パッシブの説明文 {len(learned)}種を確認（すべて登録済みの数値と一致）。')

    n = len(horses)
    res['n_field'] = n
    _bal = ((horses[0].get('_meta') or {}).get('balance') if horses else None)
    if _bal:
        res['balance'] = float(_bal)
        if abs(float(_bal) - float(s['bankroll'])) > 1:
            res['messages'].append(
                f'⚠ 貼り付けの所持金 {int(_bal):,} rrc と資金設定 {int(s["bankroll"]):,} rrc が'
                '違います。口数は資金設定のほうで計算しています。')
    _ik = ((horses[0].get('_meta') or {}).get('item_effects_skipped') if horses else None)
    if _ik:
        res['messages'].append(
            f'⚠ 常時発動ではない装備効果 {len(_ik)}件 はモデルに反映していません: '
            f'{", ".join(map(str, _ik[:4]))}。発動率が読めないため、'
            '勝手に盛らず素の値で計算しています。')
    _im = [h for h in horses if h.get('item_mult')]
    if _im:
        _d = '／'.join(f"{h['name']}: " + ' '.join(
            f'{k}×{v:.3f}' for k, v in sorted(h['item_mult'].items())) for h in _im[:4])
        res['messages'].append(f'🛡 装備・お守りの常時倍率を反映しました（{len(_im)}頭）: {_d}')
    _sk = ((horses[0].get('_meta') or {}).get('skipped_rows') if horses else None)
    if _sk:
        res['messages'].append(
            f'⚠ ステータスが読めず除外した行が {len(_sk)}件 あります: {", ".join(map(str, _sk[:6]))}。'
            '出走頭数が変わると全確率がずれるので、貼り付けデータを確認してください。')
    if n < MIN_FIELD_TRIFECTA:
        res['messages'].append(
            f'⚠ 出走 {n}頭。現行ルールでは {MIN_FIELD_TRIFECTA}頭未満のレースで3連単は購入できません'
            '（単勝のみ）。')
    if n > MAX_FIELD:
        res['messages'].append(f'⚠ 出走 {n}頭は想定（最大{MAX_FIELD}頭）を超えています。')

    # パッシブの健全性チェック
    used = [p for h in horses for p in h.get('passives', ())]
    unknown = sorted({p for p in used if p not in PASSIVE_CATALOG})
    unseen = sorted({p for p in used if p in PASSIVE_CATALOG
                     and bundle.get('passive_counts', {}).get(p, 0) == 0})
    thin = sorted({p for p in used if p in PASSIVE_CATALOG
                   and 0 < bundle.get('passive_counts', {}).get(p, 0) < 8})
    if unknown:
        res['messages'].append('⚠ カタログ外のパッシブ（効果0扱い）: ' + ', '.join(unknown))
    if unseen:
        res['messages'].append('⚠ 学習データに無いパッシブ（効果0扱い）: ' + ', '.join(unseen))
    if thin:
        res['messages'].append('△ サンプルが少ないパッシブ（推定が粗い）: ' + ', '.join(thin))
    _same = same_species_flags([h.get('name', '') for h in horses],
                               [h.get('species') for h in horses])
    _kin = [h['name'] for i, h in enumerate(horses)
            if _same[i] and '同族嫌悪' in h.get('passives', ())]
    if _kin:
        res['messages'].append('🔥 同族嫌悪が発動する馬: ' + ', '.join(_kin)
                               + '（同じおあしすっちが同レースにいるため全ステータス+20%）')
    n_pas2 = sum(1 for h in horses if len(h.get('passives', ())) >= 2)
    res['messages'].append(f'パッシブ2枠の馬: {n_pas2}/{n}頭')

    unseen_cond = sorted({h.get('condition', '普通') for h in horses
                          if h.get('condition') not in bundle.get('train_conditions', set())})
    if unseen_cond:
        res['messages'].append('⚠ 未学習コンディション（baseline扱い）: ' + ', '.join(unseen_cond))

    if s['dist'] not in DIST_LIST:
        return {'ok': False, 'error':
                f'距離「{s["dist"]}」は未知です（対応: {" / ".join(DIST_LIST)}）。'
                'このまま計算するとステータスが一切効かず、全馬同じ確率になってしまうため中止しました。'
                'サイドバーの距離を選び直すか、貼り付けデータの「レース距離」を確認してください。'}
    if s['track'] not in TRACK_LIST:
        res['messages'].append(
            f'⚠ 馬場「{s["track"]}」は未知です（対応: {" / ".join(TRACK_LIST)}）。'
            '芝得意/ダート得意が発動しない扱いになります。')
    res['dist'], res['track'], res['ground'] = s['dist'], s['track'], s['ground']

    # --- 予測 + シミュレーション ---
    # 単勝の勝率は race_sigma、3連単は tri_sigma（8頭以上の順番的中で校正した大きめの σ）で
    # 別々にシミュレーションする。1着は頑健で小さめσが合い、3連単は順番のブレが大きいため。
    base = predict_base(bundle, horses, s['dist'], s['track'])
    sigma = bundle['race_sigma']
    tri_sigma = float(bundle.get('tri_sigma') or sigma)
    n_sim = int(s.get('n_sim') or N_SIM)
    sig_vec = horse_sigmas(bundle, horses, sigma)
    if abs(tri_sigma - sigma) < 1e-9:
        # σが同じなら、単勝用と3連単用のシミュレーションは（シードも同じなので）
        # 完全に同一の結果になる。1回で両方受け取る。
        win_p, combo_prob = simulate_trifecta(base, sig_vec, n_sim=n_sim)
    else:
        win_p, _ = simulate_trifecta(base, sig_vec, n_sim=n_sim, need_combo=False)
        sig_vec_tri = horse_sigmas(bundle, horses, tri_sigma)
        _, combo_prob = simulate_trifecta(base, sig_vec_tri, n_sim=n_sim)
    n_steady = int((sig_vec < sigma * 0.999).sum())
    if n_steady:
        res['messages'].append(
            f'分散低減スキル（安定感など）を {n_steady}頭に反映しました'
            f'（σ×{float(sig_vec.min()/sigma):.2f}）。')
    disp = disambiguate([h['name'] for h in horses])

    odds = np.array([h.get('odds', np.nan) for h in horses], dtype=float)
    # 表示オッズ 1.5 は「未投票」と「本命すぎて下限に張り付いた」の両方を意味しうる。
    # Σ(1/od) から区別し、張り付きなら本当のオッズに直す（誤ると実効オッズを
    # 桁違いに過大評価して大本命に高配当の買い推奨を出してしまう）。
    _fl = diagnose_floor_odds(odds, [h.get('my_amount') for h in horses])
    odds_eff = np.asarray(_fl['odds_eff'], dtype=float)
    unbet_flags = list(_fl['unbet'])
    res['floor_diag'] = {'residual': _fl['residual'], 'ambiguous': _fl['ambiguous']}
    for _m in _fl['messages']:
        res['messages'].append(_m)
    # 市場確率は「未投票馬を除外し、下限に張り付いた本命は本当のオッズで」計算する
    odds_mkt = odds_eff.copy()
    odds_mkt[np.array(unbet_flags, dtype=bool)] = np.nan
    mkt_p = market_win_prob(odds_mkt, floor=1.0)

    # 寄与の内訳（なぜこの馬が強い/弱いか）
    contrib = _contributions(bundle, horses, s['dist'], s['track'])

    order = np.argsort(-win_p)
    single = []
    for i in order:
        h = horses[i]
        row = {'name': disp[i], 'model_p': float(win_p[i]), 'market_p': None,
               'odds': None, 'tag': '', 'base': float(base[i]),
               'passives': ' / '.join(h.get('passives', ())) or 'なし',
               'condition': h.get('condition', '普通'),
               'contrib': contrib[i]}
        if mkt_p is not None:
            row['market_p'] = float(mkt_p[i])
            od = odds[i]
            row['odds'] = float(od) if np.isfinite(od) else None
            if unbet_flags[i]:
                row['tag'] = '（未投票）'
            elif not np.isfinite(od):
                row['tag'] = '（オッズ不明）'
            elif np.isfinite(odds_eff[i]) and odds_eff[i] < od - 1e-9:
                row['tag'] = '◆下限張り付き(大本命)'
            elif win_p[i] > mkt_p[i] * MARKET_EDGE_RATIO and win_p[i] > MARKET_MIN_PROB:
                row['tag'] = '★割安'
            elif mkt_p[i] > win_p[i] * MARKET_EDGE_RATIO and mkt_p[i] > MARKET_MIN_PROB:
                row['tag'] = '割高(罠)'
        single.append(row)
    res['single_win'] = single
    res['model_pick'] = disp[order[0]]
    res['has_market'] = mkt_p is not None

    # 単勝の推奨（任意）。1口の単位は貼り付け画面の『1口（N rrc）』から自動検出。
    win_unit = detect_stake_unit(raw_text, default=WIN_STAKE_UNIT)
    res['win_unit'] = win_unit
    res['win_picks'] = []
    res['win_summary'] = None
    res['win_pool'] = None
    meta = (horses[0].get('_meta') if horses else None) or {}
    if meta.get('win_pool'):
        res['win_pool'] = float(meta['win_pool'])
        res['win_pool_info'] = {
            'delta': meta.get('win_pool_delta'), 'n': meta.get('win_pool_n'),
            'spread': meta.get('win_pool_spread'), 'err': meta.get('win_pool_err')}
        err = meta.get('win_pool_err')
        is_exact = bool(meta.get('win_pool_exact'))
        res['win_pool_info']['exact'] = is_exact
        if is_exact:
            res['messages'].append(
                f'✅ 単勝プール総額を 1,000 rrc 単位で確定: {res["win_pool"]:,.0f} rrc'
                + (f'（試し買い {meta["win_pool_delta"]:,.0f} rrc / '
                   f'{int(meta.get("win_pool_n", 0))}頭から）' if meta.get('win_pool_delta') else '）'))
        else:
            res['messages'].append(
                f'🔬 単勝プールの実測値を取り込みました: {res["win_pool"]:,.0f} rrc'
                + (f'（試し買い {meta["win_pool_delta"]:,.0f} rrc / '
                   f'{int(meta.get("win_pool_n", 0))}頭から' if meta.get('win_pool_delta') else '')
                + (f' / 精度 ±{err*200:.0f}%）' if err else '）'))
        if not is_exact and err and err > 0.05:
            res['messages'].append(
                f'⚠ 単勝プールの推定精度が ±{err*200:.0f}% と粗いです。'
                'もう一度実測すると精度が上がります。口数は控えめに。')
    if str(meta.get('win_market', '')) == 'hidden':
        res['messages'].append(
            '⚠ 全馬のオッズが下限のままですが、プールは空ではありません'
            '（1頭が大半を集めるとオッズが下限に張り付き、未投票の馬と見分けがつきません）。'
            'どの馬にお金が入っているか判別できないため、単勝の推奨は出しません。')
    if meta.get('win_market') == 'empty' or str(meta.get('win_market', '')) == 'empty':
        res['messages'].append(
            '⚠ 単勝プールが空です（全馬のオッズが初期値のまま）。'
            'この状態で単勝を買っても実効オッズは 1.0 で、自分の掛け金を取り返すだけです。'
            '他の人の投票が入ってから買ってください。')
    own = float(meta.get('win_own') or 0)
    others_ok = True
    if own and res['win_pool']:
        others = float(res['win_pool']) - own
        if others <= 0.15 * float(res['win_pool']):
            others_ok = False
            res['messages'].append(
                f'⚠ 単勝プール {res["win_pool"]:,.0f} rrc のうち {own:,.0f} rrc は自分の掛け金で、'
                f'他人のお金は {max(others,0):,.0f} rrc しかありません。'
                'パリミュチュエルは他人の掛け金を取りに行くゲームなので、'
                'この状態では**どう買っても期待値はマイナス**です。単勝の推奨は出しません。')
        elif others <= 0.4 * float(res['win_pool']):
            res['messages'].append(
                f'△ 単勝プールの {own/float(res["win_pool"])*100:.0f}% が自分の掛け金です。'
                '取りに行ける他人のお金が少ないので、控えめに。')
    if s.get('win_bets') and mkt_p is not None and res['win_pool'] and not others_ok:
        res['win_pool_mode'] = '実測プール（自分の掛け金が大半のため推奨なし）'
    lam_w = float(s.get('model_weight', 0.7))
    win_p_bet = (lam_w * win_p + (1 - lam_w) * mkt_p) if mkt_p is not None else lam_w * win_p
    if s.get('win_bets') and mkt_p is not None and res['win_pool'] and others_ok:
        my_units = [int((h.get('my_amount') or 0) // win_unit) for h in horses]
        unbet = list(unbet_flags)
        if any(unbet):
            res['messages'].append(
                f'ℹ 未投票（オッズ {UNBET_ODDS} のまま・Σ(1/od) で確認済み）の馬が '
                f'{sum(unbet)}頭 あります。'
                'この馬たちは「その馬への投入額0」として実効オッズを計算します'
                '（当たれば非常に高配当ですが、高分散です）。')
        names_o = [disp[i] for i in range(n)]
        picks, summ = win_bet_picks_pool(
            names_o, win_p_bet, odds_eff, res['win_pool'], s['bankroll'], s['kelly_fraction'],
            s.get('win_edge_min', 0.15), stake_unit=win_unit,
            risk_cap_frac=s['max_risk_frac'], my_units=my_units, unbet=unbet)
        res['win_picks'] = picks
        res['win_summary'] = summ
        res['win_pool_mode'] = '実測プール（希薄化込み）'
    elif s.get('win_bets') and mkt_p is not None:
        res['win_picks'] = win_bet_picks(
            disp, win_p_bet, odds_eff, s['bankroll'], s['kelly_fraction'],
            s.get('win_edge_min', 0.15), stake_unit=win_unit,
            risk_cap_frac=s['max_risk_frac'])
        if res['win_picks']:
            wu = sum(r['units'] for r in res['win_picks'])
            res['win_summary'] = {
                'units': wu, 'max_units': WIN_MAX_TOTAL_UNITS,
                'invest': wu * win_unit, 'unit': win_unit,
                'ev': sum(r['ev'] for r in res['win_picks']),
                'hit': float(sum(r['p'] for r in res['win_picks'])),
                'capped': wu >= WIN_MAX_TOTAL_UNITS}

    # --- 3連単 ---
    exact_cp, name_cp = {}, {}
    for (i, j, k), p in combo_prob.items():
        ek = (disp[i], disp[j], disp[k])
        exact_cp[ek] = exact_cp.get(ek, 0.0) + p
        bk = (bare(disp[i]), bare(disp[j]), bare(disp[k]))
        name_cp[bk] = name_cp.get(bk, 0.0) + p
    screen_names = set(disp)

    # 素名フォールバックは「同名馬が居ない場合」に限る。
    # 同名馬が居るのに素名で引くと、別個体の確率をコピーしてしまい
    # 実力の無い方の組にも同額を配分してしまう（重大な誤配分）。
    _bare_counts = {}
    for _d in disp:
        _b = bare(_d)
        _bare_counts[_b] = _bare_counts.get(_b, 0) + 1
    _bare_unique = {b for b, c in _bare_counts.items() if c == 1}

    def lookup_prob(combo):
        if combo in exact_cp:
            return exact_cp[combo], 'exact'
        bk = tuple(bare(x) for x in combo)
        if all(x in _bare_unique for x in bk) and bk in name_cp:
            return name_cp[bk], 'bare'
        return 0.0, 'none'

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

    # --- 3連単オッズの補正（サイト側のバグ）---
    # ここで1回だけ直せば、この先の実効オッズもEVも口数配分も自動的に正しくなる
    # （どこも「その組の賭け金 = プール総額 ÷ オッズ」で逆算しているため）。
    # Σ(1/od) を見るキャリーオーバー判定だけは**補正前**の値でないと二重計上になるので、
    # 元の値を csv_odds_raw に取っておく。
    csv_odds_raw = dict(csv_odds) if csv_odds else {}
    if csv_odds and P_total > TRIFECTA_POOL_SEED and TRIFECTA_SEED_BUG_ACTIVE:
        _f = P_total / (P_total - TRIFECTA_POOL_SEED)
        csv_odds = {k: true_trifecta_odds(v, P_total) for k, v in csv_odds.items()}
        res['odds_fix_ratio'] = _f          # 画面表示との食い違いを UI で説明するため
        res['pool_msgs'].append(
            f'3連単オッズを ×{_f:.3f} 補正しました（初期プール金 '
            f'{TRIFECTA_POOL_SEED:,} rrc が表示オッズに含まれていないサイト側の仕様。'
            f'実際の払戻はこの補正後の値です）。')
        if _f > 2.0:
            res['pool_msgs'].append(
                f'⚠ 実際の投票額は {P_total - TRIFECTA_POOL_SEED:,} rrc しかありません'
                f'（プール総額の大半が初期プール金）。補正が ×{_f:.1f} と大きく、'
                'EVは数学的には正しくても、少額の投票が入るだけで大きく動きます。'
                '口数を抑えるか見送るのが安全です。')
    elif csv_odds and 0 < P_total <= TRIFECTA_POOL_SEED:
        res['pool_msgs'].append(
            f'⚠ プール総額 {P_total:,} rrc が初期プール金 {TRIFECTA_POOL_SEED:,} rrc 以下です。'
            'オッズ補正ができないので、表示オッズのまま（＝EVを過小評価）で計算します。')
    res['mc_note'] = (
        f'モンテカルロ {n_sim:,} 試行 / σ=単勝 {sigma:.4f}'
        + (f'・3連単 {tri_sigma:.4f}' if abs(tri_sigma - sigma) > 1e-9 else '（3連単も同じ）')
        + '。確率 0.01% 付近の組は±10%程度のブレがあります。')

    odds_exact, odds_bare = {}, {}
    if csv_odds:
        for (a, b, c), od in csv_odds.items():
            odds_exact[(a, b, c)] = od
            odds_bare[(bare(a), bare(b), bare(c))] = od

    def od_of(nm):
        if nm in odds_exact:
            return odds_exact[nm]
        return odds_bare.get(tuple(bare(x) for x in nm))

    # オッズが1件も無くても、未成立スリーブだけは検討する。
    # 誰も買っていない＝全組が未成立＝当たれば初期プール金を総取りできる場面で、
    # ここを素通りすると**いちばん美味しいレースだけ何も出さない**ことになる。
    if csv_odds or (s.get('unformed_sleeve') and P_total > 0):
        lam = float(s.get('model_weight', 0.7))
        min_p = float(s.get('min_prob', 0.003))
        # 市場の暗黙確率（成立組のみ・正規化）。q_i = (1/od_i)/Σ(1/od)。
        # **補正前のオッズを使うこと。** 補正後は全オッズが _f 倍されるので
        # Σ(1/od) が 1/_f まで落ち、max(...,1.0) のクランプに当たって
        # 市場確率が一律 1/_f 倍に縮む（＝λ混合の市場側が実質無効化される）。
        # 完全な市場なら補正前の Σ(1/od) は 1.00 になる（初期プール金の分は
        # 分子・分母で相殺されるため）。回帰テスト P2 が固定している。
        _q_odds = csv_odds_raw or csv_odds
        inv_sum = sum(1.0 / od for od in _q_odds.values()
                      if od and math.isfinite(od) and od > 0)
        inv_norm = max(inv_sum, 1.0)
        _fix = float(res.get('odds_fix_ratio', 1.0) or 1.0)   # 補正後→補正前に戻す倍率

        def blend(p_model, od):
            # od は補正後。市場シェアは補正前の値で出すので _fix を掛けて戻す
            # （1/od_raw = _fix/od_corrected）。
            q = (_fix / od) / inv_norm if (od and od > 0) else 0.0
            return lam * p_model + (1.0 - lam) * q

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
            pb = blend(p, od)
            rows.append((combo, p, od, STAKE_UNIT * (pb * od - 1)))
        rows.sort(key=lambda x: x[3], reverse=True)
        res['messages'].append(
            f'ℹ EVはモデル確率 λ={lam:.0%} で市場と混合し、'
            f'モデル的中率 {min_p:.1%} 未満の組を除外して計算しています'
            '（モデルと市場の食い違いの一部は必ずモデル側の誤差のため、'
            'モデルを全信頼するとその誤差に賭けることになります）。')
        res['mode'] = ('完全名一致' if how_counts['exact'] and not how_counts['bare']
                       else '素名フォールバック' if how_counts['bare'] and not how_counts['exact']
                       else '混在')
        res['bare_used'] = bool(how_counts['bare'])
        res['unmatched_names'] = sorted(unmatched)

        if P_total > 0:
            expected = n * (n - 1) * (n - 2) if n >= 3 else 0
            co_trust = (n_tri_total > 0 and n_tri_total == expected)
            payout_pool, cinfo = resolve_payout_pool(
                P_total, csv_odds_raw.values(),      # ← 補正前。補正後だとCOを二重に足す
                manual_co=s.get('carryover_rrc'), trust=co_trust)
            inv, reg = cinfo['inv_sum'], cinfo['regime']
            if reg == 'takeout':
                res['pool_msgs'].append(f'Σ(1/od)={inv:.3f} → 控除あり(約{(1-1/inv)*100:.1f}%)。補正なし。')
            elif reg == 'neutral':
                res['pool_msgs'].append(f'Σ(1/od)={inv:.3f} → 控除0%・CO無し。プール {P_total:,} rrc。')
            elif reg == 'carryover_added':
                res['pool_msgs'].append(
                    f'Σ(1/od)={inv:.3f} → キャリーオーバー検出（推定 ≈ {cinfo["carryover"]:,.0f} rrc）。'
                    f'払戻プール {payout_pool:,.0f} rrc で計算。')
                P_total = int(round(payout_pool))
            elif reg == 'carryover_untrusted':
                res['pool_msgs'].append(
                    f'Σ(1/od)={inv:.3f} → CO検出だが全{expected}組中{n_tri_total}組のみで不完全 → 補正なし。')
            elif reg == 'manual':
                res['pool_msgs'].append(
                    f'手動CO {cinfo["carryover"]:,.0f} rrc を加算 → 払戻プール {payout_pool:,.0f} rrc。')
                P_total = int(round(payout_pool))
            elif reg == 'carryover_unsure':
                res['pool_msgs'].append(f'Σ(1/od)={inv:.3f} は異常値 → 安全側で補正なし。')
        res['pool'] = P_total

        alloc = allocate_units_stable(
            [(c, blend(p, od), od) for c, p, od, ev in rows if p >= min_p], P_total,
            bankroll=s['bankroll'], kelly_frac=s['kelly_fraction'],
            max_risk_frac=s['max_risk_frac'], edge_min=s['edge_min'],
            budget=MAX_TOTAL_UNITS, max_per_combo=MAX_UNITS)

        risk_units_cap = max(1, int((s['max_risk_frac'] * s['bankroll']) // STAKE_UNIT))
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

        n_unformed = 0
        if s.get('unformed_sleeve') and P_total > 0:
            sleeve = unformed_sleeve_picks(
                combo_prob, disp, od_of, P_total,
                p_min=s.get('unformed_p_min', 0.05),
                edge_min=s.get('unformed_edge_min', 0.30),
                max_units=s.get('unformed_max_units', 5),
                remaining_budget=min(MAX_TOTAL_UNITS, risk_units_cap) - total_units,
                p_scale=lam)
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
        ranked = sorted(combo_prob.items(), key=lambda x: x[1], reverse=True)
        res['breakeven_rows'] = [
            {'combo': ' → '.join(disp[i] for i in idx), 'model_p': p,
             'need_od': (1 / p if p > 0 else float('inf'))}
            for idx, p in ranked[:max(int(s['topn']), 1)]]
        res['mode'] = None
        res['summary'] = None
        res['alloc_rows'] = []
        res['bare_used'] = False
        res['unmatched_names'] = []

    ranked_p = sorted(combo_prob.items(), key=lambda x: x[1], reverse=True)
    shown = ranked_p[:max(int(s['topn']), 1)]
    ranking, cum = [], 0.0
    for rk, (idx, p) in enumerate(shown, 1):
        cum += p
        names = tuple(disp[i] for i in idx)
        od = od_of(names)
        row = {'rank': rk, 'combo': ' → '.join(names), 'model_p': p, 'cum': cum,
               'flag': ('成' if od else '未'), 'eff1_od': None, 'ev1': None,
               'plus_ev': False}
        if P_total > 0:
            P_c = (P_total / od) if od else 0.0
            eff = (P_total + STAKE_UNIT) / (P_c + STAKE_UNIT)
            lam_r = float(s.get('model_weight', 0.7))
            if csv_odds and od:
                # ここも補正前のオッズで正規化する（上の inv_norm と同じ理由）
                _fx = float(res.get('odds_fix_ratio', 1.0) or 1.0)
                q = (_fx / od) / max(sum(1.0 / o for o in (csv_odds_raw or csv_odds).values()
                                         if o and o > 0), 1.0)
                pb = lam_r * p + (1 - lam_r) * q
            else:
                pb = lam_r * p
            row.update(eff1_od=eff, ev1=(pb * eff - 1) * STAKE_UNIT, plus_ev=(pb * eff > 1))
        ranking.append(row)
    res['ranking'] = ranking
    res['ranking_pool_known'] = P_total > 0
    res['ranking_cover'] = min(cum, 1.0)
    res['horses_disp'] = list(disp)
    res['passive_effects'] = passive_effects(bundle, s['dist'], s['track'])
    return res


def _contributions(bundle, horses, dist, track):
    """各馬の予測値を「ステータス由来 / コンディション / パッシブ」に分解する。
    パッシブは実効ステータスに畳み込まれているので、素ステータスとの差分で寄与を出す。"""
    coef = dict(zip(bundle['feature_names'], bundle['model'].coef_))
    spec = bundle.get('spec') or default_spec()
    b_sp = coef.get(f'{dist}:log(SP)', 0.0)
    b_pw = coef.get(f'{dist}:log(PW)', 0.0)
    b_st = coef.get(f'{dist}:log(ST)', 0.0)
    l_sp = coef.get(f'{dist}:lin(SP)', 0.0)
    l_pw = coef.get(f'{dist}:lin(PW)', 0.0)
    l_st = coef.get(f'{dist}:lin(ST)', 0.0)
    same = same_species_flags([h.get('name', '') for h in horses],
                              [h.get('species') for h in horses])
    out = []
    for hi, h in enumerate(horses):
        ctx = {'same_species': same[hi]}
        e = effective_stats(h['speed'], h['power'], h['stamina'],
                            h.get('passives', ()), dist, track, spec, ctx)
        sp = math.log(max(float(h['speed']), 1.0))
        pw = math.log(max(float(h['power']), 1.0))
        st = math.log(max(float(h['stamina']), 1.0))
        lsp0, lpw0, lst0 = float(h['speed']) / 100.0, float(h['power']) / 100.0, float(h['stamina']) / 100.0
        c_sp = b_sp * sp + l_sp * lsp0
        c_pw = b_pw * pw + l_pw * lpw0
        c_st = b_st * st + l_st * lst0
        b_sur, b_sh = coef.get('スタミナ余り', 0.0), coef.get('スタミナ不足', 0.0)
        e0 = {'speed': float(h['speed']), 'power': float(h['power']),
              'stamina': float(h['stamina'])}

        def _bud(ee):
            _, sh_, su_ = stamina_budget(ee, dist)
            return (b_sur * su_ + b_sh * sh_) / 10.0
        c_st += _bud(e0)
        c_spec = (_bud(e) - _bud(e0)
                  + b_sp * (math.log(e['speed']) - sp)
                  + b_pw * (math.log(e['power']) - pw)
                  + b_st * (math.log(e['stamina']) - st)
                  + l_sp * (e['speed'] - float(h['speed'])) / 100.0
                  + l_pw * (e['power'] - float(h['power'])) / 100.0
                  + l_st * (e['stamina'] - float(h['stamina'])) / 100.0)
        cond = h.get('condition', '普通')
        c_cond = coef.get('好調', 0.0) if cond == '好調' else (
            coef.get('不調', 0.0) if cond == '不調' else 0.0)
        c_pass, detail = c_spec, []
        for p in h.get('passives', ()):
            sp_ = spec.get(p)
            if sp_ and sp_.get('mult'):
                e1 = effective_stats(h['speed'], h['power'], h['stamina'], (p,),
                                     dist, track, spec, ctx)
                v = (_bud(e1) - _bud(e0)
                     + b_sp * (math.log(e1['speed']) - sp)
                     + b_pw * (math.log(e1['power']) - pw)
                     + b_st * (math.log(e1['stamina']) - st)
                     + l_sp * (e1['speed'] - float(h['speed'])) / 100.0
                     + l_pw * (e1['power'] - float(h['power'])) / 100.0
                     + l_st * (e1['stamina'] - float(h['stamina'])) / 100.0)
            elif sp_ and sp_.get('scope') == 'variance':
                v = 0.0
            elif PASSIVE_CATALOG.get(p) == 'aptitude':
                col, val = APTITUDE_MATCH[p]
                ok = (track == val) if col == 'track' else (dist == val)
                v = coef.get(p, 0.0) if ok else 0.0
                c_pass += v
            elif p in PASSIVE_CATALOG:
                v = coef.get(p, 0.0) + INTERACTION_SHRINK * coef.get(f'{p}×{dist}', 0.0)
                c_pass += v
            else:
                v = 0.0
            detail.append((p, float(v)))
        out.append({'speed': c_sp, 'power': c_pw, 'stamina': c_st,
                    'condition': float(c_cond), 'passive': float(c_pass),
                    'passive_detail': detail})
    return out


# =====================================================================
#  11. ベットログ
# =====================================================================
# payout_kind: 払戻額の出どころ。'実績'=精算時に最終オッズを入力した / '概算'=購入時オッズ換算。
# パリミュチュエルなのでオッズは締切時に変動する。購入時オッズのままだと ROI が系統的にずれる。
LOG_COLUMNS = ['bet_id', 'time', 'race_id', 'bet_type', 'combo', 'model_prob',
               'odds', 'stake', 'status', 'result', 'payout', 'pnl', 'payout_kind']


class BetLogReadError(RuntimeError):
    """ベットログの読み込みに失敗（この場合は絶対に書き込まない）。"""


class BetLog:
    """賭けと結果の記録。保存先はローカルCSV、または store（Google スプレッドシート等）。

    store は read_df() / write_df(df) の2メソッドだけ持てばよい。
    store が指定されていればそちらを優先し、無ければ path の CSV を使う。
    """

    def __init__(self, path=None, race_sigma=None, store=None):
        self.path = path or 'oasis_bet_log.csv'
        self.race_sigma = race_sigma
        self.store = store

    @property
    def location(self):
        return 'Google スプレッドシート' if self.store else self.path

    def _normalize(self, df):
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=LOG_COLUMNS)
        if 'bet_type' not in df.columns:
            df['bet_type'] = '3連単'
        for c in ['race_id', 'combo', 'status', 'result', 'time', 'bet_type',
                  'payout_kind']:
            if c in df.columns:
                df[c] = df[c].fillna('').astype(str)
        for c in ['bet_id', 'model_prob', 'odds', 'stake', 'payout', 'pnl']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        for c in LOG_COLUMNS:
            if c not in df.columns:
                df[c] = '' if c in ('result', 'combo', 'status', 'payout_kind') else 0
        return df[LOG_COLUMNS]

    def load(self, strict=False):
        """strict=True のときは読み込み失敗を例外にする。
        書き込み前は必ず strict=True で読むこと。読めないのに「空」と誤認したまま
        保存すると、既存のログを丸ごと消してしまう。"""
        if self.store is not None:
            try:
                return self._normalize(self.store.read_df())
            except Exception as e:
                if strict:
                    raise BetLogReadError(
                        f'ベットログを読めませんでした（保存先: {self.location}）。'
                        f'既存の記録を失わないよう、書き込みを中止します。原因: {e}') from e
                return pd.DataFrame(columns=LOG_COLUMNS)
        if os.path.exists(self.path):
            try:
                return self._normalize(pd.read_csv(self.path, encoding='utf-8-sig'))
            except Exception as e:
                if strict:
                    raise BetLogReadError(
                        f'ベットログを読めませんでした（{self.path}）。'
                        f'既存の記録を失わないよう、書き込みを中止します。原因: {e}') from e
        return pd.DataFrame(columns=LOG_COLUMNS)

    def _save(self, df):
        df = self._normalize(df)
        if self.store is not None:
            self.store.write_df(df)
            return
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        # 直接上書きすると、途中で落ちたとき「途中までの正常なCSV」が残る。
        # それは load(strict=True) の例外ガードを素通りする（壊れていないので）ため、
        # 次の書き込みで欠損が確定する（実測: 5行→切断→2行として読めて→3行で保存）。
        # 一時ファイルに書いてから os.replace で差し替える（同一ディレクトリなら原子的）。
        tmp = self.path + '.tmp'
        df.to_csv(tmp, index=False, encoding='utf-8-sig')
        os.replace(tmp, self.path)

    def race_horses(self, race_id):
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

    def record(self, race_id, picks, unit=STAKE_UNIT, bet_type='3連単'):
        df = self.load(strict=True)
        ids = pd.to_numeric(df['bet_id'], errors='coerce') if len(df) else pd.Series(dtype=float)
        base = int(ids.max()) + 1 if len(ids) and ids.notna().any() else 1
        new = []
        for i, (combo, prob, od, units) in enumerate(picks):
            name = ' → '.join(combo) if isinstance(combo, (tuple, list)) else str(combo)
            try:
                od_f = float(od)
                if not math.isfinite(od_f):
                    od_f = 0.0
            except (TypeError, ValueError):
                od_f = 0.0          # 未投票馬など、オッズが決まっていない買い目
            new.append({'bet_id': base + i,
                        'time': datetime.now().isoformat(timespec='seconds'),
                        'race_id': race_id, 'bet_type': bet_type, 'combo': name,
                        'model_prob': round(float(prob), 5), 'odds': od_f,
                        'stake': int(units) * int(unit), 'status': 'pending',
                        'result': '', 'payout': 0, 'pnl': 0})
        df = pd.concat([df, pd.DataFrame(new)], ignore_index=True)
        self._save(df)
        return len(new)

    def settle(self, race_id, order3, final_odds=None):
        """レースを精算する。

        final_odds: {'3連単': 最終オッズ, '単勝': 最終オッズ} を渡すと、その値で払戻を
            計算し直して記録する（payout_kind='実績'）。
            渡さない場合は購入時オッズで概算する（payout_kind='概算'）。
            **パリミュチュエルなのでオッズは締切時まで変動する**。購入時オッズのままだと
            払戻・ROI が系統的にずれるので、「市場に勝てているか」を検証したいなら
            最終オッズを入れること。
        """
        df = self.load(strict=True)
        for col in ('payout', 'pnl', 'odds', 'stake'):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        actual = ' → '.join(order3)
        winner = order3[0]
        fo = {k: float(v) for k, v in (final_odds or {}).items()
              if v is not None and float(v) > 0}
        mask = (df['race_id'].astype(str) == str(race_id)) & (df['status'] == 'pending')
        cnt = 0
        for idx in df[mask].index:
            bt = str(df.at[idx, 'bet_type']) if 'bet_type' in df.columns else '3連単'
            won = (df.at[idx, 'combo'] == winner) if bt == '単勝' \
                else (df.at[idx, 'combo'] == actual)
            df.at[idx, 'status'] = 'won' if won else 'lost'
            df.at[idx, 'result'] = actual
            kind = ''
            if won:
                od = float(df.at[idx, 'odds'])
                if bt in fo:
                    od = fo[bt]
                    df.at[idx, 'odds'] = od      # 最終オッズで上書きして記録を正しくする
                    kind = '実績'
                else:
                    kind = '概算'
                pay = od * float(df.at[idx, 'stake'])
            else:
                pay = 0.0
            df.at[idx, 'payout_kind'] = kind
            df.at[idx, 'payout'] = pay
            df.at[idx, 'pnl'] = pay - float(df.at[idx, 'stake'])
            cnt += 1
        self._save(df)
        return cnt

    def undo_last(self):
        df = self.load(strict=True)
        if len(df) == 0:
            return None, 0
        last_rid = df.iloc[-1]['race_id']
        keep = df[df['race_id'].astype(str) != str(last_rid)]
        self._save(keep)
        return last_rid, len(df) - len(keep)

    def report(self, bet_type=None):
        df = self.load()
        if len(df) == 0:
            return {'empty': True, 'path': self.path}
        if bet_type:
            df = df[df['bet_type'] == bet_type]
        settled = df[df['status'].isin(['won', 'lost'])].copy()
        out = {'empty': False, 'path': self.path, 'n_total': len(df),
               'n_settled': len(settled),
               'n_pending': int((df['status'] == 'pending').sum()),
               'buckets': [], 'calib_hint': None, 'overall': None}
        if len(settled) == 0:
            return out
        stake = float(settled['stake'].sum())
        ret = float(settled['payout'].sum())
        pnl = ret - stake
        hits = int((settled['status'] == 'won').sum())
        # 的中のうち、最終オッズを入れずに購入時オッズで概算した件数。
        # これが残っていると払戻・ROI は「概算」であって実績ではない。
        won_rows = settled[settled['status'] == 'won']
        out['n_won'] = int(len(won_rows))
        out['n_payout_est'] = int((won_rows.get(
            'payout_kind', pd.Series([''] * len(won_rows)))
            .astype(str) != '実績').sum()) if len(won_rows) else 0
        out['overall'] = {
            'stake': stake, 'payout': ret, 'pnl': pnl,
            'roi': (pnl / stake * 100 if stake else 0), 'hits': hits,
            'n': len(settled), 'hit_rate': hits / len(settled) * 100,
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
                                     + (f'σを大きく（例 {rs*1.3:.3f}）にして再学習。' if rs else 'σを大きく。'))
            elif real > pred * 1.3:
                out['calib_hint'] = (f'過小: 予測{pred*100:.2f}% < 実測{real*100:.2f}% → '
                                     + (f'σを小さく（例 {rs*0.7:.3f}）にして再学習。' if rs else 'σを小さく。'))
            else:
                out['calib_hint'] = (f'予測≈実測（{pred*100:.2f}% vs {real*100:.2f}%）→ '
                                     + (f'現在の σ={rs:.4f} は妥当。' if rs else '現在のσは妥当。'))
        else:
            out['calib_hint'] = f'サンプル{len(settled)}件では不十分（最低20件目安）。'
        return out
