// 区間シミュレータの Python↔JS 一致検証（build_autopilot.py が node で実行する）。
// 入力（フェーズ別の実効ステ・1区間の消費・初期スタミナ）は完全一致、
// 勝率は乱数の種が違うので 4万回どうしで ±0.02 以内を見る。
const fs = require('fs');
const path = require('path');
const OasisModel = require('./bookmarklets/src/model.js');
const M = JSON.parse(fs.readFileSync(path.join(__dirname, 'model.json'), 'utf8'));
const F = JSON.parse(fs.readFileSync(path.join(__dirname, '_parity_sim.json'), 'utf8'));
let worstIn = 0, worstP = 0;
for (const r of F) {
  const js = OasisModel.simInputs(r.horses, r.dist, r.track, M);
  js.forEach((x, h) => {
    const py = r.inputs;
    for (let pi = 0; pi < 3; pi++) for (let j = 0; j < 3; j++)
      worstIn = Math.max(worstIn, Math.abs(x.st[pi][j] - py.st[h][pi][j]) / Math.max(1, py.st[h][pi][j]));
    worstIn = Math.max(worstIn, Math.abs(x.c0 - py.c0[h]) / Math.max(1e-9, py.c0[h]), Math.abs(x.s0 - py.s0[h]));
  });
  const w = OasisModel.simRace(r.horses, r.dist, r.track, M, 40000, 11).win;
  w.forEach((p, i) => { worstP = Math.max(worstP, Math.abs(p - r.win[i])); });
}
if (worstIn > 1e-6 || worstP > 0.02) {
  console.error(`❌ シミュレータ不一致: 入力の最大誤差 ${worstIn.toExponential(2)} / 勝率の最大差 ${worstP.toFixed(3)}`);
  process.exit(1);
}
console.log(`✅ シミュレータ 一致（${F.length}レース 入力誤差 ${worstIn.toExponential(1)} / 勝率差 ${worstP.toFixed(3)}）`);
