# -*- coding: utf-8 -*-
"""settle_bets.py — ベットログの未精算レースを、結果APIから自動で精算する。

    python settle_bets.py --guild <G> --user <U> --dry     # 何をするか見るだけ
    python settle_bets.py --guild <G> --user <U>           # 実際に精算する

手で入れていたもの:
  ・1着/2着/3着   → 結果APIから自動で取る
  ・最終オッズ     → /api/trifecta/odds から取り、初期プール金の補正を掛ける

**レースIDが schedule_id の行だけ**が対象。予測タブのレースID欄は
貼り付けデータから自動で埋まるので、そのまま記録していれば対象になる。
日時で採番した古い行は対象外（手で精算してください）。

最終オッズが取れなければ購入時オッズで概算する（payout_kind='概算'）。
購入は締切2分前に行っているので、購入時オッズはほぼ最終値のはず。
**その「ほぼ」を実測する**ため、両方取れた行では差を集計して最後に出す。
差が小さいと確認できたら、最終オッズの取得は無くても困らない。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc                     # noqa: E402
from harvest_results import get, API        # noqa: E402


def race_result(guild, user, sid):
    """(1〜3着の馬名, pet_id, 全馬のオッズ, race_date) を返す。"""
    info = get(f'{API}/api/race/by-id/{guild}/{sid}?user={user}')
    if not info or not info.get('race_date') or not info.get('pets'):
        return None
    res = get(f'{API}/api/race/result/{guild}/{info["race_date"]}/{sid}?user={user}')
    rr = (res or {}).get('results')
    if not isinstance(rr, list) or not rr:
        return None
    name_of = {p.get('pet_id'): (p.get('display_name') or p.get('name'))
               for p in info['pets']}
    top = sorted((r for r in rr if r.get('rank')), key=lambda r: r['rank'])[:3]
    if len(top) < 3:
        return None
    return {
        'date': info['race_date'],
        'names': [name_of.get(r['pet_id']) for r in top],
        'pet_ids': [r['pet_id'] for r in top],
        'win_odds': {name_of.get(p.get('pet_id')): p.get('odds') for p in info['pets']},
    }


def final_trifecta_odds(guild, sid, pet_ids):
    """確定した並びの最終オッズ。初期プール金の補正を掛けて返す。取れなければ None。"""
    pool = (get(f'{API}/api/trifecta/pool?guild={guild}&schedule_id={sid}') or {}).get('pool')
    d = get(f'{API}/api/trifecta/odds?guild={guild}&schedule_id={sid}'
            f'&first={pet_ids[0]}&second={pet_ids[1]}&third={pet_ids[2]}')
    od = (d or {}).get('odds')
    if not isinstance(od, (int, float)) or od <= 0:
        return None, pool
    # 表示オッズは (プール総額 − 初期プール金) 基準。払戻はプール総額から出る。
    return (oc.true_trifecta_odds(float(od), float(pool)) if pool else float(od)), pool


def final_win_odds(win_odds, winner):
    """単勝の最終オッズ。市場として使えないものは None（＝購入時オッズで概算する）。

    弾く条件:
      ・Σ(1/od) が 1 付近でない → まだ投票が入りきっていない
      ・全馬同じオッズ → 誰も賭けていない
      ・1.0倍以下が混ざる → 1.0 も 1.5 も「価格」ではなく下限の表示値。
        これを採用すると `BetLog.settle` が購入時オッズを 1.0 で上書きし、
        的中したのに payout=stake（利益ゼロ）で記録されて元の値も消える。
    """
    vals = [float(v) for v in win_odds.values() if v and float(v) > 0]
    if len(vals) < 2 or min(vals) <= 1.0:
        return None
    if len(set(round(v, 3) for v in vals)) == 1:
        return None
    inv = sum(1.0 / v for v in vals)
    if not (0.9 <= inv <= 1.15):
        return None
    od = win_odds.get(winner)
    od = float(od) if od else 0.0
    return od if od > 1.0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--guild', required=True)
    ap.add_argument('--user', required=True)
    ap.add_argument('--log', default='oasis_bet_log.csv')
    ap.add_argument('--dry', action='store_true', help='精算せず、何をするか出すだけ')
    a = ap.parse_args()

    bl = oc.BetLog(os.path.join(HERE, a.log))
    df = bl.load()
    if not len(df):
        print(f'ベットログが空です（{bl.location}）')
        return 0
    pend = df[df['status'] == 'pending']
    ids = sorted(set(pend['race_id'].astype(str)))
    if not ids:
        print('未精算のレースはありません。')
        return 0
    print(f'未精算 {len(ids)}レース / {len(pend)}点')

    done = skipped = 0
    gaps = []          # (購入時オッズ, API最終オッズ) 差を実測するため
    for rid in ids:
        n = int((pend['race_id'].astype(str) == rid).sum())
        if not rid.isdigit():
            print(f'  {rid:<16} {n}点  → 飛ばす（schedule_id ではないので自動化できません）')
            skipped += 1
            continue
        r = race_result(a.guild, a.user, int(rid))
        if not r:
            print(f'  {rid:<16} {n}点  → 飛ばす（まだ結果が出ていない／取得できない）')
            skipped += 1
            continue
        if any(x is None for x in r['names']):
            print(f'  {rid:<16} {n}点  → 飛ばす（馬名を特定できません）')
            skipped += 1
            continue
        tri, pool = final_trifecta_odds(a.guild, int(rid), r['pet_ids'])
        win = final_win_odds(r['win_odds'], r['names'][0])
        order = ' → '.join(r['names'])
        note = (f'3連単最終od {tri:.1f}' if tri else '3連単最終od 取得できず→概算')
        if pool:
            note += f'（プール {int(pool):,}）'
        if win:
            note += f' / 単勝最終od {win:.2f}'
        print(f'  {rid:<16} {n}点  {r["date"]}  {order}  {note}')
        # 購入時オッズ（＝締切2分前の実測）と API の最終オッズのズレを記録する
        if tri:
            hit = df[(df['race_id'].astype(str) == rid) & (df['bet_type'] == '3連単')
                     & (df['combo'] == order)]
            if len(hit):
                buy = float(hit['odds'].iloc[0])
                if buy > 0:
                    gaps.append((buy, tri))
                    print(f'{"":18}購入時 {buy:.1f} → 最終 {tri:.1f}'
                          f'（{(tri / buy - 1) * 100:+.1f}%）')
        if not a.dry:
            try:
                cnt = bl.settle(rid, tuple(r['names']),
                                final_odds={'3連単': tri, '単勝': win})
                got = int((bl.load().query('race_id.astype("str") == @rid')
                           ['status'] == 'won').sum())
                print(f'{"":18}→ 精算 {cnt}点（的中 {got}点）')
                done += 1
            except Exception as e:
                print(f'{"":18}→ ❌ 精算に失敗: {e}')
                skipped += 1
    if gaps:
        import statistics
        d = [t / b - 1 for b, t in gaps]
        print(f'\n■ 購入時オッズ（締切2分前）と最終オッズのズレ  n={len(d)}')
        print(f'  中央 {statistics.median(d) * 100:+.1f}% / '
              f'最大 {max(d, key=abs) * 100:+.1f}%')
        if max(abs(x) for x in d) < 0.05:
            print('  → 5%未満。購入時オッズを最終値として扱って問題ありません。')
        else:
            print('  → 無視できない差があります。最終オッズを使い続けてください。')
    if a.dry:
        print('\n--dry なので何も書き換えていません。')
    else:
        print(f'\n完了: {done}レースを精算 / {skipped}レースは対象外。')
        rep = bl.report(bet_type='3連単')
        if rep and rep.get('n'):
            print(f'  3連単 通算 {rep["n"]}点 / 的中 {rep.get("n_won", 0)}点 / '
                  f'ROI {rep.get("roi", 0) * 100:+.1f}%')
    return 0


def _selfcheck():
    """オッズの下限張り付き（＝誰も賭けていない）を単勝の最終オッズに使わないこと。"""
    real = {'a': 2.0, 'b': 4.0, 'c': 4.0}                  # Σ(1/od)=1.0
    assert final_win_odds(real, 'a') == 2.0
    floor = {'a': 1.5, 'b': 1.5, 'c': 1.5, 'd': 1.5}       # Σ=2.67 → 使わない
    assert final_win_odds(floor, 'a') is None
    assert final_win_odds({'a': 2.0}, 'a') is None          # 1頭では判定不能
    # 実データで踏んだ形。Σは1付近を通るが 1.0 は価格ではない（sid 1981 / 1897）
    assert final_win_odds({'a': 1.0, 'b': 49.5}, 'a') is None
    assert final_win_odds({'a': 1.0, 'b': 104.4, 'c': 104.4, 'd': 104.4}, 'a') is None
    assert final_win_odds({'a': 1.0, 'b': 49.5}, 'b') is None
    assert final_win_odds({'a': 2.0, 'b': 2.0}, 'a') is None          # 全馬同値
    print('selfcheck OK')


if __name__ == '__main__':
    if '--selfcheck' in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
