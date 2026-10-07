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
  本命1着 84.0%→85.6% / 単勝LL 0.563→0.474 / 3連単LL 1.578→1.501
  10/07 修正: 疲労補正を「見込みの余り」で引くようにし（余り +0〜3% を拾う）、1頭ごとのぶれ HORSE_SD を入れた。
  修正前は自信過剰で、R2628 で lv に 95%（3連単 lv→はなこ 91%）を付けて 30万負けた。

2026/10/07 から bot の予想はこれ（JS: model.js の simRace。係数は model.json の sim）。
従来の Ridge の予想は比較用に reports の rw/rc に残す。
"""
import math
import re

import numpy as np
import oasis_core as oc

PH_JA = ('序盤', '中盤', '終盤')
PH_TL = ('early', 'middle', 'late')
NSEG = {'短距離': (3, 4, 3), 'マイル': (4, 6, 5), '中距離': (6, 8, 6), '長距離': (7, 10, 8)}
V_EXP, F_EXP = 0.8709, 0.9136
SEG_NOISE = 0.0119
HORSE_SD = 0.008       # 1頭ごとのレースのぶれ（前向き検証194レースで単勝・3連単とも最良）
LOWST_LABELS = {'血走り', '骨砕き', '紅蓮点火', '深淵反転', '禍福転倒'}
LEAD_LABELS = {'首位の呪い', '王冠過給', '先導祈願'}
AVG_DUTY = {'lowst': 0.183, 'lead': 0.079}   # tail300 は 3区間/区間数


def _phase_idx(arg):
    return PH_JA.index(arg) if arg in PH_JA else None


def horse_profile(h, dist, track, spec, same_species, scope_tbl=None):
    """API の馬1頭 → (常時の実効ステ, 区間効果のリスト, σ倍率, 消費倍率)。

    区間効果は (kind, arg, {stat: 倍率}) で、kind は phase/tail300/lowst/lead。
    """
    scope_tbl = scope_tbl or oc.item_scope_table(spec)
    n = sum(NSEG[dist])
    base = {'speed': float(h['speed']), 'power': float(h['power']), 'stamina': float(h['stamina'])}
    fx, sig = [], 1.0

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
        fx.append(('phase', pi, {k: 1.0 + (float(x) - 1.0) * min(1.0, duty / frac)
                                 for k, x in m.items()}))

    passives = [oc.PASSIVE_CODE_MAP[c] for c in (h.get('passive_skill'), h.get('passive_skill_2'))
                if c and c != 'none' and c in oc.PASSIVE_CODE_MAP]
    for p in passives:
        sp = spec.get(p) or {}
        if sp.get('scope') == 'variance':
            sig *= float(sp.get('sigma_mult', 1.0))
        m = sp.get('mult')
        if not m:
            continue
        sc = sp.get('scope')
        if sc == 'aptitude' and sp.get('scope_arg') not in (dist, track):
            continue
        if sc == 'same_species' and not same_species:
            continue
        if sc == 'phase':
            phase(m, sp.get('scope_arg'), float(sp.get('duty', 1.0)))
        else:
            avg(m, float(sp.get('duty', 1.0)) if sc not in ('always', 'aptitude', 'same_species') else 1.0)

    for it in (h.get('equipment'), h.get('charm')):
        if not isinstance(it, dict):
            continue
        label = (it.get('effect_label') or '').strip()
        desc = it.get('effect_description') or ''
        sp = oc.spec_from_description(f'{label}：{desc}') or {}
        if sp.get('scope') == 'variance':
            sig *= float(sp.get('sigma_mult', 1.0))
            continue
        m = sp.get('mult')
        if not m:
            continue
        cat = oc.ITEM_EFFECT_CATALOG.get(label) or {}
        alias = cat.get('alias')
        if not alias and not cat:
            code = re.sub(r'^(?:gear|charm|item)_', '', str(it.get('effect_key') or ''))
            alias = oc.PASSIVE_CODE_MAP.get(code) or oc.ITEM_KEY_ALIAS.get(code)
        if alias:
            a = spec.get(alias) or {}
            sc, arg, duty = a.get('scope', 'conditional'), a.get('scope_arg'), float(a.get('duty', 1.0))
        elif cat:
            sc, arg = cat.get('scope', 'always'), cat.get('scope_arg')
            duty = float(cat['duty']) if cat.get('duty') is not None else 1.0
        elif '常時' in desc:
            sc, arg, duty = 'always', None, 1.0
        else:
            continue
        if label in LOWST_LABELS:
            fx.append(('lowst', None, m))
        elif label in LEAD_LABELS or sc == 'lead':
            fx.append(('lead', None, {k: x for k, x in m.items() if k != 'stamina'}))
        elif sc == 'tail300':
            fx.append(('tail300', None, m))
        elif sc == 'phase':
            phase(m, arg, duty)
        elif sc == 'aptitude':
            if arg in (dist, track):
                avg(m, 1.0)
        elif sc in ('learned', 'same_species'):
            continue
        else:
            avg(m, duty if sc != 'always' else 1.0)
    for k in base:
        base[k] = max(base[k], 1.0)
    return base, fx, sig, passives


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
            base, fx, _, _ = horse_profile(h, dist, track, self.spec, sm, self.scope_tbl)
            # 消費量は「区間効果を区間の割合で均した」実効ステから（stamina_budget と同じ式）
            e = dict(base)
            for kind, arg, m in fx:
                if kind == 'phase':
                    for k, x in m.items():
                        e[k] *= 1.0 + (x - 1.0) * NSEG[dist][arg] / n
            need, _, _ = oc.stamina_budget(e, dist)
            st = np.array([[base['speed'], base['power'], base['stamina']]] * 3)
            for kind, arg, m in fx:
                for j, k in enumerate(('speed', 'power', 'stamina')):
                    if k not in m:
                        continue
                    if kind == 'phase':
                        st[arg, j] *= m[k]
                    else:          # 残り300m・残スタミナ・先頭は平均 duty で全区間に均す
                        d = 3.0 / n if kind == 'tail300' else AVG_DUTY[kind]
                        st[:, j] *= 1.0 + (m[k] - 1.0) * d
            rows.append(dict(h=h, st=st, c0=need / n, s0=math.floor(e['stamina'])))
        return dict(dist=dist, track=track, rows=rows)

    def inputs(self, r):
        """一致検証用: 馬ごとの [フェーズ×ステ] と 1区間の消費・初期スタミナ。"""
        R = self._race(r)
        return {'dist': R['dist'], 'names': [x['h'].get('name', '') for x in R['rows']],
                'st': [x['st'].round(9).tolist() for x in R['rows']],
                'c0': [round(x['c0'], 9) for x in R['rows']], 's0': [x['s0'] for x in R['rows']]}

    # ---- 学習: rating の式・消費のぶれ・失速の表 ----
    def fit(self, races):
        X = {(d, pi): [] for d in NSEG for pi in range(3)}
        Y = {(d, pi): [] for d in NSEG for pi in range(3)}
        cr, fq = [], []
        for r in races:
            R = self._race(r)
            if not R:
                continue
            for row in R['rows']:
                tl = row['h'].get('timeline') or []
                if len(tl) < 5:
                    continue
                for pi, ph in enumerate(PH_TL):
                    v = [s['rating'] for s in tl[1:] if s.get('phase') == ph and s.get('rating')]
                    if v:
                        X[(R['dist'], pi)].append(row['st'][pi])
                        Y[(R['dist'], pi)].append(float(np.median(v)))
                cs = [s['stamina_cost'] for s in tl[1:] if s.get('stamina_cost')]
                if cs and row['c0'] > 0:
                    cr.append(math.log(np.mean(cs) / row['c0']))
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
            w, *_ = np.linalg.lstsq(np.hstack([A, np.ones((len(A), 1))]), y, rcond=None)
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

    def export(self):
        """model.json の sim（JS の simRace が読む）。"""
        return {'nseg': {d: list(v) for d, v in NSEG.items()},
                'w': {d: [[float(x) for x in self.W[(d, pi)][0]] + [float(self.W[(d, pi)][1])]
                          for pi in range(3)] for d in NSEG},
                'cost_mu': self.cost_mu, 'cost_sd': self.cost_sd,
                'fqx': [float(x) for x in self.FQX], 'fqy': [float(x) for x in self.FQY],
                'v_exp': V_EXP, 'f_exp': F_EXP, 'seg_noise': SEG_NOISE, 'horse_sd': self.horse_sd,
                'n_sim': self.n_sim,
                'avg_duty': dict(AVG_DUTY), 'lowst_labels': sorted(LOWST_LABELS),
                'lead_labels': sorted(LEAD_LABELS)}

    # ---- 予測 ----
    def predict(self, r, seed=None):
        """→ (勝率[H], 3連単 {(i,j,k): 確率}, 馬名リスト)。"""
        R = self._race(r)
        rows, d = R['rows'], R['dist']
        rng = self.rng if seed is None else np.random.default_rng(seed)   # 比較は同じ乱数で
        H, NS, ns = len(rows), self.n_sim, NSEG[d]
        c = np.array([x['c0'] for x in rows])[None, :] * np.exp(
            self.cost_mu + self.cost_sd * rng.standard_normal((NS, H)))
        s = np.repeat(np.array([x['s0'] for x in rows], float)[None, :], NS, 0)
        st = np.stack([x['st'] for x in rows])                  # H×3フェーズ×3ステ
        rating = np.stack([np.maximum(st[:, pi, :] @ self.W[(d, pi)][0] + self.W[(d, pi)][1], 1.0)
                           for pi in range(3)], 1)             # H×3
        T = np.zeros((NS, H)); S0 = np.maximum(s[0:1], 1.0); n = sum(ns)
        for k, pi in enumerate(np.repeat(np.arange(3), ns)):
            f = np.interp((s - c * (n - k)) / S0, self.FQX, self.FQY)
            T += 1.0 / (rating[None, :, pi] ** V_EXP * f ** F_EXP
                        * (1 + SEG_NOISE * rng.standard_normal((NS, H))))
            s = s - c
        if self.horse_sd:      # 1レース1頭ごとの調子のぶれ（区間の乱数だけでは自信過剰になる）
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
    print(f"R{last['schedule_id']} OK:", ', '.join(f'{n} {p:.0%}' for n, p in
                                                     sorted(zip(names, win), key=lambda t: -t[1])[:3]))
