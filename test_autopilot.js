// test_autopilot.js — autopilot.js の「金に直結する」ブロックを取り出して実際に動かす。
//   node test_autopilot.js   （build_autopilot.py が node のある環境で自動で回す）
// 文字列の一致ではなく挙動で確かめる: オッズの取り損ね・キャリーオーバー・2・3着の順番補正。
let failed = 0;
const _assert = console.assert;
console.assert = (c, m) => { if (!c) { failed++; console.error('❌ ' + m); } };
process.on('beforeExit', () => { if (failed) process.exitCode = 1; else console.log('✅ autopilot ブロックテスト OK'); });
{
// fetchOdds を単体で取り出し、API をスタブして「取り損ね」を確かめる
const fs=require('fs');
const src=require('fs').readFileSync(require('path').join(__dirname,'bookmarklets','src','autopilot.js'),'utf8');
const a=src.indexOf('async function fetchOdds('), b=src.indexOf('// ---- 解析して買い目を用意する');
const fn=src.slice(a,b);
let CO=0; const logs=[];
const M={trifecta_pool_seed:200000}, AUTH={guild:'g'}, API='x';
const getCO=()=>CO, setCO=v=>{CO=v;}, log=m=>logs.push(m);
function make(failKeys, oddsOf){
  return async u=>{ const q=new URLSearchParams(u.split('?')[1]); const k=`${q.get('first')}-${q.get('second')}-${q.get('third')}`;
    if(failKeys.has(k)) return null; const od=oddsOf[k]; return od?{odds:od}:{odds:null}; };
}
(async()=>{
  const pets=[{pet_id:0},{pet_id:1},{pet_id:2}];
  const combo=[{i:0,j:1,k:2},{i:0,j:2,k:1},{i:1,j:0,k:2}];
  // ケース1: 人気組 0-1-2 の取得が2回とも失敗
  let jget=make(new Set(['0-1-2']),{'0-1-2':1.5,'0-2-1':4.0});
  let fetchOdds; eval(fn.replace('async function fetchOdds','fetchOdds = async function'));
  let out=await fetchOdds(1,pets,combo,1_200_000,10000,1200);
  console.log('ケース1 取り損ね', [...out.failed], ' 取れた', [...out.entries()], ' CO', CO);
  console.assert(out.failed.has('0-1-2') && !out.has('0-1-2') && CO===0, 'CO を書いてはいけない');
  // ケース2: 全部取れる（未投票の組は failed に入らない）
  jget=make(new Set(),{'0-1-2':1.5,'0-2-1':4.0}); eval(fn.replace('async function fetchOdds','fetchOdds = async function'));
  out=await fetchOdds(1,pets,combo,1_200_000,10000,1200);
  console.log('ケース2 取り損ね', [...out.failed], ' 取れた', [...out.entries()], ' CO', CO);
  console.assert(out.failed.size===0, 'x');
  jget=make(new Set(),{'0-1-2':4.0,'0-2-1':8.0}); eval(fn.replace('async function fetchOdds','fetchOdds = async function'));
  out=await fetchOdds(1,pets,combo,2_000_000,10000,1200); console.log('ケース3 全部取れて残額あり CO', CO); console.assert(CO===1050000,'CO 確定');
  CO=0; jget=make(new Set(['1-0-2']),{'0-1-2':4.0,'0-2-1':8.0}); eval(fn.replace('async function fetchOdds','fetchOdds = async function'));
  out=await fetchOdds(1,pets,combo,2_000_000,10000,1200); console.log('ケース4 1組取り損ね CO', CO); console.assert(CO===0,'取り損ねがあれば CO を書かない');
  
})();

}
{
// SWAP23（2・3着の順番補正）ブロックをそのまま取り出して、実際の値で動かす

const src=require('fs').readFileSync(require('path').join(__dirname,'bookmarklets','src','autopilot.js'),'utf8');
const a=src.indexOf('  const mk = new Map();'), b=src.indexOf('  const budgetU = Math.min(CFG.TRI_MAX_UNITS, unitsLeft);');
const block=src.slice(a,b);
function run(combo, odMap, failed){
  const key3=c=>`${c.i}-${c.j}-${c.k}`, nameOf=i=>'ABCDE'[i];
  const odds=new Map(Object.entries(odMap)), oddsRaw=new Map(odds); oddsRaw.failed=new Set(failed||[]);
  const lam=1, norm=1, P=690000, U_=10000, CFG={MAX_SANE_ODDS:5000}, sid=1, logs=[];
  const mNum=(k,d)=>d, log=m=>logs.push(m);
  const byKey=new Map(), pOf=new Map(), cands=[];
  for(const c of combo){ const k=key3(c); if(!odds.get(k)||c.p<0.25) continue;
    byKey.set(k,c); pOf.set(k,c.p); const eff1=(P+U_)/(P/odds.get(k)+U_);
    cands.push({key:k,p:c.p,od:odds.get(k),eff1,edge1:c.p*eff1-1,names:[c.i,c.j,c.k].map(nameOf)}); }
  eval(block);
  return {cands:cands.map(c=>({k:c.key,p:+c.p.toFixed(3),pr:c.pRaw==null?null:+c.pRaw.toFixed(3),sw:!!c.swap23})), logs};
}
// R2544: A=おいら B=cry C=あめんぼ D=リウたん。モデルも市場も cry 2着を支持 → 何も変えない
let r=run([{i:0,j:1,k:3,p:0.442},{i:0,j:3,k:1,p:0.108},{i:0,j:1,k:2,p:0.323},{i:0,j:2,k:1,p:0.061}],
          {'0-1-3':2.65,'0-3-1':69,'0-1-2':4.06,'0-2-1':17.25});
console.log('R2544（市場も同意）', JSON.stringify(r.cands));
console.assert(r.cands.every(c=>c.pr===null), '市場が同意なら補正しない');
// 市場が反対: モデルは A>B>C を 0.5、A>C>B を 0.05。市場は A>C>B を 1.4倍で本命視
r=run([{i:0,j:1,k:2,p:0.5},{i:0,j:2,k:1,p:0.05}], {'0-1-2':20,'0-2-1':1.4});
console.log('市場が反対', JSON.stringify(r.cands), r.logs);
const x=r.cands.find(c=>c.k==='0-1-2'), y=r.cands.find(c=>c.k==='0-2-1');
console.assert(Math.abs(x.p-0.33)<1e-3 && y && Math.abs(y.p-0.22)<1e-3, '60:40 に抑えて相方を足す');
// 相方のオッズ取得に失敗 → 市場シェアが分からないので触らない
r=run([{i:0,j:1,k:2,p:0.5},{i:0,j:2,k:1,p:0.05}], {'0-1-2':20}, ['0-2-1']);
console.log('相方を取り損ね', JSON.stringify(r.cands));
console.assert(r.cands.length===1 && r.cands[0].pr===null, '取り損ねなら補正しない');


}
{
// analyseRace の予想の部分を実際に動かす: 区間シミュレータで買い、従来モデルを rw/rc に残すこと
const fs = require('fs'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, 'bookmarklets', 'src', 'autopilot.js'), 'utf8');
const fn = src.slice(src.indexOf('async function analyseRace('), src.indexOf('// ---- 3連単 ----'));
const fxPath = path.join(__dirname, '_parity_sim.json');
if (fs.existsSync(fxPath)) {
  const M = JSON.parse(fs.readFileSync(path.join(__dirname, 'model.json'), 'utf8'));
  const OasisModel = require('./bookmarklets/src/model.js');
  const F = JSON.parse(fs.readFileSync(fxPath, 'utf8'));
  const r = F.find(x => x.horses.length >= 8) || F[0];
  const code = {}; for (const [c, n] of Object.entries(M.code_map)) code[n] = c;
  const pets = r.horses.map((h, i) => ({ pet_id: i, name: h.name, display_name: h.name, adult_key: h.species,
    speed: h.speed, power: h.power, stamina: h.stamina, odds: 10,
    passive_skill: code[h.passives[0]] || 'none', passive_skill_2: code[h.passives[1]] || 'none',
    equipment: h.equipment, charm: h.charm }));
  const CFG = { N_SIM: 2000, USE_SIM: true, RACE_BUDGET: 300000, DAILY_BUDGET: 1800000, WIN_ON: true, WIN_PROBE: false };
  const ST = { spent: 0 }, log = () => {}, esc = s => s, fx = (v, d) => (+v).toFixed(d);
  const mNum = (k, d) => (M[k] != null ? +M[k] : d), bankroll = () => 2000000;
  let gotCombo = null, gotWin = null, LAST_NOBET = null;
  const analyseTrifecta = async (sid, p, combo) => { gotCombo = combo; return { picks: [], cost: 0, cand: [] }; };
  const analyseWin = (sid, p, winP) => { gotWin = winP; return { picks: [], cost: 0 }; };
  let analyseRace; eval(fn.replace('async function analyseRace', 'analyseRace = async function'));
  (async () => {
    await analyseRace(r.sid, { pets: pets, distance: r.dist, surface: r.track }, true);
    const c = LAST_NOBET || {};
    const sim = OasisModel.simRace(r.horses, r.dist, r.track, M, M.sim.n_sim, r.sid).win;
    console.assert(c.model === 'sim', 'analyseRace は区間シミュレータで予想する');
    console.assert(gotWin && gotWin.every((p, i) => Math.abs(p - sim[i]) < 1e-12), '単勝の確率はシミュレータのもの');
    console.assert(gotCombo && gotCombo.length && Math.abs(gotCombo.reduce((a, x) => a + x.p, 0) - 1) < 1e-9, '3連単の確率はシミュレータのもの');
    console.assert(c.rw && c.rw.length === Math.min(5, pets.length) && c.rc && c.rc.length >= 1 && c.rc.length <= 10, '従来モデルの予想を rw/rc に残す');
    console.log('analyseRace: model', c.model, '/ 従来の本命', c.rw && c.rw[0].n);
  })().catch(e => { failed++; console.error('❌ analyseRace が落ちた: ' + (e && e.stack || e)); });
}
}
{
// lateOdds（締切直前のオッズ・記録用）: 取れたら記録、API が固まったら4秒で null、窓の外は取らない
const src=require('fs').readFileSync(require('path').join(__dirname,'bookmarklets','src','autopilot.js'),'utf8');
const a=src.indexOf('async function lateOdds('), b=src.indexOf('// ---- 購入まとめ（買わなかったレースでも出す）');
const fn=src.slice(a,b);
(async()=>{
  const CFG={LEAD_SEC:13, LATE_SEC:5}, AUTH={guild:'g',user:'u'}, API='x', logs=[], log=m=>logs.push(m);
  let closeAt; const nextRaceTime=()=>closeAt;
  const sleep=ms=>new Promise(r=>setTimeout(r, Math.min(ms, 50)));   // テストでは待ち時間を縮める
  const pl={sid:1, pets:[{pet_id:10,name:'A'},{pet_id:11,name:'B'},{pet_id:12,name:'C'}],
            cand:[{n:['A','B','C'],od:2.5,u:3},{n:['A','C','B'],od:null,u:0}]};
  let jget=async u=>u.includes('/race/by-id/')?{pets:[{name:'A',odds:1.3},{name:'B',odds:4.1},{name:'C',odds:9}]}:{odds:2.2};
  let lateOdds; eval(fn.replace('async function lateOdds','lateOdds = async function'));
  closeAt=Date.now()+8000;
  let r=await lateOdds(pl);
  console.assert(r && r.w.length===3 && r.w[0].od===1.3 && r.t.length===1 && r.t[0].od===2.2, 'lateOdds: 単勝3頭・3連単1組（オッズの無い組は取らない）');
  jget=()=>new Promise(()=>{}); eval(fn.replace('async function lateOdds','lateOdds = async function'));
  const sleep2=sleep; closeAt=Date.now()+5000;
  r=await lateOdds(pl); console.assert(r===null, 'lateOdds: API が固まったら null（まとめは必ず出す）');
  closeAt=Date.now()+600000; r=await lateOdds(pl); console.assert(r===null, 'lateOdds: 窓の外（下見の手動購入）は取らない');
})();
}
