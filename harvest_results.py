# -*- coding: utf-8 -*-
"""harvest_results.py: 確定レースを result API から採取して JSONL に貯める。

    python harvest_results.py --guild 1310885590094450739 --user 613283912105590784 \
                              --from 1978 --count 200

ブラウザは不要（`harvest.js` と違い token も不要。guild と user だけで叩ける）。
`by-id` からステータス・パッシブ・距離を、`result` から着順・score・**timeline**
（100mごとの残スタミナ・消費・疲労補正）を取って1レース1行で保存する。

**timeline を保存するのが harvest.js との最大の違い。**
スタミナ切れの挙動はここにしか無く、モデル改修にはこのデータが要る。

【重要】**ステータス（speed/power/stamina）は当てにならない**
`race/by-id` は**そのペットの「今」のステータス**を返す。過去のレースを採ると、
レース当時ではなく現在の（育って上がった）値が付いてくる。
実測: 出力の予測精度がレースの古さに比例して落ちる（08-14 のレース 0.957 →
07-28 のレース 0.660）。

したがって、ここで採ったデータは
  ・使ってよい: timeline / 着順 / score / 距離 / 馬場 / simulation_version
  ・使ってはいけない: speed / power / stamina / passive / condition
ステータスが要る解析は `logg/`（Discordログ＝レース当時の値）を使うこと。

再実行すると既に取った schedule_id は飛ばすので、毎日追記していける。

**毎日の自動採取**（Windows タスクスケジューラ用。`--from` も要らない）:

    python harvest_results.py --guild <G> --user <U> --forward --count 30

前回の続き（既存の最大ID+1）から前へ進み、まだ走っていないレースに当たったら止まる。
1日6レースなので --count 30 なら数日空けても追いつく。
**当日中に回すこと**（上のステータス劣化の警告を参照）。
"""
# 出力を harvest.log へリダイレクトすると、Windows では cp932 になって
# 記号（警告マークなど）で落ちる。ログは `type` で読めるよう cp932 のまま、
# 書けない文字だけ置き換えて落ちないようにする。
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

import argparse
import datetime
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


def is_fresh(race):
    """この行のステータス（SP/PW/ST・パッシブ・コンディション）が当時の値か。

    `by-id` は「今」の値を返すので、開催日と採取日が同じときだけ信用できる。
    `harvested_at` が無い行は 2026/08/15 以前にまとめて採ったもの＝**信用しない**。
    """
    h = race.get('harvested_at')
    return bool(h) and str(h) == str(race.get('race_date'))


def load_races(path, need_stats, quiet=False):
    """races.jsonl を読む。need_stats=True ならステータスが腐っていない行だけ返す。

    need_stats=False は timeline / 着順 / score だけ使う用途（これらは常に正しい）。
    """
    rows, stale = [], 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if need_stats and not is_fresh(r):
                stale += 1
                continue
            rows.append(r)
    if stale and not quiet:
        print(f'【注意】ステータスが当てにならない {stale}レースを除外しました'
              f'（レース後日に採取したもの）。残り {len(rows)}レース。')
        if not rows:
            print('  → 使える行がありません。harvest_daily.bat を数日回してから再実行してください。')
    return rows


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
        # 採取日。ステータスは「今」の値なので、race_date と離れているほど当てにならない。
        # 当日採取なら 94%、1日後 78%、2日後 67%、2週間後は 50% しか一致しない（実測）。
        'harvested_at': datetime.date.today().isoformat(),
        'schedule_id': sid, 'race_date': date, 'race_time': info.get('race_time'),
        'distance': info.get('distance'), 'surface': info.get('surface'),
        'ground': info.get('ground') or info.get('track_condition'),
        'n_field': len(horses), 'horses': horses,
    }, 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--guild', required=True)
    ap.add_argument('--user', required=True)
    ap.add_argument('--from', dest='start', type=int, default=None,
                    help='ここから schedule_id を遡る（--forward なら進む）。'
                         '--forward で省略すると「既に取った最大ID+1」から始める')
    ap.add_argument('--forward', action='store_true',
                    help='IDを増やす方向に進む。毎日の自動採取はこちら'
                         '（前回の続きから、まだ終わっていないレースに当たったら止まる）')
    ap.add_argument('--count', type=int, default=200)
    ap.add_argument('--out', default='races.jsonl')
    ap.add_argument('--sleep', type=float, default=0.4, help='1レースごとの待ち（秒）')
    ap.add_argument('--stop-after-misses', type=int, default=20,
                    help='欠番（--forward では未確定も）がこれだけ続いたら打ち切り')
    a = ap.parse_args()

    done = load_done(a.out)
    if done:
        print(f'既に {len(done)} レース取得済み。飛ばします。')
    if a.start is None:
        if not a.forward or not done:
            print('[NG] --from が要ります（--forward かつ既存データがある場合のみ省略可）')
            return 1
        a.start = max(done) + 1
        print(f'続きから: schedule_id {a.start} 以降を見ます。')
    ok = miss = unfin = skip = stale = 0
    today = datetime.date.today().isoformat()
    streak = 0
    with open(a.out, 'a', encoding='utf-8') as f:
        for i in range(a.count):
            sid = a.start + i if a.forward else a.start - i
            if sid in done:
                skip += 1
                continue
            race, state = fetch_race(a.guild, a.user, sid)
            if state == 'ok':
                f.write(json.dumps(race, ensure_ascii=False) + '\n')
                f.flush()
                ok += 1
                stale += int(str(race.get('race_date')) != today)
                streak = 0
            elif state in ('unfinished', 'partial'):
                unfin += 1
                # 前方向に進んでいるとき、未確定＝これから走るレース。
                # その先も未確定なので、続いたら打ち切る。
                streak = streak + 1 if a.forward else 0
            else:
                miss += 1
                streak += 1
            sys.stdout.write(f'\r  {i+1}/{a.count}  確定{ok} / 未確定{unfin} / 欠番{miss}'
                             f' / 既存{skip}   (sid={sid})   ')
            sys.stdout.flush()
            if streak >= a.stop_after_misses:
                print(f'\n{"未確定・欠番" if a.forward else "欠番"} が {streak} 件続いたので'
                      f'打ち切りました。')
                break
            time.sleep(a.sleep)
    print(f'\n完了: {a.out} に {ok} レースを追加（合計 {len(load_done(a.out))} レース）')
    if stale:
        print(f'【注意】うち {stale} レースは開催日より後に採取したので、'
              f'**ステータス（SP/PW/ST）は当てになりません**。')
        print('  timeline・着順・score は正しいので、そちらだけ使ってください。')
        print('  毎日 --count 10 くらいで回すと、当日採取だけで貯まります。')


if __name__ == '__main__':
    # bare main() だと戻り値が捨てられ、失敗しても終了コード0になる。
    # harvest_daily.bat が `exit /b %ERRORLEVEL%` を返すので、タスクスケジューラが
    # 「成功」と記録して**何も採取していないことに永久に気づけない**。
    sys.exit(main() or 0)
