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
