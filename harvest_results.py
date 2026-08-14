# -*- coding: utf-8 -*-
"""harvest_results.py — 確定レースを result API から採取して JSONL に貯める。

    python harvest_results.py --guild 1310885590094450739 --user 613283912105590784 \
                              --from 1978 --count 200

ブラウザは不要（`harvest.js` と違い token も不要。guild と user だけで叩ける）。
`by-id` からステータス・パッシブ・距離を、`result` から着順・score・**timeline**
（100mごとの残スタミナ・消費・疲労補正）を取って1レース1行で保存する。

**timeline を保存するのが harvest.js との最大の違い。**
スタミナ切れの挙動はここにしか無く、モデル改修にはこのデータが要る。

再実行すると既に取った schedule_id は飛ばすので、毎日追記していける。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = 'https://api.oasis.red'
UA = 'oasis-harvest/1.0'


def get(url, timeout=20, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                      # 欠番。リトライしても無駄
            if i == retries - 1:
                return None
        except Exception:
            if i == retries - 1:
                return None
        time.sleep(1.5 * (i + 1))
    return None


def load_done(path):
    done = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    done.add(int(json.loads(line)['schedule_id']))
                except Exception:
                    pass
    return done


def fetch_race(guild, user, sid):
    info = get(f'{API}/api/race/by-id/{guild}/{sid}?user={user}')
    if not info or not isinstance(info.get('pets'), list) or not info['pets']:
        return None, 'nodata'
    date = info.get('race_date')
    if not date:
        return None, 'nodate'
    res = get(f'{API}/api/race/result/{guild}/{date}/{sid}?user={user}')
    rr = (res or {}).get('results')
    if not isinstance(rr, list) or not rr:
        return None, 'unfinished'

    by_id = {r.get('pet_id'): r for r in rr}
    horses = []
    for h in info['pets']:
        r = by_id.get(h.get('pet_id'))
        if not r or r.get('rank') is None or r.get('score') is None:
            continue
        horses.append({
            'pet_id': h.get('pet_id'), 'name': h.get('display_name') or h.get('name'),
            'adult_key': h.get('adult_key'),
            'speed': h.get('speed'), 'power': h.get('power'), 'stamina': h.get('stamina'),
            'condition': h.get('condition_label') or '普通',
            'passive_skill': h.get('passive_skill'), 'passive_skill_2': h.get('passive_skill_2'),
            'odds': h.get('odds'),
            'rank': r.get('rank'), 'score': r.get('score'),
            'finish_time': r.get('finish_time'), 'stamina_after': r.get('stamina_after'),
            'simulation_version': r.get('simulation_version'),
            'timeline': r.get('timeline'),          # ← 肝。区間ごとの残スタミナ・疲労補正
        })
    if not horses:
        return None, 'partial'
    return {
        'schedule_id': sid, 'race_date': date, 'race_time': info.get('race_time'),
        'distance': info.get('distance'), 'surface': info.get('surface'),
        'ground': info.get('ground') or info.get('track_condition'),
        'n_field': len(horses), 'horses': horses,
    }, 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--guild', required=True)
    ap.add_argument('--user', required=True)
    ap.add_argument('--from', dest='start', type=int, required=True,
                    help='ここから schedule_id を小さい方へ遡る')
    ap.add_argument('--count', type=int, default=200)
    ap.add_argument('--out', default='races.jsonl')
    ap.add_argument('--sleep', type=float, default=0.4, help='1レースごとの待ち（秒）')
    ap.add_argument('--stop-after-misses', type=int, default=20,
                    help='欠番がこれだけ続いたら打ち切り')
    a = ap.parse_args()

    done = load_done(a.out)
    if done:
        print(f'既に {len(done)} レース取得済み。飛ばします。')
    ok = miss = unfin = skip = 0
    streak = 0
    with open(a.out, 'a', encoding='utf-8') as f:
        for i in range(a.count):
            sid = a.start - i
            if sid in done:
                skip += 1
                continue
            race, state = fetch_race(a.guild, a.user, sid)
            if state == 'ok':
                f.write(json.dumps(race, ensure_ascii=False) + '\n')
                f.flush()
                ok += 1
                streak = 0
            elif state in ('unfinished', 'partial'):
                unfin += 1
                streak = 0
            else:
                miss += 1
                streak += 1
            sys.stdout.write(f'\r  {i+1}/{a.count}  確定{ok} / 未確定{unfin} / 欠番{miss}'
                             f' / 既存{skip}   (sid={sid})   ')
            sys.stdout.flush()
            if streak >= a.stop_after_misses:
                print(f'\n欠番が {streak} 件続いたので打ち切りました。')
                break
            time.sleep(a.sleep)
    print(f'\n完了: {a.out} に {ok} レースを追加（合計 {len(load_done(a.out))} レース）')


if __name__ == '__main__':
    main()
