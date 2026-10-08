"""レースを区間ごとに再現するシミュレータ（影の運用・2026/10/07）。

ENGINE_NOTES.md で逆算したゲームの仕組みをそのまま組み立てる:
  区間速度  v = rating^0.8709 × fatigue^0.9136 × (1 + 乱数1.19%)
  rating    = 距離×フェーズごとの線形式（実効 SP/PW/ST、学習データの timeline から当てる）
  スタミナ  初期 floor(実効ST)、区間ごとに一定量ずつ減る。消費量は**レースごとに乱数でぶれる**
            （±6%。ENGINE_NOTES 追記3）。これを1点で決め打ちせず、何千通り振る
  失速      見込みの余り（残り − この先の消費）÷ 初期スタミナ から fatigue を引く
            （余りが多いと最大 +3%、足りないと 0.65 まで）

区間で発動する効果の扱い:
  phase     序盤/中盤/終盤の区間にだけ掛ける（ロケットスタート・中盤加速・末脚・時界超越など）
  tail300 / lowst / lead（残り300m・残りスタミナ以下・先頭の間）とそれ以外の状況限定は、
            カタログの平均 duty で均す。シミュ上で発動した区間にだけ掛ける版も試したが、
            前向き検証189レースで均したほうが良かった（単勝LL 0.523 vs 0.547）ので消した。
前向き検証（9/2〜10/7・194レース、Ridge＝従来モデル）:
  単勝LL 0.563→0.472 / 3連単LL 1.578→1.442 / 本命1着 84.0%→85.6%
  10/07 修正1: 疲労補正を「見込みの余り」で引く（余り +0〜3% を拾う）・1頭ごとのぶれ HORSE_SD。
    修正前は自信過剰で、R2628 で lv に 95%（3連単 lv→はなこ 91%）を付けて 30万負けた。
  10/07 修正2（バグ探索で見つけたもの）:
    ・「中盤/終盤で下位半分なら」系（追い込み・差しの構え・終星の鎮魂歌・亡者の追走）を、その区間にだけ掛ける
    ・残り300m（幻界終走・終焉加速）を終盤にだけ掛ける（全区間に均して序盤・中盤を7%盛っていた）
    ・スタミナ消費の増減（省エネ走法・省エネの加護・ロングスパート・ペース配分・逃げの心得）を
      スタミナの倍率ではなく消費の倍率で扱う（初期スタミナを3前後、rating を2〜4%盛っていた）
    ・勝負師（5%で全ステ×1.25）を平均に均さず、シミュの中で抽選する
    ・翌日以降に採取したレース（ステータスが今の値に化けている）を学習から外し、rating の式は外れ値を除いて当て直す
  10/07 修正3: 競り合い（20m以内にライバル）を平均 duty で均さず、シミュの中で位置から判定する
    （R2628: 抜け出した lv の終盤を3%盛って lv 63% と出していた → はなこ 75%）。
    前向き194レース: 単勝LL 0.472→0.453 / 3連単LL 1.442→1.345（−0.097±0.035）/ 本命1着 85.6%→89.7%
  10/07 修正4: 王殺し（先頭から20m）・先頭の間（首位の呪い・先導祈願）・独走態勢も位置で判定する仕組みを入れたが、
    前向き検証で有意に良くならなかった（王殺し 3連単LL −0.025±0.023、先頭 +0.030±0.022）ので POS_DYNAMIC は競り合いだけ。
    ついでに 先導祈願「先頭の間 消費 −4%」を全区間に掛けていたのを、先頭にいる割合ぶんだけにした。
2026/10/07 から bot の予想はこれ（JS: model.js の simRace。係数は model.json の sim）。
従来の Ridge の予想は比較用に reports の rw/rc に残す。
"""
import math
import re
from statistics import NormalDist

import numpy as np
import oasis_core as oc

PH_JA = ('序盤', '中盤', '終盤')
PH_TL = ('early', 'middle', 'late')
NSEG = {'短距離': (3, 4, 3), 'マイル': (4, 6, 5), '中距離': (6, 8, 6), '長距離': (7, 10, 8)}
V_EXP, F_EXP = 0.8709, 0.9136
SEG_NOISE = 0.0119
# 1頭ごとの調子のぶれはレース中の速さに掛ける（ゴールタイムにだけ掛けると、レース中は全馬が実際より
# 固まって走り、競り合い・先頭の判定がずれる）。前向き197R（採取ミス除く・先頭判定込み）:
#   ゴールに 0.006: 単勝LL 0.266 / 3連単LL 1.005 → レース中に 0.004: 0.243 / 0.948（0.006 で 0.256/0.981、0.008 で悪化）
#   較正: 予測95%以上 101R 予測99.0% 実際99.0%
HORSE_IN_RACE = True
HORSE_SD = 0.004       # 1頭ごとのレースのぶれ（前向き検証194レースで 0.004〜0.008 を比べて最良）
LOWST_LABELS = {'血走り', '骨砕き', '紅蓮点火', '深淵反転', '禍福転倒', '星海反転'}
LEAD_LABELS = {'首位の呪い', '王冠過給', '先導祈願', '原海戴冠'}
LEAD_DUTY = 0.079                 # 先頭の間（平均 duty で全区間に均す）
# 「残りスタミナ30%以下」で発動する効果は平均 duty で全区間に均す。終盤だけに寄せる版
# （距離別の実測 duty を終盤に掛ける）も試したが、前向き検証で単勝LL +0.025・3連単LL +0.026 と悪化した
# （スタミナに余裕のある持ち馬まで終盤に盛ってしまうため）。
LOWST_DUTY = 0.183
# 「スタミナ消費量」を変えるパッシブ。spec ではスタミナ倍率に置き換えてあるが、ゲームでは
# 消費の倍率（clamp の後に掛かる・ENGINE_NOTES 追記1）。初期スタミナと rating を盛らないよう消費で扱う。
COST_PASSIVES = {'省エネ走法': (None, 0.92), 'ロングスパート': ('終盤', 1.08),
                 'ペース配分': ('序盤', 0.85), '逃げの心得': ('序盤', 1.12)}
# 「20m以内にライバルがいる間」の効果。平均 duty で均すと、抜け出した馬（R2628 の lv 終盤）を盛りすぎる。
# timeline では区間の始めに最も近い馬が 20m 以内なら 92〜98%、20m 超なら 3〜34% の発動
# （3万区間）なので、シミュの中で位置から判定する。
# 同じく位置で決まる効果（timeline で発動条件を確認済み）:
#   king  先頭から20m以内の2位以下（王殺し・星界破砕）   20m以内 89〜96% / 25m超 0%
#   lead  先頭の間（首位の呪い・王冠過給・先導祈願）       先頭 100% / 2位以下 0〜7%
#   solo  先頭で2位と50m以上（独走態勢）
POS_KINDS = ('duel', 'king', 'lead', 'solo')
# 先頭の判定: 同じ位置（スタート直後は全頭 0m）なら pet_id の小さい馬が前（start_rank が434レース全部で
# pet_id 順・首位の呪いの1区間目の発動 98/98 一致）。これを入れると「先頭の間」は効く:
#   前向き197レース（採取ミス除く・対 競り合いだけ）: 単勝LL 0.354→0.266（−0.088±0.058）/ 3連単LL −0.005±0.010
#   23時 32R: 単勝LL 0.790→0.240・本命1着 81%→91%（スタートで先頭を取った首位の呪いの逃げ切りを読める）
# 王殺しは 単勝LL +0.007±0.004 / 3連単LL −0.027±0.025 とどっちつかずなので均したまま
# （R2628: 王殺しのにのが lv に付いていき、lv の競り合いが終盤まで続いて lv 60% になる）。
# 調子のぶれをレース中に入れた後でも 単勝LL +0.005±0.007 / 3連単LL −0.045±0.039・本命1着 92%→91% で、まだ決め手なし。
POS_DYNAMIC = {'duel', 'lead', 'solo'}
POS_LABELS = {'競り合い', '天嵐決闘', '王殺し', '星界破砕', '首位の呪い', '王冠過給', '先導祈願', '神樹轟臂', '原海戴冠'}   # 一致検証の見本選び用
# 均すときの duty（消費量 c0 の計算は従来どおりこれで均す）
POS_DUTY = {'duel': 0.655, 'king': 0.175, 'lead': LEAD_DUTY, 'solo': 0.05}
DUEL_M, SOLO_M = 20.0, 50.0
SEG_M = 100.0                     # 1区間 100m（全距離）
LEAD_COST_UP = 1.047              # 首位の呪いの「スタミナ消費も増加」（発動中の区間消費の実測）


def _pos_kind(text):
    if '先頭から20m以内' in text:
        return 'king'
    if '20m以内' in text:
        return 'duel'
    if '2位と50m以上' in text:
        return 'solo'
    if '先頭の間' in text:
        return 'lead'
    return None
GAMBLE = '勝負師'                 # 5%の確率で全ステ×1.25。平均に均さず、シミュの中で抽選する
_CONS_RE = re.compile(r'スタミナ消費量[^。]*?(\d+(?:\.\d+)?)[%％](増加|減少)')


# 2026/10/08〜 oasis 級の装備は「固有スキル＋追加効果」の2つを持つ。説明文を節に分けて1つずつ扱う
# （API の書式が未確認なので、＋追加効果：/ 改行 / 。のどれで区切られていても拾う）。
_CLAUSE_RE = re.compile(r'[＋+]?\s*追加効果\s*[：:]|\n|。')
_NOISE_RE = re.compile(r'乱数幅を(\d+(?:\.\d+)?)[%％]狭める')                        # 区間の乱数（1.19%）に掛ける
_REC_PH_RE = re.compile(r'(序盤|中盤|終盤)突入時にスタミナを(\d+(?:\.\d+)?)[%％]回復')
_REC_LOW_RE = re.compile(r'残りスタミナ(\d+(?:\.\d+)?)[%％]以下で一度だけ(\d+(?:\.\d+)?)[%％]回復')


# 見本（selftest と JS の一致検証用）。数値は告知の範囲の中から1つ選んだもの。
OASIS_SAMPLES = [
    ('equipment', '楽園天翔', '残り300mでスピードが26%上昇＋追加効果：スタミナ消費量が常時4%減少'),
    ('equipment', '神樹轟臂', '20m以内にライバルがいる間、パワーが25%上昇＋追加効果：パワーが常時5%上昇'),
    ('equipment', '原海戴冠', '先頭の間、スピードが23%上昇（スタミナ消費も増加）スタミナ消費量が追加で6%増加（上限6%）'
                             '＋追加効果：スタミナが常時5%上昇'),
    ('equipment', '永劫機律', '中盤のパワーが24%上昇＋追加効果：レース中の乱数幅を20%狭める'),
    ('equipment', '創世天駆', '50m以内にライバルがいない間、スピードが24%上昇＋追加効果：序盤のスピードが4%上昇'),
    ('charm', '楽園核共鳴', '全ステータスが常時9.5%上昇＋追加効果：スタミナ消費量が常時3%減少'),
    ('charm', '聖輪転生', '下位半分の間、スピードとパワーがそれぞれ16%上昇＋追加効果：中盤突入時にスタミナを3%回復'),
    ('charm', '永劫時律', '中盤のスピードが24%上昇＋追加効果：レース中の乱数幅を20%狭める'),
    ('charm', '世界樹循環', 'スタミナが常時19%上昇＋追加効果：スタミナ消費量が常時4%減少'),
    ('charm', '星海反転', '残りスタミナ30%以下でスピードとパワーがそれぞれ18%上昇＋追加効果：残りスタミナ20%以下で一度だけ4%回復'),
]


def _text_scope(t):
    """カタログに無い効果（追加効果など）の範囲を文面から決める。→ (scope, arg, duty) か None。"""
    if '常時' in t:
        return 'always', None, 1.0
    if '残り300m' in t:
        return 'tail300', None, 1.0
    m = re.search(r'(序盤|中盤|終盤)の', t)
    if m:
        return 'phase', m.group(1), 1.0
    if re.search(r'残りスタミナ\d', t):
        return 'lowst', None, LOWST_DUTY
    if '50m以内にライバルがいない' in t:
        return 'conditional', None, 0.101       # 孤影の疾走と同じ
    if '下位半分' in t:
        return 'conditional', None, 0.527       # 逆境祈願と同じ
    return None


def _phase_idx(arg):
    return PH_JA.index(arg) if arg in PH_JA else None


def horse_profile(h, dist, track, spec, same_species, scope_tbl=None):
    """API の馬1頭 → dict(base=常時の実効ステ, fx=[(フェーズ, {stat: 倍率})], cost=[フェーズ別の消費倍率],
    gamble=(確率, 倍率) か None)。JS: model.js の simProfile と同じ計算。"""
    n = sum(NSEG[dist])
    base = {'speed': float(h['speed']), 'power': float(h['power']), 'stamina': float(h['stamina'])}
    fx, cost, gamble = [], [1.0, 1.0, 1.0], None
    nz, rec = 1.0, []                 # 区間の乱数の倍率 / スタミナ回復 [(フェーズ or None, 残り割合の閾値, 回復率)]
    pos = {k: [{}, 1.0, set()] for k in POS_KINDS}      # 種類 → [ステ倍率, 消費倍率, 発動キー]

    def add_pos(kind, m, cm, key):
        for k, x in m.items():
            pos[kind][0][k] = pos[kind][0].get(k, 1.0) * float(x)
        pos[kind][1] *= cm
        pos[kind][2].add(key)

    def pos_cost(m, desc):
        """位置効果の「スタミナ消費量 N% 増減」→ 消費倍率（スタミナ倍率からは外す）。"""
        cm = 1.0
        mm = _CONS_RE.search(desc)
        if mm:
            x = float(mm.group(1)) / 100.0
            cm = (1.0 - x) if mm.group(2) == '減少' else (1.0 + x)
            m.pop('stamina', None)
        if 'スタミナ消費も増加' in desc:
            cm *= LEAD_COST_UP
        return cm

    def avg(m, duty):
        for k, x in m.items():
            if k in base:
                base[k] *= 1.0 + (float(x) - 1.0) * min(max(duty, 0.0), 1.0)

    def phase(m, arg, duty):
        pi = _phase_idx(arg)
        if pi is None:
            avg(m, duty)
            return
        frac = NSEG[dist][pi] / n
        fx.append((pi, {k: 1.0 + (float(x) - 1.0) * min(1.0, duty / frac) for k, x in m.items()}))

    def put(m, sc, arg, duty, label=''):
        if label in LOWST_LABELS or sc == 'lowst':
            avg(m, LOWST_DUTY)
        elif label in LEAD_LABELS or sc == 'lead':
            avg({k: x for k, x in m.items() if k != 'stamina'}, LEAD_DUTY)
        elif sc == 'tail300':
            phase(m, '終盤', 3.0 / n)
        elif sc == 'phase' or (sc == 'conditional' and _phase_idx(arg) is not None):
            phase(m, arg, duty)             # 「中盤で下位半分なら」なども、その区間にだけ
        elif sc == 'aptitude':
            if arg in (dist, track):
                avg(m, 1.0)
        elif sc in ('learned', 'same_species', 'variance'):
            return
        else:
            avg(m, duty if sc != 'always' else 1.0)

    passives = [oc.PASSIVE_CODE_MAP[c] for c in (h.get('passive_skill'), h.get('passive_skill_2'))
                if c and c != 'none' and c in oc.PASSIVE_CODE_MAP]
    for p in passives:
        sp = spec.get(p) or {}
        m = dict(sp.get('mult') or {})
        if not m:
            continue
        sc = sp.get('scope')
        if sc == 'aptitude' and sp.get('scope_arg') not in (dist, track):
            continue
        if sc == 'same_species' and not same_species:
            continue
        if p == GAMBLE:
            gamble = (float(sp.get('duty', 0.05)), m)
            continue
        kind = _pos_kind(sp.get('desc') or '')
        if kind:
            pcm = pos_cost(m, sp.get('desc') or '')
            if kind in POS_DYNAMIC:
                add_pos(kind, m, pcm, h.get('passive_skill') if
                        oc.PASSIVE_CODE_MAP.get(h.get('passive_skill')) == p else h.get('passive_skill_2'))
                continue
            for pi in range(3):        # 均す場合も、消費の増減は発動している割合ぶんだけ
                cost[pi] *= 1.0 + (pcm - 1.0) * POS_DUTY[kind]
            if not m:
                continue
        if p in COST_PASSIVES:
            arg, cm = COST_PASSIVES[p]
            for pi in ([_phase_idx(arg)] if arg else range(3)):
                cost[pi] *= cm
            m.pop('stamina', None)
            if not m:
                continue
        if sc in ('always', 'aptitude', 'same_species'):
            avg(m, 1.0)
        else:
            put(m, sc, sp.get('scope_arg'), float(sp.get('duty', 1.0)), p)

    def item_clause(label, desc, key):
        sp = oc.spec_from_description(f'{label}：{desc}' if label else desc) or {}
        if sp.get('scope') == 'variance':
            return
        m = dict(sp.get('mult') or {})
        kind = _pos_kind(desc)
        cm = None if kind else _CONS_RE.search(desc)
        if kind:
            pcm = pos_cost(m, desc)
            if kind in POS_DYNAMIC:
                add_pos(kind, m, pcm, key)
                return
            for pi in range(3):        # 先導祈願の「先頭の間 消費 −4%」を全区間に掛けていた（〜10/07）
                cost[pi] *= 1.0 + (pcm - 1.0) * POS_DUTY[kind]
        if cm:                        # 「スタミナ消費量が常時N%減少」は消費の倍率（スタミナを盛らない）
            x = float(cm.group(1)) / 100.0
            for pi in range(3):
                cost[pi] *= (1.0 - x) if cm.group(2) == '減少' else (1.0 + x)
            m.pop('stamina', None)
        if not m:
            return
        cat = oc.ITEM_EFFECT_CATALOG.get(label) or {} if label else {}
        alias = cat.get('alias')
        if not alias and not cat and key:
            code = re.sub(r'^(?:gear|charm|item)_', '', key)
            alias = oc.PASSIVE_CODE_MAP.get(code) or oc.ITEM_KEY_ALIAS.get(code)
        if alias:
            a = spec.get(alias) or {}
            sc, arg, duty = a.get('scope', 'conditional'), a.get('scope_arg'), float(a.get('duty', 1.0))
        elif cat:
            sc, arg = cat.get('scope', 'always'), cat.get('scope_arg')
            duty = float(cat['duty']) if cat.get('duty') is not None else 1.0
        else:
            ts = _text_scope(desc)
            if not ts:
                return
            sc, arg, duty = ts
        put(m, sc, arg, duty, label)

    for it in (h.get('equipment'), h.get('charm')):
        if not isinstance(it, dict):
            continue
        label = (it.get('effect_label') or '').strip().lstrip('★☆')
        texts = [t.strip() for t in _CLAUSE_RE.split(str(it.get('effect_description') or '')) if t.strip()]
        for j, t in enumerate(texts):
            mm = _NOISE_RE.search(t)
            if mm:
                nz *= max(0.0, 1.0 - float(mm.group(1)) / 100.0)
                continue
            mm = _REC_PH_RE.search(t)
            if mm:
                rec.append((_phase_idx(mm.group(1)), 1.0, float(mm.group(2)) / 100.0))
                continue
            mm = _REC_LOW_RE.search(t)
            if mm:
                rec.append((None, float(mm.group(1)) / 100.0, float(mm.group(2)) / 100.0))
                continue
            # 2つ目以降の節（追加効果）は固有スキル名・effect_key のカタログを使わず、文面で決める
            item_clause(label if j == 0 else '', t, str(it.get('effect_key') or '') if j == 0 else '')
    for k in base:
        base[k] = max(base[k], 1.0)
    return dict(base=base, fx=fx, cost=cost, gamble=gamble, pos=pos, nz=nz, rec=rec)


class RaceSim:
    """fit(学習レース) → predict(レース) で勝率と3連単の確率を返す。"""

    def __init__(self, spec=None, n_sim=4000, seed=1):
        self.spec = spec or oc.default_spec()
        self.scope_tbl = oc.item_scope_table(self.spec)
        self.n_sim, self.rng = n_sim, np.random.default_rng(seed)
        self.horse_sd = HORSE_SD

    # ---- 1レースを「馬ごとの配列」にする（JS: simInputs と同じ計算）----
    def _race(self, r):
        dist, track = r.get('distance'), r.get('surface')
        if dist not in NSEG:
            return None
        n = sum(NSEG[dist])
        hs = [h for h in (r.get('horses') or r.get('pets') or []) if h.get('speed') is not None]
        same = oc.same_species_flags([h.get('name', '') for h in hs], [h.get('adult_key') for h in hs])
        rows = []
        for h, sm in zip(hs, same):
            pf = horse_profile(h, dist, track, self.spec, sm)
            base, fx = pf['base'], pf['fx']
            # 消費量は「区間効果を区間の割合で均した」実効ステから（stamina_budget と同じ式）
            e = dict(base)
            for pi, m in fx:
                for k, x in m.items():
                    e[k] *= 1.0 + (x - 1.0) * NSEG[dist][pi] / n
            for kind, (pm, _, _) in pf['pos'].items():     # 消費は従来どおり平均で
                for k, x in pm.items():
                    if k != 'stamina':
                        e[k] *= 1.0 + (x - 1.0) * POS_DUTY[kind]
            need, _, _ = oc.stamina_budget(e, dist)
            st = np.array([[base['speed'], base['power'], base['stamina']]] * 3)
            for pi, m in fx:
                for j, k in enumerate(('speed', 'power', 'stamina')):
                    if k in m:
                        st[pi, j] *= m[k]
            g = pf['gamble']
            rows.append(dict(h=h, pid=int(h.get('pet_id') or 0), st=st, c0=need / n, cost=list(pf['cost']), s0=math.floor(e['stamina']),
                             g_p=g[0] if g else 0.0,
                             g_m=[float(g[1].get(k, 1.0)) for k in ('speed', 'power', 'stamina')] if g else [1.0] * 3,
                             p_m=[[float(pf['pos'][kd][0].get(k, 1.0)) for k in ('speed', 'power', 'stamina')]
                                  for kd in POS_KINDS],
                             p_c=[float(pf['pos'][kd][1]) for kd in POS_KINDS],
                             p_key=[pf['pos'][kd][2] for kd in POS_KINDS],
                             nz=pf['nz'], rec_ph=[sum(x for q, _, x in pf['rec'] if q == pi) for pi in range(3)],
                             rec_low=next(([t, x] for q, t, x in pf['rec'] if q is None), [0.0, 0.0])))
        return dict(dist=dist, track=track, rows=rows)

    def inputs(self, r):
        """一致検証用: 馬ごとの [フェーズ×ステ]・1区間の消費・フェーズ別の消費倍率・初期スタミナ・勝負師。"""
        R = self._race(r)
        rows = R['rows']
        return {'dist': R['dist'], 'names': [x['h'].get('name', '') for x in rows],
                'st': [x['st'].round(9).tolist() for x in rows], 'c0': [round(x['c0'], 9) for x in rows],
                'cost': [[round(c, 9) for c in x['cost']] for x in rows], 's0': [x['s0'] for x in rows],
                'g_p': [x['g_p'] for x in rows], 'g_m': [x['g_m'] for x in rows], 'p_m': [x['p_m'] for x in rows], 'p_c': [x['p_c'] for x in rows],
                'nz': [x['nz'] for x in rows], 'rec_ph': [x['rec_ph'] for x in rows], 'rec_low': [x['rec_low'] for x in rows]}

    # ---- 学習: rating の式・消費のぶれ・失速の表 ----
    def fit(self, races):
        # 翌日以降に採取したレースは、ステータスが「その時点の今」に化けている（9/10〜12 の18レース）
        races = [r for r in races if str(r.get('harvested_at', ''))[:10] <= str(r.get('race_date', ''))]
        X = {(d, pi): [] for d in NSEG for pi in range(3)}
        Y = {(d, pi): [] for d in NSEG for pi in range(3)}
        cr, fq = [], []
        for r in races:
            R = self._race(r)
            if not R:
                continue
            ph_of = np.repeat(np.arange(3), NSEG[R['dist']])
            for row in R['rows']:
                tl = row['h'].get('timeline') or []
                if len(tl) < 5:
                    continue
                pm = np.array(row['p_m'])
                has = [j for j in range(len(POS_KINDS)) if (pm[j] != 1.0).any()]
                for pi, ph in enumerate(PH_TL):
                    seg = [s for s in tl[1:] if s.get('phase') == ph and s.get('rating')]
                    x = row['st'][pi]
                    if has:     # 位置効果持ちは「発動の組み合わせ」ごとに分け、発動なし → なければ最多の組で当てる
                        if any(s.get('activated_passives') is None for s in seg):
                            continue
                        grp = {}
                        for s in seg:
                            act = set(s['activated_passives'])
                            grp.setdefault(tuple(j for j in has if row['p_key'][j] & act), []).append(s)
                        if not grp:
                            continue
                        g = () if () in grp else max(grp, key=lambda t: len(grp[t]))
                        seg = grp[g]
                        for j in g:
                            x = x * pm[j]
                    v = [s['rating'] for s in seg]
                    if v:
                        X[(R['dist'], pi)].append(x)
                        Y[(R['dist'], pi)].append(float(np.median(v)))
                cs = [s['stamina_cost'] for s in tl[1:] if s.get('stamina_cost')]
                cexp = row['c0'] * np.mean([row['cost'][pi] for pi in ph_of])
                if cs and cexp > 0 and not row['g_p']:
                    cr.append(math.log(np.mean(cs) / cexp))
                s0, nn = tl[0].get('stamina'), len(tl) - 1
                for k in range(1, len(tl)):
                    a, c, f = tl[k - 1].get('stamina'), tl[k].get('stamina_cost'), tl[k].get('fatigue_modifier')
                    if None not in (a, c, f, s0) and c > 0 and s0 > 0:
                        fq.append(((a - c * (nn - k + 1)) / s0, f))   # この先の消費を引いた見込みの余り / 初期
        self.W = {}
        for key in X:
            A, y = np.array(X[key]), np.array(Y[key])
            if len(y) < 20:
                self.W[key] = (np.array([1.0, 1.0, 1.0]) / 3, 0.0)
                continue
            A1 = np.hstack([A, np.ones((len(A), 1))])
            w, *_ = np.linalg.lstsq(A1, y, rcond=None)
            ok = np.abs(np.log(np.maximum(A1 @ w, 1.0) / y)) < 0.15   # 勝負師の発動・採取ミスを外して当て直す
            w, *_ = np.linalg.lstsq(A1[ok], y[ok], rcond=None)
            self.W[key] = (w[:3], float(w[3]))
        self.cost_mu, self.cost_sd = float(np.mean(cr)), float(np.std(cr))
        # 疲労補正は「残りの区間を走り切ったときの見込みの余り ÷ 初期スタミナ」でほぼ決まる
        # （timeline 39,630区間: 相関0.82。余りが多いほど最大 +3%、足りないと 0.65 まで落ちる）。
        # 2026/10/07 まで「残り ÷ 1区間の消費」で引いていて、余りの +0〜3% を全部 1.0 に潰していた
        # （R2628: スタミナ118 のはなこ を 5% と見て、lv に 95% を付けて外した）。
        fq = np.array(fq)
        edges = [-9, -1, -.5, -.3, -.2, -.1, -.05, 0, .05, .1, .15, .2, .3, .4, .6, 9]
        bins = [(fq[:, 0] >= lo) & (fq[:, 0] < hi) for lo, hi in zip(edges[:-1], edges[1:])]
        self.FQX = np.array([fq[m, 0].mean() for m in bins if m.any()])
        self.FQY = np.array([fq[m, 1].mean() for m in bins if m.any()])
        return self

    def _pos_passives(self):
        """位置で決まるパッシブ（JS は説明文を持たないので、種類・消費倍率・残りの倍率をここで渡す）。"""
        out = {}
        for p, sp in self.spec.items():
            desc = sp.get('desc') or ''
            kind = _pos_kind(desc)
            if kind and sp.get('mult'):
                m, cm = dict(sp['mult']), 1.0
                mm = _CONS_RE.search(desc)
                if mm:
                    x = float(mm.group(1)) / 100.0
                    cm = (1.0 - x) if mm.group(2) == '減少' else (1.0 + x)
                    m.pop('stamina', None)
                if 'スタミナ消費も増加' in desc:
                    cm *= LEAD_COST_UP
                out[p] = {'kind': kind, 'cost': cm, 'mult': {k: float(v) for k, v in m.items()}}
        return out

    def export(self):
        """model.json の sim（JS の simRace が読む）。"""
        return {'nseg': {d: list(v) for d, v in NSEG.items()},
                'w': {d: [[float(x) for x in self.W[(d, pi)][0]] + [float(self.W[(d, pi)][1])]
                          for pi in range(3)] for d in NSEG},
                'cost_mu': self.cost_mu, 'cost_sd': self.cost_sd,
                'fqx': [float(x) for x in self.FQX], 'fqy': [float(x) for x in self.FQY],
                'v_exp': V_EXP, 'f_exp': F_EXP, 'seg_noise': SEG_NOISE, 'horse_sd': self.horse_sd,
                'horse_in_race': HORSE_IN_RACE,
                'n_sim': self.n_sim, 'lowst_labels': sorted(LOWST_LABELS), 'lead_labels': sorted(LEAD_LABELS),
                'lead_duty': LEAD_DUTY, 'lowst_duty': LOWST_DUTY,
                'cost_passives': {k: [v[0], v[1]] for k, v in COST_PASSIVES.items()}, 'gamble': GAMBLE,
                # JS は一様乱数を持たないので、勝負師の抽選は「正規乱数 < この値」で行う（確率は同じ）
                'gamble_z': NormalDist().inv_cdf(float((self.spec.get(GAMBLE) or {}).get('duty', 0.05))),
                'pos_kinds': [k for k in POS_KINDS if k in POS_DYNAMIC], 'pos_duty': POS_DUTY,
                'pos_passives': self._pos_passives(),
                'lead_cost_up': LEAD_COST_UP, 'duel_m': DUEL_M, 'solo_m': SOLO_M, 'seg_m': SEG_M}

    # ---- 予測 ----
    def predict(self, r, seed=None):
        """→ (勝率[H], 3連単 {(i,j,k): 確率}, 馬名リスト)。"""
        R = self._race(r)
        rows, d = R['rows'], R['dist']
        rng = self.rng if seed is None else np.random.default_rng(seed)   # 比較は同じ乱数で
        H, NS, ns = len(rows), self.n_sim, NSEG[d]
        n = sum(ns)
        ph_of = np.repeat(np.arange(3), ns)
        c = np.array([x['c0'] for x in rows])[None, :] * np.exp(
            self.cost_mu + self.cost_sd * rng.standard_normal((NS, H)))
        cm = np.array([[x['cost'][pi] for pi in ph_of] for x in rows])       # H×区間
        rem = np.cumsum(cm[:, ::-1], 1)[:, ::-1]                              # その区間から最後までの消費倍率の和
        st = np.stack([x['st'] for x in rows])                               # H×3フェーズ×3ステ
        gm = np.array([x['g_m'] for x in rows])
        pm = np.array([x['p_m'] for x in rows])                              # H×種類×3ステ
        pc = np.array([x['p_c'] for x in rows])                              # H×種類
        has = [(j, ((pm[:, j] != 1.0).any(1) | (pc[:, j] != 1.0))[None, :]) for j in range(len(POS_KINDS))]
        has = [(j, m) for j, m in has if m.any()]
        pid = np.array([x['pid'] for x in rows])
        on = rng.random((NS, H)) < np.array([x['g_p'] for x in rows])[None, :]   # 勝負師の抽選
        s = np.array([x['s0'] for x in rows], float)[None, :].repeat(NS, 0)
        s1 = np.array([math.floor(x['s0'] * x['g_m'][2]) for x in rows], float)[None, :].repeat(NS, 0)
        s = np.where(on, s1, s)
        S0 = np.maximum(s.copy(), 1.0)
        T = np.zeros((NS, H))
        dt = np.ones((NS, H))
        hz = np.exp(self.horse_sd * rng.standard_normal((NS, H))) if (self.horse_sd and HORSE_IN_RACE) else 1.0
        nzv = np.array([x['nz'] for x in rows])[None, :]                    # 乱数幅を狭める装備
        rph = np.array([x['rec_ph'] for x in rows])                           # H×フェーズ: 突入時の回復率
        rlt, rlx = (np.array([x['rec_low'][i] for x in rows])[None, :] for i in (0, 1))
        rl_used = np.zeros((NS, H), bool)
        for k, pi in enumerate(ph_of):
            if k > 0 and ph_of[k - 1] != pi and rph[:, pi].any():          # 「中盤突入時にスタミナを3%回復」
                s = np.minimum(s + S0 * rph[None, :, pi], S0)
            if rlx.any():                                                    # 「残りスタミナ20%以下で一度だけ4%回復」
                hit = ~rl_used & (rlx > 0) & (s <= S0 * rlt)
                s = np.where(hit, np.minimum(s + S0 * rlx, S0), s)
                rl_used |= hit
            f = np.interp((s - c * rem[None, :, k]) / S0, self.FQX, self.FQY)
            mult = np.where(on[:, :, None], gm[None, :, :], 1.0)            # NS×H×3
            cmul = np.ones((NS, H))
            if has:   # 位置効果: 区間の始めの位置で判定（時間差 × 自分の速さで m に直す）
                spd = SEG_M / dt
                # 同じ位置（スタート直後は全頭 0m）なら pet_id の小さい馬が前と判定される
                # （timeline の start_rank が 434レース全部で pet_id 順と一致。首位の呪いの1区間目の発動も 98/98 一致）
                o = np.lexsort((np.broadcast_to(pid, (NS, H)), T), axis=1)
                lead_t = np.take_along_axis(T, o[:, :1], 1)
                second = np.take_along_axis(T, o[:, 1:2], 1) if H > 1 else np.full((NS, 1), np.inf)
                is_lead = np.arange(H)[None, :] == o[:, :1]
                behind = np.where(is_lead, second - T, 0.0)                    # 先頭のときの2位との差
                if H > 1:
                    o = np.argsort(T, 1)
                    g = np.diff(np.take_along_axis(T, o, 1), axis=1)
                    near = np.empty_like(T)
                    np.put_along_axis(near, o, np.minimum(np.pad(g, ((0, 0), (1, 0)), constant_values=np.inf),
                                                          np.pad(g, ((0, 0), (0, 1)), constant_values=np.inf)), 1)
                else:
                    near = np.full((NS, H), np.inf)
                cond = {'duel': near * spd <= DUEL_M,
                        'king': ~is_lead & ((T - lead_t) * spd <= DUEL_M),
                        'lead': is_lead,
                        'solo': is_lead & (behind * spd >= SOLO_M)}
                for j, hm in has:
                    a_ = hm & cond[POS_KINDS[j]]
                    mult = mult * np.where(a_[:, :, None], pm[None, :, j, :], 1.0)
                    cmul = cmul * np.where(a_, pc[None, :, j], 1.0)
            rr = np.maximum(np.einsum('nhj,hj->nh', mult, st[:, pi, :] * self.W[(d, pi)][0][None, :])
                            + self.W[(d, pi)][1], 1.0)
            dt = hz / (rr ** V_EXP * f ** F_EXP * (1 + SEG_NOISE * nzv * rng.standard_normal((NS, H))))
            T += dt
            s = s - c * cm[None, :, k] * cmul
        if self.horse_sd and not HORSE_IN_RACE:      # 1レース1頭ごとの調子のぶれ（区間の乱数だけでは自信過剰になる）
            T *= np.exp(self.horse_sd * rng.standard_normal((NS, H)))
        order = np.argsort(T, 1)
        win = np.bincount(order[:, 0], minlength=H) / NS
        tri = {}
        if H >= 3:
            keys, cnt = np.unique(order[:, :3], axis=0, return_counts=True)
            tri = {tuple(map(int, k_)): v_ / NS for k_, v_ in zip(keys, cnt)}
        return win, tri, [x['h'].get('name', '') for x in rows]


if __name__ == '__main__':      # 動作確認: 最後の日より前で学習 → 最後のレースを予想
    import json, os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'races.jsonl')
    raw = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
    raw = [r for r in raw if r.get('race_date', '') >= '2026-08-20' and r.get('distance') in NSEG]
    last = raw[-1]
    sim = RaceSim(n_sim=2000).fit([r for r in raw if r['race_date'] < last['race_date']])
    win, tri, names = sim.predict(last, seed=1)
    assert len(win) == len(names) and abs(win.sum() - 1) < 1e-9 and abs(sum(tri.values()) - 1) < 1e-9
    # oasis 級10種: 固有スキルと追加効果がそれぞれ正しい場所に入るか
    base = dict(speed=100.0, power=100.0, stamina=100.0)
    def pf(label, desc, d='長距離'):
        it = dict(effect_label=label, effect_description=desc, effect_key='unique_x')
        return horse_profile(dict(base, equipment=it), d, '芝', sim.spec, False)
    S = {lab: pf(lab, ds) for _, lab, ds in OASIS_SAMPLES}
    ok = lambda a, b: abs(a - b) < 1e-9
    assert S['楽園天翔']['fx'][0][0] == 2 and ok(S['楽園天翔']['cost'][0], 0.96), S['楽園天翔']
    assert ok(S['神樹轟臂']['pos']['duel'][0]['power'], 1.25) and ok(S['神樹轟臂']['base']['power'], 105)
    assert ok(S['原海戴冠']['pos']['lead'][0]['speed'], 1.23) and ok(S['原海戴冠']['pos']['lead'][1], 1.06 * LEAD_COST_UP)
    assert ok(S['原海戴冠']['base']['stamina'], 105) and 'stamina' not in S['原海戴冠']['pos']['lead'][0]
    assert S['永劫機律']['fx'] == [(1, {'power': 1.24})] and ok(S['永劫機律']['nz'], 0.8)
    assert ok(S['創世天駆']['base']['speed'], 100 * (1 + 0.24 * 0.101)) and S['創世天駆']['fx'] == [(0, {'speed': 1.04})]
    assert ok(S['楽園核共鳴']['base']['power'], 109.5) and ok(S['楽園核共鳴']['cost'][2], 0.97)
    assert ok(S['聖輪転生']['base']['power'], 100 * (1 + 0.16 * 0.527)) and S['聖輪転生']['rec'] == [(1, 1.0, 0.03)]
    assert S['永劫時律']['fx'] == [(1, {'speed': 1.24})] and ok(S['永劫時律']['nz'], 0.8)
    assert ok(S['世界樹循環']['base']['stamina'], 119) and ok(S['世界樹循環']['cost'][1], 0.96)
    assert ok(S['星海反転']['base']['speed'], 100 * (1 + 0.18 * LOWST_DUTY)) and S['星海反転']['rec'] == [(None, 0.2, 0.04)]
    # 先頭の判定（同じ位置なら pet_id の小さい馬が前）: R2257 はスタートで先頭を取った首位の呪いのおいらが逃げ切った
    r2257 = next((r for r in raw if r['schedule_id'] == 2257), None)
    if r2257:
        w2, _, n2 = sim.predict(r2257, seed=1)
        assert n2[int(np.argmax(w2))] == 'おいら', dict(zip(n2, w2.round(2)))
    print(f"R{last['schedule_id']} OK:", ', '.join(f'{n} {p:.0%}' for n, p in
                                                     sorted(zip(names, win), key=lambda t: -t[1])[:3]))
