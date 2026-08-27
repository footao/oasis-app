# -*- coding: utf-8 -*-
"""make_replay.py — races.jsonl の timeline からレース再生用の単体HTMLを作る。

    python make_replay.py            # races.jsonl → replay.html
    python make_replay.py --out X.html --races races.jsonl

timeline は 100m ごとに `elapsed`（スタートからの累積秒）を持っているので、
**任意の時刻の位置が線形補間で正確に出る**（区間内は等速なので補間＝実挙動）。
推定ではなく実データの再生になる。

出力は1ファイル完結。データを埋め込むので、開くだけでオフラインでも動く。
再採取したら **これを回し直すこと**（HTMLの中のデータは焼き込みなので古くなる）。
"""
import argparse
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oasis_core as oc  # noqa: E402

# 距離 → コース長(m)。timeline の最終 meter と一致することを確認済み。
COURSE_M = {'短距離': 1000, 'マイル': 1500, '中距離': 2000, '長距離': 2500}


def code_labels():
    """発動ログのコード → 日本語名。装備由来は gear_/charm_ を外して引く。"""
    lab = dict(oc.PASSIVE_CODE_MAP)
    out = {}
    for k, v in lab.items():
        out[k] = v
    for pre in ('gear_', 'charm_'):
        for k, v in lab.items():
            out[pre + k] = v + ('（装備）' if pre == 'gear_' else '（お守り）')
    return out


def build(races_path):
    races = []
    for line in io.open(races_path, encoding='utf-8'):
        try:
            d = json.loads(line)
        except Exception:
            continue
        hs = d.get('horses') or []
        tl_ok = [h for h in hs if isinstance(h.get('timeline'), list) and h['timeline']]
        # 全頭そろっているレースだけ。1頭でも欠けると順位が嘘になる。
        if not hs or len(tl_ok) != len(hs):
            continue
        if oc.is_excluded_race(d.get('schedule_id'), d.get('race_date'), d.get('race_time')):
            continue
        course = COURSE_M.get(d.get('distance'))
        if not course:
            continue
        horses = []
        for h in sorted(hs, key=lambda x: x.get('rank') or 99):
            tl = h['timeline']
            horses.append({
                'n': h.get('name') or '?',
                'r': h.get('rank'),
                't': h.get('finish_time'),
                'sc': round(float(h.get('score') or 0), 1),
                'sp': h.get('speed'), 'pw': h.get('power'), 'st': h.get('stamina'),
                'ps': [p for p in (h.get('passive_skills') or []) if p],
                'eq': ((h.get('equipment') or {}).get('name') or ''),
                'ch': ((h.get('charm') or {}).get('name') or ''),
                # [到達m, 累積秒, 残スタミナ, 区間の走行力]
                'tl': [[t.get('meter'), round(float(t.get('elapsed') or 0), 3),
                        round(float(t.get('stamina') or 0), 2),
                        round(float(t.get('rating') or 0), 1)] for t in tl],
                'ap': [[a for a in (t.get('activated_passives') or [])] for t in tl],
            })
        races.append({
            'sid': d.get('schedule_id'), 'date': d.get('race_date'),
            'time': d.get('race_time') or '', 'dist': d.get('distance'),
            'surf': d.get('surface') or '', 'course': course, 'h': horses,
        })
    races.sort(key=lambda r: (r['date'], r['time'], r['sid'] or 0))
    return races


def verify(races):
    """再生の前提が崩れていないかを毎回確かめる。崩れたまま出すと嘘の再生になる。

    ・最終区間の meter がコース長ぴったり（途中で切れていない）
    ・最終区間の elapsed が finish_time と一致（時間軸が正しい）
    ・meter と elapsed がどちらも単調増加（補間が逆走しない）
    ・着順とタイム順が一致（順位表の「確定」が嘘にならない）
    """
    bad = []
    for r in races:
        for h in r['h']:
            tl = h['tl']
            if tl[-1][0] != r['course']:
                bad.append(f"sid{r['sid']} {h['n']}: 最終 {tl[-1][0]}m ≠ コース {r['course']}m")
            if abs(tl[-1][1] - float(h['t'] or 0)) > 0.01:
                bad.append(f"sid{r['sid']} {h['n']}: 最終 {tl[-1][1]}s ≠ 着タイム {h['t']}s")
            for i in range(1, len(tl)):
                if tl[i][0] <= tl[i - 1][0] or tl[i][1] <= tl[i - 1][1]:
                    bad.append(f"sid{r['sid']} {h['n']}: 区間{i} が単調でない")
                    break
        by_rank = [h['n'] for h in sorted(r['h'], key=lambda x: x['r'] or 99)]
        by_time = [h['n'] for h in sorted(r['h'], key=lambda x: x['t'])]
        if by_rank != by_time:
            bad.append(f"sid{r['sid']}: 着順とタイム順が食い違う")
    return bad


PAGE = r'''<!DOCTYPE html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>おあしすっち レースリプレイ</title>
<style>
:root{--bg:#12121f;--fg:#eee;--acc:#e2b96f;--pane:#0b0b14;--line:#333}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,-apple-system,sans-serif}
header{padding:.7rem 1rem;border-bottom:1px solid var(--line);display:flex;gap:.6rem;
  align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:1rem;margin:0;color:var(--acc);white-space:nowrap}
select,button{background:var(--pane);color:var(--fg);border:1px solid #444;border-radius:6px;
  padding:.4rem .6rem;font:inherit}
button{cursor:pointer}
button.p{background:var(--acc);color:#12121f;font-weight:700;border-color:var(--acc)}
#wrap{padding:.8rem 1rem 3rem;max-width:1100px;margin:0 auto}
#meta{color:#9a9aa8;font-size:.82rem;margin:.2rem 0 .6rem}
canvas{width:100%;height:auto;background:var(--pane);border:1px solid var(--line);border-radius:8px;
  display:block;touch-action:none}
#bar{display:flex;gap:.6rem;align-items:center;margin:.6rem 0}
#seek{flex:1;accent-color:var(--acc)}
#clock{font-variant-numeric:tabular-nums;color:var(--acc);min-width:5.5rem;text-align:right}
table{border-collapse:collapse;width:100%;margin-top:.8rem;font-size:.82rem}
th,td{border:1px solid var(--line);padding:.3rem .45rem;text-align:right;white-space:nowrap}
th{background:#181828;position:sticky;top:0}
td.l,th.l{text-align:left}
tr.done td{color:#7d7d8c}
.fx{color:#81c784}
#log{margin-top:.6rem;height:120px;overflow-y:auto;background:var(--pane);border:1px solid var(--line);
  border-radius:8px;padding:.4rem .6rem;font-size:.78rem;line-height:1.5}
.hint{color:#666;font-size:.78rem;margin-top:.5rem}
</style>
<header>
  <h1>🏇 レースリプレイ</h1>
  <select id=day></select>
  <select id=race></select>
  <button id=play class=p>▶ 再生</button>
  <select id=spd>
    <option value="0.5">0.5x</option>
    <option value="1" selected>1x</option>
    <option value="2">2x</option>
    <option value="4">4x</option>
  </select>
</header>
<div id=wrap>
  <div id=meta></div>
  <canvas id=cv></canvas>
  <div id=bar><input type=range id=seek min=0 max=1000 value=0 step=1><span id=clock>0.00s</span></div>
  <table id=tb></table>
  <div id=log></div>
  <div class=hint>← → で 0.5秒ずつ、Space で再生／停止。バーをドラッグすると任意の時点へ。</div>
</div>
<script>
const R = __DATA__, LAB = __LABELS__;
const $ = i => document.getElementById(i);
const cv = $('cv'), cx = cv.getContext('2d');
// 12色。頭数が増えても回り込むだけで破綻しない。
const COL = ['#e2b96f','#7fd1a0','#8ab4f8','#ef9a9a','#ce93d8','#ffcc80',
             '#80deea','#c5e1a5','#f48fb1','#b0bec5','#ffab91','#9fa8da'];

let race = null, T = 0, playing = false, last = 0, seen = new Set();

// ---- 時刻 t での位置（区間内は等速なので線形補間＝実挙動）----
function posAt(h, t) {
  const tl = h.tl, n = tl.length;
  if (t <= tl[0][1]) return { m: tl[0][0], i: 0, done: false };
  if (t >= tl[n - 1][1]) return { m: tl[n - 1][0], i: n - 1, done: true };
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (tl[mid][1] <= t) lo = mid; else hi = mid; }
  const a = tl[lo], b = tl[hi], w = (t - a[1]) / Math.max(b[1] - a[1], 1e-9);
  return { m: a[0] + (b[0] - a[0]) * w, i: hi, done: false };
}
const totalT = () => Math.max(...race.h.map(h => h.tl[h.tl.length - 1][1]));

// ---- 描画 ----
function draw() {
  const n = race.h.length;
  const W = cv.clientWidth || 900, rowH = 30, padL = 96, padR = 54, padT = 26;
  const H = padT + n * rowH + 10;
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.height = H + 'px';
  }
  cx.setTransform(dpr, 0, 0, dpr, 0, 0);
  cx.clearRect(0, 0, W, H);
  const x0 = padL, x1 = W - padR, span = x1 - x0;

  // 距離の目盛り
  cx.font = '11px system-ui'; cx.textBaseline = 'middle';
  for (let m = 0; m <= race.course; m += (race.course <= 1000 ? 200 : 500)) {
    const x = x0 + span * m / race.course;
    cx.strokeStyle = '#242436'; cx.beginPath();
    cx.moveTo(x, padT - 8); cx.lineTo(x, H - 6); cx.stroke();
    cx.fillStyle = '#5a5a6c'; cx.textAlign = 'center';
    cx.fillText(m + 'm', x, padT - 15);
  }
  cx.strokeStyle = '#8a6d3b'; cx.beginPath();
  cx.moveTo(x1, padT - 8); cx.lineTo(x1, H - 6); cx.stroke();

  // 現在の順位で並べ替えて描く（先頭が上）
  const st = race.h.map((h, k) => ({ h, k, p: posAt(h, T) }));
  st.sort((a, b) => b.p.m - a.p.m);
  st.forEach((s, row) => {
    const y = padT + row * rowH + rowH / 2;
    const h = s.h, col = COL[s.k % COL.length];
    const stam = h.tl[s.p.i][2], rate = h.tl[s.p.i][3];
    // 名前
    cx.textAlign = 'right'; cx.fillStyle = s.p.done ? '#7d7d8c' : '#eee';
    cx.font = 'bold 12px system-ui';
    cx.fillText(String(row + 1).padStart(2) + '. ' + h.n, padL - 34, y);
    // スタミナバー（マイナスは赤）
    const sw = 26, sh = 7, sx = padL - 30, sy = y - sh / 2;
    const frac = Math.max(0, Math.min(1, stam / Math.max(h.st, 1)));
    cx.fillStyle = '#242436'; cx.fillRect(sx, sy, sw, sh);
    cx.fillStyle = stam <= 0 ? '#ef5350' : (frac < .25 ? '#ffb74d' : '#7fd1a0');
    cx.fillRect(sx, sy, Math.max(stam <= 0 ? sw : sw * frac, 2), sh);
    // 走ってきた軌跡
    const x = x0 + span * s.p.m / race.course;
    cx.strokeStyle = col + '55'; cx.lineWidth = 2; cx.beginPath();
    cx.moveTo(x0, y); cx.lineTo(x, y); cx.stroke();
    // 馬
    cx.fillStyle = col; cx.beginPath(); cx.arc(x, y, s.p.done ? 5 : 7, 0, 7); cx.fill();
    if (!s.p.done) {   // 区間の走行力
      cx.textAlign = 'left'; cx.font = '10px system-ui'; cx.fillStyle = '#6f6f82';
      cx.fillText(Math.round(rate), Math.min(x + 11, x1 - 2), y);
    } else {
      cx.textAlign = 'left'; cx.font = 'bold 10px system-ui'; cx.fillStyle = col;
      cx.fillText(h.t.toFixed(2) + 's', x1 + 4, y);
    }
  });
}

// ---- 順位表 ----
function table() {
  const st = race.h.map((h, k) => ({ h, k, p: posAt(h, T) }));
  st.sort((a, b) => b.p.m - a.p.m);
  const rows = st.map((s, i) => {
    const h = s.h, tl = h.tl[s.p.i];
    return `<tr class="${s.p.done ? 'done' : ''}">`
      + `<td>${i + 1}</td><td class=l style="color:${COL[s.k % COL.length]}">${h.n}</td>`
      + `<td>${Math.round(s.p.m)}m</td><td>${tl[2].toFixed(1)}</td><td>${Math.round(tl[3])}</td>`
      + `<td>${h.sp}/${h.pw}/${h.st}</td>`
      + `<td class=l>${h.ps.map(c => LAB[c] || c).join(' / ')}</td>`
      + `<td class=l>${[h.eq, h.ch].filter(Boolean).join(' / ')}</td>`
      + `<td>${s.p.done ? h.r + '着 ' + h.t.toFixed(2) + 's' : '—'}</td></tr>`;
  }).join('');
  $('tb').innerHTML = '<tr><th>順</th><th class=l>馬名</th><th>位置</th><th>残ST</th>'
    + '<th>走行力</th><th>SP/PW/ST</th><th class=l>パッシブ</th><th class=l>装備/お守り</th>'
    + '<th>確定</th></tr>' + rows;
}

// ---- パッシブ発動ログ（通り過ぎた区間ぶんだけ出す）----
function fxlog() {
  const out = [];
  race.h.forEach(h => {
    const p = posAt(h, T);
    for (let i = 0; i <= p.i; i++) {
      for (const a of (h.ap[i] || [])) {
        const key = h.n + '#' + i + '#' + a;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push([h.tl[i][1],
          `<div><span style="color:#6f6f82">${h.tl[i][1].toFixed(1)}s ${h.tl[i][0]}m</span> `
          + `<b>${h.n}</b> <span class=fx>✨ ${LAB[a] || a}</span></div>`]);
      }
    }
  });
  if (!out.length) return;
  // 馬ごとに集めているので、そのまま出すと1頭ぶんが固まって読めない。時刻順に直す。
  out.sort((a, b) => b[0] - a[0]);
  $('log').insertAdjacentHTML('afterbegin', out.map(x => x[1]).join(''));
}
function relog() { seen = new Set(); $('log').innerHTML = ''; fxlog(); }

function frame(ts) {
  if (!playing) return;
  const dt = (ts - last) / 1000; last = ts;
  T = Math.min(T + dt * parseFloat($('spd').value), totalT());
  sync(false);
  if (T >= totalT()) { playing = false; $('play').textContent = '▶ 再生'; }
  else requestAnimationFrame(frame);
}
function sync(reset) {
  $('seek').value = Math.round(T / totalT() * 1000);
  $('clock').textContent = T.toFixed(2) + 's';
  if (reset) relog(); else fxlog();
  draw(); table();
}

// ---- 選択 ----
function fillDays() {
  const days = [...new Set(R.map(r => r.date))].sort().reverse();
  $('day').innerHTML = days.map(d => `<option>${d}</option>`).join('');
  fillRaces();
}
function fillRaces() {
  const d = $('day').value;
  const rs = R.map((r, i) => ({ r, i })).filter(x => x.r.date === d);
  $('race').innerHTML = rs.map(x =>
    `<option value="${x.i}">${x.r.time || '??:??'}　${x.r.dist}・${x.r.surf}　${x.r.h.length}頭</option>`
  ).join('');
  load();
}
function load() {
  race = R[+$('race').value];
  $('meta').textContent = `${race.date} ${race.time}　${race.dist}・${race.surf}　`
    + `${race.course}m　${race.h.length}頭　勝ち時計 `
    + `${Math.min(...race.h.map(h => h.t)).toFixed(2)}s　(sid ${race.sid})`;
  T = 0; playing = false; $('play').textContent = '▶ 再生';
  sync(true);
}
$('day').onchange = fillRaces;
$('race').onchange = load;
$('play').onclick = () => {
  if (T >= totalT()) T = 0;
  playing = !playing; $('play').textContent = playing ? '⏸ 停止' : '▶ 再生';
  if (playing) { last = performance.now(); requestAnimationFrame(frame); }
};
$('seek').oninput = e => {
  playing = false; $('play').textContent = '▶ 再生';
  T = totalT() * e.target.value / 1000;
  sync(true);
};
addEventListener('keydown', e => {
  if (e.key === ' ') { e.preventDefault(); $('play').click(); }
  else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    e.preventDefault(); playing = false; $('play').textContent = '▶ 再生';
    T = Math.max(0, Math.min(totalT(), T + (e.key === 'ArrowRight' ? .5 : -.5)));
    sync(true);
  }
});
addEventListener('resize', () => { if (race) draw(); });
fillDays();
</script>
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--races', default=os.path.join(HERE, 'races.jsonl'))
    ap.add_argument('--out', default=os.path.join(HERE, 'replay.html'))
    a = ap.parse_args()

    races = build(a.races)
    if not races:
        print('[NG] timeline のそろったレースがありません。'
              'harvest_results.py を回してから再実行してください。')
        return 1
    bad = verify(races)
    if bad:
        print(f'[NG] データの検算に失敗しました（{len(bad)}件）。再生が嘘になるので中止します。')
        for b in bad[:10]:
            print('   ', b)
        return 1
    print(f'   検算OK  {sum(len(r["h"]) for r in races)}頭 '
          '（最終m・着タイム・単調性・着順とタイム順）')
    data = json.dumps(races, ensure_ascii=False, separators=(',', ':'))
    page = (PAGE.replace('__DATA__', data)
                .replace('__LABELS__', json.dumps(code_labels(), ensure_ascii=False,
                                                  separators=(',', ':'))))
    io.open(a.out, 'w', encoding='utf-8').write(page)
    days = sorted({r['date'] for r in races})
    print(f'→ {os.path.basename(a.out)}  {len(page.encode())/1e6:.2f} MB')
    print(f'   {len(races)}レース / {days[0]} 〜 {days[-1]} / {len(days)}日ぶん')
    print('   ⚠ データは焼き込みです。再採取したらこれを回し直してください。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
