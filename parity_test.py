# -*- coding: utf-8 -*-
"""parity_test.py — Python(oasis_core) と JS(model.js) が同じ予測を出すか検証する。

    python parity_test.py          # node が要る

特徴量を1つ足すたびに、Python 側だけ直して JS 側を忘れる事故が起きる。
それを機械的に止めるためのテスト。乱数の係数でモデルを作り、同じ馬を
両方に食わせて予測値が一致するかを見る。
"""
import json
import os
import subprocess
import sys

import numpy as np
from sklearn.linear_model import Ridge

# パイプ経由で呼ばれると Windows では出力が cp932 になり、記号で落ちる。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

TOL = 1e-9


# 図鑑30種の説明文。`@` が % の数値に置き換わる。
ITEM_DESCS = [
    ('ロケットスタート', '序盤のスピードが@%上昇', 'gear_rocket_start'),
    ('中盤加速', '中盤のパワーが@%上昇', 'gear_mid_acceleration'),
    ('末脚', '終盤のパワーが@%上昇', 'gear_final_kick'),
    ('二の脚', '中盤開始後200mのスピードが@%上昇', 'gear_second_gear'),
    ('追い込み', '終盤に下位半分ならパワーが@%上昇', 'gear_late_charge'),
    ('競り合い', '20m以内にライバルがいるとパワーが@%上昇', 'gear_duel_spirit'),
    ('復讐刻印', '追い抜かれてから200mの間、パワーが@%上昇', 'gear_revenge_mark'),
    ('亡者の追走', '中盤で下位半分ならスピードが@%上昇', 'gear_grave_chase'),
    ('孤影の疾走', '50m以内に相手がいないとスピードが@%上昇', 'gear_lone_run'),
    ('血走り', '残りスタミナ25%以下でスピードが@%上昇', 'gear_blood_rush'),
    ('骨砕き', '残りスタミナ25%以下でパワーが@%上昇', 'gear_bone_break'),
    ('王殺し', '先頭から20m以内の2位以下でスピードが@%上昇', 'gear_king_slayer'),
    ('首位の呪い', '先頭の間、スピードが@%上昇するがスタミナ消費も増加', 'gear_leader_curse'),
    ('終焉加速', '残り300mでスピードが@%上昇', 'gear_end_accel'),
    ('芝啜り', '芝でスピードとパワーがそれぞれ@%上昇', 'gear_turf_gnaw'),
    ('泥啜り', 'ダートでスピードとパワーがそれぞれ@%上昇', 'gear_dirt_gnaw'),
    ('俊足の加護', 'スピードが常時@%上昇', 'charm_speed'),
    ('持久の加護', 'スタミナが常時@%上昇', 'charm_stamina'),
    ('剛力の加護', 'パワーが常時@%上昇', 'charm_power'),
    ('調和の加護', '全ステータスが常時@%上昇', 'charm_balance'),
    ('禍福の天秤', '全ステータスが常時@%上昇', 'charm_scale'),
    ('省エネの加護', 'スタミナ消費が常時@%減少', 'charm_energy_saver'),
    ('安定の加護', 'レース中の乱数幅を@%狭める', 'charm_consistency'),
    ('苦痛慣れ', 'スタミナ0でも減速しにくくなる', 'charm_pain_tolerance'),
    ('魂継ぎ', '残り20%以下で一度だけ最大スタミナの@%を回復', 'charm_soul_relay'),
    ('黄昏の護り', '終盤のパワーが@%上昇', 'charm_twilight_guard'),
    ('夜明けの護り', '序盤のスタミナ評価が@%上昇', 'charm_dawn_guard'),
    ('先導祈願', '先頭の間、スピードが@%上昇', 'charm_lead_prayer'),
    ('逆境祈願', '下位半分の間、スピードとパワーがそれぞれ@%上昇', 'charm_adversity'),
    ('禍福転倒', '残りスタミナ25%以下で全ステータスが@%上昇', 'charm_reversal'),
]



def main():
    spec = oc.load_passive_spec(os.path.join(HERE, 'passive_spec.json'))
    names = oc.feature_names(spec)
    rng = np.random.default_rng(0)
    # 適当な係数のモデル（学習済みログが無くても検証できるようにする）
    X = rng.normal(size=(400, len(names)))
    m = Ridge(alpha=1.0).fit(X, rng.normal(size=400))
    m.coef_ = rng.normal(scale=0.3, size=len(names))
    bundle = {'ok': True, 'model': m, 'feature_names': names, 'spec': spec,
              'race_sigma': 0.2, 'tri_sigma': 0.2, 'n_races': 0}
    payload = oc.export_model_json(bundle, os.path.join(HERE, '_parity_model.json'))

    pas = [p for p in oc.PASSIVE_NAMES]
    cases = []
    for i in range(60):
        dist = oc.DIST_LIST[i % 4]
        track = oc.TRACK_LIST[i % 2]
        horses = []
        for j in range(6):
            k = rng.integers(0, len(pas), size=2)
            horses.append({
                'name': f'h{j}', 'species': f'sp{j}',
                'speed': int(rng.integers(30, 160)), 'power': int(rng.integers(30, 160)),
                'stamina': int(rng.integers(20, 120)),
                'condition': ['好調', '普通', '不調'][int(rng.integers(0, 3))],
                'passives': [pas[k[0]], pas[k[1]]],
            })
        base = oc.predict_base(bundle, [dict(h, passives=tuple(h['passives']))
                                        for h in horses], dist, track)
        cases.append({'dist': dist, 'track': track, 'horses': horses,
                      'base': [float(x) for x in base]})
    # --- 装備・お守りの効果（図鑑30種）---
    # ⚠ カタログに無い効果名は Python が説明文から推測するのに対し、JS は**乗せない**。
    #    意図的な差（推測は race 2097 で15倍の誤りを出した実績がある）なので、
    #    検証はカタログにある30種だけを対象にする。
    item_cases = []
    for i, (label, desc, key) in enumerate(ITEM_DESCS):
        pct = round(1.0 + (i % 9) * 0.9, 1)
        d = desc.replace('@', str(pct))
        got = oc.item_effect_spec(f'{label}：{d}', key, spec)
        item_cases.append({'label': label, 'desc': d, 'key': key,
                           'py': (None if got is None else
                                  {k: float(v) for k, v in got.items()})})
    # --- 単勝（下限オッズの判定 → 希薄化を織り込んだ配分）---
    # ランダムなだけでは踏まない枝（未投票馬・取得漏れ NaN・下限に張り付いた大本命・
    # 自分がプールの大半を持っている状態）を、モードを回して必ず通す。
    def _nn(x):
        """JSON に NaN は書けないので null にする（JS 側で NaN に戻す）。"""
        x = float(x)
        return None if x != x else x

    win_cases = []
    for i in range(48):
        mode = i % 4
        a = np.ones(int(rng.integers(6, 13 if mode == 2 else 15))) * 1.2
        if mode == 1:
            a[0] = 20.0                                   # 1頭にシェアが集中＝下限に張り付く大本命
        od = list(np.round(1.0 / rng.dirichlet(a), 2))
        if mode == 2:
            od += [oc.UNBET_ODDS, oc.UNBET_ODDS]          # 未投票の馬（Σ(1/od) は 1 のまま）
        od = np.where(np.array(od, dtype=float) < oc.ODDS_FLOOR, oc.ODDS_FLOOR, od)
        if mode == 2 and i % 12 != 2:
            # Σ(1/od) を 1 から外し、下限表示の馬を「未投票」と言い切れない枝
            #（中間帯 / 下限が複数いて大本命を特定できない）を通す。
            od = np.where(od > oc.ODDS_FLOOR, np.round(od * (1.05 if i % 12 == 6 else 1.6), 2), od)
        if mode == 3:
            od[rng.choice(len(od), size=2, replace=False)] = np.nan
        n = len(od)
        my_amounts = np.zeros(n)
        if i % 3 == 0:                                    # 自分で買った下限表示馬（判別できる側）
            my_amounts[0 if mode == 1 else int(rng.integers(0, n))] = \
                float(rng.integers(1, 10)) * oc.WIN_STAKE_UNIT
        floor = 1.0 if i % 2 else oc.ODDS_FLOOR
        mkt = oc.market_win_prob(od, floor=floor)
        diag = oc.diagnose_floor_odds(od, my_amounts)

        win_p = rng.dirichlet(np.ones(n) * 3.0)
        pool = float([oc.WIN_POOL_SEED, 50_000, 1_000_000, 20_000][mode])
        my_units = np.zeros(n, dtype=int)
        if i % 2:
            for j in rng.choice(n, size=2, replace=False):
                my_units[j] = int(rng.integers(1, 6))
        if i % 8 == 5:                                    # 自分がプールの大半を持っている状態
            pool, my_units = 20_000.0, np.zeros(n, dtype=int)
            my_units[0] = 15
        if i % 16 == 7:                                   # 既に上限まで購入済み（note を返す枝）
            my_units = np.zeros(n, dtype=int)
            my_units[0] = oc.WIN_MAX_TOTAL_UNITS
        if i % 16 == 3:                                   # プール未測定（推奨なしの枝）
            pool = 0.0
        bankroll = float(rng.integers(50_000, 2_000_000))
        kelly, edge_min = (0.25, 0.5)[i % 2], (0.15, 0.05, 0.0)[i % 3]
        risk_cap = (0.10, 0.30)[i % 2]
        names_w = [f'h{j}' for j in range(n)]
        picks, summ = oc.win_bet_picks_pool(
            names_w, win_p, diag['odds_eff'], pool, bankroll, kelly, edge_min,
            stake_unit=oc.WIN_STAKE_UNIT, total_units=oc.WIN_MAX_TOTAL_UNITS,
            max_units=oc.WIN_MAX_UNITS, risk_cap_frac=risk_cap,
            my_units=my_units, unbet=diag['unbet'])
        win_cases.append({
            'names': names_w, 'odds': [_nn(x) for x in od], 'floor': floor,
            'my_amounts': [float(x) for x in my_amounts],
            'mkt': (None if mkt is None else [float(x) for x in mkt]),
            'diag': {'unbet': [bool(b) for b in diag['unbet']],
                     'odds_eff': [_nn(x) for x in diag['odds_eff']],
                     'ambiguous': bool(diag['ambiguous']),
                     'residual': (None if diag['residual'] is None
                                  else float(diag['residual'])),
                     'messages': list(diag['messages'])},
            'win_p': [float(x) for x in win_p], 'pool': pool, 'bankroll': bankroll,
            'kelly': kelly, 'edge_min': edge_min, 'risk_cap_frac': risk_cap,
            'my_units': [int(x) for x in my_units],
            'picks': picks, 'summary': summ})

    with open(os.path.join(HERE, '_parity_cases.json'), 'w', encoding='utf-8') as f:
        json.dump({'races': cases, 'items': item_cases, 'win': win_cases},
                  f, ensure_ascii=False)

    try:
        # Windows の既定は cp932 で、✅ や ⚠ を書けずに落ちる。明示的に UTF-8 で受ける。
        r = subprocess.run(['node', os.path.join(HERE, 'parity_test.js')],
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        code, out, err = r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        # node が無い環境（Windows の素の状態など）。検証はできないが失敗ではない。
        code, out, err = 0, ('⚠ node が見つからないので Python↔JS の一致検証を飛ばしました。\n'
                             '   model.js を触ったなら https://nodejs.org から入れて再実行してください。\n'), ''
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    for f in ('_parity_model.json', '_parity_cases.json'):
        try:
            os.remove(os.path.join(HERE, f))
        except OSError:
            pass
    print(f'  特徴量 {len(names)}列 / スタミナ収支 '
          f'{"あり" if "スタミナ余り" in names else "★なし★"}')
    return code


if __name__ == '__main__':
    sys.exit(main())
