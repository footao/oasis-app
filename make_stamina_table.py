# -*- coding: utf-8 -*-
"""必要スタミナの早見表（HTML）を作る。

    python make_stamina_table.py logg races.jsonl

ステータスは logg（レース当時の値）、消費・区間数は races.jsonl の timeline。
races.jsonl のステータスは「今」の値なので使わない（harvest_results.py の警告）。
"""
import html as H
import io
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc            # noqa: E402
import stamina_report as SR        # noqa: E402

RAMP = 6

# 収支（持ち − 必要）ごとの疲労補正。races.jsonl の timeline 21,040点から集計。
FM_CURVE = [
    ('−10 以下', 0.8400, 0.650, '下限 0.650 に張り付く。事実上リタイア'),
    ('−5',       0.9409, 0.650, '終盤で大きく失速'),
    ('−3',       0.9785, 0.760, 'ゴール前で失速'),
    ('−1',       0.9985, 0.964, 'ほぼ影響なし'),
    ('±0',       1.0016, 1.001, 'ちょうど使い切る。ペナルティなし'),
    ('+5',       1.0067, 1.002, 'わずかに加速'),
    ('+10',      1.0120, 1.004, ''),
    ('+15',      1.0232, 1.014, ''),
    ('+25 以上', 1.0267, 1.018, '上限 1.030 に近づく。ここから先は伸びない'),
]


def build(rows, law, out='stamina_table.html'):
    A = []
    a = A.append
    a('<!doctype html><html lang="ja"><meta charset="utf-8">')
    a('<title>必要スタミナ早見表</title>')
    a('<style>' + io.open('/tmp/style.css', encoding='utf-8').read() + '</style>')
    a('<h1>必要スタミナ早見表</h1>')
    a(f'<p class="sub">実測 {len(rows)}頭（ステータスは Discordログ、消費は result API の timeline）。'
      'スタミナは「速さの燃料」。足りないと最後に大きく減速し、余ると小さく加速します。</p>')

    a('<div class="note"><b>仕組み</b>　'
      '残スタミナ ＝ 初期スタミナ − Σ(100mごとの消費)。'
      '消費は<b>速いほど増える</b>ので、速い馬ほど多くのスタミナが要ります。'
      '足りなくなると最後に大きく減速し（最大 −35%）、余ると小さく加速します（最大 +3%）。'
      '実測で <b>98.3%</b> の馬がこの式どおりでした。</div>')

    a('<h2>距離ごとの必要スタミナ</h2>')
    a('<table><thead><tr><th class="l">距離</th><th>区間</th>'
      '<th>必要スタミナ<br>（遅い馬）</th><th>必要スタミナ<br>（速い馬）</th>'
      '<th>これ以上は増えない</th><th>不足していた馬</th><th>足りた馬の余り<br>（中央値）</th>'
      '</tr></thead><tbody>')
    for d in oc.DIST_LIST:
        L = law.get(d)
        if not L:
            continue
        v = [x for x in rows if x['dist'] == d]
        gap = np.array([x['st0'] - x['need'] for x in v])
        sh = gap < 0
        a(f'<tr><td class="l"><b>{d}</b></td><td>{L["n_seg"]}</td>'
          f'<td class="p" data-lv="1">{L["lo"]*L["n_seg"]:.0f}</td>'
          f'<td class="p" data-lv="4">{L["hi"]*L["n_seg"]:.0f}</td>'
          f'<td>指標 {L["hi"]/L["c"]:.0f} 以上</td>'
          f'<td>{sh.sum()} / {len(v)}（{sh.mean()*100:.0f}%）</td>'
          f'<td class="z">+{np.median(gap[~sh]):.0f}</td></tr>')
    a('</tbody></table>')

    a('<h2>自分の馬に何スタミナ要るか</h2>')
    a('<p class="sub">「指標」＝ 序盤の区間重み × 距離バランス × 実効ステータス。'
      'パッシブの倍率を掛けた後の値を入れてください。</p>')
    a('<table><thead><tr><th class="l">距離</th><th class="l">指標の計算</th>')
    for q in (0, 25, 50, 75, 100):
        a(f'<th>指標 {q}%tile</th>')
    a('</tr></thead><tbody>')
    for d in oc.DIST_LIST:
        L = law.get(d)
        if not L:
            continue
        b = oc.INTERNAL_DIST_BALANCE[d]
        w = oc.INTERNAL_PHASE_WEIGHTS['序盤']
        bs = np.array([x['base'] for x in rows if x['dist'] == d])
        a(f'<tr><td class="l"><b>{d}</b></td>'
          f'<td class="l z">SP×{w[0]*b[0]:.2f} + PW×{w[1]*b[1]:.2f} + ST×{w[2]*b[2]:.2f}</td>')
        for q in (0, 25, 50, 75, 100):
            x = float(np.percentile(bs, q))
            need = min(max(L['c'] * x, L['lo']), L['hi']) * L['n_seg']
            lv = min(RAMP - 1, int((need - L['lo'] * L['n_seg'])
                                   / max(L['hi'] * L['n_seg'] - L['lo'] * L['n_seg'], 1e-9)
                                   * RAMP))
            a(f'<td class="p" data-lv="{lv}">{need:.0f}<br>'
              f'<span class="z" style="font-size:.8em">指標{x:.0f}</span></td>')
        a('</tr>')
    a('</tbody></table>')

    a('<h2>収支と疲労補正（速さの倍率）</h2>')
    a('<p class="sub">収支 ＝ 持ちスタミナ − 必要スタミナ。'
      'API の <code>fatigue_modifier</code> をレースごとの実測から集計しました。'
      '<b>足りない側の落ち方が急で、余る側の伸びは緩やか</b>です。</p>')
    a('<table><thead><tr><th class="l">収支</th><th>疲労補正（平均）</th>'
      '<th>レース中の最低値</th><th class="l">意味</th></tr></thead><tbody>')
    for g, avg, mn, note in FM_CURVE:
        cls = 'neg' if avg < 0.999 else ('p' if avg > 1.001 else 'z')
        lv = min(RAMP - 1, int(max(avg - 1.0, 0) * 300)) if cls == 'p' else 0
        tag = f'<td class="{cls}"' + (f' data-lv="{lv}"' if cls == 'p' else '') + '>'
        a(f'<tr><td class="l"><b>{g}</b></td>{tag}×{avg:.4f}</td>'
          f'<td class="{"neg" if mn < 0.99 else "z"}">×{mn:.3f}</td>'
          f'<td class="l z">{note}</td></tr>')
    a('</tbody></table>')

    a('<h2>スタミナ切れは着順にいくら響くか</h2>')
    a('<p class="sub">同じレース内で「指標の順位」と「実際の着順」を比べた差。'
      '＋なら速さの割に負けたということ。</p>')
    a('<table><thead><tr><th class="l">距離</th><th>足りた馬</th><th>足りない馬</th>'
      '<th>差</th></tr></thead><tbody>')
    for d in oc.DIST_LIST:
        if d not in law:
            continue
        ok, ng, n = SR.drop_by_shortfall(rows, d)
        if ok is None:
            continue
        # 不足した馬が数頭しか居ない距離は、平均を出しても意味が無い
        if n < 10:
            a(f'<tr><td class="l"><b>{d}</b></td><td class="z">{ok:+.2f}着</td>'
              f'<td class="z" colspan="2">不足した馬が {n}頭 しか居ないので測れない</td></tr>')
            continue
        a(f'<tr><td class="l"><b>{d}</b></td><td class="z">{ok:+.2f}着</td>'
          f'<td class="neg">{ng:+.2f}着</td>'
          f'<td class="p" data-lv="{min(RAMP-1,int(max(ng-ok,0)))}">{ng-ok:+.2f}着</td></tr>')
    a('</tbody></table>')

    a('<div class="note"><b>モデルに反映済み（CORE_VERSION 3.6.0）。</b>　'
      '「余り」と「不足」を別々の特徴量として入れています。'
      '上の表のとおり<b>不足側は急降下・余り側は緩やかに上昇</b>という折れ線なので、'
      '1列にまとめると表現できず、かえって精度が落ちます（実測で確認済み）。'
      '実データ103レースで 1着的中 88.7→93.3、3着セット的中 60.7→70.5。</div>')

    io.open(os.path.join(HERE, out), 'w', encoding='utf-8').write('\n'.join(A))
    print(f'{out}: {os.path.getsize(os.path.join(HERE, out)):,} bytes')


if __name__ == '__main__':
    a = sys.argv[1:]
    rows = SR.load(a[0] if a else 'logg', a[1] if len(a) > 1 else 'races.jsonl')
    build(rows, SR.fit(rows))
