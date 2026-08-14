# -*- coding: utf-8 -*-
"""パッシブ35種の一覧表（HTML）を作る。

倍率・発動条件・稼働率に加えて、**距離ごとの実効効果**を内部式から計算する。
同じパッシブでも距離で価値が数倍変わるので、そこが見えないと選べない。
"""
import html as H
import io
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

REF = (100.0, 100.0, 100.0)
SPEC = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))

# 青の連続ランプ（大きさ）。負の値だけ status-critical で明示する。
RAMP_LIGHT = ['#eef5fd', '#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec']
RAMP_DARK = ['#12233a', '#16304f', '#1a3a63', '#1c4a7f', '#1c5cab', '#256abf']
NEG = '#d03b3b'

KIND = {'stat': 'ステータス', 'aptitude': '適性', 'phase': '展開'}
SCOPE = {'always': '常時', 'aptitude': '距離/馬場が一致', 'phase': '区間限定',
         'conditional': '状況限定', 'same_species': '同族が居る時',
         'variance': 'ブレ低減', 'learned': '実測から学習'}


def effect_pct(p, dist, track):
    w = oc.internal_stat_weights(dist)
    W = np.array([w['SP'], w['PW'], w['ST']])
    base = np.array(REF) @ W
    e = oc.effective_stats(*REF, (p,), dist, track, SPEC, {'same_species': True})
    return float((np.array([e['speed'], e['power'], e['stamina']]) @ W - base) / base * 100)


def collect():
    rows = []
    for p in oc.PASSIVE_NAMES:
        sp = SPEC.get(p) or {}
        m = sp.get('mult') or {}
        track = 'ダート' if p == 'ダート得意' else '芝'
        rows.append(dict(
            name=p, kind=oc.PASSIVE_CATALOG.get(p, ''),
            SP=m.get('speed'), PW=m.get('power'), ST=m.get('stamina'),
            scope=sp.get('scope', ''), arg=sp.get('scope_arg') or '',
            duty=float(sp.get('duty', 1.0)), sigma=float(sp.get('sigma_mult', 1.0)),
            source=sp.get('source', ''), desc=sp.get('desc', ''),
            eff={d: effect_pct(p, d, track) for d in oc.DIST_LIST}))
    return rows


def cell(v, vmax):
    """効果値のセル。大きさは青の濃さ、負の値は赤字で明示。

    表示は小数1桁なので、|v| < 0.05 は「-0.0%」と出て
    マイナスに見えてしまう。実質ゼロなので中立表示に寄せる。
    """
    if abs(v) < 0.05:
        return '<td class="z">±0.0%</td>' if abs(v) > 1e-9 else '<td class="z">—</td>'
    if v < 0:
        return f'<td class="neg">{v:+.1f}%</td>'
    i = min(len(RAMP_LIGHT) - 1, int(v / vmax * len(RAMP_LIGHT)))
    return f'<td class="p" data-lv="{i}">{v:+.1f}%</td>'


def build():
    rows = collect()
    vmax = max(r['eff'][d] for r in rows for d in oc.DIST_LIST) or 1.0
    mx = lambda v: f'×{v:.2f}' if v is not None else '<span class="z">—</span>'

    # 距離別のおすすめ（上位5）
    best = {}
    for d in oc.DIST_LIST:
        best[d] = sorted(rows, key=lambda r: -r['eff'][d])[:5]

    out = []
    A = out.append
    A('<!DOCTYPE html><meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width,initial-scale=1">')
    A('<title>おあしすっち パッシブ一覧</title>')
    A('''<style>
:root{color-scheme:light dark;
 --surface:#fcfcfb; --surface-2:#f4f4f1; --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#83827c;
 --line:#e3e2dd; --neg:#d03b3b;
 --p0:#eef5fd;--p1:#cde2fb;--p2:#b7d3f6;--p3:#9ec5f4;--p4:#86b6ef;--p5:#6da7ec;}
@media (prefers-color-scheme:dark){:root{
 --surface:#1a1a19; --surface-2:#232322; --ink:#fff; --ink-2:#c3c2b7; --ink-3:#8a8980;
 --line:#333330; --neg:#e66767;
 --p0:#12233a;--p1:#16304f;--p2:#1a3a63;--p3:#1c4a7f;--p4:#1c5cab;--p5:#256abf;}}
body{margin:0;padding:1.5rem 1rem 4rem;background:var(--surface);color:var(--ink);
 font:15px/1.7 system-ui,-apple-system,sans-serif;max-width:1080px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .3rem} h2{font-size:1.1rem;margin:2rem 0 .6rem;color:var(--ink)}
p.sub{color:var(--ink-2);margin:.2rem 0 1.2rem;font-size:.92rem}
table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{border-bottom:1px solid var(--line);padding:.42rem .5rem;text-align:right;
 white-space:nowrap}
th{color:var(--ink-2);font-weight:600;text-align:right;position:sticky;top:0;
 background:var(--surface);border-bottom:2px solid var(--line)}
th.l,td.l{text-align:left}
td.p{color:var(--ink);font-variant-numeric:tabular-nums}
td.p[data-lv="0"]{background:var(--p0)} td.p[data-lv="1"]{background:var(--p1)}
td.p[data-lv="2"]{background:var(--p2)} td.p[data-lv="3"]{background:var(--p3)}
td.p[data-lv="4"]{background:var(--p4)} td.p[data-lv="5"]{background:var(--p5)}
@media (prefers-color-scheme:dark){td.p[data-lv="3"],td.p[data-lv="4"],td.p[data-lv="5"]{color:#fff}}
td.neg{color:var(--neg);font-weight:600}
td.z,.z{color:var(--ink-3)}
tr:hover td{background:var(--surface-2)}
tr:hover td.p[data-lv]{filter:brightness(1.04)}
.tag{display:inline-block;font-size:.72rem;padding:.05rem .4rem;border-radius:3px;
 background:var(--surface-2);color:var(--ink-2);margin-left:.3rem}
.best{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:.8rem}
.card{background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:.7rem .9rem}
.card h3{margin:0 0 .4rem;font-size:.95rem}
.card ol{margin:0;padding-left:1.2rem;font-size:.85rem;color:var(--ink-2)}
.card b{color:var(--ink)}
.note{background:var(--surface-2);border-left:3px solid var(--p4);padding:.7rem 1rem;
 border-radius:4px;font-size:.9rem;color:var(--ink-2);margin:1rem 0}
.note b{color:var(--ink)}

/* --- 印刷（PDF）用 --- */
@page{size:A4 landscape;margin:11mm 10mm 12mm}
@media print{
 :root{--surface:#fff;--surface-2:#f4f4f1;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#83827c;
       --line:#dcdbd6;--neg:#c0322f;
       --p0:#eef5fd;--p1:#cde2fb;--p2:#b7d3f6;--p3:#9ec5f4;--p4:#86b6ef;--p5:#6da7ec;}
 *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
 body{padding:0;max-width:none;font-size:10pt;line-height:1.45}
 h1{font-size:15pt;margin:0 0 .15rem} h2{font-size:11.5pt;margin:.9rem 0 .35rem}
 p.sub{font-size:8.6pt;margin:.1rem 0 .7rem}
 table{font-size:8.2pt} th,td{padding:.22rem .38rem}
 th{position:static;border-bottom:1.5px solid #999}
 thead{display:table-header-group}      /* 改ページ後もヘッダを繰り返す */
 tr{break-inside:avoid;page-break-inside:avoid}
 tr:hover td{background:none}           /* 印刷にホバーは無い */
 td.p[data-lv]{filter:none}
 .best{gap:.5rem;break-inside:avoid} .card{padding:.45rem .6rem}
 .card h3{font-size:8.8pt;margin:0 0 .2rem} .card ol{font-size:8pt;padding-left:1rem}
 .note{font-size:8.4pt;padding:.45rem .7rem;margin:.6rem 0;break-inside:avoid}
 h2+table{break-before:avoid}
}
</style>''')

    A('<h1>おあしすっち パッシブ35種</h1>')
    A('<p class="sub">倍率・発動条件と、<b>距離ごとの実効効果</b>。'
      '効果は素100/100/100の馬に1枠付けたときの内部レート変化率で、'
      'ゲーム内部の着順式（区間重み×距離バランス）から計算しています。'
      '色が濃いほど効果大。赤字はマイナス（付けると損）。</p>')

    A('<h2>距離別のおすすめ</h2><div class="best">')
    for d in oc.DIST_LIST:
        A(f'<div class="card"><h3>{d}</h3><ol>')
        for r in best[d]:
            A(f'<li><b>{H.escape(r["name"])}</b> {r["eff"][d]:+.1f}%</li>')
        A('</ol></div>')
    A('</div>')

    A('<div class="note"><b>適性系（○○得意）は一致した距離でだけ +15%。</b>'
      'それ以外の距離では完全に無効なので、装備の2枠を丸ごと捨てることになります。'
      'また短距離ではスピード×1.25（+16.3%）が全ステ×1.15（+15.0%）を上回るため、'
      '<b>短距離得意より スピード大アップのほうが強い</b>という逆転が起きます。</div>')

    A('<h2>全35種</h2>')
    A('<table><thead><tr><th class="l">パッシブ</th><th class="l">種別</th>'
      '<th>SP</th><th>PW</th><th>ST</th><th class="l">発動条件</th><th>稼働率</th>'
      + ''.join(f'<th>{d}</th>' for d in oc.DIST_LIST) + '</tr></thead><tbody>')
    for r in sorted(rows, key=lambda x: -max(x['eff'].values())):
        cond = SCOPE.get(r['scope'], r['scope']) + (f'（{r["arg"]}）' if r['arg'] else '')
        sig = f'<span class="tag">σ×{r["sigma"]:.2f}</span>' if r['sigma'] != 1.0 else ''
        A('<tr>'
          f'<td class="l">{H.escape(r["name"])}{sig}</td>'
          f'<td class="l z">{KIND.get(r["kind"], "")}</td>'
          f'<td>{mx(r["SP"])}</td><td>{mx(r["PW"])}</td><td>{mx(r["ST"])}</td>'
          f'<td class="l z">{cond}</td>'
          f'<td>{r["duty"]*100:.0f}%</td>'
          + ''.join(cell(r['eff'][d], vmax) for d in oc.DIST_LIST) + '</tr>')
    A('</tbody></table>')

    A('<div class="note"><b>読み方の注意。</b>'
      '効果は素100/100/100を基準にした値です。実際の馬はステータスが偏っているので、'
      '得意ステータスを伸ばすパッシブほど表の値より効きます。'
      '「安定感」は倍率を持たず着順のブレ幅σを半分にするスキルなので、'
      'この表の効果欄は — になります（価値が無いという意味ではありません）。'
      '「粘り腰」はスタミナ切れの速度低下を軽減するもので倍率に落とせないため、'
      '実ログからの学習に委ねています。</div>')

    path = os.path.join(HERE, 'passive_table.html')
    io.open(path, 'w', encoding='utf-8').write('\n'.join(out))
    return path, rows, vmax


if __name__ == '__main__':
    p, rows, vmax = build()
    print(f'{p}: {os.path.getsize(p):,} bytes / {len(rows)}種 / 最大効果 {vmax:.1f}%')
