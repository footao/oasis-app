# -*- coding: utf-8 -*-
"""現在の学習済みモデルからスコア計算式の閲覧用HTMLを生成する。"""
import html
import oasis_core as oc


def esc(x):
    return html.escape(str(x))


def table(headers, rows, aligns=None):
    aligns = aligns or ['left'] * len(headers)
    th = ''.join(f'<th style="text-align:{a}">{esc(h)}</th>' for h, a in zip(headers, aligns))
    trs = []
    for r in rows:
        tds = ''.join(f'<td style="text-align:{a}">{esc(c)}</td>' for c, a in zip(r, aligns))
        trs.append(f'<tr>{tds}</tr>')
    return f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table>'


def build(bundle):
    mf = oc.model_formula(bundle)
    pw = oc.INTERNAL_PHASE_WEIGHTS
    db = oc.INTERNAL_DIST_BALANCE

    # 内部式：区間重み
    t_phase = table(['区間', 'スピード', 'パワー', 'スタミナ'],
                    [[k, v[0], v[1], v[2]] for k, v in pw.items()],
                    ['left', 'right', 'right', 'right'])
    # 内部式：距離バランス
    t_bal = table(['距離', 'スピード', 'パワー', 'スタミナ'],
                  [[k, v[0], v[1], v[2]] for k, v in db.items()],
                  ['left', 'right', 'right', 'right'])
    # 内部式：実効重み（SP=1正規化）
    t_eff = table(['距離', 'スピード', 'パワー', 'スタミナ'],
                  [[d, 1.0, oc.internal_stat_weights(d)['norm'][1],
                    oc.internal_stat_weights(d)['norm'][2]] for d in oc.DIST_LIST],
                  ['left', 'right', 'right', 'right'])
    # モデル係数
    t_model = table(
        ['距離', '切片', 'log(SP)', 'log(PW)', 'log(ST)', 'lin(SP)', 'lin(PW)', 'lin(ST)',
         '内部式比 SP:PW:ST'],
        [[r['dist'], f"{r['intercept']:+.3f}", f"{r['log_SP']:+.3f}", f"{r['log_PW']:+.3f}",
          f"{r['log_ST']:+.3f}", f"{r['lin_SP']:+.3f}", f"{r['lin_PW']:+.3f}",
          f"{r['lin_ST']:+.3f}",
          f"1 : {r['internal_norm'][1]:.2f} : {r['internal_norm'][2]:.2f}"]
         for r in mf['per_dist']],
        ['left'] + ['right'] * 7 + ['center'])
    cc = mf['condition']

    # パッシブ係数
    kind_ja = {'stat': 'ステータス系', 'aptitude': '適性系', 'phase': '展開系'}
    scope_ja = {'always': '常時', 'aptitude': '距離/馬場一致時', 'phase': '区間限定',
                'conditional': '状況限定', 'same_species': '同族が居る時', 'variance': 'ブレ低減'}

    def mx(v, has):
        return f"×{v:.2f}" if v is not None else ("×1.00" if has else "—")
    prows = []
    for r in oc.passive_coef_table(bundle.get('spec')):
        has = any(r[k] is not None for k in ('SP', 'PW', 'ST'))
        prows.append([
            r['passive'], kind_ja.get(r['kind'], r['kind']),
            mx(r['SP'], has), mx(r['PW'], has), mx(r['ST'], has),
            scope_ja.get(r['scope'], r['scope']) + (f"（{r['scope_arg']}）" if r['scope_arg'] else ''),
            (f"{r['duty']:.0%}" if r['duty'] else ''),
            (f"{r['sigma_mult']:.2f}" if r['sigma_mult'] != 1.0 else ''),
            r['desc']])
    t_pass = table(['パッシブ', '種別', 'SP', 'PW', 'ST', '発動', '稼働率', 'σ×', '説明'],
                   prows,
                   ['left', 'left', 'right', 'right', 'right', 'left', 'right', 'right', 'left'])

    meta = (f"学習レース {bundle['n_races']} / 期間 {bundle['date_min']}〜{bundle['date_max']} "
            f"/ レース内スピアマン {bundle['race_spearman']:.3f} / α={bundle['alpha']} "
            f"/ CORE {oc.CORE_VERSION}")
    rng_now = f"{oc.STAT_RNG_WIDTH * 100:g}%"
    rng_prev = f"{oc.STAT_RNG_WIDTH_PREV * 100:g}%"

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>おあしすっち スコア計算式</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:'Segoe UI','Hiragino Sans',sans-serif;background:#f5f5f0;color:#222;
margin:0;padding:2rem;line-height:1.7}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:1.4rem;margin:.2rem 0}}
h2{{font-size:1.05rem;margin:1.8rem 0 .5rem;padding-left:.5rem;border-left:4px solid #1a1a2e}}
.sub{{color:#666;font-size:.82rem;margin-bottom:1.4rem}}
.card{{background:#fff;border:1px solid #e0e0e0;border-radius:12px;padding:1.4rem 1.8rem;margin-bottom:1rem}}
table{{border-collapse:collapse;width:100%;font-size:.8rem;margin:.5rem 0}}
th,td{{border:1px solid #e6e6e6;padding:.35rem .55rem}}
th{{background:#1a1a2e;color:#e2b96f;font-weight:600;white-space:nowrap}}
tbody tr:nth-child(even){{background:#faf9f6}}
.formula{{background:#1a1a2e;color:#e2b96f;border-radius:8px;padding:.8rem 1rem;font-size:.9rem;
margin:.6rem 0;overflow-x:auto}}
.note{{font-size:.8rem;background:#fff8e1;border-left:3px solid #e2b96f;padding:.5rem .8rem;
border-radius:0 6px 6px 0;color:#555;margin:.6rem 0}}
.cap{{font-size:.76rem;color:#777;margin:.3rem 0 0}}
code{{background:#eee;padding:0 .3rem;border-radius:3px}}
footer{{text-align:center;color:#aaa;font-size:.76rem;margin-top:2rem}}
</style></head><body><div class="wrap">
<h1>🐎 おあしすっち スコア計算式</h1>
<p class="sub">{esc(meta)}</p>

<div class="card">
<h2>① ゲーム内部の本当のスコア式（result API から逆解析）</h2>
<p>レースの着順は、次の <b>rating</b> の大きい順に決まります。</p>
<div class="formula">rating ＝ 定数 × Σ<sub>区間</sub> Σ<sub>stat</sub>( 区間重み[区間][stat]
 × 距離バランス[距離][stat] × 実効ステータス<sub>±乱数</sub> ) × 疲労補正</div>
<p class="cap">stat＝スピード/パワー/スタミナ。実効ステータスにはパッシブの倍率が掛かります。
疲労補正は 1.0 近辺の小さな係数（スタミナ切れの馬だけ下がる）。</p>
<div class="note"><b>レース中の乱数幅: ±{rng_now}</b>（2026/08のプチ修正で ±{rng_prev} から拡大）。
実効ステータスにはレースごとにこの幅のランダム変動が乗ります。幅が広がった分、
レースは荒れやすくなり、ブレを抑える「安定感」などのスキルの価値が上がります。
ツールの着順ブレ幅 σ は変更後のレースログから自動で校正し直します。</div>
<h3 style="font-size:.9rem;margin:.9rem 0 .2rem">区間重み（序盤・中盤・終盤でどのステータスが効くか）</h3>
{t_phase}
<h3 style="font-size:.9rem;margin:.9rem 0 .2rem">距離バランス（距離ごとのステータス重み）</h3>
{t_bal}
<h3 style="font-size:.9rem;margin:.9rem 0 .2rem">実効重み（区間重み×距離バランスを合算、スピード=1で正規化）</h3>
{t_eff}
<div class="note">短距離はスピード偏重、長距離はスタミナ偏重。この写像がスコアの本質です。</div>
</div>

<div class="card">
<h2>② このツールが予測に使う式（学習済みモデル）</h2>
<p>予測値（レース内で中心化した相対 log スコア）は、距離ごとに次を合算します。</p>
<div class="formula">pred ＝ 切片 ＋ b_log·log(実効stat) ＋ b_lin·(実効stat/100)
 ＋ 状態係数 ＋ 未取得パッシブの係数</div>
<p class="cap">実効stat にはスペック済みパッシブの倍率が畳み込み済み。log項は比率で効く頑健な土台、
線形項は内部式の加法構造（特に長距離のスタミナ）を捉えます。</p>
{t_model}
<p class="cap">状態係数: 好調 {cc['好調']:+.3f} / 不調 {cc['不調']:+.3f}（logスコアへの加算）。
右端は参考として内部式のステータス比。モデル係数と傾向が一致していれば妥当です。</p>
</div>

<div class="card">
<h2>③ パッシブの係数（ステータス倍率）</h2>
<p class="cap">スペック済みのパッシブは実効ステータスに掛かる<b>倍率</b>で計算します
（例: スピードスター＝スピード×1.35・スタミナ×0.90）。
「—」は倍率なし（実測から直接学習）、「×1.00」は等倍。σ× はブレ低減スキルの効き。</p>
{t_pass}
</div>

<footer>おあしすっち予測ツール ／ スコア計算式（この時点の学習係数のスナップショット）</footer>
</div></body></html>"""


if __name__ == '__main__':
    b = oc.train_model('logg', spec_path='passive_spec.json')
    open('oasis_score_formula.html', 'w', encoding='utf-8').write(build(b))
    print('wrote oasis_score_formula.html')
