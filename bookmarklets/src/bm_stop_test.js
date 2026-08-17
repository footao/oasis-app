// bm.js の3連単オッズ取得（適応的な並べ替え＋残額<1口で打ち切り）の自己検査。
//   node bm_stop_test.js
// 検査すること: 金の乗っている組を1つでも取り逃したら失敗にする。
// 単勝オッズは下限に張り付いていて使えないので、初期順の当てにならなさも込みで試す。
const SEED = 200000, UNIT = 10000;
const rng = s => () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296;

// bm.js のループと同じ手順。fetch の代わりに bet(Map) を引く。
function scrape(n, bet, initOrder, BATCH) {
  BATCH = BATCH || 20;
  const BASE = [...bet.values()].reduce((s, v) => s + v, 0);
  const w = new Map([...Array(n)].map((_, i) => [i, 1]));
  const sc = c => w.get(c[0]) * w.get(c[1]) * w.get(c[2]);
  const queue = initOrder.slice(), results = []; let seen = 0, cut = 0;
  while (queue.length) {
    const batch = queue.splice(0, BATCH);
    const got = batch.map(c => bet.has(c.join('-')) ? BASE / bet.get(c.join('-')) : null);
    got.forEach((od, k) => results.push([batch[k], od]));
    cut += batch.length;
    let err = 0;
    seen = results.reduce((s, [, od]) => { if (!od) return s; const b = BASE / od; err += b * 0.005 / od; return s + b; }, 0);
    if (BASE > 0 && BASE - seen + err < UNIT) break;
    let hit = false;
    got.forEach((od, k) => { if (od !== null) { hit = true; for (const h of batch[k]) w.set(h, w.get(h) * 3); } });
    if (hit) queue.sort((p, q) => sc(q) - sc(p));
  }
  for (const [c] of results) bet.delete(c.join('-'));   // 取れた分を消す
  if (bet.size) throw new Error(`取り逃し ${bet.size}組 (n=${n}, cut=${cut})`);
  return { total: initOrder.length, cut, rounds: Math.ceil(cut / BATCH) };
}

function combosOf(n) {
  const out = [];
  for (let a = 0; a < n; a++) for (let b = 0; b < n; b++) { if (b === a) continue;
    for (let c = 0; c < n; c++) { if (c === a || c === b) continue; out.push([a, b, c]); } }
  return out;
}

// 賭けの置き方2通り。fav=true なら「人気の数頭に集中」（現実寄り）、false なら完全ランダム（最悪）
function bets(n, k, fav, r) {
  const pop = fav ? [...Array(n).keys()].slice(0, Math.max(3, Math.ceil(n / 3))) : [...Array(n).keys()];
  const m = new Map();
  while (m.size < k) {
    const pick = () => pop[Math.floor(r() * pop.length)];
    const a = pick(); let b = pick(), c = pick();
    if (a === b || b === c || a === c) continue;
    m.set([a, b, c].join('-'), UNIT * (1 + Math.floor(r() * 3)));
  }
  return m;
}

// SPEED 順を当てにできるか（＝みんなが速い馬から買うか）は未検証なので、3通り試す。
// combosOf は馬0が一番速い前提の並び、bets(fav=true) は上位数頭に金が集中する置き方。
for (const [label, fav, shuffle] of [
  ['人気集中・SPEED順が当たり', true, false],
  ['人気集中・SPEED順が外れ  ', true, true],
  ['完全ランダム（最悪）      ', false, true]]) {
  let T = 0, C = 0, races = 0;
  for (const n of [10, 12, 15, 16]) for (let s = 1; s <= 60; s++) {
    const r = rng(s * 7919 + n);
    const order = combosOf(n);
    if (shuffle) for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
    const { total, cut } = scrape(n, bets(n, 2 + Math.floor(r() * 8), fav, r), order);
    T += total; C += cut; races++;
  }
  console.log(`${label}: 取り逃しゼロ / 平均 ${Math.round(T / races)} → ${Math.round(C / races)} リクエスト (${(T / C).toFixed(1)}x)`);
}

// プール不明（BASE=0）なら打ち切らず全件取ること
{
  const order = combosOf(10);
  const { total, cut } = scrape(10, new Map(), order);
  if (cut !== total) throw new Error(`BASE=0 なのに打ち切った: ${cut}/${total}`);
  console.log(`プール不明時: 全 ${total} 件取得（従来どおり）`);
}
