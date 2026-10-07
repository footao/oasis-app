"""レースを区間ごとに再現するシミュレータ（影の運用・2026/10/07）。

ENGINE_NOTES.md で逆算したゲームの仕組みをそのまま組み立てる:
  区間速度  v = rating^0.8709 × fatigue^0.9136 × (1 + 乱数1.19%)
  rating    = 距離×フェーズごとの線形式（実効 SP/PW/ST、学習データの timeline から当てる）
  スタミナ  初期 floor(実効ST)、区間ごとに一定量ずつ減る。消費量は**レースごとに乱数でぶれる**
            （±6%。ENGINE_NOTES 追記3）。これを1点で決め打ちせず、何千通り振る
  失速      残りスタミナ/1区間の消費 から fatigue を引く（使い切ると 0.65）

区間で発動する効果の扱い:
  phase     序盤/中盤/終盤の区間にだけ掛ける（ロケットスタート・中盤加速・末脚・時界超越など）
  tail300 / lowst / lead は既定（dynamic='avg'）ではカタログの平均 duty で均す。
    dynamic=True で「残り300m・残りスタミナ25%（10/06〜30%）以下・シミュ上で先頭」の区間にだけ
    掛けることもできるが、前向き検証189レースでは均したほうが良かった
    （単勝LL 0.523 vs 0.547、3連単LL 1.561 vs 1.577。差は1σ未満）。
  それ以外（競り合い・下位半分など位置関係で決まるもの）は平均の duty で均す。
前向き検証（9/2〜10/6・189レース、Ridge＝今のモデル）:
  本命1着 84.7%→86.2% / 単勝LL 0.567→0.523（−1.6σ） / 3連単LL 1.583→1.561 / 上位3頭 75.3%→79.4%（+1.6σ）

bot の買い方には使っていない。shadow_sim.py が毎レースの予想を記録して、今のモデルと比べる。
"""
import math
import numpy as np
import oasis_core as oc

PH_JA = ('序盤', '中盤', '終盤')
PH_TL = ('early', 'middle', 'late')
NSEG = {'短距離': (3, 4, 3), 'マイル': (4, 6, 5), '中距離': (6, 8, 6), '長距離': (7, 10, 8)}
V_EXP, F_EXP = 0.8709, 0.9136
SEG_NOISE = 0.0119
LEAD_COST = 1.04                          # 先頭の間はスタミナ消費 ×1.04（ENGINE_NOTES 追記1）
LOWST_PATCH = '2026-10-06'                # この日から「スタミナ25%以下」→「30%以下」
LOWST_LABELS = {'血走り', '骨砕き', '紅蓮点火', '深淵反転', '禍福転倒'}
LEAD_LABELS = {'首位の呪い', '王冠過給', '先導祈願'}


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
            code = str(it.get('effect_key') or '').replace('gear_', '').replace('charm_', '')
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

    # ---- 1レースを「馬ごとの配列」にする ----
    def _race(self, r):
        dist, track = r.get('distance'), r.get('surface')
        if dist not in NSEG:
            return None
        hs = [h for h in (r.get('horses') or r.get('pets') or []) if h.get('speed') is not None]
        same = oc.same_species_flags([h.get('name', '') for h in hs], [h.get('adult_key') for h in hs])
        rows = []
        for h, sm in zip(hs, same):
            base, fx, sig, ps = horse_profile(h, dist, track, self.spec, sm, self.scope_tbl)
            # 消費量は「平均に均した」実効ステから（stamina_budget と同じ式）
            e = dict(base)
            for kind, arg, m in fx:
                if kind == 'phase':
                    frac = NSEG[dist][arg] / sum(NSEG[dist])
                    for k, x in m.items():
                        e[k] *= 1.0 + (x - 1.0) * frac
            need, _, _ = oc.stamina_budget(e, dist)
            pstats = []
            for pi in range(3):
                s = dict(base)
                for kind, arg, m in fx:
                    if kind == 'phase' and arg == pi:
                        for k, x in m.items():
                            s[k] *= x
                pstats.append([s['speed'], s['power'], s['stamina']])
            rows.append(dict(h=h, base=base, fx=fx, sig=sig, pstats=np.array(pstats),
                             c0=need / sum(NSEG[dist]), s0=math.floor(e['stamina'])))
        return dict(dist=dist, track=track, rows=rows, date=str(r.get('race_date') or ''))

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
                        X[(R['dist'], pi)].append(row['pstats'][pi])
                        Y[(R['dist'], pi)].append(float(np.median(v)))
                cs = [s['stamina_cost'] for s in tl[1:] if s.get('stamina_cost')]
                if cs and row['c0'] > 0:
                    cr.append(math.log(np.mean(cs) / row['c0']))
                for k in range(1, len(tl)):
                    a, c, f = tl[k - 1].get('stamina'), tl[k].get('stamina_cost'), tl[k].get('fatigue_modifier')
                    if None not in (a, c, f) and c > 0:
                        fq.append((a / c, f))
        self.W = {}
        for key in X:
            A = np.array(X[key]); y = np.array(Y[key])
            if len(y) < 20:
                self.W[key] = (np.array([1.0, 1.0, 1.0]) / 3, 0.0)
                continue
            A1 = np.hstack([A, np.ones((len(A), 1))])
            w, *_ = np.linalg.lstsq(A1, y, rcond=None)
            self.W[key] = (w[:3], w[3])
        self.cost_mu, self.cost_sd = float(np.mean(cr)), float(np.std(cr))
        fq = np.array(fq)
        edges = [-99, -.5, 0, .25, .5, .75, 1, 1.5, 3, 99]
        self.FQX = np.array([-1, -.25, .125, .375, .625, .875, 1.25, 2.25, 10])
        self.FQY = np.array([fq[(fq[:, 0] > lo) & (fq[:, 0] <= hi), 1].mean()
                             if ((fq[:, 0] > lo) & (fq[:, 0] <= hi)).any() else 0.65
                             for lo, hi in zip(edges[:-1], edges[1:])])
        return self

    # ---- 予測 ----
    def predict(self, r, dynamic='avg', use_sig=False, seed=None):
        """→ (勝率[H], 3連単 {(i,j,k): 確率}, 馬名リスト)。

        dynamic: 'avg'（既定）= 残り300m・残スタミナ・先頭の効果を平均 duty で均す /
                 True = シミュレーション上で発動した区間にだけ掛ける / False = それらを入れない
        use_sig: 安定感などの乱数幅の縮小を区間の乱数に掛けるか（検証で差なし）
        """
        R = self._race(r)
        rows, d = R['rows'], R['dist']
        rng = self.rng if seed is None else np.random.default_rng(seed)   # 比較は同じ乱数で
        H, NS, ns = len(rows), self.n_sim, NSEG[R['dist']]
        n = sum(ns)
        thr = 0.30 if R['date'] >= LOWST_PATCH else 0.25
        phase_of = np.repeat(np.arange(3), ns)
        c = np.array([x['c0'] for x in rows])[None, :] * np.exp(
            self.cost_mu + self.cost_sd * rng.standard_normal((NS, H)))
        S0 = np.array([x['s0'] for x in rows], float)[None, :]
        sig = np.array([x['sig'] if use_sig else 1.0 for x in rows])[None, :]
        stat = np.stack([x['pstats'] for x in rows])            # H×3フェーズ×3ステ
        if not dynamic:                                         # 区間効果を均した v1 相当
            avg = stat.mean(1, keepdims=True)
            w_ = np.array(ns, float) / n
            avg = (stat * w_[None, :, None]).sum(1, keepdims=True)
            stat = np.repeat(avg, 3, 1)
        dyn = [[(kind, arg, m) for kind, arg, m in x['fx'] if kind != 'phase'] for x in rows]
        if dynamic == 'avg':          # 比較用: 区間で発動する効果をカタログの平均 duty で均す
            AVG = {'tail300': 3.0 / n, 'lowst': 0.183, 'lead': 0.079}
            for hi, effs in enumerate(dyn):
                for kind, arg, m in effs:
                    for j, key in enumerate(('speed', 'power', 'stamina')):
                        if key in m:
                            stat[hi, :, j] *= 1.0 + (m[key] - 1.0) * AVG[kind]
            dyn = [[] for _ in rows]
        T = np.zeros((NS, H)); s = np.repeat(S0, NS, 0).astype(float)
        for k, pi in enumerate(phase_of):
            st = np.repeat(stat[None, :, pi, :], NS, 0)          # NS×H×3
            cost = c.copy()
            if dynamic:
                lead = (np.argmin(T, 1) if k else np.argmax(stat[:, pi, 0])[None].repeat(NS))
                for hi, effs in enumerate(dyn):
                    for kind, arg, m in effs:
                        if kind == 'tail300':
                            on = np.full(NS, k >= n - 3)
                        elif kind == 'lowst':
                            on = s[:, hi] <= thr * S0[0, hi]
                        elif kind == 'lead':
                            on = lead == hi
                            cost[on, hi] *= LEAD_COST
                        else:
                            continue
                        for j, key in enumerate(('speed', 'power', 'stamina')):
                            if key in m:
                                st[on, hi, j] *= m[key]
            w, b = self.W[(d, pi)]
            rating = np.maximum(st @ w + b, 1.0)
            f = np.interp(s / cost, self.FQX, self.FQY)
            v = rating ** V_EXP * f ** F_EXP * (1 + SEG_NOISE * sig * rng.standard_normal((NS, H)))
            T += 1.0 / v
            s = s - cost
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
