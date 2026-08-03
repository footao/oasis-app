# -*- coding: utf-8 -*-
"""
oasis_app.py — Oasis 安定運用予測ツール v2（2026/07/27 大型アプデ対応）
======================================================================
起動:  streamlit run oasis_app.py
ロジックは oasis_core.py（UI非依存）。このファイルは画面と状態管理のみ。
"""
import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

import oasis_core as oc

try:
    import sheets_backend
except Exception:
    sheets_backend = None
try:
    import drive_backend
except Exception:
    drive_backend = None


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_read(p):
    raw = os.path.expanduser((p or "").strip())
    if not raw:
        return "", False
    cands = [raw] if os.path.isabs(raw) else [
        os.path.normpath(os.path.join(_app_dir(), raw)), os.path.abspath(raw)]
    for c in cands:
        if os.path.exists(c):
            return c, True
    return cands[0], False


def _auto_log_path():
    """リポジトリ（アプリと同じ場所）にあるログを自動で探す。
    logg/ フォルダ → logs/ → data/ → 直下の *.txt の順。"""
    base = _app_dir()
    for cand in ("logg", "logs", "log", "data"):
        d = os.path.join(base, cand)
        if os.path.isdir(d) and any(f.lower().endswith((".txt", ".md"))
                                    for f in os.listdir(d)):
            return cand
    try:
        txts = [f for f in os.listdir(base) if f.lower().endswith(".txt")]
    except OSError:
        txts = []
    if len(txts) == 1:
        return txts[0]
    if txts:
        return "*.txt"
    return "logg"


def _spec_path():
    return os.path.join(_app_dir(), oc.SPEC_FILE)


def _resolve_save(p):
    raw = os.path.expanduser((p or "").strip()) or "oasis_bet_log.csv"
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(_app_dir(), raw))


st.set_page_config(page_title="Oasis 予測 v2", page_icon="🐎", layout="wide")


# ---------------------- 保存先バックエンド ----------------------
@st.cache_resource(show_spinner=False)
def _get_sheets_store():
    """ベットログの保存先（Google スプレッドシート）。未設定なら None。"""
    if sheets_backend is None:
        return None
    try:
        return sheets_backend.build_store_from_secrets(st.secrets)
    except Exception as e:
        st.session_state["_sheets_error"] = str(e)
        return None


@st.cache_resource(show_spinner=False)
def _get_drive_source():
    """学習ログの取得元（Google ドライブ）。未設定なら None。"""
    if drive_backend is None:
        return None
    try:
        return drive_backend.build_source_from_secrets(st.secrets)
    except Exception as e:
        st.session_state["_drive_error"] = str(e)
        return None


@st.cache_data(show_spinner="Google ドライブからログを取得中…", ttl=600)
def _drive_texts(fingerprint):
    """ドライブのログ本文。fingerprint（更新時刻）が変わると自動で取り直す。"""
    src = _get_drive_source()
    return src.download_texts() if src else []


@st.cache_data(show_spinner=False, ttl=600)
def _drive_fingerprint():
    src = _get_drive_source()
    return src.fingerprint() if src else ()


@st.cache_resource(show_spinner="モデル学習中…（初回は数十秒）")
def _train_cached(source_key, sigma_override, train_from, sigma_safety, _texts, log_path):
    """同じ入力なら再学習しない。source_key に指紋を入れて差し替えを検知する。"""
    return oc.train_model(log_path or None,
                          texts=list(_texts) if _texts else None,
                          sigma_override=(sigma_override or None),
                          train_from=train_from,
                          spec_path=_spec_path(),
                          sigma_safety=sigma_safety)

# --- streamlit のバージョン差を吸収する小さなラッパ ---
def _wide(**kw):
    """幅いっぱい表示のキーワードを、使っている streamlit に合わせて返す。"""
    try:
        import inspect
        params = inspect.signature(st.dataframe).parameters
        if "width" in params:
            return dict(width="stretch", **kw)
    except Exception:
        pass
    return dict(use_container_width=True, **kw)


ss = st.session_state
ss.setdefault("bundle", None)
ss.setdefault("result", None)
ss.setdefault("last_text", "")

# ============================ サイドバー ============================
with st.sidebar:
    st.header("⚙ 設定")

    st.subheader("1) モデル学習")

    drive_src = _get_drive_source()
    SRC_REPO, SRC_DRIVE, SRC_UP = "📁 リポジトリ内のファイル", "☁ Google ドライブ", "⬆ アップロード"
    src_opts = [SRC_REPO] + ([SRC_DRIVE] if drive_src is not None else []) + [SRC_UP]
    src_mode = st.radio("ログの取得元", src_opts, index=0, horizontal=False,
                        help="既定はリポジトリ内（GitHubに置いたファイル）です。"
                             "ドライブは secrets に [gdrive] を設定すると選べます。")

    log_texts, source_key, log_path, src_label = [], None, "", ""

    if src_mode == SRC_UP:
        uploaded = st.file_uploader(
            "ログファイルを選ぶ", type=["txt", "md"], accept_multiple_files=True,
            help="Discordエクスポートの .txt。複数選択できます。")
        if uploaded:
            for f in uploaded:
                raw = f.getvalue()
                for enc in ("utf-8", "utf-8-sig", "cp932"):
                    try:
                        log_texts.append(raw.decode(enc))
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    log_texts.append(raw.decode("utf-8", errors="replace"))
            source_key = ("upload", tuple(sorted((f.name, f.size) for f in uploaded)))
            src_label = f"⬆ アップロード {len(uploaded)}ファイル"
        else:
            src_label = "⬆ ファイルを選んでください"

    elif src_mode == SRC_DRIVE and drive_src is not None:
        try:
            fp = _drive_fingerprint()
            files = _drive_texts(fp)
            log_texts = [t for _, t in files]
            source_key = ("drive", fp)
            src_label = "☁ " + ", ".join(n for n, _ in files)[:90]
        except Exception as e:
            st.error(f"ドライブからの読み込みに失敗しました: {e}")
            src_label = "☁ Google ドライブ（エラー）"

    else:   # リポジトリ内（既定）
        log_path_in = st.text_input(
            "ログのパス", value=_auto_log_path(),
            help="アプリと同じ場所が基準です。フォルダ名（logg）を入れると中の .txt を"
                 "まとめて読みます。ワイルドカード（logg/*.txt）やフルパスも可。")
        log_path, found = _resolve_read(log_path_in)
        source_key = ("path", log_path, found)
        src_label = ("📁 " if found else "❌ 見つかりません: ") + log_path
        if not found:
            st.caption("GitHub のリポジトリに `logg/` フォルダごとログを置いてください。"
                       "（置き場所を変えた場合は上の欄をそれに合わせてください）")

    st.caption(src_label)
    if st.session_state.get("_drive_error"):
        st.warning("ドライブ設定エラー: " + st.session_state["_drive_error"])

    train_from = st.text_input(
        "学習に使う期間の開始日", value=oc.DEFAULT_TRAIN_FROM,
        help=f"既定 {oc.SCORING_PATCH_DATE} = スコアがタイム基準に変わった日。"
             "それ以前のレースは計算式が別物なので、原則として使いません。")
    sigma_override = st.number_input(
        "σ手動上書き (0=自動校正)", min_value=0.0, value=0.0, step=0.005, format="%.4f",
        help="0なら『実際の着順が最も出やすいσ』を自動で探します。")
    sigma_safety = st.slider(
        "σ安全係数", 1.0, 2.0, oc.SIGMA_SAFETY, 0.05,
        help="自動校正したσに掛ける係数。1.0＝データが言うとおり（強気・買い目が絞られる）、"
             "大きいほど弱気で買い目が広がり、過剰投資を防ぎます。"
             "学習レース数が少ないうちは 1.2〜1.4 を推奨。")

    auto = st.checkbox("開いたら自動で学習する", value=True,
                       help="学習結果はキャッシュされるので、2回目以降は待ち時間ゼロです。")
    retrain = st.button("📚 モデル学習 / 再学習", **_wide(), type="primary")
    if retrain:
        _train_cached.clear()
        _drive_texts.clear()
        _drive_fingerprint.clear()

    bundle = None
    if (auto or retrain) and (log_texts or log_path):
        try:
            bundle = _train_cached(source_key, sigma_override, train_from.strip()
                                   or oc.DEFAULT_TRAIN_FROM, sigma_safety,
                                   tuple(log_texts), log_path)
        except Exception as e:
            st.error(f"学習に失敗しました: {e}")
    ss.bundle = bundle or ss.bundle
    bundle = ss.bundle

    if bundle and bundle.get("ok"):
        st.success("学習済み")
        for m in bundle["messages"]:
            st.caption(m)
        for w in bundle.get("warnings", []):
            st.warning(w)
    elif bundle:
        for m in bundle.get("messages", []):
            st.error(m)
    else:
        st.info("ログを指定して学習してください。")

    st.divider()
    st.subheader("2) 安定運用パラメータ")
    bankroll = st.number_input("手元資金 BANKROLL (rrc)", min_value=10000,
                               value=1_200_000, step=50_000)
    kelly = st.slider("分数ケリー", 0.05, 1.0, 0.25, 0.05,
                      help="0.25=クォーターケリー。小さいほど低分散・低成長。")
    risk = st.slider("1レース上限（資金比）", 0.02, 0.30, 0.10, 0.01)
    edge = st.slider("エッジ下限 (実効エッジ)", 0.0, 0.50, 0.10, 0.01)
    st.caption(f"→ 1レース上限 ≒ {int(risk*bankroll//oc.STAKE_UNIT)}口"
               f"（{int(risk*bankroll):,} rrc / 3連単は最大 {oc.MAX_TOTAL_UNITS}口）")

    st.markdown("**未成立スリーブ（任意）**")
    sleeve_on = st.checkbox("未成立組も少額で買う", value=False,
                            help="市場が張っていない組に各1口。当たれば全プール総取り。高EVだが高分散。")
    sleeve_units = st.radio("未成立の最大口数", [3, 4, 5], index=2, horizontal=True,
                            disabled=not sleeve_on)
    sleeve_pmin = st.slider("未成立の的中率下限", 0.01, 0.20, 0.05, 0.01, disabled=not sleeve_on)

    st.markdown("**単勝（任意・参考）**")
    win_on = st.checkbox("単勝の推奨も出す", value=False,
                         help=f"単勝は {oc.WIN_MAX_UNITS}口まで購入可。プール額が分からないため"
                              "自分の購入によるオッズ低下を織り込めません。控えめに。")
    win_edge = st.slider("単勝のエッジ下限", 0.0, 1.0, 0.15, 0.05, disabled=not win_on)

    st.divider()
    st.subheader("3) レース条件（貼り付けデータがあれば自動）")
    dist = st.selectbox("距離", oc.DIST_LIST, index=2)
    track = st.selectbox("馬場", oc.TRACK_LIST, index=0)
    ground = st.selectbox("地面", ["良", "稍重", "重", "不良"], index=0)
    topn = st.slider("ランキング表示数", 5, 60, 20)

    with st.expander("詳細（CSV・CO・ログ保存先・試行数）"):
        csv_path = st.text_input("別CSVのパス（任意）", value="")
        co_rrc = st.number_input("キャリーオーバー手動指定 (0=自動)", min_value=0, value=0, step=10000)
        bet_log_path = st.text_input(
            "ベットログ保存先(CSV)", value="oasis_bet_log.csv",
            help="Google スプレッドシートが設定されている場合はそちらが優先されます。")
        n_sim = st.select_slider("モンテカルロ試行数", [200_000, 400_000, 800_000],
                                 value=oc.N_SIM)

csv_resolved, _ = _resolve_read(csv_path) if csv_path else ("", False)
settings = dict(dist=dist, track=track, ground=ground, topn=topn,
                bankroll=bankroll, kelly_fraction=kelly, max_risk_frac=risk,
                edge_min=edge, csv_path=csv_resolved, carryover_rrc=(co_rrc or None),
                unformed_sleeve=sleeve_on, unformed_max_units=sleeve_units,
                unformed_p_min=sleeve_pmin, unformed_edge_min=0.30,
                win_bets=win_on, win_edge_min=win_edge, n_sim=n_sim,
                spec_path=_spec_path())
bet_log_resolved = _resolve_save(bet_log_path)
_store = _get_sheets_store()
betlog = oc.BetLog(bet_log_resolved, store=_store, race_sigma=(
    bundle.get('race_sigma') if bundle and bundle.get('ok') else None))

# ============================ メイン ============================
st.title("🐎 Oasis 予測ツール v2")
st.caption(f"2026/07/27 大型アプデ（パッシブ2枠・新スキル17種）と 07/28 のスコア式変更に対応。"
           f"モデルは {oc.SCORING_PATCH_DATE} 以降のレースだけを使って学習します。")

tab_pred, tab_model, tab_log = st.tabs(["🎯 予測", "🔬 モデルを見る", "📒 実績ログ"])

# ---------------------------- 予測タブ ----------------------------
with tab_pred:
    raw_text = st.text_area(
        "レースデータを貼り付け（統合フォーマット / 購入画面のコピーどちらでもOK）",
        height=220, value=ss.last_text,
        placeholder="=== 出走馬一覧 === … === 3連単オッズ === …\n"
                    "または購入画面をそのままコピペ（パッシブの説明文ごと貼ると、\n"
                    "『スピードが35%上昇』などの数値を自動で取り込みます）")
    st.caption("💡 購入画面をパッシブの説明文ごと貼ると、スキルの実数値を自動学習して "
               "`passive_spec.json` に保存します。新しい数値を覚えたら再学習してください。")
    col_a, _ = st.columns([1, 5])
    with col_a:
        do_analyze = st.button("🎯 解析", type="primary", **_wide())

    if do_analyze:
        if not (bundle and bundle.get("ok")):
            st.error("モデル未学習です。サイドバーで学習してください。")
        elif not raw_text.strip():
            st.warning("レースデータを貼り付けてください。")
        else:
            ss.last_text = raw_text
            with st.spinner("シミュレーション中…"):
                ss.result = oc.analyze(raw_text, bundle, settings)

    result = ss.result
    if result is not None:
        if not result.get("ok"):
            st.error(result.get("error", "解析に失敗しました。"))
        else:
            for m in result["messages"]:
                (st.warning if m.startswith("⚠") else st.info)(m)

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
                st.caption(f"資金 {sm['bankroll']:,} / {sm['kelly_pct']}%ケリー / "
                           f"1レース上限 {sm['risk_pct']:.0f}%(={sm['risk_units']}口) / "
                           f"エッジ下限 {sm['edge_pct']:.0f}% / 払戻プール {sm['pool']:,} rrc")
                if sm.get('n_unformed'):
                    st.warning(f"未成立スリーブ {sm['n_unformed']}口を含みます（各1口・全プール総取り狙い）。"
                               "高EVですが当たりは稀です。")

                rows = result["alloc_rows"]
                if any(r["mark"] == "✅" for r in rows):
                    view = [r for r in rows if r["mark"]][:60]
                    st.dataframe(pd.DataFrame([{
                        "": r["mark"], "状態": r.get("flag", "成"), "買い目": r["combo"],
                        "的中率": f"{r['model_p']*100:.2f}%",
                        "表示od": (f"{r['disp_od']:.1f}" if r["disp_od"] else "—"),
                        "理論EV": (f"{r['theo_ev']:+,.0f}" if r["theo_ev"] is not None else "—"),
                        "口数": (f"{r['k']}口" if r["k"] else ""),
                        "実効od": (f"{r['eff_od']:.1f}" if r["eff_od"] else ""),
                        "実効EV": (f"{r['eff_ev']:+,.0f}" if r["eff_ev"] is not None else ""),
                    } for r in view]), **_wide(hide_index=True))
                    st.caption("✅=購入推奨（成=成立 / 未=未成立スリーブ） / △=+理論EVだが安定ルールで見送り")
                    if result.get("bare_used"):
                        st.caption("※ 一部は素名フォールバックで照合（同名別個体を合算）。")
                    if result.get("unmatched_names"):
                        st.warning("画面に無い馬: " + ", ".join(result["unmatched_names"]))
                    with st.expander("📋 購入リスト（コピー用）"):
                        comp = [f"{r['combo']} x{r['k']}" for r in rows if r["k"] > 0]
                        st.caption("一括購入ブックマークレットにそのまま貼れます。")
                        st.code("\n".join(comp) or "(なし)", language=None)
                        st.caption("1行1口の形式が必要な場合はこちら")
                        st.code("\n".join(result["purchase_lines"]) or "(なし)", language=None)
                else:
                    st.info(f"見送り: エッジ {sm['edge_pct']:.0f}% 以上の成立買い目がありません。")

            elif result.get("breakeven_rows"):
                st.subheader("🎯 損益分岐オッズ表（3連単オッズ未指定）")
                st.caption("実オッズ > 必要オッズ なら +EV。")
                st.dataframe(pd.DataFrame([{
                    "買い目": r["combo"], "モデル的中率": f"{r['model_p']*100:.2f}%",
                    "必要オッズ": f"{r['need_od']:.1f}倍"} for r in result["breakeven_rows"]]),
                    **_wide(hide_index=True))

            if result.get("win_picks"):
                st.subheader("🥇 単勝の推奨（参考）")
                ws = result.get("win_summary") or {}
                if ws:
                    w1, w2, w3 = st.columns(3)
                    w1.metric("投資額", f"{ws['invest']:,} rrc",
                              f"{ws['units']}口 / 上限{ws['max_units']}口")
                    w2.metric("理論EV合計", f"{ws['ev']:+,.0f} rrc")
                    w3.metric("いずれか的中", f"{min(ws['hit'],1.0)*100:.0f}%")
                    if ws.get("capped"):
                        st.caption(f"※ 1レースの単勝は**合計{ws['max_units']}口まで**という"
                                   "ゲーム仕様の上限に達したため、エッジの大きい順に配分しています。")
                st.dataframe(pd.DataFrame([{
                    "馬": r["name"], "モデル勝率": f"{r['p']*100:.1f}%",
                    "オッズ": f"{r['odds']:.2f}", "エッジ": f"{r['edge']*100:+.0f}%",
                    "口数": f"{r['units']}口", "投資": f"{r['stake']:,}",
                    "理論EV": f"{r['ev']:+,.0f}"} for r in result["win_picks"]]),
                    **_wide(hide_index=True))
                with st.expander("📋 単勝の購入リスト（コピー用）"):
                    st.code("\n".join(f"{r['name']} x{r['units']}"
                                      for r in result["win_picks"]) or "(なし)", language=None)
                st.caption(f"1口 = {result.get('win_unit', oc.WIN_STAKE_UNIT):,} rrc"
                           f"（1レース**合計 {oc.WIN_MAX_TOTAL_UNITS}口**まで）。"
                           "⚠ 単勝はプール額が分からないため、自分の購入によるオッズ低下を"
                           "織り込めていません。表示より実効オッズは必ず低くなります。")

            # ---------- 単勝：プール推定 ----------
            with st.expander("🥇 単勝：プールを実測して推奨を出す（少額の試し買いで測ります）"):
                st.markdown(
                    "単勝は**控除0%の純パリミュチュエル**で、実ログ379レースすべてで "
                    "`Σ(1/最終オッズ) = 1.000` でした。つまり**オッズは各馬のシェアしか表さず、"
                    "プール総額の情報を一切含みません**。"
                    "そのため少額で試し買いして、その前後のオッズの動きから逆算します。")
                st.markdown(
                    "**手順** ① オッズ取得ブックマークレットでデータを取る → "
                    "② 好きな馬に少額（3〜10口目安）だけ単勝を買う → "
                    "③ もう一度データを取る → 下に①と③を貼る")
                pc1, pc2 = st.columns(2)
                t_before = pc1.text_area("① 試し買い **前** のデータ", height=110, key="wp_b")
                t_after = pc2.text_area("③ 試し買い **後** のデータ", height=110, key="wp_a")
                if st.button("🔎 プールを推定して推奨を出す", **_wide()):
                    if not (t_before.strip() and t_after.strip()):
                        st.warning("①と③の両方を貼り付けてください。")
                    else:
                        try:
                            hb = oc.parse_unified(t_before)[0]
                            ha = oc.parse_unified(t_after)[0]
                            est = oc.estimate_win_pool(hb, ha)
                        except Exception as e:
                            est = {"ok": False, "messages": [f"解析に失敗しました: {e}"]}
                        for m in est.get("messages", []):
                            (st.warning if m.startswith("⚠") else st.info)(m)
                        if est.get("ok"):
                            st.metric("推定プール総額", f"{est['pool']:,.0f} rrc",
                                      f"{est['n_used']}頭から / ばらつき ±{est['spread']*100:.0f}%")
                            det = [d for d in est["per_horse"] if d.get("est")]
                            if det:
                                st.dataframe(pd.DataFrame([{
                                    "馬": d["name"],
                                    "前": f"{d['od_before']:.2f}", "後": f"{d['od_after']:.2f}",
                                    "この馬からの推定": f"{d['est']:,.0f}",
                                    "備考": d.get("note", "")} for d in det]),
                                    **_wide(hide_index=True))
                            res2 = oc.analyze(t_after, bundle, settings)
                            if res2.get("ok"):
                                sw = res2["single_win"]
                                nm = [r["name"] for r in sw]
                                pp = [r["model_p"] for r in sw]
                                oo = [(r["odds"] if r["odds"] else float("nan")) for r in sw]
                                mine = {h["name"]: (h.get("my_amount") or 0) for h in ha}
                                unit = res2.get("win_unit", oc.WIN_STAKE_UNIT)
                                ku = [int((mine.get(n, 0) or 0) // unit) for n in nm]
                                picks, summ = oc.win_bet_picks_pool(
                                    nm, pp, oo, est["pool"], settings["bankroll"],
                                    settings["kelly_fraction"], settings["win_edge_min"],
                                    stake_unit=unit, risk_cap_frac=settings["max_risk_frac"],
                                    my_units=ku)
                                if picks:
                                    m1, m2, m3 = st.columns(3)
                                    m1.metric("追加購入", f"{summ['invest']:,} rrc",
                                              f"{summ['units']}口"
                                              + (f"（購入済 {summ['already']}口）" if summ['already'] else ""))
                                    m2.metric("実効EV合計", f"{summ['ev']:+,.0f} rrc")
                                    m3.metric("いずれか的中", f"{min(summ['hit'],1.0)*100:.0f}%")
                                    st.dataframe(pd.DataFrame([{
                                        "馬": r["name"], "モデル勝率": f"{r['p']*100:.1f}%",
                                        "表示od": f"{r['odds']:.2f}",
                                        "実効od": f"{r['eff_od']:.2f}",
                                        "エッジ": f"{r['edge']*100:+.0f}%",
                                        "口数": f"{r['units']}口",
                                        "投資": f"{r['stake']:,}",
                                        "実効EV": f"{r['ev']:+,.0f}"} for r in picks]),
                                        **_wide(hide_index=True))
                                    st.caption(
                                        f"自分が {summ['units']}口 入れるとプールは "
                                        f"{summ['pool_before']:,.0f} → {summ['pool_after']:,.0f} rrc になり、"
                                        "その希薄化を織り込んだ実効オッズで計算しています。"
                                        f"1レース合計 {summ['max_units']}口が上限です。")
                                    st.code("\n".join(f"{r['name']} x{r['units']}"
                                                      for r in picks), language=None)
                                else:
                                    st.info("推定プールでは、エッジ条件を満たす単勝がありません"
                                            "（プールが小さいと希薄化が大きく、+EVになりにくいです）。")

            st.subheader("🏆 的中確率ランキング")
            rk = result["ranking"]
            if result["ranking_pool_known"]:
                st.dataframe(pd.DataFrame([{
                    "#": r["rank"], "買い目": r["combo"],
                    "的中率": f"{r['model_p']*100:.2f}%", "累積": f"{r['cum']*100:.1f}%",
                    "状態": r["flag"], "1口実効od": f"{r['eff1_od']:.1f}倍",
                    "1口EV": f"{r['ev1']:+,.0f}",
                    "+EV": ("◎" if r["plus_ev"] else "")} for r in rk]),
                    **_wide(hide_index=True))
            else:
                st.dataframe(pd.DataFrame([{
                    "#": r["rank"], "買い目": r["combo"],
                    "的中率": f"{r['model_p']*100:.2f}%", "累積": f"{r['cum']*100:.1f}%",
                    "状態": r["flag"]} for r in rk]),
                    **_wide(hide_index=True))
                st.caption("プール未取得のため実効odは算出不可。")
            st.caption(f"上位{len(rk)}点でモデル確率の {result['ranking_cover']*100:.1f}% をカバー。"
                       f"{result.get('mc_note','')}")

            with st.expander("🥇 単勝 勝率：モデル vs 市場 ＋ 予測の内訳", expanded=False):
                sw = result["single_win"]
                base_cols = {
                    "馬": [r["name"] for r in sw],
                    "モデル勝率": [f"{r['model_p']*100:.1f}%" for r in sw],
                    "状態": [r["condition"] for r in sw],
                    "パッシブ": [r["passives"] for r in sw],
                    "SP寄与": [f"{r['contrib']['speed']:+.2f}" for r in sw],
                    "PW寄与": [f"{r['contrib']['power']:+.2f}" for r in sw],
                    "ST寄与": [f"{r['contrib']['stamina']:+.2f}" for r in sw],
                    "パッシブ寄与": [f"{r['contrib']['passive']:+.3f}" for r in sw],
                }
                if result.get("has_market"):
                    base_cols["市場"] = [(f"{r['market_p']*100:.1f}%" if r["market_p"] is not None else "—") for r in sw]
                    base_cols["オッズ"] = [(f"{r['odds']:.2f}" if r["odds"] else "—") for r in sw]
                    base_cols["判定"] = [r["tag"] for r in sw]
                st.dataframe(pd.DataFrame(base_cols), **_wide(hide_index=True))
                st.caption(f"モデルの◎: 【{result['model_pick']}】／ 寄与は相対logスコアへの加算量"
                           "（大きいほど有利）。合計の差が σ に対して大きいほど勝率差が開きます。")

# -------------------------- モデルタブ --------------------------
with tab_model:
    if not (bundle and bundle.get("ok")):
        st.info("学習するとここにモデルの中身が出ます。")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("学習レース数", f"{bundle['n_races']}")
        c2.metric("レース内スピアマン", f"{bundle['race_spearman']:.3f}")
        c3.metric("1着的中(OOF)", f"{bundle['top1_acc']*100:.0f}%")
        c4.metric("σ (着順のブレ)", f"{bundle['race_sigma']:.4f}",
                  f"最適値 {bundle.get('sigma_mle', 0):.4f} ×{bundle.get('sigma_safety', 1):g}")
        st.caption(f"期間 {bundle['date_min']}〜{bundle['date_max']}  /  mode={bundle['mode']}  "
                   f"/  α={bundle['alpha']}  /  読み込んだファイル {len(bundle['files'])}件")

        cal = bundle.get("calibration")
        if cal:
            st.markdown("**校正チェック（学習データの外挿予測 vs 実測）**")
            st.dataframe(pd.DataFrame([
                {"指標": "1着を当てる確率", "モデル予測": f"{cal['p_top1']*100:.1f}%",
                 "実測": f"{cal['a_top1']*100:.1f}%"},
                {"指標": "本命3連単の的中率", "モデル予測": f"{cal['p_tri']*100:.2f}%",
                 "実測": f"{cal['a_tri']*100:.2f}%"}]),
                **_wide(hide_index=True))
            st.caption("実測がモデル予測より高い＝弱気（安全側）。低い＝強気で過剰投資の危険。")

        st.markdown("### ✨ パッシブスキルの効き目")
        c1, c2 = st.columns(2)
        eff_dist = c1.selectbox("距離", oc.DIST_LIST, index=2, key="eff_dist")
        eff_track = c2.selectbox("馬場", oc.TRACK_LIST, index=0, key="eff_track")
        pe = oc.passive_effects(bundle, eff_dist, eff_track)
        src_ja = {"game": "🎮 ゲーム表記", "inferred": "🔎 推定",
                  "learned": "📊 実測から学習", "variance": "🎯 ブレ低減"}
        kind_ja = {"stat": "ステータス系", "aptitude": "適性系", "phase": "展開系"}
        st.dataframe(pd.DataFrame([{
            "パッシブ": r["passive"], "出所": src_ja.get(r["source"], r["source"]),
            "種別": kind_ja.get(r["kind"], r["kind"]),
            f"{eff_dist}・{eff_track}での効果": (
                f"σ×{r['sigma_mult']:.2f}" if r["source"] == "variance" else f"{r['pct']:+.1f}%"),
            "発動条件": r.get("condition", ""),
            "サンプル数": r["n"],
            "説明": r["desc"]} for r in pe]),
            **_wide(hide_index=True, height=460))
        n_game = sum(1 for r in pe if r["source"] == "game")
        n_inf = sum(1 for r in pe if r["source"] == "inferred")
        n_learn = sum(1 for r in pe if r["source"] == "learned")
        st.caption(
            f"🎮 ゲーム表記 {n_game}種 … 購入画面の説明文から取り込んだ**確定値**。"
            f"　🔎 推定 {n_inf}種 … 他スキルとの対称性から仮置きし、実ログで最適値を確認したもの。"
            f"　📊 実測から学習 {n_learn}種 … 数値未取得のため、実ログの回帰係数をそのまま表示。"
            "サンプル数が少ないものは当てになりません。"
            "\n\n**購入画面をパッシブの説明文ごと予測タブに貼れば、🔎/📊 を 🎮 に変えられます。**")
        st.caption("※ 適性系（○○得意）は距離・馬場が一致したときだけ効くので、"
                   "上のセレクタを合わせないと 0% と表示されます。"
                   "同族嫌悪は『同じおあしすっちが同レースにいる場合』のみ発動する前提の値です。")

        with st.expander("正則化パラメータ α の選択結果"):
            st.dataframe(pd.DataFrame(bundle["cv_rows"]).round(4),
                         **_wide(hide_index=True))
        with st.expander("読み込んだログファイル"):
            for f in bundle["files"]:
                st.caption("・" + f)

# --------------------------- ログタブ ---------------------------
with tab_log:
    if _store is not None:
        st.success("🗂 保存先: Google スプレッドシート（ブラウザを閉じても残ります）")
    else:
        st.warning(f"🗂 保存先: ローカルCSV `{betlog.path}`  "
                   "※ Streamlit Community Cloud で動かしている場合、"
                   "再起動やスリープでこのファイルは消えます。"
                   "残したいときは secrets に Google スプレッドシートを設定してください。")
        if st.session_state.get("_sheets_error"):
            st.error("スプレッドシート設定エラー: " + st.session_state["_sheets_error"])
    result = ss.result
    lc1, lc2 = st.columns(2)
    with lc1:
        st.subheader("① 賭けを記録")
        rid = st.text_input("レースID", value="", placeholder="空欄なら日時を自動採番")
        can_log = bool(result and result.get("ok") and result.get("picks"))
        if st.button("✅ 3連単の推奨を記録（pending）", disabled=not can_log,
                     **_wide()):
            rid2 = rid.strip() or datetime.now().strftime("%Y%m%d_%H%M")
            if betlog.race_exists(rid2):
                st.error(f"レースID『{rid2}』は記録済み。別IDにするか取消/精算してください。")
            else:
                try:
                    n = betlog.record(rid2, result["picks"], oc.STAKE_UNIT, bet_type="3連単")
                    st.success(f"レース『{rid2}』に {n}点を記録しました。")
                except Exception as e:
                    st.error(f"保存に失敗しました: {e}")
        can_win = bool(result and result.get("ok") and result.get("win_picks"))
        if st.button("🥇 単勝の推奨も記録", disabled=not can_win, **_wide()):
            rid2 = rid.strip() or datetime.now().strftime("%Y%m%d_%H%M")
            picks = [((r["name"],), r["p"], r["odds"], r["units"]) for r in result["win_picks"]]
            try:
                n = betlog.record(rid2, picks, oc.STAKE_UNIT, bet_type="単勝")
                st.success(f"レース『{rid2}』に単勝 {n}点を記録しました。")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")
        if not can_log:
            st.caption("先に『解析』を実行し、✅推奨が出ている状態で押してください。")

    with lc2:
        st.subheader("② 結果を入力して精算")
        log_df_now = betlog.load()
        pend_ids = sorted(set(log_df_now[log_df_now["status"] == "pending"]["race_id"].astype(str))) \
            if len(log_df_now) else []
        rid_settle = st.selectbox("精算するレースID", options=(pend_ids or ["(pendingなし)"]))
        horses = betlog.race_horses(rid_settle) if pend_ids else []
        if not horses:
            horses = (result.get("horses_disp") if result and result.get("ok") else None) or []
        cc = st.columns(3)
        o1 = cc[0].selectbox("実1着", options=(horses or ["—"]), key="o1")
        o2 = cc[1].selectbox("実2着", options=(horses or ["—"]), key="o2")
        o3 = cc[2].selectbox("実3着", options=(horses or ["—"]), key="o3")
        if st.button("🏁 精算", disabled=not pend_ids, **_wide()):
            if len({o1, o2, o3}) < 3:
                st.error("1〜3着に同じ馬が選ばれています。")
            else:
                try:
                    cnt = betlog.settle(rid_settle, (o1, o2, o3))
                    if cnt == 0:
                        st.warning(f"レース『{rid_settle}』に精算対象がありませんでした。")
                    else:
                        df_after = betlog.load()
                        sub = df_after[df_after["race_id"].astype(str) == str(rid_settle)]
                        won = int((sub["status"] == "won").sum())
                        st.success(f"レース『{rid_settle}』を精算（{cnt}点中 的中{won}点）。"
                                   f"結果: {o1} → {o2} → {o3}")
                except Exception as e:
                    st.error(f"保存に失敗しました: {e}")

    rc1, rc2 = st.columns(2)
    with rc1:
        show_report = st.button("📊 成績レポート", **_wide())
    with rc2:
        if st.button("↩ 直近レースを取消", **_wide()):
            rid_del, ndel = betlog.undo_last()
            st.success(f"レース『{rid_del}』の {ndel}件を取消しました。") if ndel \
                else st.info("取り消すレコードがありません。")

    if show_report:
        for label, bt in [("3連単", "3連単"), ("単勝", "単勝"), ("全体", None)]:
            rep = betlog.report(bt)
            if rep.get("empty") or not rep.get("overall"):
                continue
            st.markdown(f"#### {label}")
            ov = rep["overall"]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("損益", f"{ov['pnl']:+,.0f} rrc", f"ROI {ov['roi']:+.1f}%")
            m2.metric("投資 / 払戻", f"{ov['stake']:,.0f} / {ov['payout']:,.0f}")
            m3.metric("的中率(実測)", f"{ov['hit_rate']:.2f}%", f"{ov['hits']}/{ov['n']}")
            m4.metric("モデル予測平均", f"{ov['pred_rate']:.2f}%")
            if rep["buckets"]:
                st.dataframe(pd.DataFrame([{
                    "予測帯": b["label"], "件数": b["n"], "予測": f"{b['pred']:.2f}%",
                    "実測": f"{b['real']:.2f}%", "損益": f"{b['pnl']:+,.0f}"}
                    for b in rep["buckets"]]), **_wide(hide_index=True))
            if rep.get("calib_hint"):
                st.info("🔧 " + rep["calib_hint"])
        if betlog.load().empty:
            st.info("ログがありません。まず✅を記録してください。")

    with st.expander("🗂 ログ全体を見る"):
        df_all = betlog.load()
        if len(df_all):
            st.dataframe(df_all, **_wide(hide_index=True))
        else:
            st.caption("（まだ記録がありません）")
