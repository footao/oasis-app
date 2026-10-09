"""見張り: レース結果の採取のあとに回して、気にすべきことだけを alerts.txt に書く。

    python watch.py            … 前回から増えたレースを調べる（harvest_daily.bat から呼ぶ）
    python watch.py --all      … 記録のある全レースを調べ直す（書き込みはしない・確認用）

調べること:
  1. 大きく外れたレース: 本命（予想80%以上）が負けた / 勝ち馬の予想が5%未満 / 予想60%以上の3連単を買って外れた
     → race_review.py の要約（どこで差がついたか）を付ける
  2. シミュに何も反映されなかった装備の効果（新しい装備・書き方の変化）
  3. 精度の低下: 直近40レースの本命1着が、予想の合計より明らかに少ない（z ≤ −2）
     ゲームのアップデートで仕様が変わったときに気づくため
alerts.txt は clockautobuyy.py が次のレポートのあとに Discord へ送る（送った位置は alerts_sent.txt）。
"""
import json
import math
import os
import re
import sys

import oasis_core as oc
import race_sim as rs

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, '..', 'autooo', 'reports.txt')     # C:\oasissfable\autooo\reports.txt
STATE = os.path.join(HERE, 'watch_state.json')
ALERTS = os.path.join(HERE, 'alerts.txt')
DRIFT_N, DRIFT_Z = 40, -2.0


def load_reports(path):
    """reports.txt の JSON 行 → {sid: 最後の行}。"""
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if line.startswith('{'):
                try:
                    d = json.loads(line)
                    out[d['sid']] = d
                except (ValueError, KeyError):
                    pass
    return out


def unknown_effects(r, spec):
    """シミュに何も反映されなかった装備・お守りの効果（説明文に % があるのに、外しても計算が同じ）。"""
    out = []
    for h in r.get('horses') or []:
        for slot in ('equipment', 'charm'):
            it = h.get(slot)
            desc = str((it or {}).get('effect_description') or '')
            if not isinstance(it, dict) or '%' not in desc:
                continue
            m = re.search(r'(芝|ダート|短距離|マイル|中距離|長距離)で', desc)
            if m and m.group(1) not in (r['distance'], r['surface']):
                continue                       # 別の馬場・距離向けの効果（このレースでは効かないのが正しい）
            a = rs.horse_profile(h, r['distance'], r['surface'], spec, False)
            b = rs.horse_profile(dict(h, **{slot: None}), r['distance'], r['surface'], spec, False)
            if repr(a) == repr(b):
                out.append(f"{it.get('effect_label')}：{it.get('effect_description')}（{h.get('name')}）")
    return out


def check_race(r, rep):
    """1レースの外れ方 → 警告文のリスト。"""
    hs = sorted(r['horses'], key=lambda h: h.get('rank') or 99)
    if not hs or not hs[0].get('rank'):
        return []
    win_name, top3 = hs[0]['name'], [h['name'] for h in hs[:3]]
    wc = rep.get('wc') or []
    pw = {c['n']: c.get('p') or 0.0 for c in wc}
    msgs = []
    if wc and wc[0].get('p', 0) >= 0.8 and wc[0]['n'] != win_name:
        msgs.append(f"本命 {wc[0]['n']}（予想{wc[0]['p']:.0%}）が負けた → 1着 {win_name}（予想{pw.get(win_name, 0):.0%}）")
    elif wc and pw.get(win_name, 0) < 0.05:
        msgs.append(f"予想外の勝ち馬 {win_name}（予想{pw.get(win_name, 0):.0%}・オッズ{hs[0].get('odds')}）")
    for b in rep.get('bets') or []:
        if b.get('t') == 'tri' and (b.get('p') or 0) >= 0.6 and b.get('n') != top3:
            msgs.append(f"3連単 {'→'.join(b['n'])}（予想{b['p']:.0%}・{b['u']}口）が外れ → 実際 {'→'.join(top3)}")
    return msgs


def drift(races, reps):
    """直近 DRIFT_N レース（シミュで予想したもの）の本命1着: 実際の数と予想の合計を比べる。→ (z, 実際, 予想, レース数)"""
    rows = []
    for sid in sorted(reps):
        rep, r = reps[sid], races.get(sid)
        wc = rep.get('wc') or []
        if rep.get('m') != 'sim' or not r or not wc:
            continue
        hs = sorted(r['horses'], key=lambda h: h.get('rank') or 99)
        if hs and hs[0].get('rank'):
            rows.append((wc[0].get('p') or 0.0, wc[0]['n'] == hs[0]['name']))
    rows = rows[-DRIFT_N:]
    if len(rows) < 20:
        return None
    exp = sum(p for p, _ in rows)
    obs = sum(1 for _, w in rows if w)
    var = sum(p * (1 - p) for p, _ in rows) or 1e-9
    return (obs - exp) / math.sqrt(var), obs, exp, len(rows)


def summary(r, raw):
    """race_review の要約（最初の3行と「予想と逆になった組」）。"""
    import race_review
    txt = race_review.review(r, raw, n_sim=4000).split('\n')
    tail = txt[txt.index(next(l for l in txt if l.startswith('予想と逆になった組'))) + 1:]
    return '\n'.join(txt[:3] + [l for l in tail if l.strip()])


def run(check_all=False):
    raw = [json.loads(l) for l in open(os.path.join(HERE, 'races.jsonl'), encoding='utf-8') if l.strip()]
    races = {r['schedule_id']: r for r in raw}
    reps = load_reports(REPORTS)
    st = json.load(open(STATE, encoding='utf-8')) if os.path.exists(STATE) and not check_all else {}
    last = st.get('last_sid', max(races) - 1 if not check_all else 0)
    spec = oc.default_spec()
    blocks = []
    new = sorted(s for s in races if s > last and races[s].get('horses'))
    for sid in new:
        r = races[sid]
        msgs = check_race(r, reps[sid]) if sid in reps else []
        unk = unknown_effects(r, spec) if r.get('distance') in rs.NSEG else []
        if msgs and r.get('distance') in rs.NSEG and not check_all:
            try:
                msgs.append(summary(r, raw))
            except Exception as e:             # 要約が失敗しても警告そのものは出す
                msgs.append(f'（答え合わせの要約に失敗: {e}）')
        msgs += [f'シミュに反映されない効果: {u}' for u in unk]
        if msgs:
            blocks.append(f"⚠ R{sid} {r['race_date']} {r['race_time']}\n" + '\n'.join(msgs))
    d = drift(races, reps)
    if d and d[0] <= DRIFT_Z and (new and new[-1] - st.get('drift_alert_sid', 0) >= 6 or check_all):
        blocks.append(f"⚠ 精度の低下: 直近{d[3]}レースの本命1着 {d[1]}回（予想の合計 {d[2]:.1f}回・z={d[0]:.1f}）。"
                      "ゲームの仕様変更を疑って race_review.py で直近の外れを見てください")
        st['drift_alert_sid'] = new[-1] if new else 0
    if new and not check_all:
        st['last_sid'] = new[-1]
        json.dump(st, open(STATE, 'w', encoding='utf-8'))
        if blocks:
            with open(ALERTS, 'a', encoding='utf-8') as f:
                f.write('\n\n'.join(blocks) + '\n\n')
    return blocks, d


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # Windows のログ（cp932）で ⚠ などが落ちないように
    blocks, d = run(check_all='--all' in sys.argv)
    print('\n\n'.join(blocks) if blocks else '気にすべきことはありません')
    if d:
        print(f'（直近{d[3]}レースの本命1着 {d[1]}回 / 予想の合計 {d[2]:.1f}回 / z={d[0]:+.1f}）')
    # 自己チェック: 本命80%が負けたレースは必ず警告になる
    fake = {'horses': [{'name': 'B', 'rank': 1, 'odds': 9.0}, {'name': 'A', 'rank': 2}, {'name': 'C', 'rank': 3}]}
    assert check_race(fake, {'wc': [{'n': 'A', 'p': 0.9}, {'n': 'B', 'p': 0.1}]})
    assert not check_race(fake, {'wc': [{'n': 'B', 'p': 0.9}]})
