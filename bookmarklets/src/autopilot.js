// ===== おあしすっち 3連単 オートパイロット（半自動）=====
// レースは 9/12/15/18/21/23 時の定期開催。各レースの発走前に
//   レース発見 → 予測 → +EVの点だけ抽出 → 買い目生成 → 通知
// までを自動で行い、**購入だけ人が1クリック**する。
//
// 認証について:
//   購入には Discord のBOTが発行したリンクの guild / user / token が要る。
//   リンクの自動発行はセルフボット＝Discord規約違反なので行わない。
//   ・token が使い回せる場合  … 一度貼れば以降は自動（localStorage に保存）
//   ・レースごとに変わる場合  … 発走前に新しいリンクを下の欄に貼り直す
//   どちらでも動くように、貼り付け欄と保存を両方持たせてある。
(async () => {
'use strict';
// 2回押されたら古いパネルを消して作り直す（javascript: URL は同じスコープで動くため）
try {
  const prev = document.getElementById('oasis-autopilot-panel');
  if (prev) prev.remove();
  if (window.__oasisAutopilotTimer) clearInterval(window.__oasisAutopilotTimer);
} catch (e) {}
const CFG = {
  RACE_HOURS: [9, 12, 15, 18, 21, 23],   // 開催時刻（時）
  RACE_MINUTE: 0,
  LEAD_SEC: 120,            // 発走の何秒前を狙って解析するか
  WINDOW_SEC: 900,          // 発走何秒前から準備を始めるか
  MAX_UNITS_PER_RACE: 3,    // 1レースの上限口数（分散のため20口上限より絞る）
  UNITS_PER_COMBO: 1,
  EDGE_MIN: 0.10,
  MODEL_WEIGHT: 0.7,
  MIN_PROB: 0.02,
  ODDS_TOP_N: 40,           // オッズを取る上位点数（全点だと最大3360リクエストになる）
  // レースサイトとAPIのドメインが違うことがあるので候補を順に試す。
  // 先に成功したものを使い、以降はそれを覚える。
  API_BASES: ['https://api.oasis.red', null],   // null = レースページと同じオリジン
  IMMEDIATE: true,          // 開いた時に受付中のレースがあれば即座に解析する（iOS向け）
  N_SIM: 200000,
  DAILY_BUDGET: 200000,     // 1日の上限（rrc）
  MAX_SANE_EDGE: 3.0,       // +300%超のエッジは計算がおかしいとみなして中止
  MAX_SANE_ODDS: 5000,
  MIN_POOL: 100000,
  MIN_TRAIN_RACES: 20,      // 学習レースがこれ未満のモデルでは賭けない（雛形のまま等）
  MODEL_URL: 'https://raw.githubusercontent.com/footao/oasis-app/main/model.json',
  MODEL_JSON: null,
};
let API = null;      // 実際に通じた API のベースURL
const LS = 'oasis_autopilot_v1', LSA = 'oasis_autopilot_auth', LSB = 'oasis_autopilot_api';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
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
let AUTH = loadAuth();

// ---- 状態（1日の使用額・実行済み・ログ）----
function loadState() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem(LS) || '{}'); } catch (e) {}
  if (s.day !== today()) s = { day: today(), spent: 0, done: {}, log: [] };
  s.done = s.done || {}; s.log = s.log || []; s.spent = s.spent || 0;
  return s;
}
const saveState = s => { try { localStorage.setItem(LS, JSON.stringify(s)); } catch (e) {} };
let ST = loadState();
let PENDING = null;   // 承認待ちの買い目

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
ov.innerHTML = '<b style="color:#e2b96f">🛩 3連単 オートパイロット</b>'
  + '<span id=_auth style="float:right;font-size:.72rem"></span>'
  + '<div id=_stat style="margin:.45rem 0;font-size:.76rem;line-height:1.6"></div>'
  + '<div id=_pick style="display:none;margin:.5rem 0;padding:.5rem;background:#0d1a0d;'
  + 'border:1px solid #2e7d32;border-radius:6px;font-size:.76rem"></div>'
  + '<details style="margin:.4rem 0"><summary style="cursor:pointer;font-size:.74rem;color:#aaa">'
  + '🔑 購入リンクを貼り直す（token が変わる場合）</summary>'
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
function render() {
  const next = nextRaceTime();
  const left = Math.max(0, Math.round((next - Date.now()) / 1000));
  $('_auth').innerHTML = AUTH
    ? `<span style="color:#81c784">🔑 ${esc(String(AUTH.user || '').slice(0, 8))}…</span>`
    : '<span style="color:#ef5350">🔑 未設定</span>';
  $('_stat').innerHTML =
    `次のレース <b>${next.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'})}</b>`
    + `（あと ${Math.floor(left/60)}分${String(left%60).padStart(2,'0')}秒）<br>`
    + `本日 <b>${ST.spent.toLocaleString()}</b>/${CFG.DAILY_BUDGET.toLocaleString()} rrc`
    + `　解析済み ${Object.keys(ST.done).length} レース`;
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
  log(`モデル読込 v${M.core_version} / 学習${M.n_races}レース`
      + ` / σ3連単 ${Number(M.tri_sigma).toFixed(4)}`, '#81c784');
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

// ---- 解析して買い目を用意する（購入はしない）----
async function analyseRace(sid, info) {
  const pets = info.pets, n = pets.length;
  if (n < (M.min_field_trifecta || 8)) { log(`R${sid}: ${n}頭 → 3連単なし`, '#888'); return null; }
  const dist = info.distance || '', track = info.surface || '';
  if (!M.dist_list.includes(dist)) { log(`R${sid}: 距離「${esc(dist)}」が未知 → 見送り`, '#ffb74d'); return null; }

  const horses = pets.map(h => ({
    name: h.display_name || h.name, species: h.adult_key || null,
    speed: h.speed, power: h.power, stamina: h.stamina,
    condition: h.condition_label || '普通',
    passives: [h.passive_skill, h.passive_skill_2]
      .map(c => (c && c !== 'none') ? (M.code_map[c] || null) : null).filter(Boolean),
  }));
  const base = OasisModel.predictBase(horses, dist, track, M);
  const sig = OasisModel.horseSigmas(horses, M.tri_sigma, M);
  const { combo } = OasisModel.simulateTrifecta(base, sig, CFG.N_SIM, 42);

  const pool = ((await jget(`${API}/api/trifecta/pool?guild=${AUTH.guild}&schedule_id=${sid}`)) || {}).pool || 0;
  if (pool < CFG.MIN_POOL) { log(`R${sid}: プール ${pool.toLocaleString()} rrc は小さすぎ → 見送り`, '#888'); return null; }

  const odds = await fetchOdds(sid, pets, combo);
  if (!odds.size) { log(`R${sid}: オッズ取得失敗 → 見送り`, '#ffb74d'); return null; }
  let inv = 0; for (const od of odds.values()) inv += 1 / od;
  const norm = Math.max(inv, 1e-9), U_ = M.stake_unit || 10000;

  const picks = [];
  for (const c of combo.slice(0, CFG.ODDS_TOP_N)) {
    const od = odds.get(`${c.i}-${c.j}-${c.k}`);
    if (!od || od <= 1 || c.p < CFG.MIN_PROB) continue;
    const pBet = CFG.MODEL_WEIGHT * c.p + (1 - CFG.MODEL_WEIGHT) * ((1 / od) / norm);
    const eff = (pool + U_) / (pool / od + U_);
    const edge = pBet * eff - 1;
    if (eff > CFG.MAX_SANE_ODDS) continue;
    if (edge > CFG.MAX_SANE_EDGE) {      // 異常値は「大当たり」ではなく「バグ」
      log(`R${sid}: エッジ +${(edge*100).toFixed(0)}% は異常 → このレースは中止`, '#ef5350');
      return null;
    }
    if (edge < CFG.EDGE_MIN) continue;
    picks.push({ c, od, eff, edge, p: c.p,
                 names: [pets[c.i], pets[c.j], pets[c.k]].map(h => h.display_name || h.name) });
  }
  if (!picks.length) { log(`R${sid}: エッジ${(CFG.EDGE_MIN*100)|0}%以上の点なし → 見送り`, '#888'); return null; }
  picks.sort((a, b) => b.edge - a.edge);
  const chosen = picks.slice(0, CFG.MAX_UNITS_PER_RACE);
  const cost = chosen.length * CFG.UNITS_PER_COMBO * U_;
  if (ST.spent + cost > CFG.DAILY_BUDGET) { log(`R${sid}: 本日の予算上限 → 見送り`, '#ffb74d'); return null; }
  return { sid, pets, picks: chosen, cost, unit: U_ };
}

// ---- 承認待ちを画面に出す ----
function showPending(pl) {
  PENDING = pl;
  $('_pick').style.display = 'block';
  $('_pick').innerHTML =
    `<b style="color:#81c784">R${pl.sid} 推奨 ${pl.picks.length}点 / ${pl.cost.toLocaleString()} rrc</b><br>`
    + pl.picks.map(p => `　${esc(p.names.join(' → '))}　実効od ${p.eff.toFixed(1)}　`
        + `<span style="color:#81c784">+${(p.edge*100).toFixed(0)}%</span>`).join('<br>')
    + '<br><button id=_buy style="width:100%;margin-top:.5rem;padding:.7rem;background:#2e7d32;'
    + 'color:#fff;border:none;border-radius:5px;font-weight:700;cursor:pointer">🛒 これを購入する</button>';
  $('_buy').onclick = doBuy;
  try {
    if (Notification && Notification.permission === 'granted') {
      new Notification('おあしすっち 買い目を用意しました',
        { body: `R${pl.sid}　${pl.picks.length}点 / ${pl.cost.toLocaleString()} rrc` });
    }
  } catch (e) {}
}

// ---- 購入（人が押したときだけ動く）----
let buying = false;
async function doBuy() {
  if (buying || !PENDING) return;
  buying = true;
  const btn = $('_buy'); if (btn) { btn.disabled = true; btn.style.opacity = .5; btn.textContent = '購入中…'; }
  const pl = PENDING;
  for (const pk of pl.picks) {
    const label = pk.names.join('→');
    try {
      const r = await fetch(`${API}/api/trifecta/buy`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user: AUTH.user, guild: AUTH.guild, race: pl.sid,
          first: pl.pets[pk.c.i].pet_id, second: pl.pets[pk.c.j].pet_id,
          third: pl.pets[pk.c.k].pet_id,
          amount: CFG.UNITS_PER_COMBO * pl.unit, token: AUTH.token }) });
      let d = {}; try { d = await r.json(); } catch (e) {}
      if (r.ok && d.status !== 'error') {
        ST.spent += CFG.UNITS_PER_COMBO * pl.unit;
        log(`R${pl.sid} ✅ ${label} +${(pk.edge*100).toFixed(0)}%`, '#81c784');
      } else {
        const msg = String(d.detail || d.message || ('HTTP ' + r.status));
        log(`R${pl.sid} ❌ ${label} ${esc(msg)}`, '#ef5350');
        if (/token|auth|unauthor|expire/i.test(msg)) {
          log('→ token が無効のようです。新しい購入リンクを貼り直してください。', '#ffb74d');
        }
      }
    } catch (e) {
      log(`R${pl.sid} ⚠ ${label} 通信エラー（送信済みか不明）: ${esc(e.message)}`, '#ffb74d');
    }
    await sleep(400);
  }
  ST.done[pl.sid] = { t: Date.now(), n: pl.picks.length };
  saveState(ST);
  PENDING = null; $('_pick').style.display = 'none'; buying = false; render();
}

// ---- メインループ ----
let busy = false, stopped = false, errors = 0;
async function tick(force) {
  if (stopped || busy || PENDING) { render(); return; }
  if (!AUTH) { render(); return; }
  const left = (nextRaceTime() - Date.now()) / 1000;
  if (!force && (left > CFG.WINDOW_SEC || left > CFG.LEAD_SEC)) { render(); return; }
  busy = true;
  try {
    const r = await findRace();
    if (!r) { if (force) log('受付中のレースが見つかりません（締切済みかもしれません）', '#888'); }
    else if (ST.done[r.sid] && !force) { /* 済み */ }
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
};
$('_x').onclick = () => { stopped = true; log('停止しました', '#ffb74d'); };
$('_clr').onclick = () => { ST.log = []; saveState(ST); render(); };
$('_now').onclick = () => { log('手動で解析します', '#e2b96f'); tick(true); };

try {
  try { if (Notification && Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
  await loadModel();
  if (!AUTH) {
    log('購入リンクが未設定です。BOTが出したリンクを開いてから実行してください。', '#ffb74d');
    render(); return;
  }
  await resolveApi();
  render();
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
