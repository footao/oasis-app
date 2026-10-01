# -*- coding: utf-8 -*-
"""settle_reports.py — オートパイロットの購入まとめ(JSON) × 実際の着順 で精算する。

    python settle_reports.py
    python settle_reports.py --reports C:\\oasissfable\\autooo\\reports.txt --races races.jsonl
    python settle_reports.py --since 2026-09-18 --json out.json

**払戻の出し方**
  単勝   口数 × 1,000 × races.jsonl の確定オッズ（実測値）
  3連単  口数 × 10,000 × まとめの実効オッズ eff（近似）
         3連単の確定オッズは採取していないので、購入時点の希薄化込みオッズを使う。
         締切までに他人が入るとプールが増えて配当は**上振れる**ので、
         この精算は3連単については**保守的**（実際より低め）に出る。
         BOTの払戻通知4件と突き合わせた実測のズレ（2026/09/20）:
             R2399 1.10x / R2408 1.03x / R2410 1.04x / R2420 1.09x → 合計 1.05x
         つまり3連単の回収率は、ここに出る値の約1.05倍が実際の値。
  未成立枠 eff は「誰も賭けていない」前提の見積りで、実際とは桁違いになりうる
         （R2529: 見積り136倍 → 実際2.3倍）。的中しても払戻は合計に入れず「未検証」として数だけ出す。
  試し買い 単勝プールの実測に使った1口（src=probe）。単勝と同じく確定オッズで精算。
"""
import argparse, io, json, os, re, sys, collections

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEF_REPORTS = r'C:\oasissfable\autooo\reports.txt'
DEF_RACES = os.path.join(HERE, 'races.jsonl')


def load_reports(path):
    """reports.txt → まとめJSONのリスト（1レース1件）。

    - 1ブロックに JSON が複数行あれば全部読む（見送りのまとめ＋手動で買ったまとめ、など）
    - 全く同じ JSON が二重に書かれていたら1回だけ数える
    - 同じレースの**別々の**まとめは買い目をつなぐ（どちらも本当に買っている）
    - 組み立てに失敗したまとめ（err）は金が動いているのに買い目が無いので、警告に回す
    """
    if not os.path.exists(path):
        sys.exit(f'見つかりません: {path}')
    txt = io.open(path, encoding='utf-8', errors='replace').read()
    seen, by_sid, errs, unreadable = set(), {}, [], 0
    for b in re.split(r'^===== ', txt, flags=re.M)[1:]:
        head = b.split('\n', 1)[0]
        lines = re.findall(r'^\{.*\}$', b, flags=re.M)
        if not lines:
            unreadable += 1
            continue
        for ln in lines:
            if ln in seen:
                continue
            seen.add(ln)
            try:
                rec = json.loads(ln)
            except ValueError:
                unreadable += 1
                continue
            if 'err' in rec:
                errs.append(rec.get('sid'))
                continue
            rec['_when'] = head.split(' race=')[0].strip()
            k = rec.get('sid')
            if k in by_sid:
                by_sid[k]['bets'] = (by_sid[k].get('bets') or []) + (rec.get('bets') or [])
            else:
                by_sid[k] = rec
    return sorted(by_sid.values(), key=lambda r: r.get('sid') or 0), unreadable, errs


def load_results(path):
    """races.jsonl → {sid: {'order': [1着,2着,3着], 'odds': {馬名: 確定オッズ}}}"""
    if not os.path.exists(path):
        sys.exit(f'見つかりません: {path}（harvest_daily を回してください）')
    res = {}
    for line in io.open(path, encoding='utf-8'):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        hs = [h for h in (r.get('horses') or []) if h.get('rank')]
        if not hs:          # 2頭立てなどでも単勝は精算できるので、1頭でも着順があれば使う
            continue
        hs.sort(key=lambda h: h['rank'])
        res[r.get('schedule_id')] = {
            'order': [h['name'] for h in hs[:3]],
            'odds': {h['name']: h.get('odds') for h in hs},
            'date': r.get('race_date'), 'time': r.get('race_time'),
        }
    return res


def settle(rec, q):
    """1レース分を精算。-> (投入, 払戻, 明細)"""
    top3 = q['order']
    stake = ret = 0.0
    lines = []
    for b in (rec.get('bets') or []):
        amt = b['u'] * b['unit']
        stake += amt
        if b['t'] == 'win':
            name = b['n'][0]
            od = q['odds'].get(name)
            hit = (name == top3[0])
            got = amt * float(od) if (hit and od) else 0.0
        else:
            hit = len(top3) == 3 and list(b['n']) == top3
            # 未成立枠の eff は「誰も賭けていない」前提の見積りで当てにならない → 払戻は未検証
            unverified = hit and b.get('src') == 'sleeve'
            got = amt * float(b.get('eff') or 0) if (hit and not unverified) else 0.0
        ret += got
        lines.append(dict(t=b['t'], src=b.get('src'), n=b['n'], amt=amt,
                          p=b.get('p'), pr=b.get('pr'), od=b.get('od'), eff=b.get('eff'),
                          hit=bool(hit), ret=round(got),
                          unverified=bool(b['t'] != 'win' and hit and b.get('src') == 'sleeve')))
    return stake, ret, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reports', default=DEF_REPORTS)
    ap.add_argument('--races', default=DEF_RACES)
    ap.add_argument('--since', default=None, help='この日付以降だけ（YYYY-MM-DD）')
    ap.add_argument('--json', default=None, help='明細をJSONで書き出す')
    ap.add_argument('--detail', action='store_true', help='買い目ごとの明細も出す')
    a = ap.parse_args()

    recs, skipped, errs = load_reports(a.reports)
    res = load_results(a.races)

    rows, missing, nobet = [], [], 0
    for r in recs:
        q = res.get(r.get('sid'))
        if not q:
            missing.append(r.get('sid'))
            continue
        if a.since and (q['date'] or '') < a.since:
            continue
        if not r.get('bets'):
            nobet += 1          # 見送ったレース（まとめはあるが買い目ゼロ）
            continue
        st, rt, lines = settle(r, q)
        rows.append(dict(sid=r['sid'], date=q['date'], time=q['time'],
                         bank=r.get('bank'), pool3=r.get('pool3'),
                         cfg=r.get('cfg'), stake=st, ret=rt, lines=lines,
                         order=q['order']))
    if errs:
        print(f'⚠ まとめの組み立てに失敗したレースが {len(errs)}件あります: {errs[-6:]}'
              '（購入は済んでいる可能性があるので、BOT の通知で確認してください）')
    if not rows:
        print('精算できるレースがありません。'
              f'（まとめ {len(recs)}件 / 着順待ち {len(missing)}件 / 見送り {nobet}件 / 読めない {skipped}件）')
        return 0

    print(f'■ 精算 {len(rows)}レース'
          + (f'（着順がまだ無い {len(missing)}件は除外: {missing[-6:]}）' if missing else '')
          + (f'（見送り {nobet}件）' if nobet else '')
          + (f'（JSON が読めない {skipped}件）' if skipped else ''))
    print(f'{"日付":<11}{"時刻":>6}{"sid":>6}{"投入":>10}{"払戻":>12}{"収支":>12}'
          f'{"的中":>6}{"プール比":>9}')
    tot = collections.Counter()
    for x in rows:
        tri_st = sum(l['amt'] for l in x['lines'] if l['t'] == 'tri')
        share = tri_st / x['pool3'] * 100 if x.get('pool3') else 0.0
        nhit = sum(1 for l in x['lines'] if l['hit'])
        print(f'{x["date"]:<11}{x["time"]:>6}{x["sid"]:>6}{x["stake"]:>10,.0f}'
              f'{x["ret"]:>12,.0f}{x["ret"]-x["stake"]:>+12,.0f}'
              f'{nhit:>4}/{len(x["lines"]):<2}{share:>8.1f}%')
        for l in x['lines']:
            # ⚠ 券種と出所で別の接頭辞を使う。単勝は t も src も 'win' なので、
            #   同じキーに足すと投入額が倍になる（2026/09/20 に実際にやらかした）。
            for k in ('t:' + l['t'], 's:' + str(l['src'])):
                tot[k + '_st'] += l['amt']
                tot[k + '_rt'] += l['ret']
                tot[k + '_n'] += 1
                tot[k + '_hit'] += 1 if l['hit'] else 0
        tot['st'] += x['stake']
        tot['rt'] += x['ret']
        if a.detail:
            for l in x['lines']:
                mark = ('的中?' if l.get('unverified') else '的中') if l['hit'] else '  － '
                print(f'      {mark} {l["t"]:<4}{str(l["src"]):<6} {"→".join(l["n"]):<34}'
                      f'{l["amt"]:>9,}rrc  p={l["p"]}  eff={l["eff"]}  払戻 {l["ret"]:>9,}')

    def line(label, key, prefix=''):
        # ⚠ 実績が無い枠では何も出さない。prefix を別の print で出すと、
        #   出さなかった行の枝記号だけが次の行にくっつく（2026/09/29 に実際に崩れた）。
        st, rt = tot[key + '_st'], tot[key + '_rt']
        if not st:
            return
        print(f'  {prefix}{label:<12} 投入 {st:>10,.0f}  払戻 {rt:>11,.0f}  '
              f'回収率 {rt/st*100:>4.0f}%  的中 {tot[key+"_hit"]}/{tot[key+"_n"]}')

    pct = tot["rt"] / tot["st"] * 100 if tot["st"] else 0.0
    print(f'\n  {"合計":<12} 投入 {tot["st"]:>10,.0f}  払戻 {tot["rt"]:>11,.0f}  '
          f'収支 {tot["rt"]-tot["st"]:>+12,.0f}  回収率 {pct:.0f}%')
    line('単勝', 't:win')
    line('試し買い', 's:probe', '└ ')
    line('3連単', 't:tri')
    line('EV枠', 's:ev', '├ ')
    line('市場本命', 's:mfav', '├ ')
    line('EV枠（順番補正）', 's:ev23', '├ ')
    line('未成立枠', 's:sleeve', '└ ')
    unv = [l for x in rows for l in x['lines'] if l.get('unverified')]
    if unv:
        print(f'    ⚠ 未成立枠の的中 {len(unv)}本は払戻を合計に入れていません（見積りが当てにならないため）。'
              '実額は BOT の通知で確認してください')

    # 2・3着の順番補正（SWAP23）の効果。pr（補正前の確率）が入っている買い目だけを見る。
    sw = [l for x in rows for l in x['lines'] if l.get('pr') is not None or l['src'] == 'ev23']
    if sw:
        st = sum(l['amt'] for l in sw); rt = sum(l['ret'] for l in sw)
        hit = sum(1 for l in sw if l['hit'])
        added = [l for l in sw if l['src'] == 'ev23']
        ast = sum(l['amt'] for l in added); art = sum(l['ret'] for l in added)
        print(f'\n  2・3着の順番補正: 対象 {len(sw)}本  投入 {st:,.0f}  払戻 {rt:,.0f}  '
              f'回収率 {rt/st*100 if st else 0:.0f}%  的中 {hit}/{len(sw)}')
        if added:
            print(f'    うち「補正で足した組」{len(added)}本  投入 {ast:,.0f}  払戻 {art:,.0f}  '
                  f'回収率 {art/ast*100 if ast else 0:.0f}%  的中 '
                  f'{sum(1 for l in added if l["hit"])}/{len(added)}')

    # 市場本命枠は「実測確率 1.3/od」で買っている。モデル確率(pm)との勝負を分けて見る。
    mf = [l for x in rows for l in x['lines'] if l['src'] == 'mfav']
    if mf:
        hit = sum(1 for l in mf if l['hit'])
        pmean = sum(l['p'] for l in mf) / len(mf)
        print(f'\n  市場本命枠の較正: 見積り平均 {pmean*100:.1f}% / 実測 {hit/len(mf)*100:.1f}%'
              f'（{hit}/{len(mf)}）')
    # 安牌モードの閾値が効いているか
    tri = [l for x in rows for l in x['lines'] if l['t'] == 'tri']
    if tri:
        hit = sum(1 for l in tri if l['hit'])
        pmean = sum(l['p'] for l in tri) / len(tri)
        print(f'  3連単全体の較正: 予測平均 {pmean*100:.1f}% / 実測 {hit/len(tri)*100:.1f}%'
              f'（{hit}/{len(tri)}）')
    if len(rows) < 30:
        print(f'\n  ⚠ {len(rows)}レースは統計的にほぼ無意味です。'
              '30レース溜まるまで、この数字で設定を変えないこと。')

    _sum = tot['t:win_st'] + tot['t:tri_st']
    if abs(_sum - tot['st']) > 1:
        print(f'\n  ⚠ 検算が合いません: 券種別の合計 {_sum:,.0f} ≠ 全体 {tot["st"]:,.0f}')

    if a.json:
        io.open(a.json, 'w', encoding='utf-8').write(
            json.dumps(rows, ensure_ascii=False, indent=1))
        print(f'\n  明細を {a.json} に書き出しました。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
