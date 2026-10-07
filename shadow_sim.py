"""影の運用: シミュレータ（race_sim.py）の予想を毎レース記録し、bot の実際の予想と比べる。

bot の買い方には一切使わない。harvest_daily.bat が採取のあとに毎回呼ぶ。

  python shadow_sim.py                    # 新しいレースだけ追記して成績表を出す
  python shadow_sim.py --since 2026-10-03 # 比べ始める日（既定 2026-10-03 = core 3.46.0 から）

公正さ: 各レースの予想は**その日より前のレースだけ**で学習したシミュレータで出す。
races.jsonl のステータスはレース時点の値（stats_at_race）なので、結果は予想に使っていない。
bot 側は reports.txt に残っている実際の予想（単勝は上位5頭、3連単は上位20組）。
そこに無い馬・組は「ほぼ0%と見ていた」として 1e-4 / 1e-5 で数える。
"""
import argparse
import json
import math
import os
import sys

import numpy as np

import oasis_core as oc
import race_sim as rs

HERE = os.path.dirname(os.path.abspath(__file__))
RACES = os.path.join(HERE, 'races.jsonl')
OUT = os.path.join(HERE, 'shadow.jsonl')
REPORT_TXT = os.path.join(HERE, 'shadow_report.txt')
REPORTS = os.path.join(HERE, '..', 'autooo', 'reports.txt')
TRAIN_FROM = '2026-08-20'


def load_reports(path):
    out = {}
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding='utf-8', errors='replace'):
        if line.startswith('{'):
            try:
                j = json.loads(line)
                out[int(j['sid'])] = j
            except (ValueError, KeyError):
                pass
    return out


def usable(r):
    return (r.get('race_date', '') >= TRAIN_FROM and r.get('distance') in rs.NSEG
            and len([h for h in r.get('horses') or [] if h.get('rank')]) >= 4)


def record(sim, r, rep):
    hs = [h for h in r['horses'] if h.get('rank') and h.get('speed') is not None]
    r2 = dict(r, horses=hs)
    win, tri, names = sim.predict(r2, seed=int(r['schedule_id']))
    res = [h['name'] for h in sorted(hs, key=lambda h: h['rank'])[:3]]
    idx = {nm: i for i, nm in enumerate(names)}
    act = tuple(idx[n] for n in res)
    top = sorted(range(len(names)), key=lambda i: -win[i])[:5]
    tri_top = sorted(tri.items(), key=lambda kv: -kv[1])[:10]
    out = {'sid': int(r['schedule_id']), 'date': r['race_date'], 'time': r.get('race_time'),
           'dist': r['distance'], 'n': len(hs), 'result': res,
           'sim_win': [[names[i], round(float(win[i]), 4)] for i in top],
           'sim_p_winner': round(float(win[act[0]]), 5),
           'sim_tri': [[[names[i] for i in k], round(v, 4)] for k, v in tri_top],
           'sim_p_tri': round(float(tri.get(act, 0.0)), 5)}
    if rep:
        out['bot_win'] = [[w['n'], w['p']] for w in (rep.get('wc') or [])]
        out['bot_tri'] = [[c['n'], c['p']] for c in (rep.get('cand') or [])]
        out['bot_ver'] = f"{rep.get('v')}/{rep.get('c')}"
    return out


def metrics(rows, who):
    fav, wll, tll, s3 = [], [], [], []
    for x in rows:
        w = x['result'][0]
        if who == 'sim':
            wl, pw, tl = x['sim_win'], x['sim_p_winner'], x['sim_tri']
            pt = x['sim_p_tri']
        else:
            wl, tl = x['bot_win'], x['bot_tri']
            pw = next((p for n, p in wl if n == w), 0.0)
            pt = next((p for n, p in tl if list(n) == x['result']), 0.0)
        fav.append(int(bool(wl) and wl[0][0] == w))
        wll.append(-math.log(max(pw, 1e-4)))
        if x['n'] >= 8:
            tll.append(-math.log(max(pt, 1e-5)))
            s3.append(int(bool(tl) and sorted(tl[0][0]) == sorted(x['result'])))
    return dict(fav=fav, wll=wll, tll=tll, s3=s3)


def diff_line(label, a, b, fmt):
    d = np.array(a, float) - np.array(b, float)
    if len(d) < 2:
        return f'{label}: データ不足'
    se = d.std(ddof=1) / math.sqrt(len(d))
    z = d.mean() / se if se > 0 else 0.0
    return (f'{label}: bot {fmt(np.mean(b))} / シミュ {fmt(np.mean(a))}'
            f'（差 {d.mean():+.4f} ± {se:.4f}、{z:+.1f}σ）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-10-03')
    ap.add_argument('--rebuild', action='store_true', help='shadow.jsonl を作り直す')
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raw = [json.loads(l) for l in open(RACES, encoding='utf-8') if l.strip()]
    raw = [r for r in raw if usable(r)]
    reps = load_reports(REPORTS)
    done = {}
    if os.path.isfile(OUT) and not a.rebuild:
        for l in open(OUT, encoding='utf-8'):
            try:
                x = json.loads(l)
                done[x['sid']] = x
            except ValueError:
                pass
    todo = [r for r in raw if r['race_date'] >= a.since and int(r['schedule_id']) not in done]
    sims = {}
    new = []
    for r in sorted(todo, key=lambda r: int(r['schedule_id'])):
        d = r['race_date']
        if d not in sims:      # その日より前のレースだけで学習（同じ日は使い回す）
            sims[d] = rs.RaceSim(n_sim=8000).fit([x for x in raw if x['race_date'] < d])
        x = record(sims[d], r, reps.get(int(r['schedule_id'])))
        done[x['sid']] = x
        new.append(x)
    mode = 'w' if a.rebuild else 'a'
    with open(OUT, mode, encoding='utf-8') as f:
        for x in new:
            f.write(json.dumps(x, ensure_ascii=False) + '\n')

    rows = [x for x in done.values() if x['date'] >= a.since and x.get('bot_win')]
    rows.sort(key=lambda x: x['sid'])
    L = [f'影の運用（シミュレータ vs bot の実際の予想） {a.since}〜  追加 {len(new)}レース']
    if rows:
        S, B = metrics(rows, 'sim'), metrics(rows, 'bot')
        L.append(f'比べたレース {len(rows)}（3連単は8頭以上 {len(S["tll"])}）')
        pct = lambda v: f'{v * 100:.1f}%'
        num = lambda v: f'{v:.3f}'
        L.append(diff_line('本命1着', S['fav'], B['fav'], pct))
        L.append(diff_line('単勝の予想誤差（小さいほど良い）', S['wll'], B['wll'], num))
        L.append(diff_line('3連単の予想誤差（小さいほど良い）', S['tll'], B['tll'], num))
        L.append(diff_line('3連単本命の3頭が上位3頭', S['s3'], B['s3'], pct))
        last = rows[-1]
        L.append(f"直近 R{last['sid']}: 結果 {' → '.join(last['result'])} / "
                 f"シミュ本命 {last['sim_win'][0][0]}（{last['sim_win'][0][1]:.0%}） / "
                 f"bot本命 {last['bot_win'][0][0]}（{last['bot_win'][0][1]:.0%}）")
    text = '\n'.join(L)
    print(text)
    with open(REPORT_TXT, 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
