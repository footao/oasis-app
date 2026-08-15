# -*- coding: utf-8 -*-
"""fetch_race.py — これから走るレースの出走表を API から取る。**トークン不要**。

    python fetch_race.py --guild 1310885590094450739 --user 613283912105590784 --race 1993

`race/by-id` は guild と user だけで叩ける（購入だけがトークンを要る）。
出力は Streamlit の🎯予測タブにそのまま貼れる書式。

⚠ 3連単のオッズはここには含まれない（購入画面にしか無い）。
   単勝オッズと出走表だけなので、出せるのは「モデルの本命」と「単勝のEV」まで。
   3連単の買い目まで出すなら、購入画面でブックマークレットを使うこと。

⚠ ステータスは「今」の値。**これから走るレースなら、それが正しい値**。
   終わったレースを採るときは harvest_results.py（あちらは古いと値が腐る）。
"""
import argparse
import csv
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc               # noqa: E402
from harvest_results import get, API  # noqa: E402

COLS = ['馬名', 'レース距離', '馬場', '地面', 'コンディション',
        'SPEED', 'POWER', 'STAMINA', 'パッシブスキル1', 'パッシブスキル2',
        '単勝オッズ', '成体種']


def build(info):
    pets = info.get('pets') or []
    if not pets:
        return None
    dist, track = info.get('distance'), info.get('surface')
    ground = info.get('ground') or info.get('track_condition') or ''
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(COLS)
    for h in pets:
        p1 = oc.passive_from_code(h.get('passive_skill')) or ''
        p2 = oc.passive_from_code(h.get('passive_skill_2')) or ''
        od = h.get('odds')
        w.writerow([h.get('display_name') or h.get('name'), dist, track, ground,
                    h.get('condition_label') or '普通',
                    h.get('speed'), h.get('power'), h.get('stamina'), p1, p2,
                    '' if od in (None, '') else od,
                    h.get('adult_key') or ''])
    return '=== 出走馬一覧 ===\n' + buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--guild', required=True)
    ap.add_argument('--user', required=True)
    ap.add_argument('--race', type=int, required=True, help='schedule_id')
    ap.add_argument('--out', default='', help='ファイルにも保存する')
    a = ap.parse_args()

    info = get(f'{API}/api/race/by-id/{a.guild}/{a.race}?user={a.user}')
    if not info:
        print(f'❌ レース {a.race} を取得できませんでした（IDを確認してください）')
        return 1
    text = build(info)
    if not text:
        print(f'❌ レース {a.race} に出走馬がいません（まだ枠が埋まっていない？）')
        return 1
    print(f'# {info.get("race_date")} {info.get("race_time")} '
          f'{info.get("distance")}／{info.get("surface")}／'
          f'{len(info.get("pets") or [])}頭\n', file=sys.stderr)
    print(text)
    if a.out:
        io.open(os.path.join(HERE, a.out), 'w', encoding='utf-8').write(text)
        print(f'\n→ {a.out} に保存しました', file=sys.stderr)
    print('この上の「=== 出走馬一覧 ===」以下をコピーして予測タブに貼ってください。\n'
          '3連単のオッズは購入画面にしか無いので、それが要るならブックマークレットを。',
          file=sys.stderr)
    return 0


def _selfcheck():
    """作った書式を parse_unified が読めるか（APIに繋がらない環境でも回せる）。"""
    info = {'distance': 'マイル', 'surface': 'ダート', 'ground': '良',
            'race_date': '2026-08-16', 'race_time': '09:00',
            'pets': [
                {'display_name': 'おいら', 'adult_key': 'k1', 'speed': 155, 'power': 46,
                 'stamina': 48, 'condition_label': '好調', 'passive_skill': 'speed_star',
                 'passive_skill_2': 'speed_l', 'odds': 2.5},
                {'display_name': 'カレー', 'adult_key': 'k2', 'speed': 154, 'power': 48,
                 'stamina': 50, 'condition_label': '普通', 'passive_skill': 'speed_l',
                 'passive_skill_2': None, 'odds': 3.1},
            ]}
    text = build(info)
    got = oc.parse_unified(text)
    hs = got['horses'] if isinstance(got, dict) else got[0]
    assert len(hs) == 2, got
    assert hs[0]['name'] == 'おいら' and hs[0]['speed'] == 155, hs[0]
    assert hs[0]['condition'] == '好調', hs[0]
    assert 'スピードスター' in hs[0]['passives'], hs[0]
    assert hs[0]['species'] == 'k1', hs[0]
    assert abs(hs[0]['odds'] - 2.5) < 1e-9, hs[0]
    assert hs[1]['passives'] == ('スピード大アップ',), hs[1]     # 2枠目が空でも壊れない
    print('selfcheck OK  ', {k: hs[0][k] for k in ('name', 'speed', 'condition', 'passives')})


if __name__ == '__main__':
    if '--selfcheck' in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
