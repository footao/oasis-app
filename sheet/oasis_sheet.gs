/**
 * おあしすっち 予想シート（Google スプレッドシート用・見るだけ）
 *
 * 計算は bot と同じ model.js / model.json（GitHub の main）をそのまま動かす。
 * 使い方: B1 にレースサイトの URL（トークン入りのままでOK・トークンは消して保存し直す）、
 *        B2 にレース番号（空なら今の時刻の最新レース）→ メニュー「おあしすっち → 計算する」。
 * 購入はしない。API は読み取り（レース情報・3連単のプールとオッズ）だけ。
 */
var RAW = 'https://raw.githubusercontent.com/footao/oasis-app/main';
var API = 'https://api.oasis.red';
var SHEET = 'おあしすっち';
var TRI_SHOW_MIN = 0.01, TRI_ROWS = 20;
// レースは JST 9/12/15/18/21/23時。番号は1日12ずつ進む（10/27 9時 = R2864）。ダッシュボードと同じ
var SLOTS = [9, 12, 15, 18, 21, 23], PER_DAY = 12, ANCHOR = { sid: 2864, day: Date.UTC(2026, 9, 27) };

function onOpen() {
  SpreadsheetApp.getUi().createMenu('おあしすっち').addItem('計算する', 'calc').addToUi();
  sheet_();
}

function sheet_() {
  var ss = SpreadsheetApp.getActive(), sh = ss.getSheetByName(SHEET);
  if (!sh) {
    sh = ss.insertSheet(SHEET);
    sh.getRange('A1:A3').setValues([['レースサイトのURL'], ['レース番号（空なら最新）'], ['状態']]);
    sh.getRange('A1:A3').setFontWeight('bold');
    sh.setColumnWidth(1, 170); sh.setColumnWidth(2, 260); sh.setColumnWidth(9, 320);
  }
  return sh;
}

// URL から guild・user・race だけ取り出す（token は使わない）
function parseUrl_(s) {
  var q = {}, m = String(s || '').match(/\?([^#]*)/);
  if (!m) return null;
  m[1].split('&').forEach(function (kv) { var p = kv.split('='); q[decodeURIComponent(p[0])] = decodeURIComponent(p[1] || ''); });
  return (q.guild && q.user) ? { guild: q.guild, user: q.user, sid: parseInt(q.race || q.schedule_id, 10) } : null;
}

function estimateSid_(now, last) {
  var j = new Date(now + 9 * 3600e3), h = j.getUTCHours();
  var day = Date.UTC(j.getUTCFullYear(), j.getUTCMonth(), j.getUTCDate());
  var slot = last ? SLOTS.filter(function (s) { return s <= h; }).length - 1 : SLOTS.findIndex(function (s) { return h < s; });
  if (!last && slot < 0) { slot = 0; day += 864e5; }
  if (last && slot < 0) { slot = SLOTS.length - 1; day -= 864e5; }
  return ANCHOR.sid + PER_DAY * Math.round((day - ANCHOR.day) / 864e5) + slot;
}

function jget_(url) {
  var r = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (r.getResponseCode() !== 200) return null;
  try { return JSON.parse(r.getContentText()); } catch (e) { return null; }
}
function raceInfo_(a, sid) { return jget_(API + '/api/race/by-id/' + encodeURIComponent(a.guild) + '/' + sid + '?user=' + encodeURIComponent(a.user)); }

// 受付中のレース（次のレースから10先まで）→ 無ければ直前の組まれているレース。ダッシュボードの findOpen と同じ
function findOpen_(a, now) {
  var next = estimateSid_(now, false), prev = estimateSid_(now, true), d, r;
  for (d = 0; d <= 10; d++) { r = raceInfo_(a, next + d); if (r && r.pets && r.pets.length && r.phase === 'betting') return next + d; }
  for (d = 0; d <= PER_DAY; d++) { r = raceInfo_(a, prev - d); if (r && r.pets && r.pets.length) return prev - d; }
  return prev;
}

function loadModel_() {
  var t = Date.now();
  var src = UrlFetchApp.fetch(RAW + '/bookmarklets/src/model.js?t=' + t).getContentText();
  var M = JSON.parse(UrlFetchApp.fetch(RAW + '/model.json?t=' + t).getContentText());
  var OasisModel = new Function(src + '\nreturn OasisModel;')();
  if (!OasisModel.predictSim || !M.sim) throw new Error('model.js / model.json が古いか読めません');
  return { O: OasisModel, M: M };
}

function calc() {
  var sh = sheet_();
  var status = function (s) { sh.getRange('B3').setValue(s); SpreadsheetApp.flush(); };
  var a = parseUrl_(sh.getRange('B1').getValue());
  if (!a) { status('B1 にレースサイトの URL（guild と user が入ったもの）を貼ってください'); return; }
  sh.getRange('B1').setValue('?guild=' + a.guild + '&user=' + a.user);      // トークンをシートに残さない
  status('計算中…');
  var md = loadModel_(), O = md.O, M = md.M, D = M.defaults || {};
  var sid = parseInt(sh.getRange('B2').getValue(), 10);
  if (!isFinite(sid)) sid = findOpen_(a, Date.now());
  var info = raceInfo_(a, sid);
  if (!info || !info.pets || !info.pets.length) { status('R' + sid + ' のデータがありません（API に届かないか、まだ出走馬が決まっていません）'); return; }
  var pets = info.pets, dist = info.distance || '', track = info.surface || '';
  if (!M.sim.w[dist]) { status('距離「' + dist + '」はモデルにありません'); return; }
  var skipped = [], horses = O.racePets(pets, M, dist, track, skipped);
  var pr = O.predictSim(pets, horses, dist, track, M, sid);                 // bot と同じ（乱数の種もレース番号）
  var open = info.phase === 'betting';
  var unbet = M.unbet_odds || 1.5, U = M.stake_unit || 10000, seed = M.trifecta_pool_seed || 200000;
  var name = function (i) { return pets[i].display_name || pets[i].name; };

  sh.getRange('A5:Z200').clearContent().setBackground(null);
  sh.getRange('A5').setValue('R' + sid + '  ' + (info.race_date || '') + ' ' + (info.race_time || '') + '  ' + dist + '・' + track
    + '・' + pets.length + '頭  ' + ({ entry: 'エントリー中', betting: '受付中', closed: '締切' }[info.phase] || info.phase || ''));

  // 単勝
  var ods = pets.map(function (h) { return +h.odds; });
  var inv = ods.filter(function (o) { return o > 0 && o !== unbet; }).reduce(function (s, o) { return s + 1 / o; }, 0);
  var idx = pets.map(function (_, i) { return i; }).sort(function (x, y) { return pr.win[y] - pr.win[x]; });
  var rows = [['馬', '勝率', open ? '単勝オッズ' : '最終オッズ', '期待値', '市場の勝率', '']], bg = [];
  idx.forEach(function (i) {
    var p = pr.win[i], od = ods[i], nb = od === unbet, ev = nb ? '' : p * od;
    var cand = open && !nb && p >= (M.win_min_prob || 0.4) && p * od - 1 >= (D.win_edge_min || 0.15);
    rows.push([name(i), p, nb ? '未投票' : od, ev, nb || !inv ? '' : (1 / od) / inv, cand ? 'bot の候補' : '']);
    bg.push(cand ? '#e3f5e8' : null);
  });
  sh.getRange(7, 1, rows.length, 6).setValues(rows);
  sh.getRange(8, 2, rows.length - 1, 1).setNumberFormat('0.0%'); sh.getRange(8, 5, rows.length - 1, 1).setNumberFormat('0.0%');
  sh.getRange(8, 4, rows.length - 1, 1).setNumberFormat('0.00');
  bg.forEach(function (c, k) { if (c) sh.getRange(8 + k, 1, 1, 6).setBackground(c); });

  // 3連単（8頭以上）
  if (pets.length < (M.min_field_trifecta || 8)) { status('最終更新 ' + new Date().toLocaleTimeString('ja-JP') + '・8頭未満なので3連単はありません'); return; }
  var shown = pr.combo.filter(function (c) { return c.p >= TRI_SHOW_MIN; }).slice(0, TRI_ROWS);
  var P = 0, od3 = {};
  if (open) {
    var pj = jget_(API + '/api/trifecta/pool?guild=' + encodeURIComponent(a.guild) + '&schedule_id=' + sid);
    P = (pj && pj.pool) || seed;
    var reqs = shown.map(function (c) {
      return { url: API + '/api/trifecta/odds?guild=' + encodeURIComponent(a.guild) + '&schedule_id=' + sid + '&first=' + pets[c.i].pet_id
        + '&second=' + pets[c.j].pet_id + '&third=' + pets[c.k].pet_id, muteHttpExceptions: true };
    });
    UrlFetchApp.fetchAll(reqs).forEach(function (r, n) {
      var d = null; try { d = r.getResponseCode() === 200 ? JSON.parse(r.getContentText()) : null; } catch (e) {}
      od3[n] = !d ? 'fail' : (typeof d.odds === 'number' ? d.odds : null);
    });
  }
  var t = [['#', '組（1着 → 2着 → 3着）', '的中率', 'オッズ', '期待値', '']], bg3 = [];
  shown.forEach(function (c, n) {
    var od = od3[n], eff = null, tag = '', col = null;
    if (open && typeof od === 'number') { eff = (P + U) / (P / od + U); if (c.p >= (D.min_prob || 0.1) && c.p * eff - 1 >= (D.edge_min || 0.01)) { tag = 'bot の候補'; col = '#e3f5e8'; } }
    else if (open && od === null) { eff = (P + U) / U; if (c.p >= (D.unformed_p_min || 0.1) && c.p * eff - 1 >= (D.unformed_edge_min || 0.3)) { tag = '未成立の候補'; col = '#fdf3d8'; } }
    if (tag && c.p >= 0.9) tag = '★ ' + tag;
    t.push([n + 1, name(c.i) + ' → ' + name(c.j) + ' → ' + name(c.k), c.p,
            !open ? '—' : (od === 'fail' ? '取得失敗' : (od === null ? '未投票' : od)), eff === null ? '' : c.p * eff, tag]);
    bg3.push(col);
  });
  sh.getRange(7, 8, t.length, 6).setValues(t);
  if (t.length > 1) { sh.getRange(8, 10, t.length - 1, 1).setNumberFormat('0.0%'); sh.getRange(8, 12, t.length - 1, 1).setNumberFormat('0.00'); }
  bg3.forEach(function (c, k) { if (c) sh.getRange(8 + k, 8, 1, 6).setBackground(c); });
  sh.getRange('H6').setValue(open ? '3連単プール ' + P.toLocaleString() + ' rrc（的中率1%未満は省略）'
                                  : '受付中ではないので3連単のオッズは出しません（的中率だけ）');
  status('最終更新 ' + new Date().toLocaleTimeString('ja-JP') + (skipped.length ? '・反映しなかった効果: ' + skipped.join(' / ') : ''));
}
