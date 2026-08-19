# -*- coding: utf-8 -*-
"""harvest_results.py: 確定レースを result API から採取して JSONL に貯める。

    python harvest_results.py --guild 1310885590094450739 --user 613283912105590784 \
                              --from 1978 --count 200

ブラウザは不要（`harvest.js` と違い token も不要。guild と user だけで叩ける）。
`by-id` からステータス・パッシブ・距離を、`result` から着順・score・**timeline**
（100mごとの残スタミナ・消費・疲労補正）を取って1レース1行で保存する。

**timeline を保存するのが harvest.js との最大の違い。**
スタミナ切れの挙動はここにしか無く、モデル改修にはこのデータが要る。

【重要】**ステータスは当日採取のものしか使えない**（2026/08/19 訂正）
`race/by-id` も `race/result` の `base_* + train_*` も、返ってくるのは**そのペットの「今」**。
`logg`（Discordの結果告知＝当時の値）と突き合わせると、不一致率がレースの古さに
比例して上がる: 直近週 6.2% → 8週前 33.2%。当時の値ならこの傾斜は出ない。

  ・使ってよい（採取日に関係なく正しい）:
      timeline / 着順 / score / 距離 / 馬場 / simulation_version /
      **passive_skills**（logg との不一致 1.3%）/ equipment / charm
  ・当日採取したものだけ使ってよい: speed / power / stamina / コンディション

ステータスが要る解析は `logg/`（Discordログ）を使うこと。
`races.jsonl` を学習に混ぜると**精度が落ちる**（8分割CVで実測:
logg のみ スピアマン 0.941 / 1着 92.1% → races.jsonl のみ 0.921 / 84.3%）。


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
import re
import sys
import time
import urllib.error
import urllib.request

import oasis_core as oc

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
    """この行のステータス（SP/PW/ST）が当時の値か。**開催日に採ったものだけ信用する。**

    ⚠ 2026/08/19 訂正: `result` API の `base_* + train_*` は当時の値**ではない**。
      `logg`（Discordの結果告知＝当時の値で確定）と突き合わせると、不一致率が
      レースの古さに比例して上がる（直近週 6.2% → 8週前 33.2%）。当時の値なら
      古さに関係なく一定のはずで、これは「今」の値を返しているサイン。
      `stats_at_race`（base/train が取れたか）は**鮮度の保証にならない**ので使わない。

    ○ 一方 `passive_skills` は当時の値で正しい（logg との不一致 1.3%）。
      パッシブが要るだけの解析なら `need_stats=False` で全行使ってよい。
    """
    h = race.get('harvested_at')
    return bool(h) and str(h) == str(race.get('race_date'))


def load_races(path, need_stats, quiet=False):
    """races.jsonl を読む。need_stats=True ならステータスが腐っていない行だけ返す。

    need_stats=False は timeline / 着順 / score だけ使う用途（これらは常に正しい）。
    """
    rows, stale, excluded = [], 0, 0
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            # 結果が壊れているレースは oasis_core 側の一覧で一元管理する
            if oc.is_excluded_race(r.get('schedule_id'), r.get('race_date'),
                                   r.get('race_time')):
                excluded += 1
                continue
            if need_stats and not is_fresh(r):
                stale += 1
                continue
            rows.append(r)
    if excluded and not quiet:
        print(f'【除外】結果が壊れているレース {excluded}件 を外しました（EXCLUDED_RACES）。')
    if stale and not quiet:
        print(f'【注意】ステータスが当てにならない {stale}レースを除外しました'
              f'（レース後日に採取したもの）。残り {len(rows)}レース。')
        if not rows:
            print('  → 使える行がありません。harvest_daily.bat を数日回してから再実行してください。')
    return rows


def _at_race(r, key):
    """result API のレース当時ステータス = base_* + train_*。両方無ければ None。"""
    b, t = r.get('base_' + key), r.get('train_' + key)
    if b is None and t is None:
        return None
    return (b or 0) + (t or 0)


_DESC_PCT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*[%％]')


def _desc_pct(desc):
    """効果説明の中の最初の「N%」。effect_value との食い違いを見るためだけに使う。"""
    m = _DESC_PCT_RE.search(str(desc or ''))
    return float(m.group(1)) if m else None


def _pick_item(full, slim):
    """装備オブジェクトを1つにまとめる。`name` を持つほう（by-id の完全形）を優先。

    result API 側は {id, rarity, effect_key} しか返さないが、`id` はそちらにしか無いので
    両方あるときは重ねる。片方しか無ければそれをそのまま使う。
    """
    if not isinstance(full, dict) or not full.get('name'):
        full, slim = slim, full
    if not isinstance(full, dict):
        return full or slim or None
    out = dict(full)
    if isinstance(slim, dict):
        for k, v in slim.items():
            out.setdefault(k, v)
    return out


def item_catalog(path):
    """races.jsonl を走査して、見つかった装備・お守りの一覧を作る。

    アイテムは自分の仕様（stat_* / effect_key / effect_value / 説明）を丸ごと持っているので、
    **カタログは貯めるものではなく、貯まったデータから引き出すもの**。
    キーは template_key（無ければ名前）。同じテンプレでもレアリティで数値が変わるので、
    レアリティごとに1件にする。
    """
    cat = {}
    for line in open(path, encoding='utf-8'):
        try:
            race = json.loads(line)
        except Exception:
            continue
        for h in race.get('horses') or []:
            for slot in ('equipment', 'charm'):
                it = h.get(slot)
                if not isinstance(it, dict):
                    continue
                key = f"{it.get('template_key') or it.get('name') or it.get('id')}"
                key = f"{key}/{it.get('rarity') or ''}"
                e = cat.setdefault(key, {'slot': slot, 'n': 0, 'ids': [],
                                         'first_seen': race.get('race_date')})
                e['n'] += 1
                if it.get('id') is not None and it['id'] not in e['ids']:
                    e['ids'].append(it['id'])      # 同じテンプレの個体は複数ある
                for k in ('name', 'template_key', 'rarity', 'rarity_label', 'id',
                          'item_type', 'effect_description', 'effect_label',
                          'passive_skill_key'):
                    if it.get(k) is not None and e.get(k) is None:
                        e[k] = it[k]
                # ⚠ 同じテンプレ・同じレアリティでも、ステータスも効果値も**個体ごとに違う**。
                #   交易所の実例: 鉄の手甲[ノーマル] が「中盤加速2.1%」と「末脚1.9%」の2種類、
                #   布のサッシュ[ノーマル] が PW+1/2.3% と ST+1/2.1%。
                #   1件目で固定すると嘘のカタログになるので、観測された幅を持つ。
                for k in ('stat_speed', 'stat_power', 'stat_stamina', 'effect_value'):
                    v = it.get(k)
                    if v is None:
                        continue
                    lo, hi = e.setdefault('range_' + k, [v, v])
                    e['range_' + k] = [min(lo, v), max(hi, v)]
                if it.get('effect_key'):
                    e.setdefault('effect_keys', [])
                    if it['effect_key'] not in e['effect_keys']:
                        e['effect_keys'].append(it['effect_key'])
                d = str(race.get('race_date') or '')
                if d and d < str(e['first_seen'] or 'z'):
                    e['first_seen'] = d
    return cat


def merge_horse(h, r):
    """by-id の1頭 `h` と result の1頭 `r` を1行にまとめる。

    ステータスとパッシブは **result 優先**（レース当時の値）。無ければ by-id に落ちる。
    コンディションだけは result に無いので by-id の「今」の値しか入らない。
    """
    at = {k: _at_race(r, k) for k in ('speed', 'power', 'stamina')}
    fresh = all(v is not None for v in at.values())
    ps = r.get('passive_skills')
    prev = [x for x in (h.get('passive_skill'), h.get('passive_skill_2')) if x]
    if not isinstance(ps, list) or not ps:
        ps = prev
    return {
        'pet_id': h.get('pet_id'), 'name': h.get('display_name') or h.get('name'),
        'adult_key': h.get('adult_key'),
        'speed': at['speed'] if fresh else h.get('speed'),
        'power': at['power'] if fresh else h.get('power'),
        'stamina': at['stamina'] if fresh else h.get('stamina'),
        'stats_at_race': fresh,          # True ならレース当時の値（採取日に依存しない）
        # 内訳も残す（素の個体値と特訓ぶんを分けて見たいとき用）
        'base_speed': r.get('base_speed'), 'train_speed': r.get('train_speed'),
        'base_power': r.get('base_power'), 'train_power': r.get('train_power'),
        'base_stamina': r.get('base_stamina'), 'train_stamina': r.get('train_stamina'),
        # by-id（＝「今」）の値。当時との差分を見る用。
        'speed_now': h.get('speed'), 'power_now': h.get('power'),
        'stamina_now': h.get('stamina'), 'item_bonus': h.get('item_bonus'),
        # ⚠ 当時の値ではない。`h` は by-id の1頭（condition_label）だが、
        # --refresh では保存済みの行（condition）が渡る。両方見ないと値を消してしまう。
        'condition': h.get('condition_label') or h.get('condition') or '普通',
        'passive_skill': ps[0] if len(ps) > 0 else None,
        'passive_skill_2': ps[1] if len(ps) > 1 else None,
        'passive_skills': ps,                     # 3つ以上に増えても取りこぼさない
        # result の passive_skills は「当時」、by-id の2枠は「今」。食い違ったら両方残す
        # （2枠目は後から生えるので、古いレースでは当時1枠が正しい）。
        'passive_skills_now': prev if prev != ps else None,
        'initial_activated_passives': r.get('initial_activated_passives'),
        # ⚠ result API の装備は {id, rarity, effect_key} だけの**簡略形**。
        #   by-id は name / stat_* / effect_value / effect_description まで入った**完全形**。
        #   名前や倍率が要るので完全形（name を持つほう）を優先し、両方あれば併合する。
        'equipment': _pick_item(h.get('equipment'), r.get('equipment')),
        'charm': _pick_item(h.get('charm'), r.get('charm')),
        'initial_activated_charms': r.get('initial_activated_charms'),
        'odds': h.get('odds'),
        'rank': r.get('rank'), 'score': r.get('score'),
        'finish_time': r.get('finish_time'), 'stamina_after': r.get('stamina_after'),
        'simulation_version': r.get('simulation_version'),
        'timeline': r.get('timeline'),          # ← 肝。区間ごとの残スタミナ・疲労補正
    }


def refresh(guild, user, path):
    """既存の races.jsonl を result API で採り直して、当時のステータスに入れ替える。

    by-id は叩かない（距離・馬場・馬名は既存行のものを使う）ので1レース1リクエスト。
    書き込みは一時ファイル経由なので、途中で落ちても原本は無事。
    """
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows:
        print(f'[NG] {path} に読める行がありません。')
        return 1
    print(f'{len(rows)} レースを result API で採り直します。')
    ok = skip = miss = 0
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for i, race in enumerate(rows):
            hs = race.get('horses') or []
            if hs and all(h.get('stats_at_race') for h in hs):
                skip += 1
            else:
                res = get(f"{API}/api/race/result/{guild}/{race.get('race_date')}"
                          f"/{race.get('schedule_id')}?user={user}")
                rr = (res or {}).get('results')
                new = []
                if isinstance(rr, list) and rr:
                    by_pid = {x.get('pet_id'): x for x in rr}
                    # result に居ない馬は**落とさず元の行のまま残す**（黙って消える事故を防ぐ）
                    new = [merge_horse(h, by_pid[h['pet_id']])
                           if h.get('pet_id') in by_pid else h for h in hs]
                if any(x.get('stats_at_race') for x in new):
                    race['horses'] = new
                    ok += 1
                else:
                    miss += 1
                time.sleep(0.4)
            f.write(json.dumps(race, ensure_ascii=False) + '\n')
            sys.stdout.write(f'\r  {i+1}/{len(rows)}  更新{ok} / 既に当時の値{skip}'
                             f' / 取得失敗{miss}   ')
            sys.stdout.flush()
    os.replace(tmp, path)
    print(f'\n完了: {ok}レースを当時のステータスに入れ替えました'
          f'（{skip}件は既に当時の値 / {miss}件は取得できず旧値のまま）。')
    return 0


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
        horses.append(merge_horse(h, r))
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
    ap.add_argument('--guild', default=None)
    ap.add_argument('--user', default=None)
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
    ap.add_argument('--items', action='store_true',
                    help='採取済みの races.jsonl から装備・お守りの一覧を作って '
                         '--item-out に書き出す（APIは叩かない）')
    ap.add_argument('--item-out', default='item_spec.json',
                    help='--items の出力先')
    ap.add_argument('--refresh', action='store_true',
                    help='新規取得はせず、既存の races.jsonl を result API で採り直して'
                         'レース当時のステータス（base_*+train_*）に入れ替える')
    a = ap.parse_args()

    if not a.items and not (a.guild and a.user):
        print('[NG] --guild と --user が要ります（--items のときだけ省略できます）')
        return 1

    if a.items:
        cat = item_catalog(a.out)
        if not cat:
            print(f'{a.out} に装備・お守りは1件もありませんでした。')
            return 0
        with open(a.item_out, 'w', encoding='utf-8') as f:
            json.dump(cat, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f'{len(cat)} 種類を {a.item_out} に書きました。\n')
        hdr = ('枠', 'テンプレ', 'レア', 'ステ補正', '効果キー', '値', '個体', '頭', '初出')
        print('%-10s %-22s %-8s %-10s %-22s %5s %5s %4s %s' % hdr)
        for k, v in sorted(cat.items(),
                           key=lambda kv: (kv[1].get('item_type') or kv[1]['slot'],
                                           kv[1].get('effect_key') or '', kv[0])):
            def _rng(key, lab):
                r = v.get('range_' + key)
                if not r or not any(r):
                    return None
                return f'{lab}+{r[0]:g}' if r[0] == r[1] else f'{lab}+{r[0]:g}〜{r[1]:g}'
            st = '/'.join(x for x in (_rng('stat_speed', 'SP'), _rng('stat_power', 'PW'),
                                      _rng('stat_stamina', 'ST')) if x) or '-'
            _er = v.get('range_effect_value') or [None, None]
            ev = _er[0] if _er[0] == _er[1] else None
            print('%-10s %-22s %-8s %-10s %-22s %5s %5d %4d %s' % (
                v.get('item_type') or v['slot'], v.get('name') or v.get('template_key') or k,
                v.get('rarity_label') or v.get('rarity') or '', st,
                '/'.join(v.get('effect_keys') or []),
                ('%g' % ev) if ev is not None
                else ('%g〜%g' % tuple(_er) if _er[0] is not None else ''),
                len(v.get('ids') or []), v['n'], v.get('first_seen') or ''))
            if v.get('effect_description'):
                # ⚠ effect_value と説明文の%が食い違うものがある（実測: charm_balance は
                #   effect_value=2.0 に対し説明は「全ステータスが常時1.1%上昇」）。
                #   モデルは**説明文のほう**を使うので、食い違いは目印として出しておく。
                pct = _desc_pct(v['effect_description'])
                warn = ''
                if pct is not None and ev is not None and abs(pct - float(ev)) > 0.01:
                    warn = f'   ⚠ 効果値{ev}≠説明{pct}%（説明文を採用）'
                print('    └ ' + v['effect_description'] + warn)
        n_named = sum(1 for v in cat.values() if v.get('name'))
        if n_named < len(cat):
            print(f'\n※ 日本語名が付いているのは {n_named}/{len(cat)} 種類です。'
                  '名前・効果説明は by-id 側にしか無いので、')
            print('  この先の日次採取（--forward）で装備した馬を拾うたびに埋まります。')
        return 0

    if a.refresh:
        return refresh(a.guild, a.user, a.out)

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
