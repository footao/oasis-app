// parity_test.js — parity_test.py から呼ばれる。単体では動かない。
const fs = require('fs');
const path = require('path');
const HERE = __dirname;
const M = JSON.parse(fs.readFileSync(path.join(HERE, '_parity_model.json'), 'utf8'));
const cases = JSON.parse(fs.readFileSync(path.join(HERE, '_parity_cases.json'), 'utf8'));
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
const n = cases.reduce((s, c) => s + c.base.length, 0);
console.log(`Python↔JS 一致検証: ${n}頭  最大誤差 ${worst.toExponential(2)}`);
if (bad) { console.log(`❌ 不一致 ${bad}件 — model.js が oasis_core に追随していません`); process.exit(1); }
console.log('✅ 一致');
