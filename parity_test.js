// parity_test.js — parity_test.py から呼ばれる。単体では動かない。
const fs = require('fs');
const path = require('path');
const HERE = __dirname;
const M = JSON.parse(fs.readFileSync(path.join(HERE, '_parity_model.json'), 'utf8'));
const _all = JSON.parse(fs.readFileSync(path.join(HERE, '_parity_cases.json'), 'utf8'));
const cases = _all.races, items = _all.items || [];
const OM = require(path.join(HERE, 'bookmarklets/src/model.js'));

let worst = 0, bad = 0;
for (const c of cases) {
  const js = OM.predictBase(c.horses, c.dist, c.track, M);
  for (let i = 0; i < js.length; i++) {
    const d = Math.abs(js[i] - c.base[i]);
    if (d > worst) worst = d;
    if (d > 1e-9) bad++;
  }
}
// --- 装備・お守りの効果（図鑑30種）---
let ibad = 0, iworst = 0;
for (const c of items) {
  const js = OM.itemMult({ effect_label: c.label, effect_description: c.desc, effect_key: c.key }, M, []);
  const py = c.py;
  if ((js == null) !== (py == null)) { ibad++; console.log(`  ❌ ${c.label}: JS=${JSON.stringify(js)} / PY=${JSON.stringify(py)}`); continue; }
  if (js == null) continue;
  const keys = new Set([...Object.keys(js), ...Object.keys(py)]);
  for (const k of keys) {
    const d = Math.abs((js[k] == null ? 1 : js[k]) - (py[k] == null ? 1 : py[k]));
    if (d > iworst) iworst = d;
    if (d > 1e-9) { ibad++; console.log(`  ❌ ${c.label}.${k}: JS=${js[k]} / PY=${py[k]}`); }
  }
}
console.log(`装備効果 一致検証: ${items.length}種  最大誤差 ${iworst.toExponential(2)}`);
if (ibad) { console.log(`❌ 装備効果 不一致 ${ibad}件 — model.js の itemMult が oasis_core に追随していません`); process.exit(1); }

// --- 単勝（下限オッズの判定・希薄化を織り込んだ配分）---
const wins = _all.win || [];
const _nan = v => (v === null ? NaN : v);   // Python の NaN は JSON に書けないので null で来る
let wbad = 0, wworst = 0;
const chk = (a, b, what) => {               // NaN 同士は一致、片方だけ NaN は不一致
  a = _nan(a); b = _nan(b);
  const d = (Number.isNaN(a) || Number.isNaN(b))
    ? ((Number.isNaN(a) && Number.isNaN(b)) ? 0 : Infinity) : Math.abs(a - b);
  if (d > wworst) wworst = d;
  if (d > 1e-9) { wbad++; console.log(`  ❌ ${what}: JS=${a} / PY=${b}`); }
};
const eq = (a, b, what) => {
  if (a !== b) { wbad++; console.log(`  ❌ ${what}: JS=${JSON.stringify(a)} / PY=${JSON.stringify(b)}`); }
};
for (let ci = 0; ci < wins.length; ci++) {
  const c = wins[ci], tag = `win[${ci}]`;
  const od = c.odds.map(_nan);
  const mkt = OM.marketWinProb(od, c.floor);
  eq(mkt === null, c.mkt === null, `${tag}.mkt null`);
  if (mkt && c.mkt) for (let i = 0; i < mkt.length; i++) chk(mkt[i], c.mkt[i], `${tag}.mkt[${i}]`);

  const dg = OM.diagnoseOddsFloor(od, c.my_amounts, M);
  eq(dg.ambiguous, c.diag.ambiguous, `${tag}.ambiguous`);
  chk(dg.residual, c.diag.residual, `${tag}.residual`);
  eq(JSON.stringify(dg.unbet), JSON.stringify(c.diag.unbet), `${tag}.unbet`);
  eq(JSON.stringify(dg.messages), JSON.stringify(c.diag.messages), `${tag}.messages`);
  for (let i = 0; i < od.length; i++) chk(dg.odds_eff[i], c.diag.odds_eff[i], `${tag}.odds_eff[${i}]`);

  const r = OM.winBetPicksPool(c.names, c.win_p, dg.odds_eff, c.pool, c.bankroll,
    c.kelly, c.edge_min, { stakeUnit: M.win_stake_unit, totalUnits: M.win_max_total_units,
      maxUnits: M.win_max_units, riskCapFrac: c.risk_cap_frac,
      myUnits: c.my_units, unbet: dg.unbet });
  const picks = r[0], summ = r[1];
  eq(picks.length, c.picks.length, `${tag}.picks 件数`);
  for (let i = 0; i < Math.min(picks.length, c.picks.length); i++) {
    const a = picks[i], b = c.picks[i];
    eq(a.name, b.name, `${tag}.picks[${i}].name`);
    eq(a.unbet, b.unbet, `${tag}.picks[${i}].unbet`);
    eq(a.odds === null, b.odds === null, `${tag}.picks[${i}].odds null`);
    if (a.odds !== null && b.odds !== null) chk(a.odds, b.odds, `${tag}.picks[${i}].odds`);
    for (const k of ['p', 'eff_od', 'edge', 'units', 'stake', 'ev']) chk(a[k], b[k], `${tag}.picks[${i}].${k}`);
  }
  eq(summ === null, c.summary === null, `${tag}.summary null`);
  if (summ && c.summary) for (const k of Object.keys(c.summary)) {
    if (typeof c.summary[k] === 'number') chk(summ[k], c.summary[k], `${tag}.summary.${k}`);
    else eq(summ[k], c.summary[k], `${tag}.summary.${k}`);
  }
}
console.log(`単勝配分 一致検証: ${wins.length}件  最大誤差 ${wworst.toExponential(2)}`);
if (wbad) { console.log(`❌ 単勝配分 不一致 ${wbad}件 — model.js の単勝ロジックが oasis_core に追随していません`); process.exit(1); }

// --- 3連単（1組の最適口数・成立組の配分・未成立組の1口買い）---
const tris = _all.tri || [];
const _od = v => (v === null ? NaN : (v === 'inf' ? Infinity : v));
let tbad = 0, tworst = 0;
const tchk = (a, b, what) => {              // NaN 同士は一致、片方だけ NaN は不一致
  const d = (Number.isNaN(a) || Number.isNaN(b))
    ? ((Number.isNaN(a) && Number.isNaN(b)) ? 0 : Infinity) : Math.abs(a - b);
  if (d > tworst) tworst = d;
  if (d > 1e-9) { tbad++; console.log(`  ❌ ${what}: JS=${a} / PY=${b}`); }
};
const teq = (a, b, what) => {
  if (a !== b) { tbad++; console.log(`  ❌ ${what}: JS=${JSON.stringify(a)} / PY=${JSON.stringify(b)}`); }
};
for (let ci = 0; ci < tris.length; ci++) {
  const c = tris[ci], tag = `tri[${ci}]`;
  const cands = c.cands.map(x => ({ key: x.key, p: x.p, od: _od(x.od) }));
  for (let i = 0; i < cands.length; i++) {
    const js = OM.optimalUnitsEv(cands[i].p, cands[i].od, c.pool, M.stake_unit, M.max_units);
    const py = c.opt[i];
    for (let k = 0; k < 3; k++) tchk(js[k], _od(py[k]), `${tag}.opt[${i}][${k}]`);
  }

  const alloc = OM.allocateUnitsStable(cands, c.pool, c.bankroll, c.kelly,
    c.max_risk_frac, c.edge_min,
    { budget: c.budget, stakeUnit: M.stake_unit, maxPerCombo: c.max_per_combo });
  // Python の dict は挿入順を保つ。並びが違えば貪欲法の同点処理がズレている。
  teq(Array.from(alloc.keys()).join(','), Object.keys(c.alloc).join(','), `${tag}.alloc 並び`);
  for (const k of Object.keys(c.alloc)) {
    const a = alloc.get(k);
    if (!a) { tbad++; console.log(`  ❌ ${tag}.alloc[${k}]: JS に無い`); continue; }
    for (let j = 0; j < 3; j++) tchk(a[j], c.alloc[k][j], `${tag}.alloc[${k}][${j}]`);
  }

  const odMap = new Map(c.od_map);
  const o = c.sleeve_opts;
  const sleeve = OM.unformedSleevePicks(c.combo_prob, c.disp,
    nm => (odMap.has(nm.join('|')) ? odMap.get(nm.join('|')) : null), c.pool,
    { pMin: o.pMin, edgeMin: o.edgeMin, maxUnits: o.maxUnits,
      remainingBudget: o.remainingBudget, stakeUnit: M.stake_unit, pScale: o.pScale });
  teq(sleeve.length, c.sleeve.length, `${tag}.sleeve 件数`);
  for (let i = 0; i < Math.min(sleeve.length, c.sleeve.length); i++) {
    teq(sleeve[i][0].join('|'), c.sleeve[i][0].join('|'), `${tag}.sleeve[${i}].names`);
    for (let j = 1; j < 4; j++) tchk(sleeve[i][j], c.sleeve[i][j], `${tag}.sleeve[${i}][${j}]`);
  }
}
console.log(`3連単配分 一致検証: ${tris.length}件  最大誤差 ${tworst.toExponential(2)}`);
if (tbad) { console.log(`❌ 3連単配分 不一致 ${tbad}件 — model.js の3連単ロジックが oasis_core に追随していません`); process.exit(1); }

const n = cases.reduce((s, c) => s + c.base.length, 0);
console.log(`Python↔JS 一致検証: ${n}頭  最大誤差 ${worst.toExponential(2)}`);
if (bad) { console.log(`❌ 不一致 ${bad}件 — model.js が oasis_core に追随していません`); process.exit(1); }
console.log('✅ 一致');
