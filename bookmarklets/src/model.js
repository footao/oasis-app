// ===== oasis model.js — Python の oasis_core と同じ予測をブラウザで行う =====
// export_model_json() が出す model.json を読み、
//   実効ステータス → 特徴量 → 線形結合 → モンテカルロ → 3連単の組確率
// までを再現する。**特徴量は名前で対応付ける**（位置対応にすると、パッシブの
// 増減で静かにズレるため）。Python 側との一致は tools/parity_test.js で検証する。
const OasisModel = (() => {

  // --- パッシブの倍率を畳み込んだ実効ステータス（Python: effective_stats）---
  function effectiveStats(speed, power, stamina, passives, dist, track, M, ctx) {
    ctx = ctx || {};
    const v = { speed: +speed, power: +power, stamina: +stamina };
    for (const p of (passives || [])) {
      const sp = M.spec[p];
      if (!sp || !sp.mult || !Object.keys(sp.mult).length) continue;
      if (sp.scope === 'aptitude' && sp.scope_arg !== dist && sp.scope_arg !== track) continue;
      if (sp.scope === 'same_species' && !ctx.same_species) continue;
      const duty = Math.min(Math.max(sp.duty == null ? 1 : sp.duty, 0), 1);
      for (const k of Object.keys(sp.mult)) {
        if (k in v) v[k] *= (1 + (+sp.mult[k] - 1) * duty);
      }
    }
    for (const k of Object.keys(v)) v[k] = Math.max(v[k], 1);
    return v;
  }

  // --- 装備・お守りの効果 → 倍率（Python: _pct_mults + item_effect_spec）---
  // 倍率の**大きさ**は説明文から、**発動範囲(scope/duty)は M.item_scope から**取る。
  // 効果名で引けなければ effect_key、それでも引けなければ**乗せない**。
  // （説明文だけから推測すると『首位の呪い』が 0.49% → 6.2% に化ける。12倍。）
  const _STAT_JA = {'スピード':'speed','パワー':'power','スタミナ':'stamina','全ステータス':'all'};
  const _RE_PCT  = /(スピード|パワー|スタミナ|全ステータス)が[^。、%％\d]{0,6}(\d+(?:\.\d+)?)[%％](上昇|低下|アップ|ダウン)/g;
  const _RE_PAIR = /(スピード|パワー|スタミナ)と(スピード|パワー|スタミナ)が[^。、%％\d]{0,6}(\d+(?:\.\d+)?)[%％](上昇|低下|アップ|ダウン)/g;
  const _RE_ABIL = /走行能力が(\d+(?:\.\d+)?)[%％](上昇|低下)/g;
  const _RE_CONS = /スタミナ消費量[^。]*?(\d+(?:\.\d+)?)[%％](増加|減少)/g;
  const _RE_RECV = /(?:最大)?スタミナの(\d+(?:\.\d+)?)[%％]を?回復/g;
  const _UP = new Set(['上昇','アップ']);
  const _all = (re, d) => { re.lastIndex = 0; const o = []; let m; while ((m = re.exec(d))) o.push(m); return o; };

  function pctMults(desc) {
    let d = String(desc || ''), mult = {};
    const mul = (k, v) => { mult[k] = (mult[k] == null ? 1 : mult[k]) * v; };
    for (const m of _all(_RE_CONS, d)) mul('stamina', m[2] === '減少' ? 1 + +m[1] / 100 : 1 - +m[1] / 100);
    for (const m of _all(_RE_RECV, d)) mul('stamina', 1 + +m[1] / 100);
    for (const m of _all(_RE_ABIL, d)) { const v = m[2] === '上昇' ? 1 + +m[1] / 100 : 1 - +m[1] / 100;
      for (const k of ['speed','power','stamina']) mul(k, v); }
    // 「AとBがそれぞれN%」を先に処理し、**その部分を文字列から抜いてから** _RE_PCT に渡す
    // （抜かないと後半が二重に掛かって 1.03 が 1.0609 になる）。
    for (const m of _all(_RE_PAIR, d)) { const v = _UP.has(m[4]) ? 1 + +m[3] / 100 : 1 - +m[3] / 100;
      mul(_STAT_JA[m[1]], v); mul(_STAT_JA[m[2]], v); }
    d = d.replace(_RE_PAIR, '');
    for (const m of _all(_RE_PCT, d)) { const v = _UP.has(m[3]) ? 1 + +m[2] / 100 : 1 - +m[2] / 100;
      for (const k of (_STAT_JA[m[1]] === 'all' ? ['speed','power','stamina'] : [_STAT_JA[m[1]]])) mul(k, v); }
    return mult;
  }

  // item = APIの equipment / charm。戻り値 {speed,power,stamina,_sigma} か null。
  // 反映できなかったものは skipped 配列に効果名を積む（黙って落とさない）。
  function itemMult(item, M, skipped, ctx) {
    if (!item) return null;
    const label = String(item.effect_label || '').trim();
    const desc  = String(item.effect_description || '');
    const push  = () => { if (skipped) skipped.push(label || item.name || '?'); return null; };
    const scopeTbl = M.item_scope || {};
    const alias = (M.item_key_alias || {})[String(item.effect_key || '').replace(/^(?:gear|charm|item)_/, '')];
    // 未登録でも「常時」と書いてあれば範囲の誤読は起きないので常時で掛ける
    // （Python は未登録を説明文から推測して掛けるので、少なくともここは揃える）。
    // R2566: 未登録の『無窮潮流（スタミナ常時+18.1%）』を捨てて勝ち馬を p≈0 と読んだ。
    const c = scopeTbl[label] || scopeTbl[alias || ''] || (/常時/.test(desc) ? { scope: 'always' } : null);
    if (!c) return push();
    if (c.scope === 'variance') {
      const m = desc.match(/約?(半分|\d+(?:\.\d+)?)[%％]?/);
      let sg = 0.5;
      if (m && m[1] !== '半分') sg = 1 - parseFloat(m[1]) / 100;
      return { _sigma: Math.max(0.05, sg) };
    }
    if (c.scope === 'learned' || c.scope === 'same_species') return push();
    // 馬場限定（芝啜り／泥啜り）は、そのレースの馬場が分かっていれば判定できる。
    // ctx を渡さない呼び出しでは従来どおり採用しない。
    let aptDuty = null;
    if (c.scope === 'aptitude') {
      const arg = c.scope_arg;
      if (!ctx || !arg || (arg !== ctx.dist && arg !== ctx.track)) return push();
      aptDuty = 1;                 // 条件が合っている間はずっと効く
    }
    const mult = pctMults(desc);
    if (!Object.keys(mult).length) return push();
    let duty = aptDuty != null ? aptDuty
      : Math.min(Math.max(c.duty == null ? 1 : c.duty, 0), 1);
    // 「残り300m」（終焉加速・幻界終走）は距離で決まる決定論的な窓＝3区間ぶん。
    // 距離が分かれば正確に引ける。分からなければ M.item_scope の 0.200 のまま。
    if (c.scope === 'tail300') {
      const L = (M.stamina_cost_law || {})[(ctx && ctx.dist) || ''];
      if (L && L.n_seg) duty = 3 / L.n_seg;
    }
    const out = {};
    // 先頭の間だけ効く装備（首位の呪い）。発動割合は馬によって桁違いに違うので、
    // 1着確率が分かっていればそこから引き直す（Python: LEAD_DUTY_A/B と同じ式）。
    // 分からない1パス目は c.duty のまま。素の倍率と使った duty を持ち帰り、
    // 2パス目（leadAdjustedBase）で割り戻す。
    if (c.scope === 'lead') {
      if (ctx && ctx.p_win != null && ctx.n_field) {
        const a = M.lead_duty_a == null ? 0.655 : M.lead_duty_a;
        const b = M.lead_duty_b == null ? 0.345 : M.lead_duty_b;
        duty = Math.min(1, a * ctx.p_win + b / ctx.n_field);
      }
      for (const k of Object.keys(mult)) out['_lead_' + k] = mult[k];
      out._lead_duty0 = duty;
    }
    for (const k of Object.keys(mult)) out[k] = 1 + (mult[k] - 1) * duty;
    return out;
  }

  // 1頭ぶんの装備＋お守りを畳み込んで {speed,power,stamina} に掛ける。
  // ⚠ ステータスの**加算**ぶん（SP+25 など）は API の speed に既に入っている。
  //    ここで掛けるのは**倍率**だけ（Python: parse_unified と同じ切り分け）。
  // 「先頭の間だけ効く」装備の duty を1着確率から引き直して、もう一度予測する。
  // Python: lead_adjusted_base と同じ。1パス目で (1+(m-1)*duty0) を掛けてあるので、
  // それで割ってから新しい duty を掛け直す。戻り値 [新しいbase, 引き直した馬の記録]。
  function leadAdjustedBase(horses, base, dist, track, M, sigma, nSim) {
    const idx = [];
    for (let i = 0; i < horses.length; i++) if (horses[i].item_lead) idx.push(i);
    if (!idx.length) return [base, null];
    const win = simulateTrifecta(base, horseSigmas(horses, sigma, M),
                                 nSim || 20000, 42, false).win;
    let tot = 0; for (const w of win) tot += w;
    if (!(tot > 0)) return [base, null];
    const n = horses.length;
    const a = M.lead_duty_a == null ? 0.655 : M.lead_duty_a;
    const b = M.lead_duty_b == null ? 0.345 : M.lead_duty_b;
    const hs2 = horses.map(h => Object.assign({}, h));
    const notes = [];
    for (const i of idx) {
      const L = horses[i].item_lead, p = win[i] / tot;
      const d0 = +L.duty0 || 0, d1 = Math.min(1, a * p + b / n);
      for (const k of ['speed', 'power', 'stamina']) {
        const m = +L[k] || 1;
        if (m === 1) continue;
        hs2[i][k] = horses[i][k] * (1 + (m - 1) * d1) / (1 + (m - 1) * d0);
      }
      const lsc = M.lead_stamina_cost == null ? 1.04 : M.lead_stamina_cost;
      hs2[i].stamina_cost_mult = 1 + (lsc - 1) * d1;
      notes.push({ name: horses[i].name, p: p, d0: d0, d1: d1 });
    }
    return [predictBase(hs2, dist, track, M), notes];
  }

  function applyItems(h, M, skipped, ctx) {
    let sig = 1;
    for (const it of [h.equipment, h.charm]) {
      const m = itemMult(it, M, skipped, ctx);
      if (!m) continue;
      if (m._sigma != null) { sig *= m._sigma; continue; }
      if (m._lead_duty0 != null) {
        h.item_lead = { duty0: m._lead_duty0 };
        // 先頭でだけ効く装備はスタミナ消費も増やす（実測 ×1.04）。1パス目は一律 duty ぶん。
        const lsc = M.lead_stamina_cost == null ? 1.04 : M.lead_stamina_cost;
        h.stamina_cost_mult = 1 + (lsc - 1) * m._lead_duty0;
        for (const k of ['speed','power','stamina']) {
          if (m['_lead_' + k]) h.item_lead[k] = m['_lead_' + k];
        }
      }
      for (const k of ['speed','power','stamina']) if (m[k]) h[k] = h[k] * m[k];
    }
    h.item_sigma_mult = sig;
    return h;
  }

  // --- 分散低減スキル（安定感など）による σ 倍率（Python: sigma_multiplier）---
  function sigmaMultiplier(passives, M, extraMult) {
    let m = (extraMult == null ? 1 : +extraMult);
    for (const p of (passives || [])) {
      const sp = M.spec[p];
      if (sp) m *= (sp.sigma_mult == null ? 1 : +sp.sigma_mult);
    }
    if (m === 1) return 1;
    const f = Math.min(Math.max(M.variance_share, 0), 1);
    return Math.sqrt(f * m * m + (1 - f));
  }

  // --- スタミナ収支（Python: stamina_budget）---
  // 定数は model.json 経由で Python から来る。JS 側には数値を持たせない。
  function staminaBudget(e, dist, M, costMult) {
    const L = (M.stamina_cost_law || {})[dist];
    if (!L) return [0, 0, 0];
    const w = M.phase_early, b = (M.dist_balance || {})[dist] || [1, 1, 1];
    const base = e.speed * w[0] * b[0] + e.power * w[1] * b[1] + e.stamina * w[2] * b[2];
    // costMult は消費を変える装備・パッシブぶん。clamp の**後**に掛かる（Python と同じ）。
    const need = Math.min(Math.max(L.c * base, L.lo), L.hi) * L.n_seg * (+costMult || 1);
    const have = Math.floor(e.stamina);
    return [need, Math.max(0, need - have), Math.max(0, have - need)];
  }

  // --- 1頭ぶんの特徴量（Python: _row_features）を {名前: 値} で返す ---
  function rowFeatures(h, dist, track, M, ctx) {
    const e = effectiveStats(h.speed, h.power, h.stamina, h.passives, dist, track, M, ctx);
    // log は内部レーティング R = Σ 実効重み×実効ステ の1本（Python: internal_rating）。
    // ステータス別の log は低い値の差を拡大しすぎる（R2568）。
    const w = (M.internal_w || {})[dist] || [1, 1, 1];
    const lr = Math.log(Math.max(w[0] * e.speed + w[1] * e.power + w[2] * e.stamina, 1));
    const ln = { SP: e.speed / 100, PW: e.power / 100, ST: e.stamina / 100 };
    const f = {};
    for (const d of M.dist_list) {
      const m = (dist === d) ? 1 : 0;
      f[`${d}:切片`] = m;
      f[`${d}:log(R)`] = m * lr;
      for (const s of ['SP', 'PW', 'ST']) f[`${d}:lin(${s})`] = m * ln[s];
    }
    const bud = staminaBudget(e, dist, M, (ctx && ctx.stamina_cost_mult) || 1);
    f['スタミナ余り'] = bud[2] / 10;
    // 不足は**必要量に対する割合**。同じ「不足5」でも短距離ではレースの2割を
    // 空っぽで走ることになり、長距離では6%で済む。×5 は列の桁を合わせるため。
    // （Python: _row_features と同じ式。timeline 2,159頭の実測に基づく）
    f['スタミナ不足'] = 5 * bud[1] / Math.max(bud[0], 1);
    const cond = h.condition || '普通';
    // 状態は学習で使わない設定のとき 0（Python: USE_CONDITION）。古い model.json はキーが無いので従来どおり。
    const useCond = M.use_condition !== false;
    f['好調'] = useCond && cond === '好調' ? 1 : 0;
    f['不調'] = useCond && cond === '不調' ? 1 : 0;
    const pset = new Set(h.passives || []);
    for (const p of M.unspecced) {
      const has = pset.has(p) ? 1 : 0;
      if (M.catalog[p] === 'aptitude') {
        const [col, val] = M.aptitude_match[p];
        const ok = (col === 'track') ? (track === val) : (dist === val);
        f[p] = ok ? has : 0;
      } else {
        f[p] = has;
        for (const d of M.dist_list) {
          f[`${p}×${d}`] = M.interaction_shrink * has * ((dist === d) ? 1 : 0);
        }
      }
    }
    return f;
  }

  // --- 同族嫌悪の発動条件（Python: same_species_flags）---
  function sameSpeciesFlags(horses) {
    const key = h => String(h.species || String(h.name || '').replace(/\s*#\s*\d+\s*$/, '')).trim();
    const cnt = {};
    horses.forEach(h => { const k = key(h); cnt[k] = (cnt[k] || 0) + 1; });
    return horses.map(h => cnt[key(h)] >= 2);
  }

  // --- レース内で中心化した予測値（Python: predict_base）---
  function predictBase(horses, dist, track, M) {
    const same = sameSpeciesFlags(horses);
    const raw = horses.map((h, i) => {
      const f = rowFeatures(h, dist, track, M,
        { same_species: same[i], stamina_cost_mult: h.stamina_cost_mult || 1 });
      let s = M.intercept;
      for (const k of Object.keys(f)) {
        const c = M.coef[k];
        if (c !== undefined) s += c * f[k];
      }
      return s;
    });
    const mean = raw.reduce((a, b) => a + b, 0) / raw.length;
    return raw.map(x => x - mean);
  }

  // --- 決定的な正規乱数（Python の default_rng と値は違うが、統計的性質は同じ）---
  // 同じシードなら毎回同じ結果になり、検証・再現ができる。
  function makeRng(seed) {
    let s = seed >>> 0;
    const u32 = () => {                       // mulberry32
      s = (s + 0x6D2B79F5) >>> 0;
      let t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    let spare = null;
    return () => {                            // Box-Muller
      if (spare !== null) { const v = spare; spare = null; return v; }
      let u = 0, v = 0, sq = 0;
      do { u = u32() * 2 - 1; v = u32() * 2 - 1; sq = u * u + v * v; }
      while (sq >= 1 || sq === 0);
      const m = Math.sqrt(-2 * Math.log(sq) / sq);
      spare = v * m;
      return u * m;
    };
  }

  // --- モンテカルロ（Python: simulate_trifecta）---
  // 上位3頭だけ分かればよいので全体ソートはしない（16頭でも軽い）。
  // needCombo=false で組の集計（Map への出し入れ）を省く。単勝の勝率しか要らない
  // 呼び出しではここが処理時間の大半（Python の need_combo と同じ）。
  function simulateTrifecta(base, sigmas, nSim, seed, needCombo) {
    const wantCombo = needCombo !== false;
    const n = base.length;
    const randn = makeRng(seed == null ? 42 : seed);
    const win = new Float64Array(n);
    const combo = new Map();
    const val = new Float64Array(n);
    for (let it = 0; it < nSim; it++) {
      for (let i = 0; i < n; i++) val[i] = base[i] + randn() * sigmas[i];
      // 上位3つを1パスで求める
      let a = -1, b = -1, c = -1;
      for (let i = 0; i < n; i++) {
        const x = val[i];
        if (a < 0 || x > val[a]) { c = b; b = a; a = i; }
        else if (b < 0 || x > val[b]) { c = b; b = i; }
        else if (c < 0 || x > val[c]) { c = i; }
      }
      win[a]++;
      if (wantCombo && n >= 3) {
        const k = (a * n + b) * n + c;
        combo.set(k, (combo.get(k) || 0) + 1);
      }
    }
    const winP = Array.from(win, x => x / nSim);
    const comboP = [];
    for (const [k, v] of combo) {
      comboP.push({ i: Math.floor(k / (n * n)), j: Math.floor(k / n) % n, k: k % n,
                    p: v / nSim });
    }
    comboP.sort((x, y) => y.p - x.p);
    return { win: winP, combo: comboP };
  }

  function horseSigmas(horses, sigma, M) {
    return horses.map(h => sigma * sigmaMultiplier(h.passives, M, h.item_sigma_mult));
  }

  // --- 表示オッズ → 市場の暗黙勝率（Python: market_win_prob）---
  // floor 以下は「まだ誰も賭けていない（プレースホルダ）」として除外する。
  // 下限に張り付いた本命の**本当の**オッズを渡すときは floor=1.0 で呼ぶこと。
  // floor は呼び出し側が M.odds_floor を渡す（JS 側に 1.5 を持たせない）。
  function marketWinProb(odds, floor) {
    const raw = [];
    let sum = 0;
    for (let i = 0; i < odds.length; i++) {
      const o = +odds[i];
      const r = (Number.isFinite(o) && o > floor) ? 1.0 / o : 0.0;
      raw.push(r);
      sum += r;
    }
    if (!(sum > 0)) return null;
    return raw.map(r => r / sum);
  }

  // --- 下限オッズの正体を判定（Python: diagnose_floor_odds）---
  // 1.5 は未投票馬のプレースホルダであると同時に**ゲームの最低オッズ**でもある。
  // シェア 2/3 超の大本命はお金が入っていても 1.50 と表示されるので、これを
  // 「投入額0」と誤認すると実効オッズを桁違いに過大評価して大本命に高配当の
  // 買い推奨を出してしまう。単勝は控除0%なので、お金が入っている馬だけで
  // Σ(1/od)=1 が成り立つ。その不足分 residual が下限表示の馬のシェアになる。
  function diagnoseOddsFloor(odds, myAmounts, M) {
    const fl = M.odds_floor, n = odds.length;
    const od = [], mine = [], atFloor = [], priced = [];
    let mineSum = 0;
    for (let i = 0; i < n; i++) {
      const o = +odds[i];
      // Python の nan_to_num(nan=0.0) 相当。未入力（undefined）も 0 扱い。
      const m = myAmounts ? +myAmounts[i] : 0;
      od.push(o);
      mine.push(Number.isNaN(m) ? 0 : m);
      mineSum += mine[i];
      atFloor.push(Number.isFinite(o) && Math.abs(o - fl) < 1e-9);
      priced.push(Number.isFinite(o) && o > fl);
    }
    const out = { unbet: od.map(() => false), odds_eff: od.slice(),
                  ambiguous: false, residual: null, messages: [] };
    if (!atFloor.some(Boolean)) return out;

    // 自分で買った馬は、下限表示でも「お金が入っている」ことが確定している
    const knownBet = atFloor.map((f, i) => f && mine[i] > 0);
    const cand = atFloor.map((f, i) => f && !knownBet[i]);   // 未投票かもしれない馬
    const nanAtFloor = () => { for (let i = 0; i < n; i++) if (atFloor[i]) out.odds_eff[i] = NaN; };
    const markUnbet = () => { for (let i = 0; i < n; i++) if (cand[i]) out.unbet[i] = true; };

    if (!priced.some(Boolean)) {
      // 全馬が下限表示。誰も賭けていない（プール空）か、1頭が総取りしている状態。
      if (mineSum > 0) {
        out.ambiguous = true;
        nanAtFloor();
        out.messages.push(`⚠ 全馬のオッズが下限 ${fl} のままですが、自分の購入額があるためプールは` +
          '空ではありません。どの馬にお金が入っているか判別できないため、' +
          '単勝の推奨は出しません。');
      } else {
        markUnbet();
      }
      return out;
    }

    let S = 0;
    for (let i = 0; i < n; i++) if (priced[i]) S += 1.0 / od[i];
    const residual = 1.0 - S;
    out.residual = residual;

    if (residual <= M.floor_residual_unbet) {
      // Σ(1/od) がほぼ 1 → 下限表示の馬にはお金が入っていない
      markUnbet();
      if (knownBet.some(Boolean)) for (let i = 0; i < n; i++) if (knownBet[i]) out.odds_eff[i] = NaN;
      return out;
    }
    if (residual < M.floor_residual_real) {
      // どちらとも言い切れない中間帯（丸め誤差・データの取得ずれなど）。安全側に倒す。
      out.ambiguous = true;
      nanAtFloor();
      out.messages.push(`△ 単勝オッズの合計 Σ(1/od)=${S.toFixed(3)} がわずかに 1 を割っています` +
        `（残り ${(residual * 100).toFixed(1)}%）。オッズ ${fl} 表示の馬が未投票か` +
        '本命かを判別できないため、その馬は単勝の推奨から外します。');
      return out;
    }

    // residual が大きい＝下限表示の馬に実際のお金が入っている
    const idx = [];
    for (let i = 0; i < n; i++) if (cand[i]) idx.push(i);
    if (idx.length === 1 && !knownBet.some(Boolean)) {
      out.odds_eff[idx[0]] = 1.0 / residual;   // 下限で隠れていた本当のオッズ
      out.messages.push(`⚠ オッズ ${fl} 表示の馬は「未投票」ではなく、プールの約 ${(residual * 100).toFixed(0)}% を` +
        `集めた大本命です（Σ(1/od)=${S.toFixed(3)}）。表示は下限に張り付いているだけなので、` +
        `本当のオッズ ≒ ${(1.0 / residual).toFixed(2)} 倍として計算します。`);
    } else {
      out.ambiguous = true;
      nanAtFloor();
      out.messages.push(`⚠ オッズ ${fl} 表示の馬が ${atFloor.filter(Boolean).length}頭 あり、そのうち1頭が` +
        `プールの約 ${(residual * 100).toFixed(0)}% を集めた大本命です（Σ(1/od)=${S.toFixed(3)}）。` +
        `どの馬かはオッズだけでは判別できないため、${fl} 表示の馬は` +
        '単勝の推奨から外します（ゲーム画面の投票額を確認してください）。');
    }
    return out;
  }

  // --- 単勝の配分（Python: win_bet_picks_pool）---
  // パリミュチュエルなので自分が k口 入れると 実効オッズ=(P+Σk·u)/(P_i+k_i·u) と必ず下がる。
  // 表示オッズのまま計算すると期待値を過大評価するので、合計EVが最大になる配分を
  // 「限界EVが一番大きい馬へ1口ずつ」の貪欲法で求める。
  // opts = {stakeUnit, totalUnits, maxUnits, riskCapFrac, myUnits, unbet}（単位は M.win_* から）。
  function winBetPicksPool(names, winP, odds, pool, bankroll, kellyFrac, edgeMin, opts) {
    const o = opts || {};
    const unit = +o.stakeUnit, totalUnits = +o.totalUnits, maxUnits = +o.maxUnits;
    const riskCapFrac = (o.riskCapFrac == null ? 0.10 : +o.riskCapFrac);
    const minP = (o.minProb == null ? 0.50 : +o.minProb);
    // モデルだけが強気で市場が同意しない帯は買わない（実測 0/6・全損）。
    const skipP = (o.skipP == null ? 0.90 : +o.skipP);
    const skipOd = (o.skipOd == null ? 10.0 : +o.skipOd);
    // 乖離 = p × od。実結果72件で 12以上は 0/9・全損。p 閾値が取りこぼす帯を止める。
    const skipRatio = (o.skipRatio == null ? 3.0 : +o.skipRatio);
    const n = names.length;
    const od = [], p = [], unb = [], k0 = [], ok = [];
    for (let i = 0; i < n; i++) {
      od.push(+odds[i]);
      p.push(+winP[i]);
      unb.push(o.unbet ? !!o.unbet[i] : false);
      // k0 = すでに買った分。pool と P_i は「現在の値」＝ k0 を含んでいるので、
      // プールの再計算で k0 を足してはいけない（二重計上で希薄化を過小評価する）。
      k0.push(o.myUnits && o.myUnits[i] != null ? Math.trunc(o.myUnits[i]) : 0);
      // 予測が低い馬は買わない（実測で 50%未満は 25本中1本・回収率78%）。
      ok.push(Number.isFinite(od[i]) && p[i] >= minP && (od[i] > 1.0 || unb[i])
              && !(p[i] >= skipP && od[i] >= skipOd && !unb[i])
              && !(skipRatio > 0 && p[i] * od[i] >= skipRatio && !unb[i]));
    }
    if (pool == null || pool <= 0 || !ok.some(Boolean)) return [[], null];
    // 未投票の馬（オッズが初期値のまま）は「その馬への投入額 0」。
    // 表示オッズ 1.5 をそのまま使うと実際とかけ離れるので 0 として扱う。
    const P_i = [];
    for (let i = 0; i < n; i++) P_i.push((ok[i] && !unb[i]) ? pool / (od[i] > 0 ? od[i] : 1.0) : 0.0);

    const k = od.map(() => 0);
    const already = k0.reduce((a, b) => a + b, 0);
    const riskUnits = Math.max(1, Math.floor(riskCapFrac * bankroll / unit));
    const budget = Math.min(Math.trunc(totalUnits), riskUnits) - already;
    if (budget <= 0) return [[], { note: '既に上限まで購入済み' }];

    // これから追加する kv 口ぶんの期待値。既存の k0 は pool / P_i に織り込み済み。
    const totalEv = kv => {
      let sk = 0;
      for (let i = 0; i < n; i++) sk += kv[i];
      const Pnew = pool + sk * unit;
      let ev = 0;
      for (let i = 0; i < n; i++) {
        if (kv[i] <= 0) continue;
        const Pi = P_i[i] + kv[i] * unit;
        ev += kv[i] * unit * (p[i] * (Pi > 0 ? Pnew / Pi : 0.0) - 1.0);
      }
      return ev;
    };

    // ケリー上限（1口時の実効オッズで計算）
    const caps = od.map(() => 0);
    for (let i = 0; i < n; i++) {
      if (!ok[i]) continue;
      const eff1 = (pool + unit) / (P_i[i] + unit);
      const edge = p[i] * eff1 - 1;
      if (edge < edgeMin || eff1 <= 1) continue;
      const f = edge / (eff1 - 1);
      caps[i] = Math.max(0, Math.min(Math.floor(kellyFrac * f * bankroll / unit), Math.trunc(maxUnits)));
    }

    let base = totalEv(k), used = 0;
    while (used < budget) {
      let bestI = -1, bestGain = 1e-9;
      for (let i = 0; i < n; i++) {
        if (!ok[i] || k[i] + k0[i] >= caps[i]) continue;
        const trial = k.slice();
        trial[i] += 1;
        const g = totalEv(trial) - base;
        if (g > bestGain) { bestGain = g; bestI = i; }
      }
      if (bestI < 0) break;
      k[bestI] += 1;
      base = totalEv(k);
      used += 1;
    }

    const units = k.reduce((a, b) => a + b, 0);
    const Pnew = pool + units * unit;
    const out = [];
    for (let i = 0; i < n; i++) {
      if (k[i] <= 0) continue;
      const eff = Pnew / (P_i[i] + k[i] * unit);
      out.push({ name: names[i], p: p[i], odds: (unb[i] ? null : od[i]), unbet: unb[i],
                 eff_od: eff, edge: p[i] * eff - 1, units: k[i], stake: k[i] * unit,
                 ev: k[i] * unit * (p[i] * eff - 1) });
    }
    out.sort((a, b) => b.ev - a.ev);
    const summary = { units: units, invest: units * unit, ev: base,
                      pool_before: pool, pool_after: Pnew,
                      hit: out.reduce((s, r) => s + r.p, 0), unit: unit,
                      max_units: Math.trunc(totalUnits), already: already };
    return [out, summary];
  }

  // --- 3連単1組の最適口数（Python: optimal_units_ev）---
  // パリミュチュエルなので自分が k口 入れるとその組の取り分が薄まる。
  // 連続解 k_raw の floor だけを見ると EV を取りこぼすので floor+1 と比べる。
  function optimalUnitsEv(p, od, pTot, stakeUnit, maxUnits) {
    if (p <= 0 || p >= 1 || !od || !Number.isFinite(od) || od <= 1) return [0, 0.0, od];
    if (p <= 1.0 / od) return [0, 0.0, od];
    if (pTot <= 0) return [1, (p * od - 1) * stakeUnit, od];
    const pc = pTot / od;
    const inner = p * (od - 1) / (1.0 - p);
    const kRaw = (pTot / (od * stakeUnit)) * (Math.sqrt(inner) - 1);
    if (kRaw <= 0) return [0, 0.0, od];
    let k;
    if (kRaw < 1) {
      const eff1 = (pTot + stakeUnit) / (pc + stakeUnit);
      if (p * eff1 > 1) k = 1;
      else return [0, 0.0, od];
    } else {
      const ev = kk => {
        if (kk <= 0) return -1.0;
        const e = (pTot + kk * stakeUnit) / (pc + kk * stakeUnit);
        return (p * e - 1) * stakeUnit * kk;
      };
      const kLo = Math.min(maxUnits, Math.trunc(kRaw));   // Python の int() は 0 方向に切る
      const kHi = Math.min(maxUnits, kLo + 1);
      k = ev(kHi) > ev(kLo) ? kHi : kLo;
    }
    const effOd = (pTot + k * stakeUnit) / (pc + k * stakeUnit);
    return [k, (p * effOd - 1) * stakeUnit * k, effOd];
  }

  // --- 3連単の安定運用配分（Python: allocate_units_stable）---
  // cands = [{key, p, od}]（key は結果 Map のキーになる任意の文字列）。
  // opts = {budget, stakeUnit, maxPerCombo}。戻り値は key → [k, ev, eff] の Map。
  // 他の組に置いた口数もプールに入って**全組の**払戻を押し上げるので、1口足すかは
  // ポートフォリオ全体の EV で判断する（組ごとの EV で見ると払戻を過小評価する）。
  function allocateUnitsStable(cands, pTotal, bankroll, kellyFrac, maxRiskFrac, edgeMin, opts) {
    const o = opts || {};
    const stakeUnit = +o.stakeUnit;
    const budget = +o.budget;
    const maxPerCombo = (o.maxPerCombo == null ? budget : +o.maxPerCombo);
    const res = new Map();
    if (pTotal <= 0 || bankroll <= 0) return res;
    const riskUnits = Math.max(1, Math.floor(maxRiskFrac * bankroll / stakeUnit));
    const totalCap = Math.min(budget, riskUnits);

    const items = [];
    for (const c of cands) {
      const p = +c.p, od = +c.od;
      // inf/nan のオッズが通ると口数計算が NaN になって画面が落ちる
      if (!od || !Number.isFinite(od) || od <= 1 || !(p > 0 && p < 1)) continue;
      const pc = pTotal / od;
      const eff1 = (pTotal + stakeUnit) / (pc + stakeUnit);
      const edge = p * eff1 - 1;
      if (edge < edgeMin) continue;
      const f = edge / (eff1 - 1);
      const kKelly = Math.floor(kellyFrac * f * bankroll / stakeUnit);
      const kEvmax = optimalUnitsEv(p, od, pTotal, stakeUnit, maxPerCombo)[0];
      let cap = Math.min(kEvmax > 0 ? kEvmax : 0, maxPerCombo);
      cap = Math.min(cap, Math.max(1, kKelly));
      if (cap >= 1) items.push({ key: c.key, p: p, od: od, cap: cap });
    }
    if (!items.length) return res;

    // alloc は key ごと（同じ key が2件あれば Python の dict と同じく共有される）
    const alloc = new Map();
    for (const it of items) alloc.set(it.key, 0);
    const totalEv = () => {
      let totU = 0;
      for (const v of alloc.values()) totU += v;
      let s = 0.0;
      for (const it of items) {
        const k = alloc.get(it.key);
        if (k <= 0) continue;
        const pc = pTotal / it.od;
        const eff = (pTotal + totU * stakeUnit) / (pc + k * stakeUnit);
        s += (it.p * eff - 1) * stakeUnit * k;
      }
      return s;
    };

    // 限界EVが一番大きい組へ1口ずつ。同点は先に出てきた組が勝つ（> で比較）ので、
    // items の順序＝Python の dict の挿入順を崩すと結果がズレる。
    let used = 0, baseEv = 0.0;
    while (used < totalCap) {
      let best = null, bestM = 1e-9;
      for (const it of items) {
        if (alloc.get(it.key) >= it.cap) continue;
        alloc.set(it.key, alloc.get(it.key) + 1);
        const m = totalEv() - baseEv;
        alloc.set(it.key, alloc.get(it.key) - 1);
        if (m > bestM) { bestM = m; best = it.key; }
      }
      if (best === null) break;
      alloc.set(best, alloc.get(best) + 1);
      used += 1;
      baseEv += bestM;
    }

    for (const it of items) {
      const k = alloc.get(it.key);
      if (k > 0) {
        const pc = pTotal / it.od;
        const eff = (pTotal + used * stakeUnit) / (pc + k * stakeUnit);
        res.set(it.key, [k, (it.p * eff - 1) * stakeUnit * k, eff]);
      }
    }
    return res;
  }

  // --- 未成立の組に各1口だけ置く（Python: unformed_sleeve_picks）---
  // 的中時は全プール総取りなので実効オッズ = (プール総額 + 1口) / 1口。
  // ⚠ 高EVだが**全部外れる確率も高い**。既定は OFF。
  // comboProb = [[馬indexの配列, p], ...]（Map でも可。Python の dict と同じ順序で渡すこと）。
  // opts = {pMin, edgeMin, maxUnits, remainingBudget, stakeUnit, pScale}。
  function unformedSleevePicks(comboProb, disp, odOf, pTotal, opts) {
    const o = opts || {};
    const stakeUnit = +o.stakeUnit;
    const pMin = (o.pMin == null ? 0.05 : +o.pMin);
    const edgeMin = (o.edgeMin == null ? 0.30 : +o.edgeMin);
    const maxUnits = (o.maxUnits == null ? 5 : +o.maxUnits);
    const remainingBudget = +o.remainingBudget;
    const pScale = (o.pScale == null ? 1.0 : +o.pScale);
    if (pTotal <= 0 || maxUnits <= 0 || remainingBudget <= 0) return [];
    const eff = (pTotal + stakeUnit) / stakeUnit;
    const cand = [];
    for (const e of comboProb) {
      const p = e[1] * pScale;           // 市場情報が無い組は λ でモデル確率を割り引く
      const names = e[0].map(i => disp[i]);
      if (odOf(names) != null) continue;
      if (p < pMin || (p * eff - 1) < edgeMin) continue;
      cand.push({ names: names, p: p, i: cand.length });
    }
    // Python の sort は安定。同確率の組の順番が入れ替わると採用される組が変わる。
    cand.sort((a, b) => (b.p - a.p) || (a.i - b.i));
    const cap = Math.min(Math.trunc(maxUnits), Math.trunc(remainingBudget));
    return cand.slice(0, cap).map(c => [c.names, c.p, eff, 1]);
  }

  // --- 市場の一番人気の組（Python: market_fav_pick）---
  // EVは見ない。「市場が一番人気にしている組を薄く買う」だけの別枠。
  // 同オッズが並んだときの選び方（キー順）も Python と揃える。
  function marketFavPick(oddsByKey, minOd, units, already, maxOd) {
    let bk = null, bo = 0;
    for (const [k, v] of oddsByKey) {
      const o = +v;
      if (!(o > 0)) continue;
      if (bk === null || o < bo || (o === bo && String(k) < String(bk))) { bk = k; bo = o; }
    }
    if (bk === null) return null;
    const u = Math.trunc(units == null ? 1 : units);
    // 実測（28本）で採算に乗るのは od1.5以下だけ（〜1.5 回収率110% / 1.5〜2 44% / 2〜3 43%）。
    if (bo < (minOd == null ? 1.0 : minOd) || bo > (maxOd == null ? 1.5 : maxOd)
        || u < 1) return null;
    if (already && already.has && already.has(bk)) return null;
    return [bk, bo, u];
  }

  // ===== 区間シミュレータ（Python: race_sim.py。2026/10/07〜 bot の予想はこちら）=====
  // ゲームと同じく区間ごとに rating → 速度、スタミナを減らし、残りに応じて失速、乱数を入れて
  // タイムを足し上げる。消費量はレースごとにぶれる（±6%）ので、それも振る。
  // 序盤/中盤/終盤だけの効果はその区間にだけ、それ以外の状況限定はカタログの平均 duty で均す。
  // 係数（rating の式・消費のぶれ・失速の表）は model.json の sim。
  const SIM_PH = ['序盤', '中盤', '終盤'];
  const SIM_CONS = /スタミナ消費量[^。]*?(\d+(?:\.\d+)?)[%％](増加|減少)/;
  const SIM_POS = ['duel', 'king', 'lead', 'solo'];
  const simPosKind = t => t.includes('先頭から20m以内') ? 'king' : t.includes('20m以内') ? 'duel'
    : t.includes('2位と50m以上') ? 'solo' : t.includes('先頭の間') ? 'lead' : null;
  // Python: race_sim.horse_profile と同じ計算。
  function simProfile(h, dist, track, M, same) {
    const S = M.sim, ns = S.nseg[dist], n = ns[0] + ns[1] + ns[2];
    const base = { speed: +h.speed, power: +h.power, stamina: +h.stamina };
    const fx = [], cost = [1, 1, 1];
    let gamble = null;
    // 位置で決まる効果（20m以内にライバル・先頭から20m以内の2位以下・先頭の間・2位と50m以上）
    const pos = {}; for (const k of SIM_POS) pos[k] = [{}, 1];
    const dyn = new Set(S.pos_kinds || []);
    const addPos = (kind, m, cm) => {
      for (const k of Object.keys(m)) pos[kind][0][k] = (pos[kind][0][k] == null ? 1 : pos[kind][0][k]) * m[k];
      pos[kind][1] *= cm;
    };
    const posCost = (m, desc) => {
      let cm = 1;
      const mm = desc.match(SIM_CONS);
      if (mm) { const x = parseFloat(mm[1]) / 100; cm = mm[2] === '減少' ? 1 - x : 1 + x; delete m.stamina; }
      if (desc.includes('スタミナ消費も増加')) cm *= S.lead_cost_up;
      return cm;
    };
    const avgCost = (kind, cm) => { for (let pi = 0; pi < 3; pi++) cost[pi] *= 1 + (cm - 1) * S.pos_duty[kind]; };
    const clamp01 = d => Math.min(Math.max(d, 0), 1);
    const avg = (m, duty) => { for (const k of Object.keys(m)) if (k in base) base[k] *= 1 + (m[k] - 1) * clamp01(duty); };
    const phase = (m, arg, duty) => {
      const pi = SIM_PH.indexOf(arg);
      if (pi < 0) { avg(m, duty); return; }
      const frac = ns[pi] / n, mm = {};
      for (const k of Object.keys(m)) mm[k] = 1 + (m[k] - 1) * Math.min(1, duty / frac);
      fx.push([pi, mm]);
    };
    const lowL = new Set(S.lowst_labels || []), leadL = new Set(S.lead_labels || []);
    const put = (m, sc, arg, duty, label) => {
      if (lowL.has(label)) avg(m, S.lowst_duty);
      else if (leadL.has(label) || sc === 'lead') {
        const mm = {}; for (const k of Object.keys(m)) if (k !== 'stamina') mm[k] = m[k];
        avg(mm, S.lead_duty);
      }
      else if (sc === 'tail300') phase(m, '終盤', 3 / n);
      else if (sc === 'phase' || (sc === 'conditional' && SIM_PH.includes(arg))) phase(m, arg, duty);
      else if (sc === 'aptitude') { if (arg === dist || arg === track) avg(m, 1); }
      else if (sc === 'learned' || sc === 'same_species' || sc === 'variance') return;
      else avg(m, sc !== 'always' ? duty : 1);
    };
    for (const p of (h.passives || [])) {
      const sp = M.spec[p] || {};
      const m = Object.assign({}, sp.mult || {});
      if (!Object.keys(m).length) continue;
      const sc = sp.scope;
      if (sc === 'aptitude' && sp.scope_arg !== dist && sp.scope_arg !== track) continue;
      if (sc === 'same_species' && !same) continue;
      if (p === S.gamble) { gamble = [sp.duty == null ? 0.05 : +sp.duty, m]; continue; }
      const pp = (S.pos_passives || {})[p];
      if (pp) {
        const mm = Object.assign({}, pp.mult);
        if (dyn.has(pp.kind)) { addPos(pp.kind, mm, pp.cost); continue; }
        avgCost(pp.kind, pp.cost);
        for (const k of Object.keys(m)) if (!(k in mm)) delete m[k];
        if (!Object.keys(m).length) continue;
      }
      const cp = (S.cost_passives || {})[p];
      if (cp) {
        const pis = cp[0] ? [SIM_PH.indexOf(cp[0])] : [0, 1, 2];
        for (const pi of pis) cost[pi] *= cp[1];
        delete m.stamina;
        if (!Object.keys(m).length) continue;
      }
      if (sc === 'always' || sc === 'aptitude' || sc === 'same_species') avg(m, 1);
      else put(m, sc, sp.scope_arg, sp.duty == null ? 1 : +sp.duty, p);
    }
    for (const it of [h.equipment, h.charm]) {
      if (!it || typeof it !== 'object') continue;
      const label = String(it.effect_label || '').trim(), desc = String(it.effect_description || '');
      const m = pctMults(desc);
      const kind = simPosKind(desc);
      const cm = kind ? null : desc.match(SIM_CONS);
      if (kind) {
        const pcm = posCost(m, desc);
        if (dyn.has(kind)) { addPos(kind, m, pcm); continue; }
        avgCost(kind, pcm);
      }
      if (cm) {                      // 「スタミナ消費量が常時N%減少」は消費の倍率（スタミナを盛らない）
        const x = parseFloat(cm[1]) / 100;
        for (let pi = 0; pi < 3; pi++) cost[pi] *= cm[2] === '減少' ? 1 - x : 1 + x;
        delete m.stamina;
      }
      if (!Object.keys(m).length) continue;
      const c = (M.item_scope || {})[label];
      let sc, arg, duty;
      if (c) { sc = c.scope; arg = c.scope_arg; duty = c.duty == null ? 1 : +c.duty; }
      else {
        const code = String(it.effect_key || '').replace(/^(?:gear|charm|item)_/, '');
        const alias = (M.code_map || {})[code] || (M.item_key_alias || {})[code];
        const a = alias ? (M.spec[alias] || {}) : null;
        if (a) { sc = a.scope || 'conditional'; arg = a.scope_arg; duty = a.duty == null ? 1 : +a.duty; }
        else if (desc.includes('常時')) { sc = 'always'; arg = null; duty = 1; }
        else continue;
      }
      put(m, sc, arg, duty, label);
    }
    for (const k of Object.keys(base)) base[k] = Math.max(base[k], 1);
    // 消費量は区間効果を区間の割合で均した実効ステから（Python: stamina_budget と同じ式）
    const e = Object.assign({}, base);
    for (const [pi, m] of fx) for (const k of Object.keys(m)) e[k] *= 1 + (m[k] - 1) * ns[pi] / n;
    for (const kd of SIM_POS) for (const k of Object.keys(pos[kd][0]))       // 消費は従来どおり平均で
      if (k !== 'stamina') e[k] *= 1 + (pos[kd][0][k] - 1) * S.pos_duty[kd];
    const need = staminaBudget(e, dist, M, 1)[0];
    const KS = ['speed', 'power', 'stamina'];
    const st = [0, 1, 2].map(() => KS.map(k => base[k]));
    for (const [pi, m] of fx) KS.forEach((k, j) => { if (k in m) st[pi][j] *= m[k]; });
    return { st: st, c0: need / n, cost: cost, s0: Math.floor(e.stamina),
             g_p: gamble ? gamble[0] : 0, g_m: KS.map(k => gamble ? (gamble[1][k] == null ? 1 : +gamble[1][k]) : 1),
             p_m: SIM_POS.map(kd => KS.map(k => pos[kd][0][k] == null ? 1 : pos[kd][0][k])),
             p_c: SIM_POS.map(kd => pos[kd][1]) };
  }

  function simInputs(horses, dist, track, M) {
    const same = sameSpeciesFlags(horses);
    return horses.map((h, i) => simProfile(h, dist, track, M, same[i]));
  }

  // horses: [{name, species, speed, power, stamina, passives(日本語名), equipment, charm}]
  // （speed などは API の値そのまま＝装備の加算込み・倍率なし）
  function simRace(horses, dist, track, M, nSim, seed) {
    const S = M.sim, ns = S.nseg[dist], H = horses.length;
    const prof = simInputs(horses, dist, track, M);
    const segPh = []; for (let pi = 0; pi < 3; pi++) for (let k = 0; k < ns[pi]; k++) segPh.push(pi);
    const nseg = segPh.length;
    const W = S.w[dist];
    // 位置効果を持つ馬と種類（Python: has）
    const hasP = prof.map(x => SIM_POS.map((_, j) => x.p_m[j].some(v => v !== 1) || x.p_c[j] !== 1));
    const anyP = hasP.some(a => a.some(Boolean));
    const pid = horses.map(h => +h.pet_id || 0);
    // 区間ごとの消費倍率と、その区間から最後までの和（見込みの余りに使う）
    const cmS = prof.map(x => segPh.map(pi => x.cost[pi]));
    const remS = cmS.map(a => { const r = new Array(nseg); let acc = 0; for (let k = nseg - 1; k >= 0; k--) { acc += a[k]; r[k] = acc; } return r; });
    const fqx = S.fqx, fqy = S.fqy;
    const fat = q => {                       // np.interp と同じ（端は端の値）
      if (q <= fqx[0]) return fqy[0];
      for (let i = 1; i < fqx.length; i++) if (q <= fqx[i]) return fqy[i - 1] + (fqy[i] - fqy[i - 1]) * (q - fqx[i - 1]) / (fqx[i] - fqx[i - 1]);
      return fqy[fqy.length - 1];
    };
    const randn = makeRng(seed == null ? 1 : seed);
    const NS = nSim || S.n_sim || 8000;
    const win = new Float64Array(H), combo = new Map(), T = new Float64Array(H);
    const C = new Float64Array(H), ON = new Uint8Array(H), Sv = new Float64Array(H), S0 = new Float64Array(H), DT = new Float64Array(H);
    const HZ = new Float64Array(H).fill(1);
    const inRace = !!(S.horse_sd && S.horse_in_race);   // 調子のぶれをレース中の速さに掛ける（Python と同じ）
    const mul = [1, 1, 1];
    for (let it = 0; it < NS; it++) {
      for (let h = 0; h < H; h++) {
        const x = prof[h];
        C[h] = x.c0 * Math.exp(S.cost_mu + S.cost_sd * randn());
        ON[h] = x.g_p > 0 && randn() < S.gamble_z ? 1 : 0;      // 勝負師の抽選（5%）
        Sv[h] = ON[h] ? Math.floor(x.s0 * x.g_m[2]) : x.s0;
        S0[h] = Math.max(Sv[h], 1);
        T[h] = 0; DT[h] = 1;
        if (inRace) HZ[h] = Math.exp(S.horse_sd * randn());
      }
      for (let k = 0; k < nseg; k++) {
        const pi = segPh[k];
        // 先頭と2番手（同じ位置なら pet_id の小さい馬が前。スタート直後は全頭 0m なので pet_id 順）
        let i1 = -1, i2 = -1;
        const ahead = (a, b) => T[a] < T[b] || (T[a] === T[b] && pid[a] < pid[b]);
        if (anyP) for (let h = 0; h < H; h++) {
          if (i1 < 0 || ahead(h, i1)) { i2 = i1; i1 = h; } else if (i2 < 0 || ahead(h, i2)) i2 = h;
        }
        const t1 = i1 >= 0 ? T[i1] : Infinity, t2 = i2 >= 0 ? T[i2] : Infinity;
        for (let h = 0; h < H; h++) {
          const x = prof[h];
          for (let j = 0; j < 3; j++) mul[j] = ON[h] ? x.g_m[j] : 1;
          let cmul = 1;
          if (anyP && hasP[h].some(Boolean)) {
            // 区間の始めの位置で判定（時間差 × 自分の速さで m に直す。Python と同じ近似）
            const spd = S.seg_m / DT[h];
            let near = Infinity;
            for (let o = 0; o < H; o++) if (o !== h) near = Math.min(near, Math.abs(T[o] - T[h]));
            const lead = h === i1;
            const cond = [near * spd <= S.duel_m, !lead && (T[h] - t1) * spd <= S.duel_m,
                          lead, lead && (t2 - T[h]) * spd >= S.solo_m];
            for (let q = 0; q < SIM_POS.length; q++) if (hasP[h][q] && cond[q]) {
              for (let j = 0; j < 3; j++) mul[j] *= x.p_m[q][j];
              cmul *= x.p_c[q];
            }
          }
          const w = W[pi], st = x.st[pi];
          const rv = Math.pow(Math.max(st[0] * w[0] * mul[0] + st[1] * w[1] * mul[1] + st[2] * w[2] * mul[2] + w[3], 1), S.v_exp);
          // 疲労補正は「この先を走り切ったときの見込みの余り ÷ 初期スタミナ」で引く（Python と同じ）
          const f = Math.pow(fat((Sv[h] - C[h] * remS[h][k]) / S0[h]), S.f_exp);
          DT[h] = HZ[h] / (rv * f * (1 + S.seg_noise * randn()));
          Sv[h] -= C[h] * cmS[h][k] * cmul;
        }
        for (let h = 0; h < H; h++) T[h] += DT[h];      // 全頭の区間が終わってから進める（Python と同じ）
      }
      // 1レース1頭ごとの調子のぶれ（区間の乱数だけだと自信過剰になる）
      if (S.horse_sd && !inRace) for (let h = 0; h < H; h++) T[h] *= Math.exp(S.horse_sd * randn());
      let a = -1, b = -1, c3 = -1;            // タイムの小さい順に上位3頭
      for (let i = 0; i < H; i++) {
        const v = T[i];
        if (a < 0 || v < T[a]) { c3 = b; b = a; a = i; }
        else if (b < 0 || v < T[b]) { c3 = b; b = i; }
        else if (c3 < 0 || v < T[c3]) { c3 = i; }
      }
      win[a]++;
      if (H >= 3) { const key = (a * H + b) * H + c3; combo.set(key, (combo.get(key) || 0) + 1); }
    }
    const comboP = [];
    for (const [k, v] of combo) comboP.push({ i: Math.floor(k / (H * H)), j: Math.floor(k / H) % H, k: k % H, p: v / NS });
    comboP.sort((x, y) => y.p - x.p);
    return { win: Array.from(win, x => x / NS), combo: comboP, inputs: prof };
  }

  return { effectiveStats, sigmaMultiplier, rowFeatures, predictBase, marketFavPick,
           simInputs, simRace,
           sameSpeciesFlags, simulateTrifecta, horseSigmas, makeRng,
           pctMults, itemMult, applyItems, leadAdjustedBase,
           marketWinProb, diagnoseOddsFloor, winBetPicksPool,
           optimalUnitsEv, allocateUnitsStable, unformedSleevePicks };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = OasisModel;
