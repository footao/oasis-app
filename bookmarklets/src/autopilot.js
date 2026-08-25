// ===== おあしすっち オートパイロット（3連単＋単勝）=====
// レースは 9/12/15/18/21/23 時の定期開催。各レースの締切前に
//   レース発見 → 予測（装備・お守り込み） → 3連単と単勝の+EV点を抽出
//   → 買い目生成 → 通知 → 購入
// までを自動で行う。
//
// 認証について:
//   購入には Discord のBOTが発行したリンクの guild / user / token が要る。
//   リンクの自動発行はセルフボット＝Discord規約違反なので**行わない**。
//   リンクは人が発行して貼る。トークンはレースごとに失効するので、
//   **貼った時点で「次の1レースぶん」だけ自動購入を予約（アーム）する**。
//   そのレースが終わればアームは自動で解除され、次は貼り直しが要る。
//   これが「事前アームで全自動」の実体で、放置で走り続けることはない。
//
// 計算は Python の oasis_core と同じ（model.js が同じ式を持ち、
// parity_test.py で数値一致を検証している）。装備・お守りの倍率も同じ表を使う。
(async () => {
'use strict';
// 2回押されたら古いパネルを消して作り直す（javascript: URL は同じスコープで動くため）
try {
  const prev = document.getElementById('oasis-autopilot-panel');
  if (prev) prev.remove();
  if (window.__oasisAutopilotTimer) clearInterval(window.__oasisAutopilotTimer);
  if (window.__oasisAutopilotClock) clearInterval(window.__oasisAutopilotClock);
} catch (e) {}
const CFG = {
  RACE_HOURS: [9, 12, 15, 18, 21, 23],   // 開催時刻（時）
  RACE_MINUTE: 0,
  LEAD_SEC: 60,             // 締切の何秒前を狙って解析・購入するか
  WINDOW_SEC: 900,          // 発走何秒前から準備を始めるか
  MAX_UNITS_PER_RACE: 3,    // 3連単の1レース上限口数（分散のため20口上限より絞る）
  UNITS_PER_COMBO: 1,
  EDGE_MIN: 0.10,
  MODEL_WEIGHT: 1.0,        // λ。model.json の defaults から上書きされる
  MIN_PROB: 0.02,
  WIN_ON: true,             // 単勝も買う（NPCが40万入れるのでプールが常にある）
  // 単勝プールの実測（試し買い）。1口ずつ買って前後のオッズの動きから逆算する。
  // **これは実際の購入**なので、アーム中（＝人が購入を許可したレース）でしか走らせない。
  // 買う先はモデルの本命なので、どのみち買いたい馬＝試し買いは無駄にならない。
  WIN_PROBE: true,
  WIN_PROBE_MAX_UNITS: 5,   // 0 にすると実測しない（初期金40万を下限として使う）
  WIN_PROBE_TARGET_ERR: 0.04,
  ODDS_TOP_N: 40,           // オッズを取る上位点数（全点だと最大3360リクエストになる）
  // レースサイトとAPIのドメインが違うことがあるので候補を順に試す。
  // 先に成功したものを使い、以降はそれを覚える。
  API_BASES: ['https://api.oasis.red', null],   // null = レースページと同じオリジン
  IMMEDIATE: true,          // 開いた時に受付中のレースがあれば即座に解析する（iOS向け）
  N_SIM: 200000,
  DAILY_BUDGET: 200000,     // 1日の上限（rrc）
  MAX_SANE_EDGE: 3.0,       // +300%超のエッジは計算がおかしいとみなして中止
  MAX_SANE_ODDS: 5000,
  // 3連単プールがこれ未満なら3連単は見送る。既定は model.json の初期プール金
  // （＝賭け0件の状態）。プールがちょうど初期金なら全組が未成立なので、
  // オッズは1件も取りに行かず「未成立スリーブ」として扱う（下の unformed）。
  MIN_POOL: null,           // null = model.json の trifecta_pool_seed を使う
  UNFORMED_ON: true,        // 未成立組（誰も賭けていない組）にも置くか
  UNFORMED_MAX_UNITS: 10,
  MIN_TRAIN_RACES: 20,      // 学習レースがこれ未満のモデルでは賭けない（雛形のまま等）
  MODEL_URL: 'https://raw.githubusercontent.com/footao/oasis-app/main/model.json',
  MODEL_JSON: null,
};
let API = null;      // 実際に通じた API のベースURL
const LS = 'oasis_autopilot_v1', LSA = 'oasis_autopilot_auth', LSB = 'oasis_autopilot_api';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 表示用の数値整形。undefined/NaN でも落とさない（表示のために解析全体を落とさない）。
const fx = (v, d) => (Number.isFinite(+v) ? (+v).toFixed(d) : '—');
const jget = async u => { try { const r = await fetch(u); if (!r.ok) return null; return await r.json(); } catch (e) { return null; } };
const today = () => new Date().toLocaleDateString('sv-SE');

// ---- 認証情報（URL → 保存済み の順で拾う）----
function authFromUrl(str) {
  try {
    const u = new URL(str, location.href);
    const p = u.searchParams;
    const a = { guild: p.get('guild'), user: p.get('user'), token: p.get('token'),
                sid: parseInt(p.get('race') || p.get('schedule_id'), 10) };
    return (a.guild && a.token) ? a : null;
  } catch (e) { return null; }
}
function loadAuth() {
  const fromUrl = authFromUrl(location.href);
  if (fromUrl) { localStorage.setItem(LSA, JSON.stringify(fromUrl)); return fromUrl; }
  try { return JSON.parse(localStorage.getItem(LSA) || 'null'); } catch (e) { return null; }
}
const AUTH_FROM_URL = !!authFromUrl(location.href);   // 購入ページ上で開いたか
let AUTH = loadAuth();

// ---- 状態（1日の使用額・実行済み・ログ）----
function loadState() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem(LS) || '{}'); } catch (e) {}
  if (s.day !== today()) s = { day: today(), spent: 0, done: {}, log: [] };
  s.done = s.done || {}; s.log = s.log || []; s.spent = s.spent || 0;
  s.probed = s.probed || {};   // 試し買い済みのレース（[今すぐ解析]の連打で二重に買わない）
  return s;
}
const saveState = s => { try { localStorage.setItem(LS, JSON.stringify(s)); } catch (e) {} };
let ST = loadState();
let PENDING = null;   // 承認待ちの買い目

// ---- アーム（自動購入の予約）----
// token はレースごとに失効するので、リンクを貼った時点で「次の1レースぶん」だけ
// 自動購入を許可する。ST.armFor はそのレースの締切時刻(ms)。
// レースが過ぎたら自動で外れるので、放置で走り続けることはない。
function isArmed() {
  return !!(ST.armFor && Date.now() < ST.armFor && nextRaceTime().getTime() === ST.armFor);
}
function arm() {
  ST.armFor = nextRaceTime().getTime();
  saveState(ST);
  const t = new Date(ST.armFor).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
  log(`⏳ ${t} のレースを自動購入にアームしました（このレース限り）`, '#e2b96f');
  render();
}
function disarm(msg) {
  if (!ST.armFor) return;
  ST.armFor = null; saveState(ST);
  if (msg) log(msg, '#888');
  render();
}

// ---- 画面 ----
const ov = document.createElement('div');
ov.id = 'oasis-autopilot-panel';
// iOS Safari の下部ツールバーに隠れないよう safe-area を見る。狭い画面では全幅。
ov.style.cssText = 'position:fixed;z-index:99999;right:8px;left:auto;width:470px;'
  + 'max-width:calc(100vw - 16px);max-height:82vh;overflow-y:auto;'
  + 'bottom:calc(12px + env(safe-area-inset-bottom,0px));'
  + 'background:#12121f;border:2px solid #e2b96f;border-radius:10px;padding:.8rem;color:#eee;'
  + 'font-family:sans-serif;font-size:.85rem;box-shadow:0 6px 20px rgba(0,0,0,.5);'
  + '-webkit-overflow-scrolling:touch';
if (window.matchMedia && window.matchMedia('(max-width:640px)').matches) {
  ov.style.left = '8px'; ov.style.width = 'auto';    // スマホは全幅にする
}
ov.innerHTML = '<b style="color:#e2b96f">🛩 オートパイロット（3連単＋単勝）</b>'
  + '<span id=_auth style="float:right;font-size:.72rem"></span>'
  + '<div id=_stat style="margin:.45rem 0;font-size:.76rem;line-height:1.6"></div>'
  + '<label style="display:block;margin:.35rem 0;font-size:.76rem;cursor:pointer">'
  + '<input type=checkbox id=_arm> <b>次の1レースだけ自動購入する</b>'
  + '<span style="color:#888"> — 購入ページで開いたならチェックだけでOK</span></label>'
  + '<div id=_pick style="display:none;margin:.5rem 0;padding:.5rem;background:#0d1a0d;'
  + 'border:1px solid #2e7d32;border-radius:6px;font-size:.76rem"></div>'
  + '<details style="margin:.4rem 0"><summary style="cursor:pointer;font-size:.74rem;color:#aaa">'
  + '🔑 購入リンクを貼る（購入ページ以外で開いたとき用）</summary>'
  + '<textarea id=_link placeholder="BOTが発行した購入リンクを貼り付け" '
  + 'style="width:100%;height:46px;margin-top:.35rem;background:#0b0b14;color:#e2b96f;'
  + 'border:1px solid #444;border-radius:5px;padding:.35rem;font-size:.7rem"></textarea>'
  + '<button id=_save style="margin-top:.3rem;padding:.3rem .7rem;background:#e2b96f;color:#12121f;'
  + 'border:none;border-radius:5px;cursor:pointer;font-weight:700">保存</button></details>'
  + '<div id=_log style="max-height:200px;overflow-y:auto;font-size:.72rem;line-height:1.5;'
  + 'background:#0b0b14;border:1px solid #333;border-radius:6px;padding:.4rem"></div>'
  + '<div style="display:flex;gap:.4rem;margin-top:.5rem">'
  + '<button id=_now style="flex:1;padding:.6rem;background:#2e7d32;color:#fff;border:none;border-radius:5px;cursor:pointer">今すぐ解析</button>'
  + '<button id=_clr style="padding:.6rem .7rem;background:#444;color:#fff;border:none;border-radius:5px;cursor:pointer">ログ消去</button>'
  + '<button id=_x style="padding:.6rem .7rem;background:#7a2222;color:#fff;border:none;border-radius:5px;cursor:pointer">停止</button></div>';
document.body.appendChild(ov);
const $ = i => document.getElementById(i);

function log(m, c) {
  ST.log.unshift({ m: `[${new Date().toLocaleTimeString('ja-JP')}] ${m}`, c: c || '' });
  ST.log = ST.log.slice(0, 300); saveState(ST); render();
}
// 残り時間だけを毎秒書き換える。render() ごと1秒で回すとログのHTMLも作り直して
// しまい、テキスト選択が切れるうえ無駄なので、カウントダウンだけ別にする。
function renderClock() {
  const el = document.getElementById('_cd');
  if (!el) return;
  const left = Math.max(0, Math.round((nextRaceTime() - Date.now()) / 1000));
  el.textContent = `${Math.floor(left / 60)}分${String(left % 60).padStart(2, '0')}秒`;
}
function render() {
  const next = nextRaceTime();
  const left = Math.max(0, Math.round((next - Date.now()) / 1000));
  $('_auth').innerHTML = AUTH
    ? `<span style="color:#81c784">🔑 ${esc(String(AUTH.user || '').slice(0, 8))}…</span>`
    : '<span style="color:#ef5350">🔑 未設定</span>';
  $('_stat').innerHTML =
    `次のレース <b>${next.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'})}</b>`
    + `（あと <span id=_cd>${Math.floor(left/60)}分${String(left%60).padStart(2,'0')}秒</span>）<br>`
    + `本日 <b>${ST.spent.toLocaleString()}</b>/${CFG.DAILY_BUDGET.toLocaleString()} rrc`
    + `　解析済み ${Object.keys(ST.done).length} レース`;
  const ab = $('_arm');
  if (ab) ab.checked = isArmed();
  $('_stat').innerHTML += isArmed()
    ? '<br><span style="color:#e2b96f">⏳ このレースは自動で購入します</span>'
    : '<br><span style="color:#888">🛑 自動購入なし（買い目を出して止まります）</span>';
  $('_log').innerHTML = ST.log.map(x =>
    `<span style="color:${x.c || '#aaa'}">${esc(x.m)}</span>`).join('<br>')
    || '<span style="color:#666">（待機中）</span>';
}
function nextRaceTime() {
  const now = new Date();
  for (let d = 0; d < 2; d++) for (const h of CFG.RACE_HOURS) {
    const t = new Date(now); t.setDate(now.getDate() + d); t.setHours(h, CFG.RACE_MINUTE, 0, 0);
    if (t > now) return t;
  }
  return new Date(now.getTime() + 3600000);
}

// ---- API のベースURLを決める（サイトとAPIのドメインが違う場合に備える）----
async function resolveApi() {
  const saved = localStorage.getItem(LSB);
  const cands = [];
  if (saved) cands.push(saved);
  for (const b of CFG.API_BASES) {
    const u = (b === null) ? location.origin : b;
    if (!cands.includes(u)) cands.push(u);
  }
  for (const b of cands) {
    const r = await jget(`${b}/api/race/by-id/${AUTH.guild}/${AUTH.sid}?user=${AUTH.user}`);
    if (r && Array.isArray(r.pets)) {
      API = b; localStorage.setItem(LSB, b);
      log(`API: ${esc(b)}`, '#81c784');
      return b;
    }
  }
  throw new Error('APIに到達できません（レースページで実行しているか確認してください）');
}

// ---- モデル ----
let M = null;
async function loadModel() {
  // 優先順: 直接埋め込み > バンドルが置いた window.__OASIS_MODEL > 外部URL
  M = CFG.MODEL_JSON || window.__OASIS_MODEL || await jget(CFG.MODEL_URL);
  if (!M || !M.coef || !M.spec) throw new Error('model.json を読めません（MODEL_URL を確認）');
  // 雛形のまま／学習不足のモデルで賭けに行かせない。
  // build_autopilot.py を実ログで走らせると正しい model.json が入る。
  if (M.placeholder) {
    throw new Error('model.json が雛形のままです。build_autopilot.py で再生成してください');
  }
  if (!(M.n_races >= CFG.MIN_TRAIN_RACES)) {
    throw new Error(`学習レースが ${M.n_races} 件しかありません`
      + `（${CFG.MIN_TRAIN_RACES}件以上必要）。model.json を作り直してください`);
  }
  // 既定値は model.json（＝Python の DEFAULT_SETTINGS）から取る。
  // JS 側に数値を持たせると、Python を直したときに静かにズレる。
  const D = M.defaults || {};
  if (D.model_weight != null) CFG.MODEL_WEIGHT = +D.model_weight;
  if (D.edge_min != null) CFG.EDGE_MIN = +D.edge_min;
  if (D.min_prob != null) CFG.MIN_PROB = Math.max(+D.min_prob, 0.02);
  if (D.unformed_max_units != null) CFG.UNFORMED_MAX_UNITS = +D.unformed_max_units;
  if (CFG.MIN_POOL == null) CFG.MIN_POOL = M.trifecta_pool_seed || 300000;
  log(`モデル読込 v${M.core_version} / 学習${M.n_races}レース`
      + ` / σ3連単 ${Number(M.tri_sigma).toFixed(4)} / σ単勝 ${Number(M.race_sigma).toFixed(4)}`,
      '#81c784');
}

// ---- 受付中のレースを探す ----
async function findRace() {
  const from = AUTH.sid || 0;
  for (let d = 0; d <= 10; d++) {
    const info = await jget(`${API}/api/race/by-id/${AUTH.guild}/${from + d}?user=${AUTH.user}`);
    if (info && Array.isArray(info.pets) && info.pets.length && info.phase === 'betting') {
      return { sid: from + d, info };
    }
  }
  return null;
}

// ---- 上位N点のオッズだけ取得 ----
async function fetchOdds(sid, pets, combo) {
  const out = new Map(), want = combo.slice(0, CFG.ODDS_TOP_N);
  for (let i = 0; i < want.length; i += 10) {
    const got = await Promise.all(want.slice(i, i + 10).map(async c => {
      const d = await jget(`${API}/api/trifecta/odds?guild=${AUTH.guild}&schedule_id=${sid}`
        + `&first=${pets[c.i].pet_id}&second=${pets[c.j].pet_id}&third=${pets[c.k].pet_id}`);
      return { c, od: (d && typeof d.odds === 'number') ? d.odds : null };
    }));
    got.forEach(g => { if (g.od) out.set(`${g.c.i}-${g.c.j}-${g.c.k}`, g.od); });
    await sleep(120);
  }
  return out;
}

// ---- 解析して買い目を用意する（購入はここではしない）----
async function analyseRace(sid, info) {
  const pets = info.pets, n = pets.length;
  const dist = info.distance || '', track = info.surface || '';
  if (!M.dist_list.includes(dist)) { log(`R${sid}: 距離「${esc(dist)}」が未知 → 見送り`, '#ffb74d'); return null; }

  // 装備・お守りの**加算**ぶん（SP+25 など）は API の speed に既に入っている。
  // ここで掛けるのは**倍率**だけ（Python: parse_unified と同じ切り分け）。
  const fxSkipped = [];
  const horses = pets.map(h => OasisModel.applyItems({
    name: h.display_name || h.name, species: h.adult_key || null,
    speed: h.speed, power: h.power, stamina: h.stamina,
    condition: h.condition_label || '普通',
    passives: [h.passive_skill, h.passive_skill_2]
      .map(c => (c && c !== 'none') ? (M.code_map[c] || null) : null).filter(Boolean),
    equipment: h.equipment, charm: h.charm,
  }, M, fxSkipped));
  if (fxSkipped.length) {
    log(`R${sid}: 反映しなかった効果 ${esc([...new Set(fxSkipped)].join(' / '))}`, '#ffb74d');
  }
  const base = OasisModel.predictBase(horses, dist, track, M);
  // 単勝は race_sigma、3連単は tri_sigma。Python と同じ使い分け。
  const winP = OasisModel.simulateTrifecta(
    base, OasisModel.horseSigmas(horses, M.race_sigma, M), CFG.N_SIM, 42).win;
  // 7頭以下のレースに3連単は無い。組のシミュレーション自体を回さない
  // （Python の analyze も need_combo=tri_ok で同じことをしている）。
  // このレースで買えるのは単勝だけなので、WIN_ON を切っていても単勝は出す。
  const triOk = n >= (M.min_field_trifecta || 8);
  const combo = triOk ? OasisModel.simulateTrifecta(
    base, OasisModel.horseSigmas(horses, M.tri_sigma, M), CFG.N_SIM, 42).combo : [];
  const winOn = CFG.WIN_ON || !triOk;

  const U_ = M.stake_unit || 10000;
  const triPicks = triOk ? await analyseTrifecta(sid, pets, combo, U_)
                         : { picks: [], cost: 0 };
  if (!triOk) log(`R${sid}: ${n}頭 → 3連単なし（8頭未満）。単勝だけ出します`, '#888');
  // 単勝プールの実測はここで。試し買いでオッズが動くので、
  // **実測後の pets** をそのまま単勝の計算に使う（プールと同じ時点に揃える）。
  let wpets = pets, wpool = null;
  if (winOn && CFG.WIN_PROBE && CFG.WIN_PROBE_MAX_UNITS > 0) {
    const pr = await probeWinPool(sid, pets, winP);
    if (pr) { wpets = pr.pets; wpool = pr.pool; }
  }
  const winPicks = winOn ? analyseWin(sid, wpets, winP, wpool) : { picks: [], cost: 0 };
  const cost = (triPicks.cost || 0) + (winPicks.cost || 0);
  if (!cost) return null;
  if (ST.spent + cost > CFG.DAILY_BUDGET) { log(`R${sid}: 本日の予算上限 → 見送り`, '#ffb74d'); return null; }
  return { sid, pets: wpets, picks: triPicks.picks, win: winPicks.picks, cost,
           unit: U_, winUnit: M.win_stake_unit || 1000 };
}

// ---- 3連単 ----
async function analyseTrifecta(sid, pets, combo, U_) {
  const none = { picks: [], cost: 0 };
  const n = pets.length;
  if (n < (M.min_field_trifecta || 8)) { log(`R${sid}: ${n}頭 → 3連単なし`, '#888'); return none; }
  const pool = ((await jget(`${API}/api/trifecta/pool?guild=${AUTH.guild}&schedule_id=${sid}`)) || {}).pool || 0;
  const SEED = (M.trifecta_pool_seed == null ? 300000 : M.trifecta_pool_seed);
  const BASE = Math.max(pool - SEED, 0);

  // プールが初期プール金のまま ＝ 賭け0件 ＝ **全組が未成立**。
  // 賭け金は必ず1口の倍数なので BASE < 1口 なら誰も賭けていない。
  // オッズを取りに行っても全部 null が返るだけなので1リクエストも投げない。
  if (BASE < U_) {
    if (!CFG.UNFORMED_ON) { log(`R${sid}: 賭け0件（プール ${pool.toLocaleString()}）→ 見送り`, '#888'); return none; }
    const eff = (pool + U_) / U_;
    const pmin = (M.defaults || {}).unformed_p_min || 0.05;
    const emin = (M.defaults || {}).unformed_edge_min || 0.30;
    const picks = [];
    for (const c of combo) {
      if (c.p < pmin) continue;
      const edge = c.p * eff - 1;
      if (edge < emin) continue;
      picks.push({ c, od: eff, eff, edge, p: c.p, unformed: true,
                   names: [pets[c.i], pets[c.j], pets[c.k]].map(h => h.display_name || h.name) });
      if (picks.length >= CFG.UNFORMED_MAX_UNITS) break;
    }
    if (!picks.length) { log(`R${sid}: 賭け0件だが+EVの組なし → 見送り`, '#888'); return none; }
    log(`R${sid}: 賭け0件（プール ${pool.toLocaleString()}）→ 全組未成立・実効od ${eff.toFixed(1)}倍`, '#e2b96f');
    const chosen = picks.slice(0, Math.min(CFG.UNFORMED_MAX_UNITS, CFG.MAX_UNITS_PER_RACE * 3));
    return { picks: chosen, cost: chosen.length * CFG.UNITS_PER_COMBO * U_ };
  }
  if (pool < CFG.MIN_POOL) { log(`R${sid}: プール ${pool.toLocaleString()} rrc は小さすぎ → 見送り`, '#888'); return none; }

  const oddsRaw = await fetchOdds(sid, pets, combo);
  if (!oddsRaw.size) { log(`R${sid}: オッズ取得失敗 → 3連単は見送り`, '#ffb74d'); return none; }
  let odds = oddsRaw;
  // かつてサイト側に「表示オッズが (プール総額 − 初期プール金) 基準」というバグがあり、
  // 払戻はプール総額から出るので x pool/(pool−SEED) の補正を掛けていた。
  // 2026/08/24 のアプデで修正されたことを race 2097 で確認済み（Σ(1/od)=(P−S)/P）。
  // **補正を掛けたままだとEVを 1.5倍ほど過大評価する**ので、Python の
  // TRIFECTA_SEED_BUG_ACTIVE を model.json 経由で見て、生きているときだけ掛ける。
  if (M.trifecta_seed_bug_active && pool > SEED) {
    const f = pool / (pool - SEED);
    const fixed = new Map();
    for (const [k, v] of odds) fixed.set(k, v * f);
    odds = fixed;
    log(`R${sid}: オッズを x${f.toFixed(3)} 補正（初期プール金 ${SEED.toLocaleString()} rrc）`, '#888');
  }
  // 市場の暗黙確率は**補正前**オッズで出し、Python と同じく 1.0 でクランプする。
  // 取得できた ODDS_TOP_N 組だけで正規化すると、その40組の確率が強制的に合計1になり、
  // 実際には市場の一部しか持っていないのに q が膨らむ（12頭立てで約1.8倍、
  // エッジ +0.06 過大 = EDGE_MIN と同じ大きさ）。オッズ取得が落ちるほど悪化する。
  let inv = 0; for (const od of oddsRaw.values()) inv += 1 / od;
  const norm = Math.max(inv, 1.0);

  const picks = [];
  for (const c of combo.slice(0, CFG.ODDS_TOP_N)) {
    const od = odds.get(`${c.i}-${c.j}-${c.k}`);
    if (!od || od <= 1 || c.p < CFG.MIN_PROB) continue;
    const odRaw = oddsRaw.get(`${c.i}-${c.j}-${c.k}`) || od;
    const pBet = CFG.MODEL_WEIGHT * c.p + (1 - CFG.MODEL_WEIGHT) * ((1 / odRaw) / norm);
    const eff = (pool + U_) / (pool / od + U_);
    const edge = pBet * eff - 1;
    if (eff > CFG.MAX_SANE_ODDS) continue;
    if (edge > CFG.MAX_SANE_EDGE) {      // 異常値は「大当たり」ではなく「バグ」
      log(`R${sid}: エッジ +${(edge*100).toFixed(0)}% は異常 → このレースは中止`, '#ef5350');
      return none;
    }
    if (edge < CFG.EDGE_MIN) continue;
    picks.push({ c, od, eff, edge, p: c.p,
                 names: [pets[c.i], pets[c.j], pets[c.k]].map(h => h.display_name || h.name) });
  }
  if (!picks.length) { log(`R${sid}: エッジ${(CFG.EDGE_MIN*100)|0}%以上の点なし → 3連単は見送り`, '#888'); return none; }
  picks.sort((a, b) => b.edge - a.edge);
  const chosen = picks.slice(0, CFG.MAX_UNITS_PER_RACE);
  return { picks: chosen, cost: chosen.length * CFG.UNITS_PER_COMBO * U_ };
}

// ---- 単勝プールの実測（試し買い）----
// 単勝は控除0%の純パリミュチュエルなので Σ(1/od)=1.000。つまり**オッズはシェアしか
// 表さず、プール総額の情報を含まない**。自分で少額入れて前後の動きから逆算するしかない。
//   od_j = P / P_j。自分が Δ 入れると P→P+Δ。**自分が買っていない**馬 j は P_j 不変なので
//   od_j後 / od_j前 = (P+Δ)/P = R（全馬共通）→ P = Δ/(R−1)
// オッズは小数2桁なので丸め誤差は od に反比例する。比 R は**重み od² の加重平均**で取る
// （分散最小）。1口ずつ買って目標精度に届いた時点で止める。
// bm.js の同じ処理と式を揃えてある（Python: oasis_core.estimate_win_pool）。
async function probeWinPool(sid, pets, winP) {
  if (!isArmed()) {           // 試し買いは**実際の購入**。許可の無いレースでは走らせない。
    log(`R${sid}: アームされていないので単勝プールの実測は行いません`, '#888');
    return null;
  }
  // [今すぐ解析] を連打すると同じレースで何度も試し買いしてしまう。1レース1回に固定する。
  if (ST.probed[sid]) {
    log(`R${sid}: 単勝プールは実測済み（${(ST.probed[sid] || 0).toLocaleString()} rrc）`, '#888');
    const again = await jget(`${API}/api/race/by-id/${AUTH.guild}/${sid}?user=${AUTH.user}`);
    return { pets: (again && again.pets) || pets, pool: ST.probedPool && ST.probedPool[sid] };
  }
  const WU = M.win_stake_unit || 1000;
  const FLOOR = M.unbet_odds == null ? 1.5 : M.unbet_odds;
  const STEP = M.odds_step == null ? 0.01 : M.odds_step;
  const priced = pets.filter(h => Number.isFinite(+h.odds) && +h.odds > 0 && +h.odds !== FLOOR);
  if (priced.length < 3) { log(`R${sid}: オッズの出ている馬が少なく単勝プールを測れません`, '#888'); return null; }
  // 試し買い先はモデルの本命（オッズが出ている馬の中で）。どのみち買いたい馬。
  let ti = -1;
  pets.forEach((h, i) => {
    if (!priced.includes(h)) return;
    if (ti < 0 || winP[i] > winP[ti]) ti = i;
  });
  if (ti < 0) return null;
  const tgt = pets[ti];
  const maxU = Math.min(CFG.WIN_PROBE_MAX_UNITS,
                        Math.floor(Math.max(CFG.DAILY_BUDGET - ST.spent, 0) / WU));
  if (maxU < 1) { log(`R${sid}: 予算が残っておらず実測できません`, '#ffb74d'); return null; }
  const before = new Map(pets.map(h => [h.pet_id, +h.odds]));
  let spent = 0, cur = pets, wp = null;
  for (let k = 0; k < maxU; k++) {
    try {
      const r = await fetch(`${API}/api/bet`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user: AUTH.user, guild: AUTH.guild, race: sid,
                               pet_id: tgt.pet_id, amount: WU, token: AUTH.token }) });
      if (!r.ok) { if (!spent) log(`R${sid}: 単勝の試し買いに失敗（実測なし）`, '#ffb74d'); break; }
      spent += WU; ST.spent += WU;
      ST.probed[sid] = spent;
      saveState(ST);
    } catch (e) { break; }
    const info2 = await jget(`${API}/api/race/by-id/${AUTH.guild}/${sid}?user=${AUTH.user}`);
    if (!info2 || !Array.isArray(info2.pets) || !info2.pets.length) break;
    cur = info2.pets;
    // 買った馬は P_j が動くので比に使えない。残りの馬で od² 加重平均を取る。
    let sw = 0, sr = 0, n = 0; const seenOd = new Map();
    for (const h of cur) {
      if (h.pet_id === tgt.pet_id) continue;
      const ob = before.get(h.pet_id), oa = +h.odds;
      if (!ob || !Number.isFinite(oa) || oa <= 0 || ob === FLOOR || oa === FLOOR) continue;
      const w = oa * oa; sw += w; sr += w * (oa / ob); n++;
      // 誤差の見積もりだけは**オッズが同じ馬をまとめて1つ**として数える。
      // 丸め誤差は「オッズの値」に対して決まるので、同値の馬を独立サンプル扱いすると
      // 1/√n ぶん精度を過大評価する（NPCが均等に賭けて全馬同オッズのとき実際に外した）。
      if (!seenOd.has(oa)) seenOd.set(oa, w);
    }
    if (sw <= 0 || n < 2) continue;
    const R = sr / sw;
    if (R <= 1) continue;
    let P = spent / (R - 1);
    let swErr = 0; for (const w of seenOd.values()) swErr += w;
    const sdR = (STEP / Math.sqrt(12)) * Math.sqrt(2 / swErr);
    let rel = sdR * P / spent;
    const sdAbs = rel * P;
    let exact = false;
    if (sdAbs < WU) {                       // 1σ が1格子未満 → 1,000rrc単位に確定できる
      P = Math.round(P / WU) * WU;
      if (sdAbs < WU / 4) exact = true;
      // 格子に載せた以上、誤差は**半格子より小さくは名乗れない**（量子化の下限）
      rel = Math.max(rel, (WU / 2) / P);
    }
    wp = { pool: P + spent, delta: spent, n: n, err: rel, exact: exact };
    if (exact || rel <= CFG.WIN_PROBE_TARGET_ERR) break;
  }
  if (!wp) {
    if (spent) log(`R${sid}: 試し買い ${spent.toLocaleString()} rrc したが実測できず → 初期金で計算`, '#ffb74d');
    return spent ? { pets: cur, pool: null } : null;
  }
  log(`R${sid}: ${wp.exact ? '✅ 単勝プールを確定' : '🔬 単勝プールを実測'} `
      + `${wp.pool.toLocaleString()} rrc`
      + `（試し買い ${wp.delta.toLocaleString()} rrc / ${wp.n}頭`
      + (wp.exact ? '' : ` / 精度 ±${(wp.err * 200).toFixed(0)}%`) + `）`, '#81c784');
  ST.probedPool = ST.probedPool || {}; ST.probedPool[sid] = wp.pool; saveState(ST);
  return { pets: cur, pool: wp.pool };
}

// ---- 単勝（Python: analyze の単勝ブロックと同じ手順）----
function analyseWin(sid, pets, winP, measuredPool) {
  const none = { picks: [], cost: 0 };
  const WU = M.win_stake_unit || 1000;
  const odds = pets.map(h => (typeof h.odds === 'number' ? h.odds : NaN));
  const mine = pets.map(h => +(h.my_amount || 0));
  const ownRrc = mine.reduce((a, b) => a + b, 0);
  const ownUnits = Math.floor(ownRrc / WU);
  const left = Math.max(0, (M.win_max_total_units || 100) - ownUnits);
  if (left <= 0) { log(`R${sid}: 単勝は既に上限 ${ownUnits}口 → 追加なし`, '#888'); return none; }

  const fl = OasisModel.diagnoseOddsFloor(odds, mine, M);
  for (const m of fl.messages) log(`R${sid}: ${esc(m)}`, '#ffb74d');
  // 市場確率は「未投票馬を除外し、下限に張り付いた本命は本当のオッズで」計算する
  const oddsMkt = fl.odds_eff.map((o, i) => (fl.unbet[i] ? NaN : o));
  const mktP = OasisModel.marketWinProb(oddsMkt, 1.0);
  if (!mktP) { log(`R${sid}: 単勝オッズを読めません → 単勝は見送り`, '#888'); return none; }

  // 実測できたらそれを使う。無ければ NPC の初期金を**下限として**使う
  // （実際のプールはこれ以上あるので希薄化を多めに見積もる＝買い控える方向で安全側）。
  const pool = measuredPool || M.win_pool_seed || 0;
  if (!measuredPool && (M.win_pool_seed || 0) > 0) {
    log(`R${sid}: 単勝プールは初期金 ${(M.win_pool_seed).toLocaleString()} rrc と仮定（控えめ）`, '#888');
  }
  if (pool <= 0) return none;
  const others = pool - ownRrc;
  if (others <= 0.15 * pool) {
    log(`R${sid}: 単勝プールの大半が自分の掛け金 → どう買っても期待値マイナス。単勝なし`, '#ffb74d');
    return none;
  }
  const lam = CFG.MODEL_WEIGHT;
  const pBet = winP.map((p, i) => lam * p + (1 - lam) * (Number.isFinite(mktP[i]) ? mktP[i] : p));
  const D = M.defaults || {};
  // 同名馬がいると name で引き戻せないので、一意キー「i:名前」を渡して後で剥がす。
  const key = pets.map((h, i) => `${i}:${h.display_name || h.name}`);
  const [picks] = OasisModel.winBetPicksPool(
    key, pBet, fl.odds_eff, pool,
    D.bankroll || 1200000, D.kelly_fraction || 0.25, D.win_edge_min || 0.15,
    { stakeUnit: WU, totalUnits: Math.min(left, M.win_max_total_units || 100),
      maxUnits: M.win_max_units || 100, riskCapFrac: D.max_risk_frac || 0.10,
      myUnits: mine.map(a => Math.floor(a / WU)), unbet: fl.unbet });
  if (!picks || !picks.length) { log(`R${sid}: 単勝に+EVの馬なし`, '#888'); return none; }
  const out = picks.map(r => {
    const i = parseInt(String(r.name).split(':')[0], 10);
    return { i, name: pets[i].display_name || pets[i].name,
             // ⚠ winBetPicksPool が返すキーは Python と同じ `eff_od`。
             //   `eff` にすると undefined になって .toFixed() で落ちる（実際に落とした）。
             units: r.units, eff: r.eff_od, edge: r.edge, p: r.p };
  }).filter(x => x.i >= 0 && pets[x.i]);
  return { picks: out, cost: out.reduce((a, x) => a + x.units * WU, 0) };
}

// ---- 買い目を画面に出す（アーム中ならそのまま購入する）----
function showPending(pl) {
  PENDING = pl;
  const WU = pl.winUnit;
  const rows = [];
  for (const p of pl.picks) {
    rows.push(`　3連単 ${esc(p.names.join(' → '))}　実効od ${fx(p.eff, 1)}`
      + (p.unformed ? '（未成立）' : '')
      + `　<span style="color:#81c784">+${fx(p.edge * 100, 0)}%</span>`);
  }
  for (const w of (pl.win || [])) {
    rows.push(`　単勝 ${esc(w.name)} ${w.units}口　実効od ${fx(w.eff, 2)}`
      + `　<span style="color:#81c784">+${fx(w.edge * 100, 0)}%</span>`);
  }
  const armed = isArmed();
  $('_pick').style.display = 'block';
  $('_pick').innerHTML =
    `<b style="color:#81c784">R${pl.sid} 推奨 ${rows.length}件 / ${pl.cost.toLocaleString()} rrc</b><br>`
    + rows.join('<br>')
    + '<br><button id=_buy style="width:100%;margin-top:.5rem;padding:.7rem;background:#2e7d32;'
    + `color:#fff;border:none;border-radius:5px;font-weight:700;cursor:pointer">${armed ? '⏳ 自動購入します…' : '🛒 これを購入する'}</button>`;
  $('_buy').onclick = doBuy;
  try {
    if (Notification && Notification.permission === 'granted') {
      new Notification('おあしすっち 買い目を用意しました',
        { body: `R${pl.sid}　${rows.length}件 / ${pl.cost.toLocaleString()} rrc` });
    }
  } catch (e) {}
  // アーム済み（＝人がこのレースぶんのリンクを貼って自動購入を許可した）なら
  // ここでそのまま買う。アームは1レース限りで、買った時点で外れる。
  if (armed) { log(`R${pl.sid}: アーム中なので自動購入します`, '#e2b96f'); doBuy(); }
}

// ---- 購入 ----
// 3連単は /api/trifecta/buy、単勝は /api/bet。1リクエストあたりの口数は
// buy.js と同じ（3連単10口・単勝20口）。それを超えると弾かれる。
let buying = false;
async function doBuy() {
  if (buying || !PENDING) return;
  buying = true;
  const btn = $('_buy'); if (btn) { btn.disabled = true; btn.style.opacity = .5; btn.textContent = '購入中…'; }
  const pl = PENDING;
  let bought = 0;
  const post = async (url, body, label, amount) => {
    try {
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                   body: JSON.stringify(body) });
      let d = {}; try { d = await r.json(); } catch (e) {}
      if (r.ok && d.status !== 'error') {
        ST.spent += amount; bought++;
        log(`R${pl.sid} ✅ ${label}`, '#81c784');
      } else {
        const msg = String(d.detail || d.message || ('HTTP ' + r.status));
        log(`R${pl.sid} ❌ ${label} ${esc(msg)}`, '#ef5350');
        if (/token|auth|unauthor|expire/i.test(msg)) {
          log('→ token が無効です。新しい購入リンクを貼り直してください。', '#ffb74d');
        }
      }
    } catch (e) {
      // 届いているかもしれないので**予算は減らす**。減らさないと、実際には
      // 買えているのに残額が過大なまま1日分ずっとズレ続ける（安全側に倒す）。
      ST.spent += amount;
      log(`R${pl.sid} ⚠ ${label} 通信エラー（送信済みか不明・予算からは引きました）: `
          + esc(e.message), '#ffb74d');
    }
    await sleep(400);
  };
  for (const pk of pl.picks) {
    const amount = CFG.UNITS_PER_COMBO * pl.unit;
    await post(`${API}/api/trifecta/buy`,
      { user: AUTH.user, guild: AUTH.guild, race: pl.sid,
        first: pl.pets[pk.c.i].pet_id, second: pl.pets[pk.c.j].pet_id,
        third: pl.pets[pk.c.k].pet_id, amount, token: AUTH.token },
      `3連単 ${pk.names.join('→')} +${fx(pk.edge * 100, 0)}%`, amount);
  }
  for (const w of (pl.win || [])) {
    let leftU = w.units;
    while (leftU > 0) {
      const u = Math.min(leftU, 20);          // 単勝は1リクエスト20口まで
      const amount = u * pl.winUnit;
      await post(`${API}/api/bet`,
        { user: AUTH.user, guild: AUTH.guild, race: pl.sid,
          pet_id: pl.pets[w.i].pet_id, amount, token: AUTH.token },
        `単勝 ${w.name} ${u}口 +${fx(w.edge * 100, 0)}%`, amount);
      leftU -= u;
    }
  }
  ST.done[pl.sid] = { t: Date.now(), n: bought };
  disarm(`R${pl.sid} の購入が終わったのでアームを解除しました`);
  saveState(ST);
  PENDING = null; $('_pick').style.display = 'none'; buying = false; render();
}

// ---- メインループ ----
let busy = false, stopped = false, errors = 0;
async function tick(force) {
  if (stopped || busy) { render(); return; }
  // 🛒 を押さないまま締切を過ぎた買い目は捨てる。放置すると PENDING が残り続け、
  // 以降このセッションでは一切レースを解析しなくなる（画面は動いて見えるので気づけない）。
  if (PENDING && (nextRaceTime() - Date.now()) / 1000 <= 0) {
    log(`R${PENDING.sid}: 締切までに購入されなかったので破棄しました`, '#888');
    ST.done[PENDING.sid] = { t: Date.now(), n: 0 };
    saveState(ST);
    PENDING = null; $('_pick').style.display = 'none'; buying = false;
  }
  if (PENDING) { render(); return; }
  if (!AUTH) { render(); return; }
  const left = (nextRaceTime() - Date.now()) / 1000;
  // 旧: (left > WINDOW_SEC || left > LEAD_SEC) は LEAD_SEC < WINDOW_SEC なので
  // 常に `left > LEAD_SEC` に潰れ、WINDOW_SEC が死んでいた。
  // 「締切まで LEAD_SEC 以内」かつ「まだ締切前」の窓でだけ動かす。
  if (!force && (left > CFG.LEAD_SEC || left <= 0)) { render(); return; }
  busy = true;
  try {
    const r = await findRace();
    if (!r) { if (force) log('受付中のレースが見つかりません（締切済みかもしれません）', '#888'); }
    // force（今すぐ解析）でも「購入済み」は上書きしない。ここを抜けると
    // 1レース MAX_UNITS_PER_RACE 口の上限を何度でも回避できてしまう。
    else if (ST.done[r.sid] && ST.done[r.sid].n > 0) {
      if (force) log(`R${r.sid}: 購入済みです（1レースの上限を超えないため再解析しません）`, '#888');
    }
    else if (ST.done[r.sid] && !force) { /* 見送り済み */ }
    else {
      const pl = await analyseRace(r.sid, r.info);
      if (pl) showPending(pl); else ST.done[r.sid] = { t: Date.now(), n: 0 };
      saveState(ST);
    }
    errors = 0;
  } catch (e) {
    errors++;
    log(`エラー(${errors}/3): ${esc(e.message)}`, '#ef5350');
    if (errors >= 3) { stopped = true; log('連続エラーのため停止しました', '#ef5350'); }
  } finally { busy = false; render(); }
}

$('_save').onclick = () => {
  const a = authFromUrl($('_link').value.trim());
  if (!a) { log('リンクを読めません（guild と token を含むURLを貼ってください）', '#ef5350'); return; }
  AUTH = a; localStorage.setItem(LSA, JSON.stringify(a));
  $('_link').value = ''; log('購入リンクを更新しました', '#81c784');
  arm();               // 貼った＝そのレースぶんの購入を人が許可した、とみなす
};
$('_arm').onchange = () => { if ($('_arm').checked) arm(); else disarm('自動購入を解除しました'); };
$('_x').onclick = () => {
  stopped = true;
  if (window.__oasisAutopilotTimer) clearInterval(window.__oasisAutopilotTimer);
  if (window.__oasisAutopilotClock) clearInterval(window.__oasisAutopilotClock);
  log('停止しました（監視・カウントダウンとも止めました）', '#ffb74d');
};
$('_clr').onclick = () => { ST.log = []; saveState(ST); render(); };
$('_now').onclick = () => { log('手動で解析します', '#e2b96f'); tick(true); };

try {
  try { if (Notification && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
  await loadModel();
  if (!AUTH) {
    log('購入リンクが未設定です。BOTが出したリンクを開いてから実行してください。', '#ffb74d');
    render(); return;
  }
  // 購入ページの URL に guild/user/token が入っているので、そこで開いたなら
  // 貼り付けは要らない。**アームだけ**が人の意思表示として残る。
  if (AUTH_FROM_URL) {
    log(`購入ページのリンクから認証を取り込みました（R${AUTH.sid || '?'}）。`
        + '自動で買うなら上のチェックを入れてください。', '#81c784');
  } else {
    log('保存済みのリンクを使います。トークンが失効していたら'
        + '新しいリンクを開き直すか、下の欄に貼り直してください。', '#ffb74d');
  }
  await resolveApi();
  render();
  window.__oasisAutopilotClock = setInterval(renderClock, 1000);
  // iOS はタブが背面に回るとタイマーが止まるので、定期監視には頼れない。
  // 「開いた時点で受付中のレースがあれば即解析」を基本動作にする。
  if (CFG.IMMEDIATE) {
    log('受付中のレースを確認します…', '#e2b96f');
    await tick(true);
  }
  const mobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
  if (mobile) {
    log('スマホでは定期監視は動きません（タブが止まるため）。'
      + 'レースごとにリンクを開いて実行してください。', '#ffb74d');
  } else {
    log(`監視開始。${CFG.RACE_HOURS.join('/')}時の発走${CFG.LEAD_SEC}秒前に解析します。`, '#e2b96f');
    window.__oasisAutopilotTimer = setInterval(() => tick(false), 20000);
  }
} catch (e) { log('起動できません: ' + esc(e.message), '#ef5350'); }
})();
