"""レースの答え合わせ: シミュの予想と実際の timeline を区間・フェーズごとに並べる。

    python race_review.py          … races.jsonl の最新レース
    python race_review.py 2649     … R2649

予想はそのレースの日より前のデータだけで学習したシミュ（bot と同じ条件）。
「どこで差がついたか」を フェーズごとのタイム差・rating・スタミナ・効果の発動 で見る。
"""
import json
import os
import sys
from collections import defaultdict

import numpy as np

import oasis_core as oc
import race_sim as rs

PH = ('序盤', '中盤', '終盤')


def actual(h):
    """timeline → フェーズごとのタイム・rating、終わりのスタミナ、最小の疲労補正、フェーズごとの発動（キー: 区間数）。"""
    tl = h.get('timeline') or []
    t, r, act = [0.0] * 3, [[], [], []], [defaultdict(int) for _ in range(3)]
    fmin = 9.0
    for a, b in zip(tl, tl[1:]):
        pi = rs.PH_TL.index(b['phase'])
        t[pi] += b['elapsed'] - a['elapsed']
        if b.get('rating'):
            r[pi].append(b['rating'])
        if b.get('fatigue_modifier'):
            fmin = min(fmin, b['fatigue_modifier'])
        for k in b.get('activated_passives') or []:
            act[pi][k] += 1
    return dict(t=t, total=sum(t), r=[float(np.mean(x)) if x else None for x in r],
                s0=tl[0].get('stamina') if tl else None, s_end=tl[-1].get('stamina') if tl else None,
                fmin=fmin if fmin < 9 else None, act=act)


def review(r, raw, n_sim=8000):
    since = oc.JSONL_TRAIN_FROM.replace('/', '-')
    train = [x for x in raw if since <= x['race_date'] < r['race_date'] and x.get('distance') in rs.NSEG]
    sim = rs.RaceSim(n_sim=n_sim).fit(train)
    hs = [h for h in r['horses'] if h.get('rank') and h.get('speed') is not None]
    win, tri, names, info = sim.predict(dict(r, horses=hs), seed=r['schedule_id'], detail=True)
    top3 = np.zeros(len(hs))
    for (i, j, k), p in tri.items():
        top3[[i, j, k]] += p
    A = [actual(h) for h in hs]
    # シミュの時間の単位を秒に直す。ゲームの速さの定数はフェーズごとに違うので、フェーズごとに
    # 上位半分の馬の「実際 ÷ 予想」の中央値で合わせる
    ok = [i for i in np.argsort([h['rank'] for h in hs])[:max(2, len(hs) // 2)] if A[i]['total'] > 0]
    sec = np.array([float(np.median([A[i]['t'][q] / info['ph_dt'][q][i] for i in ok])) for q in range(3)])
    simsec = info['ph_dt'] * sec[:, None]                                   # フェーズ×馬（秒）
    sim_tot = simsec.sum(0)
    ns = rs.NSEG[r['distance']]
    out = []
    p = out.append
    order = list(np.argsort([h['rank'] for h in hs]))
    pred = list(np.argsort(-win))
    p(f"━━ R{r['schedule_id']} {r['race_date']} {r['race_time']} {r['distance']}・{r['surface']}・{len(hs)}頭 ━━")
    p(f"予想の本命 {names[pred[0]]} {win[pred[0]]:.0%} → 1着 {names[order[0]]}（予想 {win[order[0]]:.0%}）")
    if len(hs) >= 3:
        best = max(tri, key=tri.get)
        act3 = tuple(int(i) for i in order[:3])
        p(f"3連単の本命 {'→'.join(names[i] for i in best)} {tri[best]:.0%} / 実際 {'→'.join(names[i] for i in act3)}"
          f"（予想 {tri.get(act3, 0):.1%}）")
    p('')
    p('着 馬            勝率  3着内  オッズ | 序盤/中盤/終盤のタイム 実際/予想（差 秒） | rating 実際/予想（予想は発動なし） | スタミナ 初→終 実際/予想')
    for i in order:
        a, h = A[i], hs[i]
        tm = ' '.join(f"{a['t'][q]:5.1f}/{simsec[q][i]:5.1f}({a['t'][q] - simsec[q][i]:+.1f})" for q in range(3))
        rt = ' '.join(f"{a['r'][q]:.0f}/{info['rating'][i][q]:.0f}" if a['r'][q] else '-' for q in range(3))
        p(f"{h['rank']:>2} {names[i][:7]:<8} {win[i]:5.0%} {top3[i]:5.0%} {h.get('odds') or 0:6.1f} | {tm} | {rt} |"
          f" {a['s0']}→{a['s_end']:.0f} / {info['s0'][i]}→{info['s_end'][i]:.0f}")
    p('')
    p('効果の発動（実際 区間数/区間 ・ シミュの予想は位置で判定する効果だけ）')
    for i in order:
        parts = []
        for q in range(3):
            acts = ', '.join(f'{k} {v}/{ns[q]}' for k, v in sorted(A[i]['act'][q].items()))
            simk = ', '.join(f'{k} {float(v[i]):.0%}' for k, v in info['act'][q].items() if float(v[i]) > 0.005)
            if acts or simk:
                parts.append(f"{PH[q]}[実際 {acts or 'なし'} | 予想 {simk or '-'}]")
        if parts:
            p(f"  {names[i][:8]}: " + ' '.join(parts))
    # 予想と実際で順番が入れ替わった上位の組: 差がどのフェーズでついたか
    p('')
    p('予想と逆になった組（上位4頭まで）: 実際の差 − 予想の差（秒・＋なら前の馬が予想より稼いだ）')
    pos = {int(i): n for n, i in enumerate(order)}
    for x in order[:4]:
        for y in order[:4]:
            x, y = int(x), int(y)
            if pos[x] < pos[y] and win[x] + top3[x] < win[y] + top3[y]:
                d = [(A[y]['t'][q] - A[x]['t'][q]) - (simsec[q][y] - simsec[q][x]) for q in range(3)]
                p(f"  {names[x]} が {names[y]} に先着: " + ' / '.join(f'{PH[q]} {d[q]:+.2f}' for q in range(3))
                  + f"（予想の差 {sim_tot[y] - sim_tot[x]:+.2f}秒 → 実際 {A[y]['total'] - A[x]['total']:+.2f}秒）")
    return '\n'.join(out)


if __name__ == '__main__':
    here = os.path.dirname(os.path.abspath(__file__))
    raw = [json.loads(l) for l in open(os.path.join(here, 'races.jsonl'), encoding='utf-8') if l.strip()]
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else raw[-1]['schedule_id']
    r = next((x for x in raw if x['schedule_id'] == sid), None)
    if r is None:
        sys.exit(f'R{sid} が races.jsonl にありません（採取前かもしれません）')
    txt = review(r, raw)
    print(txt)
    assert f'R{sid}' in txt and '効果の発動' in txt      # 最低限の自己チェック（途中で例外なら上で落ちる）
