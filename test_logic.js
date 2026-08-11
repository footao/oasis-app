// autopilot.js の判定式と同じ計算で、境界と安全弁が意図どおり効くか確認する。
const CFG = { MODEL_WEIGHT: 0.7, MIN_PROB: 0.02, EDGE_MIN: 0.10,
              MAX_SANE_EDGE: 3.0, MAX_SANE_ODDS: 5000 };
const U = 10000;

function judge(p, od, pool, norm) {
  const pBet = CFG.MODEL_WEIGHT * p + (1 - CFG.MODEL_WEIGHT) * ((1 / od) / norm);
  const eff = (pool + U) / (pool / od + U);
  const edge = pBet * eff - 1;
  if (eff > CFG.MAX_SANE_ODDS) return ['skip', '実効odが異常', eff, edge];
  if (edge > CFG.MAX_SANE_EDGE) return ['ABORT', 'エッジ異常→レース中止', eff, edge];
  if (p < CFG.MIN_PROB) return ['skip', 'モデル確率が低すぎ', eff, edge];
  if (edge < CFG.EDGE_MIN) return ['skip', 'エッジ不足', eff, edge];
  return ['BUY', '購入', eff, edge];
}

// 注意: λ混合（p_bet = 0.7·モデル + 0.3·市場）があるため、
// モデルが市場を「かなり」上回らないと +10% のエッジには届かない。
// 例: od=3.0（市場33%）なら、モデルが 38.6% 以上でようやく BUY。
const cases = [
  ['モデルが市場を大きく上回る', 0.45,   3.0, 2000000, 1.0, 'BUY'],
  ['境界のすぐ上(モデル39%)',  0.39,   3.0, 2000000, 1.0, 'BUY'],
  ['境界のすぐ下(モデル37%)',  0.37,   3.0, 2000000, 1.0, 'skip'],
  ['モデルと市場がほぼ一致',   0.35,   3.0, 2000000, 1.0, 'skip'],
  ['エッジ不足',            0.20,   5.0, 2000000, 1.0, 'skip'],
  ['確率が低すぎる大穴',      0.005, 900.0, 2000000, 1.0, 'skip'],
  ['R1級のバグ相当',        0.72,  201.0,  200000, 1.0, 'ABORT'],
];
let ng = 0;
console.log('ケース'.padEnd(22) + '判定'.padEnd(10) + '実効od'.padStart(9) + 'エッジ'.padStart(10));
console.log('-'.repeat(58));
for (const [nm, p, od, pool, norm, want] of cases) {
  const [v, why, eff, edge] = judge(p, od, pool, norm);
  const ok = v === want;
  if (!ok) ng++;
  const mark = v === 'BUY' ? '🟢' : (v === 'ABORT' ? '🛑' : '⚪');
  console.log(nm.padEnd(22) + (mark + v).padEnd(12)
    + eff.toFixed(1).padStart(8) + ((edge * 100).toFixed(0) + '%').padStart(10)
    + (ok ? '' : `  ❌ 期待=${want}`));
}
// 予算上限
let spent = 190000; const cost = 30000, budget = 200000;
console.log('\n予算上限: 使用済 ' + spent.toLocaleString() + ' + 今回 ' + cost.toLocaleString()
  + ' > 上限 ' + budget.toLocaleString() + ' → '
  + (spent + cost > budget ? '🛑 見送り（正しい）' : '❌ 通してしまう'));
if (spent + cost <= budget) ng++;
console.log(ng ? `\n❌ ${ng}件が想定と違います` : '\n✅ 判定と安全弁はすべて想定どおり');
process.exit(ng ? 1 : 0);
