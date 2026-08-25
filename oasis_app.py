# -*- coding: utf-8 -*-
"""
oasis_app.py — Oasis 安定運用予測ツール v2（2026/07/27 大型アプデ対応）
======================================================================
起動:  streamlit run oasis_app.py
ロジックは oasis_core.py（UI非依存）。このファイルは画面と状態管理のみ。
"""
import hashlib
import math
import os
import re
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


def _content_key(texts, path):
    """ログ本文（またはファイルの中身）のハッシュ。これが変わったら学習し直す。"""
    h = hashlib.sha1()
    if texts:
        for t in texts:
            h.update(t.encode("utf-8", "replace"))
    elif path:
        for f in sorted(oc._iter_log_files(path)):
            try:
                with open(f, "rb") as fp:
                    h.update(fp.read())
            except OSError:
                h.update(f.encode())
    return h.hexdigest()


def _spec_key():
    """passive_spec.json の中身のハッシュ。

    train_model はスペックを**ファイルから**読み、analyze はパッシブ説明文を貼るたびに
    そのファイルを書き換える。キャッシュキーに入れないと、ゲーム側が倍率を変更 →
    貼り付けで取り込み → 再読み込みしても**古い倍率で学習したモデルが黙って返る**。
    """
    try:
        with open(_spec_path(), "rb") as fp:
            return hashlib.sha1(fp.read()).hexdigest()
    except OSError:
        return ""


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


def _result_sig(settings, raw_text):
    """「いま画面に出ている結果」の指紋。設定＋レース本文。"""
    base = repr(sorted((k, str(v)) for k, v in settings.items() if k != "spec_path"))
    return base + "|" + hashlib.sha1((raw_text or "").encode("utf-8", "replace")).hexdigest()


def _resolve_save(p):
    raw = os.path.expanduser((p or "").strip()) or "oasis_bet_log.csv"
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(_app_dir(), raw))


st.set_page_config(page_title="Oasis 予測 v2", page_icon="🐎", layout="wide")

# ---------------------------------------------------------------
#  oasis_core.py との整合チェック
#  片方だけ更新すると「AttributeError（内容は伏せられます）」になって
#  原因が分からなくなるので、起動時に分かる形で止める。
# ---------------------------------------------------------------
REQUIRED_CORE = "3.15.1"
_NEEDED = [
    "CORE_VERSION", "WIN_MAX_TOTAL_UNITS", "WIN_STAKE_UNIT", "UNBET_ODDS",
    "MAX_TOTAL_UNITS", "SIGMA_SAFETY", "DIST_LIST", "TRACK_LIST",
    "SCORING_PATCH_DATE", "DEFAULT_TRAIN_FROM", "SPEC_FILE",
    "train_model", "analyze", "BetLog", "passive_effects",
    "estimate_win_pool", "win_bet_picks_pool", "load_passive_spec",
    "BetLogReadError", "model_formula", "passive_coef_table",
    "internal_stat_weights", "INTERNAL_PHASE_WEIGHTS", "INTERNAL_DIST_BALANCE",
    "STAT_RNG_WIDTH", "STAT_RNG_WIDTH_PREV",
    "diagnose_floor_odds", "ENABLE_POOL_API",
]
_missing = [a for a in _NEEDED if not hasattr(oc, a)]
_core_ver = getattr(oc, "CORE_VERSION", None)
if _missing or _core_ver != REQUIRED_CORE:
    st.error(
        f"**oasis_core.py と oasis_app.py の版が合っていません。**\n\n"
        f"- この画面（oasis_app.py）が必要とする版: `{REQUIRED_CORE}`\n"
        f"- 実際に読み込まれた oasis_core.py の版: "
        f"`{_core_ver or '（版番号なし＝かなり古い）'}`\n"
        + (f"- 足りない機能: `{', '.join(_missing[:8])}`"
           f"{' ほか' if len(_missing) > 8 else ''}\n" if _missing else "")
        + "\n**直し方**: 配布 zip の中身を**まとめて**アップロードし直してください。"
        "`oasis_app.py` と `oasis_core.py` は必ずセットで差し替える必要があります。\n\n"
        "アップロード済みなのにこの表示が出る場合は、Streamlit Cloud の "
        "`Manage app` → `Reboot app` を実行してください（古いモジュールが"
        "読み込まれたままになっていることがあります）。")
    st.stop()


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
def _train_cached(source_key, sigma_override, train_from, sigma_safety, _texts, log_path,
                  content_key, spec_key):
    """同じ入力なら再学習しない。

    source_key に取得元の指紋を、content_key にログ本文のハッシュを入れてキャッシュキーにする。
    `_texts` は先頭が `_` のため Streamlit のキャッシュキーに含まれない（中身が変わっても
    再学習されない）ので、本文の変化は content_key 側で検知している。
    """
    return oc.train_model(log_path or None,
                          texts=list(_texts) if _texts else None,
                          sigma_override=(sigma_override or None),
                          train_from=train_from,
                          spec_path=_spec_path(),
                          sigma_safety=sigma_safety)

def _embed_html(html, height=0):
    """HTML+JS を埋め込む。st.components.v1.html は 2026-06-01 で廃止予定なので、
    新しい st.iframe があればそちらを使い、無ければ従来APIにフォールバックする。"""
    try:
        if hasattr(st, "iframe"):
            # st.iframe は height=0 を受け付けない。中身は <script> だけなので
            # 'content'（内容にフィット＝実質0px）を使う。
            return st.iframe(html, height=(height or "content"))
    except Exception:
        pass
    try:
        return st.components.v1.html(html, height=height)
    except Exception:
        return None


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
ss.setdefault("race_input", "")      # 貼り付け欄の中身（key方式にして外から操作可能にする）


def _clear_race_input():
    """入力欄を空にする。on_click コールバックなので、ウィジェット生成前に実行される。"""
    ss["race_input"] = ""
    ss["result"] = None              # 古い解析結果が残ると新データの結果と誤解しやすい


# 入力欄の上に「クリップボードから貼り付け」ボタンを差し込むスクリプト。
# Streamlit には貼り付け用のAPIが無いため、親ドキュメント（Streamlit本体のDOM）に
# 実ボタンを注入する。クリックも clipboard 読み取りもトップレベル文脈で起きるので、
# ブラウザの権限（clipboard-read）が正しく効く。
# 読み取りを拒否された場合（Firefox など）は Ctrl+V を案内するだけで、壊れない。
_CLIP_JS = """
<style>html,body{margin:0;padding:0;overflow:hidden}</style>
<script>
(function(){
  const LABEL="レースデータを貼り付け", BAR="oasis-clip-bar";
  const doc=()=>{ try{ return window.parent.document; }catch(e){ return null; } };
  function findTA(d){
    for (const t of d.querySelectorAll("textarea")){
      const a=(t.getAttribute("aria-label")||"")+(t.getAttribute("placeholder")||"");
      if (a.indexOf(LABEL)>=0 || a.indexOf("出走馬一覧")>=0) return t;
    }
    return null;
  }
  function setVal(ta, text){
    // React 管理下の textarea はネイティブ setter 経由でないと値が反映されない
    const setter=Object.getOwnPropertyDescriptor(
      window.parent.HTMLTextAreaElement.prototype,"value").set;
    setter.call(ta, text);
    ta.dispatchEvent(new (window.parent.Event)("input",{bubbles:true}));
    ta.blur();   // Streamlit は blur で値をサーバへ確定させる
  }
  function mount(){
    const d=doc(); if(!d || d.getElementById(BAR)) return;
    const ta=findTA(d); if(!ta) return;
    const bar=d.createElement("div"); bar.id=BAR;
    bar.style.cssText="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin:.25rem 0 .5rem";
    const btn=d.createElement("button");
    btn.type="button"; btn.textContent="📋 クリアして貼り付け";
    btn.title="入力欄を空にして、クリップボードの内容を貼り付けます";
    btn.style.cssText="padding:.35rem .85rem;border-radius:.5rem;cursor:pointer;"
      +"border:1px solid rgba(128,128,128,.45);background:transparent;color:inherit;"
      +"font-size:.85rem;font-family:inherit";
    const msg=d.createElement("span"); msg.style.cssText="font-size:.8rem;opacity:.8";
    let timer=null;
    const say=(t,c)=>{ msg.textContent=t; msg.style.color=c||"inherit";
      clearTimeout(timer); timer=setTimeout(()=>{msg.textContent="";},5000); };
    btn.onclick=async()=>{
      try{
        const text=await navigator.clipboard.readText();
        if(!text || !text.trim()){ say("クリップボードが空です","#e6a23c"); return; }
        setVal(ta, text);
        say("貼り付けました（"+text.length.toLocaleString()+"文字）→ 🎯解析 を押してください","#67c23a");
      }catch(e){
        say("クリップボードを読めませんでした（"+e.name+"）。枠内で Ctrl+V / ⌘V してください","#e6a23c");
      }
    };
    bar.appendChild(btn); bar.appendChild(msg);
    const anchor=ta.closest("div[data-testid]")||ta.parentElement;
    anchor.parentElement.insertBefore(bar, anchor.nextSibling);
  }
  mount();
  const d=doc();
  if(d) new MutationObserver(()=>mount()).observe(d.body,{childList:true,subtree:true});
})();
</script>
"""

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
        "σ係数", 1.0, 2.0, oc.SIGMA_SAFETY, 0.05,
        help="自動校正したσに掛ける係数。既定1.0。"
             "⚠ 大きくしても安全にはなりません。σを膨らませると確率が平坦になり、"
             "ロングショット（大穴）の確率を過大評価して偽の+EVを量産します。"
             "資金を守る安全弁は分数ケリーと下の『モデル信頼度』です。")
    model_weight = st.slider(
        "モデル信頼度 λ", 0.3, 1.0, 1.0, 0.05,
        help="EV計算で使う確率 = λ×モデル + (1−λ)×市場。"
             "モデルと市場が食い違うとき、食い違いの一部は必ずモデル側の誤差です。"
             "λ=1（モデル全信頼）は、その誤差にそのまま賭けることを意味します。"
             "実績ログで予測≈実測が確認できるまでは 0.7 以下を推奨。")

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
                                   tuple(log_texts), log_path,
                                   _content_key(log_texts, log_path), _spec_key())
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
    # ブックマークレットが出す `balance=` を資金にそのまま使う。
    # 上書きするのは**値が変わったときだけ**なので、貼り付け後に手で直した額は保持される。
    # （毎回上書きすると、手で入れた額が再実行のたびに戻ってしまう）
    _bm = re.search(r'^balance=(\d+)', st.session_state.get("race_input", "") or "", re.M)
    _bal = int(_bm.group(1)) if _bm else None
    if _bal is not None and st.session_state.get("bankroll_seen") != _bal:
        st.session_state["bankroll_v"] = _bal
        st.session_state["bankroll_seen"] = _bal
    st.session_state.setdefault("bankroll_v", 1_200_000)
    bankroll = st.number_input("手元資金 BANKROLL (rrc)", min_value=10000,
                               step=50_000, key="bankroll_v",
                               help="貼り付けデータに balance= があれば自動で入ります。"
                                    "手で変えた値は、次に残高が動くまで保持されます。")
    if _bal is not None:
        st.caption(f"💰 貼り付けの残高 {_bal:,} rrc を反映済み"
                   + ("" if _bal == bankroll else f"（現在は手動で {bankroll:,} rrc）"))
    kelly = st.slider("分数ケリー", 0.05, 1.0, 0.25, 0.05,
                      help="0.25=クォーターケリー。小さいほど低分散・低成長。")
    risk = st.slider("1レース上限（資金比）", 0.02, 0.30, 0.10, 0.01)
    edge = st.slider("エッジ下限 (実効エッジ)", 0.0, 0.50, 0.10, 0.01)
    st.caption(f"→ 1レース上限 ≒ {int(risk*bankroll//oc.STAKE_UNIT)}口"
               f"（{int(risk*bankroll):,} rrc / 3連単は最大 {oc.MAX_TOTAL_UNITS}口）")

    st.markdown("**未成立スリーブ（既定でON）**")
    sleeve_on = st.checkbox("未成立組も少額で買う", value=True,
                            help="市場が張っていない組に各1口。当たれば全プール総取り"
                                 "（2026/08/16 確認済み）。実効オッズ=(プール+1口)/1口 なので、"
                                 "誰も買っていない薄いプールほど跳ねる。高EVだが高分散。")
    sleeve_units = st.radio("未成立の最大口数", [3, 4, 5, 6, 8, 10], index=5, horizontal=True,
                            disabled=not sleeve_on)
    sleeve_pmin = st.slider("未成立の的中率下限", 0.01, 0.20, 0.05, 0.01, disabled=not sleeve_on)

    st.markdown("**単勝（任意・参考）**")
    win_on = st.checkbox("単勝の推奨も出す", value=True,
                         help=f"単勝は {oc.WIN_MAX_UNITS}口まで購入可。2026/08/23 以降は NPC の初期プール "
                              f"{oc.WIN_POOL_SEED:,} rrc があるので、実測しなくても希薄化を織り込めます。")
    win_edge = st.slider("単勝のエッジ下限", 0.0, 1.0, 0.15, 0.05, disabled=not win_on)

    st.divider()
    st.subheader("3) レース条件（貼り付けデータがあれば自動）")
    dist = st.selectbox("距離", oc.DIST_LIST, index=2)
    track = st.selectbox("馬場", oc.TRACK_LIST, index=0)
    ground = st.selectbox("地面", ["良", "稍重", "重", "不良"], index=0)
    topn = st.slider("ランキング表示数", 5, 60, 20)

    with st.expander("詳細（CO・ログ保存先・試行数）"):
        co_rrc = st.number_input("キャリーオーバー手動指定 (0=自動)", min_value=0, value=0, step=10000)
        bet_log_path = st.text_input(
            "ベットログ保存先(CSV)", value="oasis_bet_log.csv",
            help="Google スプレッドシートが設定されている場合はそちらが優先されます。")
        n_sim = st.select_slider("モンテカルロ試行数", [200_000, 400_000, 800_000],
                                 value=oc.N_SIM)
        min_prob = st.number_input(
            "最小モデル的中率（これ未満は買わない）", min_value=0.0, max_value=0.05,
            value=0.003, step=0.001, format="%.3f",
            help="確率が小さすぎる組はモンテカルロの推定ノイズが支配的で、"
                 "「偽の+EV」のほぼ全てがこの領域から出ます。既定0.3%。")

settings = dict(dist=dist, track=track, ground=ground, topn=topn,
                bankroll=bankroll, kelly_fraction=kelly, max_risk_frac=risk,
                edge_min=edge, carryover_rrc=(co_rrc or None),
                unformed_sleeve=sleeve_on, unformed_max_units=sleeve_units,
                unformed_p_min=sleeve_pmin, unformed_edge_min=0.30,
                win_bets=win_on, win_edge_min=win_edge, n_sim=n_sim,
                model_weight=model_weight, min_prob=min_prob,
                spec_path=_spec_path())
bet_log_resolved = _resolve_save(bet_log_path)
_store = _get_sheets_store()
betlog = oc.BetLog(bet_log_resolved, store=_store, race_sigma=(
    bundle.get('race_sigma') if bundle and bundle.get('ok') else None))

# ============================ メイン ============================
st.title("🐎 Oasis 予測ツール v2")
# 版はデプロイが反映されたかの確認に使うので、常に見える場所に出しておく。
# （版ズレ時のエラーにしか出ていなかったため、正常時に確認する手段が無かった）
st.caption(f"core `{oc.CORE_VERSION}`　|　"
           f"2026/07/27 大型アプデ（パッシブ2枠・新スキル17種）と 07/28 のスコア式変更に対応。"
           f"モデルは {oc.SCORING_PATCH_DATE} 以降のレースだけを使って学習します。")

tab_pred, tab_model, tab_log = st.tabs(["🎯 予測", "🔬 モデルを見る", "📒 実績ログ"])

# ---------------------------- 予測タブ ----------------------------
with tab_pred:
    raw_text = st.text_area(
        "レースデータを貼り付け（統合フォーマット / 購入画面のコピーどちらでもOK）",
        height=220, key="race_input",
        placeholder="=== 出走馬一覧 === … === 3連単オッズ === …\n"
                    "または購入画面をそのままコピペ（パッシブの説明文ごと貼ると、\n"
                    "『スピードが35%上昇』などの数値を自動で取り込みます）")
    _embed_html(_CLIP_JS, height=0)
    st.caption("💡 購入画面をパッシブの説明文ごと貼ると、スキルの実数値を自動学習して "
               "`passive_spec.json` に保存します。新しい数値を覚えたら再学習してください。")
    col_a, col_b, _ = st.columns([1, 1, 4])
    with col_a:
        do_analyze = st.button("🎯 解析", type="primary", **_wide())
    with col_b:
        st.button("🗑 クリア", on_click=_clear_race_input, **_wide(),
                  help="入力欄と直前の解析結果を消します。")

    if do_analyze:
        if not (bundle and bundle.get("ok")):
            st.error("モデル未学習です。サイドバーで学習してください。")
        elif not raw_text.strip():
            st.warning("レースデータを貼り付けてください。")
        else:
            with st.spinner("シミュレーション中…"):
                ss.result = oc.analyze(raw_text, bundle, settings)
            # レース本文も指紋に含める。設定だけだと、レースを貼り替えたときに
            # 前のレースの推奨・購入リスト・schedule_id が無表示で残り、
            # そのまま記録すると**別レースの買い目**がログに入る。
            ss.result_settings = _result_sig(settings, raw_text)

    result = ss.result
    _sig = _result_sig(settings, raw_text)
    if result is not None and ss.get("result_settings") not in (None, _sig):
        st.warning("⚠ 設定かレースデータを変更しました。下の推奨は**変更前**の結果です"
                   "（別のレースの買い目かもしれません）。『🎯 解析』を押し直してください。")
    if result is not None:
        if not result.get("ok"):
            st.error(result.get("error", "解析に失敗しました。"))
        else:
            for m in result["messages"]:
                (st.warning if m.startswith("⚠") else st.info)(m)

            # ---------- 推奨購入（3連単＋単勝まとめ） ----------
            if result.get("buy_all"):
                st.subheader("🧾 推奨購入（3連単＋単勝まとめ）")
                ba = result["buy_all"]
                b1, b2, b3 = st.columns(3)
                b1.metric("合計投資", f"{result['buy_total']:,} rrc",
                          f"{len(ba)}点 / {sum(x['units'] for x in ba)}口")
                b2.metric("実効EV合計", f"{result['buy_ev']:+,.0f} rrc")
                b3.metric("内訳", f"3連単 {sum(1 for x in ba if x['kind']=='3連単')}点"
                                  f" / 単勝 {sum(1 for x in ba if x['kind']=='単勝')}点",
                          (f"単勝 購入済{result['win_own_units']}口 / "
                           f"残枠{result.get('win_left_units', 0)}口"
                           if result.get("win_own_units") else None))
                st.dataframe(pd.DataFrame([{
                    "種別": x["kind"], "状態": x["flag"], "買い目": x["target"],
                    "口数": f"{x['units']}口",
                    "1口": f"{x['unit']:,}", "投資": f"{x['stake']:,}",
                    "的中率": (f"{x['p']*100:.2f}%" if x.get("p") is not None else "—"),
                    "実効od": (f"{x['od']:.1f}" if x.get("od") else "—"),
                    "実効EV": (f"{x['ev']:+,.0f}" if x.get("ev") is not None else "—"),
                } for x in ba]), **_wide(hide_index=True))
                st.caption("一括購入ブックマークレットにそのまま貼れます"
                           "（3連単と単勝を1回でまとめて購入します）。")
                st.code("\n".join(result["buy_lines"]), language=None)

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
                        # ゲーム画面のオッズは初期プール金20万を含まない値なので、
                        # ここは補正後（＝実際に払い戻される倍率）。名前で取り違えないこと。
                        "実od(補正後)": (f"{r['disp_od']:.1f}" if r["disp_od"] else "—"),
                        "理論EV": (f"{r['theo_ev']:+,.0f}" if r["theo_ev"] is not None else "—"),
                        "口数": (f"{r['k']}口" if r["k"] else ""),
                        "実効od": (f"{r['eff_od']:.1f}" if r["eff_od"] else ""),
                        "実効EV": (f"{r['eff_ev']:+,.0f}" if r["eff_ev"] is not None else ""),
                    } for r in view]), **_wide(hide_index=True))
                    st.caption("✅=購入推奨（成=成立 / 未=未成立スリーブ） / △=+理論EVだが安定ルールで見送り")
                    if result.get("odds_fix_ratio", 1.0) > 1.001:
                        st.caption(
                            f"⚠ **「実od(補正後)」はゲーム画面の表示と一致しません**"
                            f"（画面の値 ×{result['odds_fix_ratio']:.2f}）。"
                            f"画面のオッズは初期プール金 {oc.TRIFECTA_POOL_SEED:,} rrc を"
                            "含めずに計算されていますが、払戻はプール総額から出ます。"
                            "こちらが実際にもらえる倍率です。")
                    if result.get("bare_used"):
                        st.caption("※ 一部は素名フォールバックで照合（同名別個体を合算）。")
                    if result.get("unmatched_names"):
                        st.warning("画面に無い馬: " + ", ".join(result["unmatched_names"]))
                    with st.expander("📋 購入リスト（コピー用）"):
                        comp = [f"{r['combo']} x{r['k']}" for r in rows if r["k"] > 0]
                        st.caption("一括購入ブックマークレットにそのまま貼れます。")
                        st.code("\n".join(comp) or "(なし)", language=None)
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
                _wp = result.get("win_pool")
                _assumed = bool(result.get("win_pool_assumed"))
                _wi = result.get("win_pool_info") or {}
                if _wp and not _assumed:
                    _src = "実測プール" + ("・確定" if _wi.get("exact") else "・推定")
                elif _wp:
                    _src = "初期金の仮定のみ"
                else:
                    _src = "プール未測定"
                st.subheader(f"🥇 単勝の推奨（{_src}）")
                ws = result.get("win_summary") or {}
                if ws:
                    w0, w1, w2, w3 = st.columns(4)
                    if _wp:
                        _d = ("実測なし・NPC初期金" if _assumed
                              else (f"1,000rrc単位で確定" if _wi.get("exact")
                                    else f"精度 ±{(_wi.get('err') or 0)*200:.0f}%"
                                         f"（{int(_wi.get('n') or 0)}頭から）"))
                        w0.metric("単勝プール総額", f"{_wp:,.0f} rrc", _d)
                    else:
                        w0.metric("単勝プール総額", "未測定")
                    _ou = result.get("win_own_units", 0)
                    w1.metric("投資額", f"{ws['invest']:,} rrc",
                              f"追加{ws['units']}口"
                              + (f" / 購入済{_ou}口" if _ou else "")
                              + f" / 残枠{result.get('win_left_units', oc.WIN_MAX_TOTAL_UNITS)}口")
                    w2.metric("理論EV合計", f"{ws['ev']:+,.0f} rrc")
                    w3.metric("いずれか的中", f"{min(ws['hit'],1.0)*100:.0f}%")
                if _assumed:
                    st.warning(
                        f"⚠ **実測していません。** プールを初期金 {oc.WIN_POOL_SEED:,} rrc と"
                        "仮定して計算しています。実際のプールはこれより大きいので、"
                        "**実効オッズは表示よりさらに低くなります**。"
                        "下の「単勝：プールを実測して推奨を出す」で測ってから買ってください。")
                elif not _wp:
                    st.warning("プールを測っていないため、**自分の購入によるオッズ低下を"
                               "織り込めていません**。下の「単勝：プールを実測して推奨を出す」で"
                               "測ってから買うことを強くおすすめします。")
                    if ws.get("capped"):
                        st.caption(f"※ 1レースの単勝は**合計{oc.WIN_MAX_TOTAL_UNITS}口まで**という"
                                   "ゲーム仕様の上限に達したため、エッジの大きい順に配分しています。")
                    if result.get("win_own_units"):
                        st.caption(
                            f"※ このレースでは既に **{result['win_own_units']}口"
                            f"（{int(result.get('win_own_rrc', 0)):,} rrc）** 購入済みです"
                            "（オッズ取得時の試し買いぶんを含む）。"
                            f"上限 {oc.WIN_MAX_TOTAL_UNITS}口 のうち残り "
                            f"**{result.get('win_left_units', 0)}口**、"
                            "上の推奨はその範囲に収めています。")
                st.dataframe(pd.DataFrame([{
                    "馬": r["name"], "モデル勝率": f"{r['p']*100:.1f}%",
                    "オッズ": ("未投票" if r.get("unbet") else f"{r['odds']:.2f}"),
                    "実効od": (f"{r['eff_od']:.1f}" if r.get("eff_od") else "—"),
                    "エッジ": f"{r['edge']*100:+.0f}%",
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
                    f"2026/08/23 のアプデで NPC の自動投票により**初期プールが "
                    f"{oc.WIN_POOL_SEED:,} rrc** 入るようになりました。実測しない場合は"
                    "この値を下限として使いますが、実際のプールはこれより大きいので"
                    "**推奨が控えめに出ます**。正確に測ると口数を伸ばせます。")
                st.markdown(
                    "単勝は**控除0%の純パリミュチュエル**なので `Σ(1/オッズ) = 1.000`。"
                    "つまり**オッズは各馬のシェアしか表さず、プール総額の情報を含みません**。"
                    "少額で試し買いして、その前後のオッズの動きから逆算します。")
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
                            if est["pool"] < oc.WIN_POOL_SEED * 0.9:
                                st.warning(
                                    f"⚠ 推定 {est['pool']:,.0f} rrc は NPC の初期プール "
                                    f"{oc.WIN_POOL_SEED:,} rrc を大きく下回っています。"
                                    "測り方か、初期プール額の想定のどちらかがずれています。")
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
                                unit = res2.get("win_unit", oc.WIN_STAKE_UNIT)
                                disp2 = res2.get("horses_disp") or []
                                mine = {}
                                for i, h in enumerate(ha):
                                    key = disp2[i] if i < len(disp2) else h.get("name")
                                    mine[key] = (h.get("my_amount") or 0)
                                ku = [int((mine.get(n, 0) or 0) // unit) for n in nm]
                                fl = oc.diagnose_floor_odds(
                                    oo, [mine.get(n, 0) for n in nm])
                                for _m in fl["messages"]:
                                    (st.warning if _m.startswith("⚠") else st.info)(_m)
                                picks, summ = oc.win_bet_picks_pool(
                                    nm, pp, fl["odds_eff"], est["pool"], settings["bankroll"],
                                    settings["kelly_fraction"], settings["win_edge_min"],
                                    stake_unit=unit, risk_cap_frac=settings["max_risk_frac"],
                                    my_units=ku, unbet=fl["unbet"])
                                if picks:
                                    m1, m2, m3 = st.columns(3)
                                    m1.metric("追加購入", f"{summ['invest']:,} rrc",
                                              f"{summ['units']}口"
                                              + (f"（購入済 {summ['already']}口）" if summ['already'] else ""))
                                    m2.metric("実効EV合計", f"{summ['ev']:+,.0f} rrc")
                                    m3.metric("いずれか的中", f"{min(summ['hit'],1.0)*100:.0f}%")
                                    st.dataframe(pd.DataFrame([{
                                        "馬": r["name"], "モデル勝率": f"{r['p']*100:.1f}%",
                                        "表示od": ("未投票" if r.get("unbet") else f"{r['odds']:.2f}"),
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
                                    st.caption("一括購入ブックマークレット用（単勝のみ）")
                                    st.code("\n".join(f"単勝\t{r['name']}\t{r['units']}"
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

            # --- 3連単の買い方ガイド（1点では当たりにくいので、カバー点数や軸流しを案内）---
            def _pts_for(cov):
                for r in rk:
                    if r["cum"] >= cov:
                        return r["rank"]
                return len(rk)
            p50, p70, p80 = _pts_for(0.50), _pts_for(0.70), _pts_for(0.80)
            top1p = rk[0]["model_p"] * 100 if rk else 0
            with st.expander("🎫 3連単の買い方ガイド（1点で当てるのは難しい）", expanded=True):
                st.markdown(
                    f"3連単は「順番」まで当てる必要があり、**本命1点の的中率は約 {top1p:.0f}%**です"
                    "（モデルは3頭の顔ぶれは高確率で当てますが、2着3着の順番は乱数で入れ替わります）。"
                    "狙う的中率に応じて点数を広げるのが基本です。")
                st.dataframe(pd.DataFrame([
                    {"狙う的中率(累積)": "50%", "必要な点数": f"上位 {p50} 点"},
                    {"狙う的中率(累積)": "70%", "必要な点数": f"上位 {p70} 点"},
                    {"狙う的中率(累積)": "80%", "必要な点数": f"上位 {p80} 点"},
                ]), **_wide(hide_index=True))
                if rk:
                    axis = rk[0]["combo"].split("→")[0].strip() if "→" in rk[0]["combo"] else result.get("model_pick", "")
                    st.markdown(
                        f"**軸1頭ながしの目安**: モデルの◎【{result.get('model_pick','')}】を1着固定にして、"
                        "上位数頭を2・3着に流すと、点数を抑えつつ顔ぶれ的中を取りにいけます。"
                        "上のランキングで◎が1着の行だけを買う、という買い方です。")
                st.caption("※ ゲーム上限は3連単 合計20口。予算と相談しつつ、"
                           "上限内で狙う的中率に届く点数を選んでください。")

            with st.expander("🐴 馬ごとの予測スコア ＋ 勝率：モデル vs 市場", expanded=True):
                sw = result["single_win"]
                base_cols = {
                    "馬": [r["name"] for r in sw],
                    "予測スコア": [f"{math.exp(r['base'])*1000:,.0f}" for r in sw],
                    "対平均": [f"{(math.exp(r['base'])-1)*100:+.1f}%" for r in sw],
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
                st.caption(
                    f"モデルの◎: 【{result['model_pick']}】／ **予測スコア** はレースの"
                    "平均を 1,000 に揃えた相対値です（ゲームの実スコアと同じ縮尺で読めます）。"
                    "絶対値はレースごとの水準に依存するので、比べるのは**同じレース内だけ**。"
                    "寄与は相対logスコアへの加算量で、合計の差が σ に対して大きいほど"
                    "勝率差が開きます。")

# -------------------------- モデルタブ --------------------------
with tab_model:
    if not (bundle and bundle.get("ok")):
        st.info("学習するとここにモデルの中身が出ます。")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("学習レース数", f"{bundle['n_races']}")
        c2.metric("レース内スピアマン", f"{bundle['race_spearman']:.3f}")
        c3.metric("1着的中(OOF)", f"{bundle['top1_acc']*100:.0f}%")
        c4.metric("σ 単勝 / 3連単",
                  f"{bundle['race_sigma']:.4f} / {bundle.get('tri_sigma', bundle['race_sigma']):.4f}",
                  "3連単は順番のブレが大きいぶん大きめ")
        st.caption(f"期間 {bundle['date_min']}〜{bundle['date_max']}  /  mode={bundle['mode']}  "
                   f"/  α={bundle['alpha']}  /  読み込んだファイル {len(bundle['files'])}件")

        cal = bundle.get("calibration")
        cal_tri = bundle.get("calibration_tri")
        if cal:
            st.markdown("**校正チェック（学習データの外挿予測 vs 実測）**")
            rows = [{"指標": "1着を当てる確率（単勝σ）", "モデル予測": f"{cal['p_top1']*100:.1f}%",
                     "実測": f"{cal['a_top1']*100:.1f}%"}]
            if cal_tri:
                rows.append({"指標": f"本命3連単1点の的中率（8頭以上{cal_tri['n_races']}レース・3連単σ）",
                             "モデル予測": f"{cal_tri['p_tri']*100:.1f}%",
                             "実測": f"{cal_tri['a_tri']*100:.1f}%"})
            st.dataframe(pd.DataFrame(rows), **_wide(hide_index=True))
            st.caption("3連単は「順番」まで当てる必要があり、1点だと当たりにくいのが普通です"
                       "（モデルは3頭の顔ぶれは高確率で当てます）。実測がモデル予測より高い＝弱気（安全側）、"
                       "低い＝強気で過剰投資の危険。")

        st.markdown("### 📐 スコアの計算式")
        with st.expander("① ゲーム内部の本当のスコア式（result API から逆解析）", expanded=False):
            st.markdown(
                "レースの着順は、次の式で計算される **rating** の大きい順に決まります。\n\n"
                "> rating ＝ 定数 × Σ<sub>区間</sub> Σ<sub>stat</sub>"
                "( **区間重み**[区間][stat] × **距離バランス**[距離][stat] × 実効ステータス ) × 疲労補正\n\n"
                "・stat は スピード / パワー / スタミナ。実効ステータスにはパッシブの倍率が掛かった値が入ります。\n"
                "・疲労補正は 1.0 近辺の小さな係数（スタミナが切れた馬だけ下がる）。",
                unsafe_allow_html=True)
            st.info(
                f"レース中の乱数幅は **±{oc.STAT_RNG_WIDTH*100:g}%**"
                f"（2026/08/17 の開発者告知で ±{oc.STAT_RNG_WIDTH_PREV*100:g}% から縮小、元の値に戻りました）。"
                "実効ステータスにレースごとにこの幅の乱数が乗ります。荒れにくくなった分、"
                "「安定感」などブレ低減スキルの価値は下がります。着順ブレ幅 σ は変更後のログから"
                "自動で校正し直すので、**変更後のレースが貯まるまでは σ が広めのまま＝弱気寄り**になります。")
            st.markdown("**区間重み**（レースの序盤・中盤・終盤で、どのステータスが効くか）")
            st.dataframe(pd.DataFrame([
                {"区間": k, "スピード": v[0], "パワー": v[1], "スタミナ": v[2]}
                for k, v in oc.INTERNAL_PHASE_WEIGHTS.items()],
                ), **_wide(hide_index=True))
            st.markdown("**距離バランス**（距離ごとの、ステータスの重み付け）")
            st.dataframe(pd.DataFrame([
                {"距離": k, "スピード": v[0], "パワー": v[1], "スタミナ": v[2]}
                for k, v in oc.INTERNAL_DIST_BALANCE.items()],
                ), **_wide(hide_index=True))
            st.markdown("**実効重み**（区間重み×距離バランスを合算し、スピード=1 で正規化）")
            st.dataframe(pd.DataFrame([
                {"距離": d, "スピード": 1.0,
                 "パワー": oc.internal_stat_weights(d)["norm"][1],
                 "スタミナ": oc.internal_stat_weights(d)["norm"][2]}
                for d in oc.DIST_LIST]), **_wide(hide_index=True))
            st.caption("短距離はスピード偏重、長距離はスタミナ偏重。この写像がスコアの本質です。")

        with st.expander("② このツールが予測に使う式（学習済みモデル）", expanded=True):
            mf = oc.model_formula(bundle)
            st.markdown(
                "予測値（レース内で中心化した相対 log スコア）は、距離ごとに次を合算します。\n\n"
                "> pred ＝ 切片 ＋ **b_log**·log(実効stat) ＋ **b_lin**·(実効stat/100) "
                "＋ 状態係数 ＋ 未取得パッシブの係数\n\n"
                "実効stat にはスペック済みパッシブの倍率が畳み込まれています。"
                "log 項は「比率で効く」頑健な土台、線形項は内部式の加法構造（特に長距離のスタミナ）を捉えます。")
            st.dataframe(pd.DataFrame([{
                "距離": r["dist"], "切片": round(r["intercept"], 3),
                "log(SP)": round(r["log_SP"], 3), "log(PW)": round(r["log_PW"], 3),
                "log(ST)": round(r["log_ST"], 3), "lin(SP)": round(r["lin_SP"], 3),
                "lin(PW)": round(r["lin_PW"], 3), "lin(ST)": round(r["lin_ST"], 3),
                "内部式比(SP:PW:ST)":
                    f"1 : {r['internal_norm'][1]:.2f} : {r['internal_norm'][2]:.2f}",
            } for r in mf["per_dist"]]), **_wide(hide_index=True))
            cc = mf["condition"]
            st.caption(
                f"状態係数: 好調 {cc['好調']:+.3f} / 不調 {cc['不調']:+.3f}"
                "（log スコアへの加算。値が小さいのは新スコア式のサンプルが少ないため）。"
                "　右端は参考として内部式のステータス比を並べたもので、モデルの係数と傾向が一致していれば妥当です。")

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

        with st.expander("パッシブの係数（ステータス倍率）一覧"):
            st.caption("スペック済みのパッシブは、実効ステータスに掛かる**倍率**で計算します"
                       "（例: スピードスターは スピード×1.35・スタミナ×0.9）。"
                       "空欄は等倍（1.0）、未取得のパッシブは倍率がなく実ログから直接学習します。")
            pct = oc.passive_coef_table(bundle.get("spec"))
            kind_ja2 = {"stat": "ステータス系", "aptitude": "適性系", "phase": "展開系"}
            scope_ja = {"always": "常時", "aptitude": "距離/馬場一致時",
                        "phase": "区間限定", "conditional": "状況限定",
                        "same_species": "同族が居る時", "variance": "ブレ低減"}

            def _mx(v, has_mult):
                if v is not None:
                    return f"×{v:.2f}"
                return "×1.00" if has_mult else "—"
            st.dataframe(pd.DataFrame([{
                "パッシブ": r["passive"], "種別": kind_ja2.get(r["kind"], r["kind"]),
                "SP": _mx(r["SP"], r["SP"] is not None or r["PW"] is not None or r["ST"] is not None),
                "PW": _mx(r["PW"], r["SP"] is not None or r["PW"] is not None or r["ST"] is not None),
                "ST": _mx(r["ST"], r["SP"] is not None or r["PW"] is not None or r["ST"] is not None),
                "発動": scope_ja.get(r["scope"], r["scope"])
                        + (f"（{r['scope_arg']}）" if r["scope_arg"] else ""),
                "稼働率": (f"{r['duty']:.0%}" if r["duty"] else ""),
                "σ×": (f"{r['sigma_mult']:.2f}" if r["sigma_mult"] != 1.0 else ""),
                "説明": r["desc"]} for r in pct]),
                **_wide(hide_index=True, height=460))
            st.caption("「—」は倍率なし（実測から直接学習）、「×1.00」は等倍を表します。"
                       "σ× はブレ低減スキル（安定感など）の効き。")

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
        # schedule_id を既定にしておくと settle_bets.py が自動精算できる。
        _sid = str((result or {}).get("schedule_id") or "") if result else ""
        rid = st.text_input("レースID", value=_sid,
                            placeholder="空欄なら日時を自動採番",
                            help="貼り付けデータに schedule_id があれば自動で入ります。"
                                 "この番号のままにしておくと、精算を "
                                 "`python settle_bets.py` で自動化できます。")
        can_log = bool(result and result.get("ok") and result.get("picks"))
        if st.button("✅ 3連単の推奨を記録（pending）", disabled=not can_log,
                     **_wide()):
            rid2 = rid.strip() or datetime.now().strftime("%Y%m%d_%H%M")
            _df = betlog.load()
            _dup = len(_df) and ((_df["race_id"].astype(str) == str(rid2))
                                 & (_df["bet_type"] == "3連単")).any()
            if _dup:
                st.error(f"レース『{rid2}』の3連単は記録済み。別IDにするか取消/精算してください。")
            else:
                try:
                    n = betlog.record(rid2, result["picks"], oc.STAKE_UNIT, bet_type="3連単")
                    st.success(f"レース『{rid2}』に {n}点を記録しました。")
                except Exception as e:
                    st.error(f"保存に失敗しました: {e}")
        can_win = bool(result and result.get("ok") and result.get("win_picks"))
        if st.button("🥇 単勝の推奨も記録", disabled=not can_win, **_wide()):
            rid2 = rid.strip() or datetime.now().strftime("%Y%m%d_%H%M")
            _df = betlog.load()
            _dup = len(_df) and ((_df["race_id"].astype(str) == str(rid2))
                                 & (_df["bet_type"] == "単勝")).any()
            if _dup:
                st.error(f"レース『{rid2}』の単勝は記録済みです。取消してから記録してください。")
            else:
                picks = [((r["name"],), r["p"], (r.get("eff_od") or r.get("odds")), r["units"])
                         for r in result["win_picks"]]
                wunit = int(result.get("win_unit") or oc.WIN_STAKE_UNIT)
                try:
                    n = betlog.record(rid2, picks, wunit, bet_type="単勝")
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
        # 単勝だけを記録したレースは候補が1頭しか出ず、1〜3着を選べなくなる。
        # 直近の解析結果の出走馬を足して補う。
        extra = (result.get("horses_disp") if result and result.get("ok") else None) or []
        horses = list(dict.fromkeys(list(horses) + list(extra)))
        if len(horses) < 3:
            st.caption("着順の候補が3頭に足りません。先に同じレースIDで3連単を記録するか、"
                       "そのレースを解析してから精算してください。")
        cc = st.columns(3)
        o1 = cc[0].selectbox("実1着", options=(horses or ["—"]), key="o1")
        o2 = cc[1].selectbox("実2着", options=(horses or ["—"]), key="o2")
        o3 = cc[2].selectbox("実3着", options=(horses or ["—"]), key="o3")
        st.caption("**最終オッズ**（分かれば入力・0なら購入時オッズで概算）。"
                   "締切2分前に取ったオッズならほぼ最終値なので、そのままでも大きくは"
                   "ずれません。`python settle_bets.py` を使えば着順も最終オッズも"
                   "結果APIから自動で入ります（そちらが正確）。")
        fc = st.columns(2)
        fo_tri = fc[0].number_input("最終オッズ（3連単）", min_value=0.0, value=0.0,
                                    step=1.0, format="%.1f", key="fo_tri")
        fo_win = fc[1].number_input("最終オッズ（単勝）", min_value=0.0, value=0.0,
                                    step=0.1, format="%.2f", key="fo_win")
        if st.button("🏁 精算", disabled=not pend_ids, **_wide()):
            if len({o1, o2, o3}) < 3:
                st.error("1〜3着に同じ馬が選ばれています。")
            else:
                try:
                    cnt = betlog.settle(rid_settle, (o1, o2, o3),
                                        final_odds={"3連単": (fo_tri or None),
                                                    "単勝": (fo_win or None)})
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
        st.caption("↩ は直近レースの記録を**精算済みの行も含めて**すべて削除します。")
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
            if rep.get("n_payout_est"):
                st.caption(f"※ 的中 {rep['n_won']}件のうち **{rep['n_payout_est']}件は"
                           "購入時オッズ換算の概算**払戻です（最終オッズ未入力）。"
                           "損益・ROIはその分ずれています。精算時に最終オッズを入れると実績になります。")
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
