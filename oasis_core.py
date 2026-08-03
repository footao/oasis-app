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
CORE_VERSION = '2.8.0'

# =====================================================================
#  0. ゲーム仕様の定数
# =====================================================================
STAKE_UNIT          = 10_000   # 3連単 1口 = 10,000 rrc
MAX_UNITS           = 20       # 1組あたり上限口数
MAX_TOTAL_UNITS     = 20       # 1レース合計口数の上限（2026/04/17 で 10→20）
WIN_MAX_UNITS       = 100      # 単勝の1頭あたり上限口数
WIN_MAX_TOTAL_UNITS = 100      # 単勝は【1レース合計】100口まで（全頭の合算）
WIN_STAKE_UNIT      = 1_000    # 単勝は 1口 = 1,000 rrc（購入画面の表記）
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
VARIANCE_SHARE = 0.5

SPEC_FILE = 'passive_spec.json'      # 学習した数値を貯めるファイル（アプリと同じ場所）

DIST_LIST  = ['短距離', 'マイル', '中距離', '長距離']
TRACK_LIST = ['芝', 'ダート']
COND_LIST  = ['好調', '普通', '不調']

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
_PCT_RE = re.compile(r'(スピード|パワー|スタミナ|全ステータス)が(\d+(?:\.\d+)?)[%％](上昇|低下|アップ|ダウン)')


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

    # --- ばらつき低減系（安定感など）---
    if re.search(r'ばらつき|ブレ', d) and not _PCT_RE.search(d):
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
            out[name] = spec
    return out


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
    return {k: _norm_spec(v) for k, v in PASSIVE_SPEC_SEED.items()}


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
                v = _norm_spec(v)
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


def sigma_multiplier(passives, spec, variance_share=VARIANCE_SHARE):
    """安定感のような分散低減スキルによる σ の倍率。
    σ全体のうち variance_share だけが『ゲーム側のランダム性』とみなし、そこにだけ効かせる
    （残りはモデルの推定誤差なのでスキルでは減らない）。"""
    m = 1.0
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
        r_key = f"{date} {r_time}"

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
        r_key = f"{date} {r_time}"

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
            if s_m and st_m and p_m and sc_m:
                rows.append({
                    'race_key': r_key, 'race_no': race_no,
                    'rank': RANK_MAP.get(hm.group(1)) or int(hm.group(2)),
                    'name': hm.group(3).strip(), 'owner': _owner(hm.group(4)),
                    'speed': int(s_m.group(1)), 'stamina': int(st_m.group(1)),
                    'power': int(p_m.group(1)), 'score': float(sc_m.group(1)),
                    'passives': parse_passives(pa_m.group(1)) if pa_m else (),
                    'win_odds': float(od_m.group(1)) if od_m else np.nan,
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

    out = []
    for r in all_rows:
        passives = r['passives']
        condition = '不明'
        ent = all_entries.get(r['race_key'])
        if ent:
            target = horse_identity(r['name'], r['owner'], r['speed'], r['stamina'], r['power'])
            for h in ent['horses']:
                if horse_identity(h['name'], h['owner'], h['speed'],
                                  h['stamina'], h['power']) == target:
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
        # 同じログを重ねて置いた場合の二重カウントを防ぐ（新旧エクスポートの期間が重なる等）
        before = len(df)
        df = df.drop_duplicates(
            subset=['race_key', 'name', 'owner', 'speed', 'stamina', 'power', 'rank'],
            keep='first').reset_index(drop=True)
        df.attrs['n_duplicates'] = before - len(df)
        df['n_field'] = df.groupby('race_key')['score'].transform('size')
        df['_d'] = pd.to_datetime(df['date'], format='%Y/%m/%d', errors='coerce')
    return df


# =====================================================================
#  4. 特徴量
# =====================================================================
def feature_names(spec):
    """スペックが分かっているパッシブは実効ステータスに畳み込むので、ダミー列は作らない。
    スペック未知のパッシブだけ、ダミー＋距離交互作用で学習する。"""
    names = []
    for d in DIST_LIST:
        names += [f'{d}:切片', f'{d}:log(SP)', f'{d}:log(PW)', f'{d}:log(ST)']
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
    f = []
    for d in DIST_LIST:
        m = 1.0 if dist == d else 0.0
        f += [m, m * sp, m * pw, m * st]
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


def _order_loglik(base, actual_score, sigma, z):
    """base(予測相対スコア) + sigma*z のモンテカルロで、実際の上位3着順が出る確率の log。"""
    n = len(base)
    k = min(3, n)
    truth = tuple(np.argsort(-np.asarray(actual_score))[:k])
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


def _calibrate_sigma(oof, df, resid_std, n_draw=40_000, seed=7):
    """OOF予測に対して、実際の上位3着順の尤度が最大になる σ を選ぶ。"""
    races = _recent_races(df)
    if len(races) < 6:
        return max(resid_std * 0.6, 1e-4), []
    # 乱数を全レース分まとめて持つと 500MB 超になるので、レースごとに使い捨てる。
    # σの比較で同じ乱数を使いたいので、レースごとに固定シードで作り直す。
    rng_seeds = {k: seed * 1000003 + i for i, (k, g) in enumerate(races)}
    grid = np.unique(np.round(np.concatenate([
        np.linspace(0.15, 2.0, 20) * max(resid_std, 1e-4),
        np.array([0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2])]), 5))
    curve = []
    for s in grid:
        tot = 0.0
        for k, g in races:
            z = np.random.default_rng(rng_seeds[k]).standard_normal((n_draw, len(g)))
            tot += _order_loglik(oof[g.index.values], g['score'].values, s, z)
        curve.append((float(s), tot / len(races)))
    best = max(curve, key=lambda t: t[1])[0]
    return max(best, 1e-4), curve


SIGMA_SAFETY = 1.25   # 校正で得たσに掛ける安全係数（>1 = 弱気側＝過剰投資を防ぐ）


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
        sigma_mle, sigma_curve = _calibrate_sigma(oof, df, resid_std)
        race_sigma = sigma_mle * max(float(sigma_safety), 0.1)
        sigma_note = (f'（着順尤度の最適値 {sigma_mle:.4f} × 安全係数 {sigma_safety:g}。'
                      f'学習レースが少ないうちは弱気側に倒して過剰投資を防ぎます）')

    # --- 校正チェック: OOF予測の「予測確率 vs 実測」 ---
    calib = _calibration_check(oof, df, race_sigma)

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
    msgs.append(f'RACE_SIGMA={race_sigma:.4f} {sigma_note}')
    if calib:
        msgs.append(
            f'校正チェック(OOF {calib["n_races"]}レース)  '
            f'1着: 予測{calib["p_top1"]*100:.0f}% vs 実測{calib["a_top1"]*100:.0f}%   '
            f'3連単: 予測{calib["p_tri"]*100:.2f}% vs 実測{calib["a_tri"]*100:.2f}%')
        if calib['a_top1'] > calib['p_top1'] * 1.35:
            warns.append('△ モデルは実測よりやや弱気（σが大きめ）。実績ログが貯まったら σ を'
                         '少し下げると期待値が上がる可能性があります。')
        elif calib['p_top1'] > calib['a_top1'] * 1.35:
            warns.append('⚠ モデルが実測より強気（σが小さめ）。σを上げないと過剰投資になります。')
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


def _calibration_check(oof, df, sigma, n_draw=60_000, seed=11):
    """OOF予測 + σ で作った確率が、実測とどれくらい合っているかを見る。"""
    races = _recent_races(df)
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
        truth = np.argsort(-g['score'].values)[:3]
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
        r = _spearman(pred[g.index.values], g['score'].values)
        if r == r:
            vals.append(r)
    return float(np.mean(vals)) if vals else float('nan')


def _top1_accuracy(pred, df):
    hits = []
    for _, g in df.groupby('race_key'):
        if len(g) < 4:
            continue
        hits.append(int(np.argmax(pred[g.index.values]) == np.argmax(g['score'].values)))
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
    ref = (100.0, 100.0, 100.0)
    out = []
    for p in PASSIVE_NAMES:
        sp_ = spec.get(p) or {}
        src = sp_.get('source')
        if sp_.get('mult'):
            e = effective_stats(*ref, (p,), d, t, spec, {'same_species': same_species})
            eff = (b_sp * math.log(e['speed'] / ref[0])
                   + b_pw * math.log(e['power'] / ref[1])
                   + b_st * math.log(e['stamina'] / ref[2]))
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
    return np.array([sigma * sigma_multiplier(h.get('passives', ()), spec)
                     for h in horses], dtype=float)


def simulate_trifecta(base, sigma, n_sim=N_SIM, seed=SIM_SEED, chunk=SIM_CHUNK):
    """メモリを抑えたチャンク実行。 -> (win_prob[n], {(i,j,k): prob})"""
    n = len(base)
    sig = np.broadcast_to(np.asarray(sigma, dtype=float), (n,))
    rng = np.random.default_rng(seed)
    win = np.zeros(n)
    counts = {}
    done = 0
    while done < n_sim:
        m = min(chunk, n_sim - done)
        sim = base[None, :] + rng.standard_normal((m, n)) * sig[None, :]
        order = np.argsort(-sim, axis=1)[:, :3]
        np.add.at(win, order[:, 0], 1)
        if n >= 3:
            key = (order[:, 0] * n * n + order[:, 1] * n + order[:, 2])
            u, c = np.unique(key, return_counts=True)
            for kk, cc in zip(u.tolist(), c.tolist()):
                counts[kk] = counts.get(kk, 0) + cc
        done += m
    win /= n_sim
    combo = {(k // (n * n), (k // n) % n, k % n): c / n_sim for k, c in counts.items()}
    return win, combo


# 後方互換
def simulate_rankings(base, sigma, n_sim=N_SIM, seed=SIM_SEED):
    rng = np.random.default_rng(seed)
    sim = base + rng.normal(0, sigma, (n_sim, len(base)))
    return np.argsort(-sim, axis=1)


def market_win_prob(odds):
    odds = np.asarray(odds, dtype=float)
    raw = np.where((odds > ODDS_FLOOR) & np.isfinite(odds), 1.0 / odds, 0.0)
    if raw.sum() <= 0:
        return None
    return raw / raw.sum()


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


def bare(name):
    return re.sub(re.escape(DUP_MARK) + r'\d+$', '', str(name))


_SPECIES_RE = re.compile(r'\s*#\s*\d+\s*$')


def species_name(name):
    """おあしすっちの種類名。ゲームが同名馬に付ける '#2' と、こちらの重複マーカーを外す。"""
    return _SPECIES_RE.sub('', bare(name)).strip()


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


def parse_unified(text):
    """統合フォーマット（ブックマークレット出力）を解析。
    -> (horses, trifecta_odds, dist, track, ground, guild, schedule_id, pool, n_tri_total)
    """
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
    for key in ('win_pool', 'win_pool_before', 'win_pool_delta', 'win_pool_n',
                'win_pool_spread', 'win_pool_err', 'win_own', 'win_pool_min'):
        m = re.search(rf'^{key}=([0-9.]+)', text, re.M)
        if m:
            meta[key] = float(m.group(1))
    m = re.search(r'^win_market=(\w+)', text, re.M)
    if m:
        meta['win_market'] = m.group(1)

    mh = re.search(r'===\s*出走馬一覧\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)
    mo = re.search(r'===\s*3連単オッズ\s*===\s*\n(.*?)(?=\n\s*===|\Z)', text, re.S)

    skipped = []
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
            horses.append({
                'my_amount': (float(mine) if pd.notna(mine) else None),
                'name': str(r.get('馬名', r.get('名前', ''))).strip(),
                'species': spc or None,
                'speed': int(sp), 'power': int(pw), 'stamina': int(st),
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


def _fetch_pool_api(guild, schedule_id, timeout=5):
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
    if p <= 0 or p >= 1 or od <= 1:
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

    def ev_c(p, od, k):
        if k <= 0:
            return 0.0
        Pc = P_total / od
        eff = (P_total + k * stake_unit) / (Pc + k * stake_unit)
        return (p * eff - 1) * stake_unit * k

    items = []
    for (c, p, od) in cands:
        if not od or od <= 1 or not (0 < p < 1):
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
            Pc = P_total / od
            eff = (P_total + k * stake_unit) / (Pc + k * stake_unit)
            res[c] = (k, (p * eff - 1) * stake_unit * k, eff)
    return res


def unformed_sleeve_picks(combo_prob, disp, od_of, P_total, p_min=0.05, edge_min=0.30,
                          max_units=5, remaining_budget=MAX_TOTAL_UNITS,
                          stake_unit=STAKE_UNIT):
    if P_total <= 0 or max_units <= 0 or remaining_budget <= 0:
        return []
    eff = (P_total + stake_unit) / stake_unit
    cand = []
    for idx, p in combo_prob.items():
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
    pos = sorted(x for x in singles if x > 0)
    spread = float((pos[-1] - pos[0]) / pool) if len(pos) > 1 else 0.0

    if rel_err > 0.05:
        msgs.append(f'△ 推定精度は ±{rel_err*200:.0f}%（95%目安）。オッズが小数2桁までしか'
                    '出ないため、プールが大きいと1口の影響が小さく精度が出ません。'
                    'もう一度試し買いすると累積で精度が上がります。')
    if spread > 0.5 and spread > rel_err * 6:
        msgs.append('⚠ 馬ごとの推定のばらつきが、丸め誤差だけでは説明できないほど大きいです。'
                    '試し買いの前後で他の人も投票した可能性があります。')
    if len(pos) < 3:
        msgs.append('△ 推定に使えた馬が少ないため精度は粗いです。')
    msgs.append(f'自分の投入 {total_delta:,.0f} rrc の前後で、{len(pos)}頭のオッズ変化から'
                f'推定しました（推定精度 ±{rel_err*200:.0f}%）。')
    # pool は試し買い"前"の総額。②のオッズは"後"なので、そちらに合わせた値も返す。
    return {'ok': True, 'pool': float(pool + total_delta), 'pool_before': float(pool),
            'per_horse': detail, 'n_used': len(pos), 'rel_err': rel_err,
            'spread': spread, 'delta': total_delta, 'messages': msgs}


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
)


def analyze(raw_text, bundle, settings=None):
    s = dict(DEFAULT_SETTINGS)
    if settings:
        s.update({k: v for k, v in settings.items() if v is not None or k in DEFAULT_SETTINGS})
    if not bundle or not bundle.get('ok'):
        return {'ok': False, 'error': 'モデル未学習。先にログを読み込んで学習してください。'}

    res = {'ok': True, 'error': None, 'messages': [], 'pool_msgs': []}
    P_total = 0
    csv_odds = None
    n_tri_total = 0

    if '出走馬一覧' in raw_text:
        (horses, csv_odds, a_dist, a_track, a_ground,
         guild, schedule_id, clip_pool, n_tri_total) = parse_unified(raw_text)
        res['auto_race_info'] = bool(a_dist and a_track)
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
    base = predict_base(bundle, horses, s['dist'], s['track'])
    sigma = bundle['race_sigma']
    n_sim = int(s.get('n_sim') or N_SIM)
    sig_vec = horse_sigmas(bundle, horses, sigma)
    win_p, combo_prob = simulate_trifecta(base, sig_vec, n_sim=n_sim)
    n_steady = int((sig_vec < sigma * 0.999).sum())
    if n_steady:
        res['messages'].append(
            f'分散低減スキル（安定感など）を {n_steady}頭に反映しました'
            f'（σ×{float(sig_vec.min()/sigma):.2f}）。')
    disp = disambiguate([h['name'] for h in horses])

    odds = np.array([h.get('odds', np.nan) for h in horses], dtype=float)
    mkt_p = market_win_prob(odds)

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
            if (not np.isfinite(od)) or od <= ODDS_FLOOR:
                row['tag'] = '（未投票）'
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
        res['messages'].append(
            f'🔬 単勝プールの実測値を取り込みました: {res["win_pool"]:,.0f} rrc'
            + (f'（試し買い {meta["win_pool_delta"]:,.0f} rrc / '
               f'{int(meta.get("win_pool_n", 0))}頭から' if meta.get('win_pool_delta') else '')
            + (f' / 精度 ±{err*200:.0f}%）' if err else '）'))
        if err and err > 0.05:
            res['messages'].append(
                f'⚠ 単勝プールの推定精度が ±{err*200:.0f}% と粗いです。'
                'もう一度実測すると精度が上がります。口数は控えめに。')
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
    if s.get('win_bets') and mkt_p is not None and res['win_pool'] and others_ok:
        my_units = [int((h.get('my_amount') or 0) // win_unit) for h in horses]
        unbet = [bool(np.isfinite(odds[i]) and abs(odds[i] - UNBET_ODDS) < 1e-9
                      and not (horses[i].get('my_amount') or 0)) for i in range(n)]
        if any(unbet):
            res['messages'].append(
                f'ℹ 未投票（オッズ {UNBET_ODDS} のまま）の馬が {sum(unbet)}頭 あります。'
                'この馬たちは「その馬への投入額0」として実効オッズを計算します'
                '（当たれば非常に高配当ですが、高分散です）。')
        names_o = [disp[i] for i in range(n)]
        picks, summ = win_bet_picks_pool(
            names_o, win_p, odds, res['win_pool'], s['bankroll'], s['kelly_fraction'],
            s.get('win_edge_min', 0.15), stake_unit=win_unit,
            risk_cap_frac=s['max_risk_frac'], my_units=my_units, unbet=unbet)
        res['win_picks'] = picks
        res['win_summary'] = summ
        res['win_pool_mode'] = '実測プール（希薄化込み）'
    elif s.get('win_bets') and mkt_p is not None:
        res['win_picks'] = win_bet_picks(
            disp, win_p, odds, s['bankroll'], s['kelly_fraction'],
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
    res['mc_note'] = (f'モンテカルロ {n_sim:,} 試行 / σ={sigma:.4f}。'
                      f'確率 0.01% 付近の組は±10%程度のブレがあります。')

    odds_exact, odds_bare = {}, {}
    if csv_odds:
        for (a, b, c), od in csv_odds.items():
            odds_exact[(a, b, c)] = od
            odds_bare[(bare(a), bare(b), bare(c))] = od

    def od_of(nm):
        if nm in odds_exact:
            return odds_exact[nm]
        return odds_bare.get(tuple(bare(x) for x in nm))

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
        res['mode'] = ('完全名一致' if how_counts['exact'] and not how_counts['bare']
                       else '素名フォールバック' if how_counts['bare'] and not how_counts['exact']
                       else '混在')
        res['bare_used'] = bool(how_counts['bare'])
        res['unmatched_names'] = sorted(unmatched)

        if P_total > 0:
            expected = n * (n - 1) * (n - 2) if n >= 3 else 0
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
            [(c, p, od) for c, p, od, ev in rows], P_total,
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
                remaining_budget=min(MAX_TOTAL_UNITS, risk_units_cap) - total_units)
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
            row.update(eff1_od=eff, ev1=(p * eff - 1) * STAKE_UNIT, plus_ev=(p * eff > 1))
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
        c_sp = b_sp * sp
        c_pw = b_pw * pw
        c_st = b_st * st
        c_spec = (b_sp * (math.log(e['speed']) - sp)
                  + b_pw * (math.log(e['power']) - pw)
                  + b_st * (math.log(e['stamina']) - st))
        cond = h.get('condition', '普通')
        c_cond = coef.get('好調', 0.0) if cond == '好調' else (
            coef.get('不調', 0.0) if cond == '不調' else 0.0)
        c_pass, detail = c_spec, []
        for p in h.get('passives', ()):
            sp_ = spec.get(p)
            if sp_ and sp_.get('mult'):
                e1 = effective_stats(h['speed'], h['power'], h['stamina'], (p,),
                                     dist, track, spec, ctx)
                v = (b_sp * (math.log(e1['speed']) - sp)
                     + b_pw * (math.log(e1['power']) - pw)
                     + b_st * (math.log(e1['stamina']) - st))
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
LOG_COLUMNS = ['bet_id', 'time', 'race_id', 'bet_type', 'combo', 'model_prob',
               'odds', 'stake', 'status', 'result', 'payout', 'pnl']


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
        for c in ['race_id', 'combo', 'status', 'result', 'time', 'bet_type']:
            if c in df.columns:
                df[c] = df[c].fillna('').astype(str)
        for c in ['bet_id', 'model_prob', 'odds', 'stake', 'payout', 'pnl']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        for c in LOG_COLUMNS:
            if c not in df.columns:
                df[c] = '' if c in ('result', 'combo', 'status') else 0
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
        df.to_csv(self.path, index=False, encoding='utf-8-sig')

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

    def settle(self, race_id, order3):
        df = self.load(strict=True)
        for col in ('payout', 'pnl', 'odds', 'stake'):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        actual = ' → '.join(order3)
        winner = order3[0]
        mask = (df['race_id'].astype(str) == str(race_id)) & (df['status'] == 'pending')
        cnt = 0
        for idx in df[mask].index:
            bt = str(df.at[idx, 'bet_type']) if 'bet_type' in df.columns else '3連単'
            won = (df.at[idx, 'combo'] == winner) if bt == '単勝' \
                else (df.at[idx, 'combo'] == actual)
            df.at[idx, 'status'] = 'won' if won else 'lost'
            df.at[idx, 'result'] = actual
            pay = float(df.at[idx, 'odds']) * float(df.at[idx, 'stake']) if won else 0.0
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
