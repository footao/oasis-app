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

  // --- 分散低減スキル（安定感など）による σ 倍率（Python: sigma_multiplier）---
  function sigmaMultiplier(passives, M) {
    let m = 1;
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
  function staminaBudget(e, dist, M) {
    const L = (M.stamina_cost_law || {})[dist];
    if (!L) return [0, 0, 0];
    const w = M.phase_early, b = (M.dist_balance || {})[dist] || [1, 1, 1];
    const base = e.speed * w[0] * b[0] + e.power * w[1] * b[1] + e.stamina * w[2] * b[2];
    const need = Math.min(Math.max(L.c * base, L.lo), L.hi) * L.n_seg;
    const have = Math.floor(e.stamina);
    return [need, Math.max(0, need - have), Math.max(0, have - need)];
  }

  // --- 1頭ぶんの特徴量（Python: _row_features）を {名前: 値} で返す ---
  function rowFeatures(h, dist, track, M, ctx) {
    const e = effectiveStats(h.speed, h.power, h.stamina, h.passives, dist, track, M, ctx);
    const lg = { SP: Math.log(Math.max(e.speed, 1)), PW: Math.log(Math.max(e.power, 1)),
                 ST: Math.log(Math.max(e.stamina, 1)) };
    const ln = { SP: e.speed / 100, PW: e.power / 100, ST: e.stamina / 100 };
    const f = {};
    for (const d of M.dist_list) {
      const m = (dist === d) ? 1 : 0;
      f[`${d}:切片`] = m;
      for (const s of ['SP', 'PW', 'ST']) {
        f[`${d}:log(${s})`] = m * lg[s];
        f[`${d}:lin(${s})`] = m * ln[s];
      }
    }
    const bud = staminaBudget(e, dist, M);
    f['スタミナ余り'] = bud[2] / 10;
    f['スタミナ不足'] = bud[1] / 10;
    const cond = h.condition || '普通';
    f['好調'] = cond === '好調' ? 1 : 0;
    f['不調'] = cond === '不調' ? 1 : 0;
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
      const f = rowFeatures(h, dist, track, M, { same_species: same[i] });
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
  function simulateTrifecta(base, sigmas, nSim, seed) {
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
      if (n >= 3) {
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
    return horses.map(h => sigma * sigmaMultiplier(h.passives, M));
  }

  return { effectiveStats, sigmaMultiplier, rowFeatures, predictBase,
           sameSpeciesFlags, simulateTrifecta, horseSigmas, makeRng };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = OasisModel;
