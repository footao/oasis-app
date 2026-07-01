# -*- coding: utf-8 -*-
"""
oasis_app.py — Oasis 安定運用予測ツール（ローカル Streamlit アプリ）
==================================================================
起動:  streamlit run oasis_app.py
ロジックは oasis_core.py（UI非依存）。このファイルは画面と状態管理のみ。
ベットログはローカルCSVに永続化（Colabと違い手元に残る）。
"""
import os
import sys
import pandas as pd
import streamlit as st

import oasis_core as oc


def _disp_map(names):
    """表示用: 同名馬（ベース名が同じ）を出現順に いぬ1/いぬ2… へ。単独馬はベース名のまま。
    内部名（例 いぬ#3410）は一切変えず、画面表示のときだけ置き換えるための対応表を返す。"""
    order = []
    for n in names:
        n = str(n)
        if n not in order:
            order.append(n)
    groups = {}
    for n in order:
        groups.setdefault(oc.base_name(n), []).append(n)
    m = {}
    for base, lst in groups.items():
        if len(lst) <= 1:
            m[lst[0]] = base
        else:
            for i, n in enumerate(lst, 1):   # 出現（エントリー）順に 1,2,3…
                m[n] = f"{base}{i}"
    return m


def _dname(name, m):
    """1頭ぶんの内部名を表示名に。対応表に無ければそのまま。"""
    return m.get(str(name).strip(), str(name))


def _dcombo(combo, m):
    """『A → B → C』形式の買い目を表示名に変換。"""
    return ' → '.join(_dname(x, m) for x in str(combo).split('→'))

def _render_win(result, dmap, settings):
    """単勝モードの表示（推奨購入・購入リスト・全馬の勝率/オッズ）。"""
    st.subheader("🎯 単勝モード 推奨")
    if not result.get("has_market"):
        st.info("単勝オッズが取得できていないため推奨できません（貼り付けデータに単勝オッズが必要）。")
    else:
        recs = result.get("win_recs") or []
        edge_pct = settings.get("edge_min", 0.10) * 100
        if not recs:
            st.info(f"見送り: エッジ {edge_pct:.0f}% 以上の +EV 単勝がありません（無理に張りません）。")
        else:
            tu = sum(r["k"] for r in recs)
            tc = sum(r["cost"] for r in recs)
            tev = sum(r["ev"] for r in recs)
            hit = min(1.0, sum(r["model_p"] for r in recs))
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("投資額", f"{tc:,} rrc")
            c2.metric("推奨頭数 / 口数", f"{len(recs)}頭 / {tu}口")
            c3.metric("実効EV合計", f"{tev:+,.0f} rrc")
            c4.metric("いずれか的中", f"{hit*100:.0f}%")
            df = pd.DataFrame([{
                "": "✅", "馬": _dname(r["name"], dmap),
                "モデル勝率": f"{r['model_p']*100:.1f}%", "予想実効オッズ": f"{r['odds']:.1f}倍",
                "エッジ": f"{r['edge']*100:+.0f}%", "口数": f"{r['k']}口",
                "投資額": f"{r['cost']:,}rrc", "実効EV": f"{r['ev']:+,.0f}",
                "スタミナ": ("⚠不足" if r.get("below_cutoff") else "OK")} for r in recs])
            st.dataframe(df, use_container_width=True, hide_index=True)
            with st.expander("📋 購入リスト（単勝・1行1口）"):
                lines = []
                for r in recs:
                    for _ in range(r["k"]):
                        lines.append(_dname(r["name"], dmap))
                st.code("\n".join(lines) or "(なし)", language=None)
            st.caption("単勝＝1着を当てる賭け。1口1,000rrc・1頭100口・1レース合計100口（3連単とは独立）。"
                       "「予想実効オッズ」は現在の単勝オッズを使用（自分の投資でオッズが下がる分は未反映の近似。"
                       "厳密化には単勝プール総額が必要）。")
    sw = result.get("single_win") or []
    with st.expander("🥇 全馬の単勝 勝率・オッズ・EV"):
        df2 = pd.DataFrame([{
            "馬": _dname(r["name"], dmap), "モデル勝率": f"{r['model_p']*100:.1f}%",
            "単勝od": (f"{r['odds']:.1f}" if r.get("odds") else "—"),
            "市場勝率": (f"{r['market_p']*100:.1f}%" if r.get("market_p") is not None else "—"),
            "EV(1口)": (f"{(r['model_p']*r['odds']-1)*100:+.0f}%" if r.get("odds") else "—"),
            "スタミナ": ("⚠不足" if r.get("below_cutoff") else "OK")} for r in sw])
        st.dataframe(df2, use_container_width=True, hide_index=True)


def _app_dir():
    """アプリ（exeなら実行ファイル）のあるフォルダ。相対パスの基準にする。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_read(p):
    """読み込み元を解決。絶対/CWD相対で在ればそれ、無ければアプリフォルダ基準。
    戻り値: (解決済みパス, 存在するか)。"""
    raw = os.path.expanduser((p or "").strip())
    if not raw:
        return "", False
    if os.path.isabs(raw):
        cands = [raw]
    else:
        cands = [os.path.normpath(os.path.join(_app_dir(), raw)),  # アプリ隣（推奨）
                 os.path.abspath(raw)]                              # CWD相対
    for c in cands:
        if os.path.exists(c):
            return c, True
    return cands[0], False


def _resolve_save(p):
    """書き込み先を解決。絶対パスはそのまま、相対はアプリフォルダ基準（CWDに散らからない）。"""
    raw = os.path.expanduser((p or "").strip()) or "oasis_bet_log.csv"
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(_app_dir(), raw))

st.set_page_config(page_title="Oasis 安定運用予測", page_icon="🐎", layout="wide")

# ---- セッション状態 ----
ss = st.session_state
ss.setdefault("bundle", None)       # 学習済みモデル
ss.setdefault("result", None)       # 直近の解析結果
ss.setdefault("last_text", "")

DIST_OPTS = ["短距離", "マイル", "中距離", "長距離"]
TRACK_OPTS = ["芝", "ダート"]
GROUND_OPTS = ["良", "稍重", "重", "不良", "普通"]

# ============================ サイドバー ============================
with st.sidebar:
    st.header("⚙ 設定")

    st.subheader("1) モデル学習")
    st.caption(f"📁 アプリの場所: {_app_dir()}")
    st.caption("相対パスはこの場所が基準です。`OASISUTTI.txt` はここに置くのが簡単。")
    log_path = st.text_input("レースログのパス", value="OASISUTTI.txt",
                             help="ファイル名だけならアプリのフォルダ基準。フルパスも可。")
    log_resolved, log_found = _resolve_read(log_path)
    if log_resolved:
        st.caption(("✅ 見つかりました: " if log_found else "❌ 見つかりません: ") + log_resolved)
    sigma_override = st.number_input("σ手動上書き (0=自動)", min_value=0.0, value=0.0, step=0.5,
                                     help="成績レポートの校正ヒントに従い調整。0なら学習データから自動推定。")
    if st.button("📚 モデル学習 / 再学習", use_container_width=True):
        if not log_found:
            st.error(f"ログが見つかりません: {log_resolved}\n"
                     f"このファイルをアプリのフォルダ（{_app_dir()}）に置くか、フルパスを入力してください。")
        else:
            with st.spinner("学習中…（数秒）"):
                ss.bundle = oc.train_model(log_resolved,
                                           sigma_override=(sigma_override or None))
            ss.result = None
    bundle = ss.bundle
    if bundle and bundle.get("ok"):
        st.success("学習済み")
        for m in bundle["messages"]:
            st.caption(m)
    elif bundle:
        for m in bundle.get("messages", []):
            st.error(m)
    else:
        st.info("まずログを指定して学習してください。")

    st.divider()
    st.subheader("2) 安定運用パラメータ")
    bankroll = st.number_input("手元資金 BANKROLL (rrc)", min_value=10000,
                               value=1_200_000, step=50_000,
                               help="ケリーサイジングの基準。増減したら更新。")
    kelly = st.slider("分数ケリー", 0.05, 1.0, 0.25, 0.05,
                      help="0.25=クォーターケリー。小さいほど低分散・低成長。")
    risk = st.slider("1レース上限（資金比）", 0.02, 0.30, 0.10, 0.01,
                     help="このレースで賭ける総額の上限。")
    edge = st.slider("エッジ下限 (実効エッジ)", 0.0, 0.50, 0.10, 0.01,
                     help="p×実効od−1 がこの値未満の薄い買い目は見送り。")
    st.caption(f"→ 1レース上限 ≒ {int(risk*bankroll//oc.STAKE_UNIT)}口（{int(risk*bankroll):,} rrc）")

    st.markdown("**未成立スリーブ（任意）**")
    sleeve_on = st.checkbox("未成立組も少額で買う", value=False,
                            help="市場が張っていない組に各1口。当たれば全プール総取り＝高EVだが"
                                 "高分散・モデル依存。少額上限つき。")
    sleeve_units = st.radio("未成立の最大口数", [3, 4, 5], index=2, horizontal=True,
                            disabled=not sleeve_on)
    sleeve_pmin = st.slider("未成立の的中率下限", 0.01, 0.20, 0.05, 0.01,
                            disabled=not sleeve_on,
                            help="この的中率以上の未成立組のみ対象（既定5%）。")

    st.divider()
    st.subheader("3) レース条件")
    dist = st.selectbox("距離", DIST_OPTS, index=2)
    track = st.selectbox("馬場", TRACK_OPTS, index=0)
    topn = st.slider("ランキング表示数", 5, 40, 15)
    st.caption("「地面」は解析ボタンの隣で選べます。")

    with st.expander("詳細（CSV・CO・ログ保存先）"):
        csv_path = st.text_input("別CSVのパス（任意）", value="",
                                 help="貼り付けに3連単オッズが含まれない場合のみ使用。")
        co_rrc = st.number_input("キャリーオーバー手動指定 (0=自動)", min_value=0, value=0, step=10000)
        bet_log_path = st.text_input("ベットログ保存先(CSV)", value="oasis_bet_log.csv")

csv_resolved, _ = _resolve_read(csv_path) if csv_path else ("", False)
settings = dict(dist=dist, track=track, ground=GROUND_OPTS[0], topn=topn,
                bankroll=bankroll, kelly_fraction=kelly, max_risk_frac=risk,
                edge_min=edge, csv_path=csv_resolved, carryover_rrc=(co_rrc or None),
                unformed_sleeve=sleeve_on, unformed_max_units=sleeve_units,
                unformed_p_min=sleeve_pmin, unformed_edge_min=0.30)
# ground は解析ボタンの隣の selectbox で選び、解析直前に settings["ground"] に入れる。
bet_log_resolved = _resolve_save(bet_log_path)


@st.cache_resource(show_spinner=False)
def _get_sheets_store():
    """st.secrets に Sheets 設定があれば SheetsStore を返す。無ければ None。
    cache_resource で接続を1回だけ確立（毎回の再実行で再認証しない）。"""
    try:
        from sheets_backend import build_store_from_secrets
        return build_store_from_secrets(st.secrets)
    except Exception as e:
        ss["_sheets_err"] = str(e)
        return None


_store = _get_sheets_store()
betlog = oc.BetLog(bet_log_resolved, race_sigma=(
    bundle.get('race_sigma') if bundle and bundle.get('ok') else None),
    store=_store)

# ============================ メイン ============================
st.title("🐎 Oasis 安定運用予測ツール")
st.caption("安定運用版（成立のみ・分数ケリー・資金管理）。出力はモデルの的中率に依存します。"
           "実績ログで予測≈実測を確認してから本格運用してください。")

st.subheader("レースデータを貼り付け")
raw_text = st.text_area(
    "ステータス画面＋3連単オッズ（統合フォーマット）をそのまま貼り付け",
    height=200, value=ss.last_text,
    placeholder="=== 出走馬一覧 === … === 3連単オッズ === …")

col_a, col_b, col_c = st.columns([1.2, 1.2, 3])
with col_a:
    do_analyze = st.button("🎯 解析", type="primary", use_container_width=True)
with col_b:
    ground = st.selectbox("地面", GROUND_OPTS, index=0,
                          help="このレースの地面状態。解析の直前にここで選びます。")
with col_c:
    bet_mode = st.radio("賭け方", ["3連単", "単勝"], horizontal=True,
                        help="単勝＝1着を当てる賭け。単勝モードでは単勝の推奨購入を表示します。")
settings["ground"] = ground
settings["bet_mode"] = bet_mode

if do_analyze:
    if not raw_text.strip():
        st.warning("レースデータを貼り付けてください。")
    else:
        # 未学習なら、まず学習してから解析する
        if not (bundle and bundle.get("ok")):
            if not log_found:
                st.error(f"学習用ログが見つかりません: {log_resolved}\n"
                         f"サイドバー「レースログのパス」を確認してください。")
            else:
                with st.spinner("モデル未学習のため学習中…（数秒）"):
                    ss.bundle = oc.train_model(
                        log_resolved, sigma_override=(sigma_override or None))
                bundle = ss.bundle
                if bundle and bundle.get("ok"):
                    st.toast("モデルを自動学習しました。", icon="📚")
                else:
                    for m in (bundle.get("messages", []) if bundle else []):
                        st.error(m)
        # 学習済みになっていれば解析を実行
        if bundle and bundle.get("ok"):
            ss.last_text = raw_text
            ss.result = oc.analyze(raw_text, bundle, settings)

result = ss.result

# ---------------------- 解析結果 ----------------------
if result is not None:
    if not result.get("ok"):
        st.error(result.get("error", "解析に失敗しました。"))
    else:
        for m in result["messages"]:
            (st.warning if m.startswith("⚠") else st.info)(m)

        # 表示用の同名馬ナンバリング（内部名は不変・画面表示だけ いぬ1/いぬ2…）
        dmap = _disp_map(result.get("horses_disp") or [])

        if bet_mode == "単勝":
            _render_win(result, dmap, settings)
        else:
            # 推奨配分サマリ
            sm = result.get("summary")
            if sm:
                st.subheader("🎯 推奨配分（安定運用）")
                for pm in result["pool_msgs"]:
                    st.caption("🔎 " + pm)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("投資額", f"{sm['invest']:,} rrc", f"資金の {sm['invest_pct']:.1f}%")
                split = (f"成{sm['n_formed']}+未{sm['n_unformed']}口"
                         if sm.get('n_unformed') else f"{sm['total_units']}口")
                c2.metric("推奨点数 / 口数", f"{sm['n_points']}点 / {split}")
                c3.metric("実効EV合計", f"{sm['tev']:+,.0f} rrc")
                c4.metric("いずれか的中", f"{sm['hit']*100:.0f}%", f"全外し {sm['miss']*100:.0f}%")
                st.caption(f"プロファイル: 資金 {sm['bankroll']:,} / {sm['kelly_pct']}%ケリー / "
                           f"1レース上限 {sm['risk_pct']:.0f}%(={sm['risk_units']}口) / "
                           f"エッジ下限 {sm['edge_pct']:.0f}% / 払戻プール {sm['pool']:,} rrc")
                if sm.get('n_unformed'):
                    st.warning(f"未成立スリーブ {sm['n_unformed']}口を含みます（各1口・全プール総取り狙い）。"
                               "高EVですが高分散で、当たりは稀。モデルの的中率が正しい前提です"
                               "（成績レポートで低確率帯の校正を確認してから）。")

                rows = result["alloc_rows"]
                if any(r["mark"] == "✅" for r in rows):
                    df = pd.DataFrame([{
                        "": r["mark"], "状態": r.get("flag", "成"), "買い目": _dcombo(r["combo"], dmap),
                        "的中率": f"{r['model_p']*100:.2f}%",
                        "表示od": (f"{r['disp_od']:.1f}" if r["disp_od"] else "—"),
                        "理論EV": (f"{r['theo_ev']:+,.0f}" if r["theo_ev"] is not None else "—"),
                        "口数": (f"{r['k']}口" if r["k"] else ""),
                        "実効od": (f"{r['eff_od']:.1f}" if r["eff_od"] else ""),
                        "実効EV": (f"{r['eff_ev']:+,.0f}" if r["eff_ev"] is not None else ""),
                    } for r in rows])
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    st.caption("✅=購入推奨（状態 成=成立 / 未=未成立スリーブ） / "
                               "△=+理論EVだが安定ルールで見送り")
                    if result.get("bare_used"):
                        st.caption("※ 一部は素名フォールバックで照合（同名別個体を合算）。")
                    if result.get("unmatched_names"):
                        st.warning("画面に無い馬: " + ", ".join(result["unmatched_names"]))

                    # 購入リスト（コピー用）
                    with st.expander("📋 購入リスト（コピー用・1行1口）"):
                        st.code("\n".join(result["purchase_lines"]) or "(なし)", language=None)
                else:
                    st.info(f"見送り: エッジ {sm['edge_pct']:.0f}% 以上の成立買い目がありません"
                            "（安定運用では無理に張りません）。")

            # 損益分岐（CSV未指定時）
            elif result.get("breakeven_rows"):
                st.subheader("🎯 損益分岐オッズ表（CSV未指定）")
                st.caption("実オッズ > 必要オッズ なら +EV。")
                st.dataframe(pd.DataFrame([{
                    "買い目": _dcombo(r["combo"], dmap), "モデル的中率": f"{r['model_p']*100:.2f}%",
                    "必要オッズ": f"{r['need_od']:.1f}倍"} for r in result["breakeven_rows"]],
                    ), use_container_width=True, hide_index=True)

            # 的中確率ランキング
            st.subheader("🏆 的中確率ランキング（1口購入時の実効オッズ付き）")
            rk = result["ranking"]
            if result["ranking_pool_known"]:
                df = pd.DataFrame([{
                    "#": r["rank"], "買い目": _dcombo(r["combo"], dmap),
                    "的中率": f"{r['model_p']*100:.2f}%",
                    "累積": f"{r['cum']*100:.1f}%", "状態": r["flag"],
                    "1口実効od": f"{r['eff1_od']:.1f}倍",
                    "1口EV": f"{r['ev1']:+,.0f}",
                    "+EV": ("◎" if r["plus_ev"] else "")} for r in rk])
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"上位{len(rk)}点でモデル確率の {result['ranking_cover']*100:.1f}% をカバー。"
                           "状態 未=未成立（自分が唯一なら全プール総取り＝高倍率。安定版では非推奨）。")
            else:
                df = pd.DataFrame([{
                    "#": r["rank"], "買い目": _dcombo(r["combo"], dmap),
                    "的中率": f"{r['model_p']*100:.2f}%", "累積": f"{r['cum']*100:.1f}%",
                    "状態": r["flag"]} for r in rk])
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption("プール未取得のため実効odは算出不可。")

            # 単勝（参考）
            with st.expander("🥇 単勝 勝率：モデル vs 市場（参考・3連単EVには未使用）"):
                sw = result["single_win"]
                if result.get("has_market"):
                    df = pd.DataFrame([{
                        "馬": _dname(r["name"], dmap), "モデル": f"{r['model_p']*100:.1f}%",
                        "市場": (f"{r['market_p']*100:.1f}%" if r["market_p"] is not None else "—"),
                        "オッズ": (f"{r['odds']:.2f}" if r["odds"] else "—"),
                        "スタミナ": ("⚠不足" if r.get("below_cutoff") else "OK"),
                        "判定": r["tag"]} for r in sw])
                else:
                    df = pd.DataFrame([{
                        "馬": _dname(r["name"], dmap), "モデル勝率": f"{r['model_p']*100:.1f}%",
                        "スタミナ": ("⚠不足" if r.get("below_cutoff") else "OK"),
                        "フェアod": (f"{1/r['model_p']:.1f}倍" if r['model_p'] > 0.001 else "—")}
                        for r in sw])
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.caption(f"モデルの◎: 【{_dname(result['model_pick'], dmap)}】"
                           "／ スタミナ⚠不足=攻略本の必要最低スタミナ未満（スコア大幅減）")

# ============================ ベットログ ============================
st.divider()
st.header("📒 実績ログ（答え合わせ）")
if _store is not None:
    st.caption("🗂 保存先: Google スプレッドシート（クラウド保存・再起動でも消えません）")
elif ss.get("_sheets_err"):
    st.warning("Google Sheets 接続に失敗したためローカルCSVを使用します: "
               + ss["_sheets_err"] + f"\n保存先: {betlog.path}")
else:
    st.caption(f"保存先: {betlog.path}（このフォルダに残ります）")

lc1, lc2 = st.columns(2)
with lc1:
    st.subheader("① 賭けを記録")
    default_rid = ""
    rid = st.text_input("レースID", value=default_rid,
                        placeholder="空欄なら日時を自動採番")
    can_log = bool(result and result.get("ok") and result.get("picks"))
    if st.button("✅ 推奨を記録（pending）", disabled=not can_log, use_container_width=True):
        from datetime import datetime
        rid2 = rid.strip() or datetime.now().strftime("%Y%m%d_%H%M")
        if betlog.race_exists(rid2):
            st.error(f"レースID『{rid2}』は記録済み。別IDにするか取消/精算してください。")
        else:
            try:
                n = betlog.record(rid2, result["picks"], oc.STAKE_UNIT)
                st.success(f"レース『{rid2}』に {n}点を記録しました（pending）。")
                st.caption(f"保存先: {betlog.path}")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}\n保存先: {betlog.path}")
    if not can_log:
        st.caption("先に『解析』を実行し、✅推奨が出ている状態で押してください。")

with lc2:
    st.subheader("② 結果を入力して精算")
    log_df_now = betlog.load()
    pend_ids = sorted(set(log_df_now[log_df_now["status"] == "pending"]["race_id"].astype(str))) \
        if len(log_df_now) else []
    rid_settle = st.selectbox("精算するレースID", options=(pend_ids or ["(pendingなし)"]))

    # 着順候補: 記録済みの買い目に出る馬 ∪ 直近解析の全出走馬（できるだけ全頭そろえる）
    horses = set(betlog.race_horses(rid_settle)) if pend_ids else set()
    if result and result.get("ok"):
        horses |= set(result.get("horses_disp") or [])
    horses = sorted(horses)

    # 表示用ナンバリング（選択の内部値は不変。解析中レースなら出現順を引き継ぐ）
    _seed = (result.get("horses_disp") if (result and result.get("ok")) else None) or []
    smap = _disp_map(list(_seed) + horses)

    manual = st.checkbox("候補にない馬を手入力する", value=False, key="settle_manual",
                         help="賭けていない馬が着順に入った等で、プルダウンに無い馬を直接入力したいとき")
    cc = st.columns(3)
    if manual:
        o1 = cc[0].text_input("実1着", key="o1t")
        o2 = cc[1].text_input("実2着", key="o2t")
        o3 = cc[2].text_input("実3着", key="o3t")
    else:
        o1 = cc[0].selectbox("実1着", options=(horses or ["—"]), key="o1",
                              format_func=lambda x: _dname(x, smap))
        o2 = cc[1].selectbox("実2着", options=(horses or ["—"]), key="o2",
                              format_func=lambda x: _dname(x, smap))
        o3 = cc[2].selectbox("実3着", options=(horses or ["—"]), key="o3",
                              format_func=lambda x: _dname(x, smap))
    if st.button("🏁 精算", disabled=not pend_ids, use_container_width=True):
        o1s, o2s, o3s = (o1 or "").strip(), (o2 or "").strip(), (o3 or "").strip()
        if not (o1s and o2s and o3s):
            st.error("1〜3着すべてを入力してください。")
        elif len({o1s, o2s, o3s}) < 3:
            st.error("1〜3着に同じ馬が選ばれています。")
        else:
            try:
                cnt = betlog.settle(rid_settle, (o1s, o2s, o3s))
                if cnt == 0:
                    st.warning(f"レース『{rid_settle}』に精算対象（pending）がありませんでした。")
                else:
                    df_after = betlog.load()
                    sub = df_after[df_after["race_id"].astype(str) == str(rid_settle)]
                    won = int((sub["status"] == "won").sum())
                    st.success(f"レース『{rid_settle}』を精算（{cnt}点中 的中{won}点）。"
                               f"結果: {_dname(o1s, smap)} → {_dname(o2s, smap)} → {_dname(o3s, smap)}")
                    st.caption(f"保存先: {betlog.path}")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}\n保存先: {betlog.path}")
    st.caption("候補は「記録済みの買い目＋直近解析の全出走馬」から作成。"
               "賭けていない馬が入線した等で出てこない場合は、上のチェックで手入力できます。")

rc1, rc2 = st.columns([1, 1])
with rc1:
    show_report = st.button("📊 成績レポート", use_container_width=True)
with rc2:
    if st.button("↩ 直近レースを取消", use_container_width=True):
        rid_del, ndel = betlog.undo_last()
        if ndel:
            st.success(f"レース『{rid_del}』の {ndel}件を取消しました。")
        else:
            st.info("取り消すレコードがありません。")

if show_report:
    rep = betlog.report()
    if rep.get("empty"):
        st.info("ログがありません。まず✅を記録してください。")
    else:
        st.write(f"総ベット {rep['n_total']}件（精算済 {rep['n_settled']} / 未精算 {rep['n_pending']}）")
        ov = rep.get("overall")
        if ov:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("損益", f"{ov['pnl']:+,.0f} rrc", f"ROI {ov['roi']:+.1f}%")
            m2.metric("投資 / 払戻", f"{ov['stake']:,.0f} / {ov['payout']:,.0f}")
            m3.metric("的中率(実測)", f"{ov['hit_rate']:.2f}%", f"{ov['hits']}/{ov['n']}")
            m4.metric("モデル予測平均", f"{ov['pred_rate']:.2f}%")
            if rep["buckets"]:
                st.caption("キャリブレーション（予測帯ごとの 予測 vs 実測）")
                st.dataframe(pd.DataFrame([{
                    "予測帯": b["label"], "件数": b["n"],
                    "予測": f"{b['pred']:.2f}%", "実測": f"{b['real']:.2f}%",
                    "損益": f"{b['pnl']:+,.0f}"} for b in rep["buckets"]],
                    ), use_container_width=True, hide_index=True)
            if rep.get("calib_hint"):
                st.info("🔧 校正ヒント: " + rep["calib_hint"])
            st.caption("予測≈実測なら的中率は信頼でき、ROIが+ならこの買い方は本当に+EV。"
                       "数十レース貯めてから判断を。")
        else:
            st.info("まだ精算済みのレースがありません。")

with st.expander("🗂 ログ全体を見る"):
    df_all = betlog.load()
    if len(df_all):
        st.dataframe(df_all, use_container_width=True, hide_index=True)
    else:
        st.caption("（まだ記録がありません）")
