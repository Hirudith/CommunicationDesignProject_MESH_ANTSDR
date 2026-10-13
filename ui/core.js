"use strict";
(function(){

// ------------------------------------------------------------------ helpers
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
const set = (id, text) => { const e = $(id); if(e) e.textContent = text; };
const cssv = k => (getComputedStyle(document.documentElement)
  .getPropertyValue(k).trim() || '120,120,120');

function bytes(n){
  n = Number(n)||0;
  if(n < 1024) return n + ' B';
  if(n < 1048576) return (n/1024).toFixed(n < 10240 ? 1 : 0) + ' kB';
  return (n/1048576).toFixed(2) + ' MB';
}
function rate(bps){
  bps = Number(bps)||0;
  if(bps < 1000) return bps.toFixed(0) + ' b/s';
  if(bps < 1e6) return (bps/1000).toFixed(bps < 10000 ? 1 : 0) + ' kb/s';
  return (bps/1e6).toFixed(2) + ' Mb/s';
}
function clock(ts){
  const d = new Date((Number(ts)||Date.now()/1000)*1000);
  return d.toTimeString().slice(0,5);
}
function ext(name){
  const m = /\.([A-Za-z0-9]{1,5})$/.exec(name||'');
  return m ? m[1].toUpperCase().slice(0,4) : 'FILE';
}
function initials(nick){
  return String(nick||'?').trim().split(/\s+/).slice(0,2)
    .map(w => w[0] ? w[0].toUpperCase() : '').join('') || '?';
}
const isImage = f => /^image\//.test((f && f.mime) || '');

// ------------------------------------------------------------------- state
const S = {
  nick:'me', peerNick:'peer', myAddr:'-', peerAddr:'-', peerUp:false,
  demo:false, connected:false, rows:new Map(), msgs:new Map(), stats:{},
  tp:[], snr:[], stick:true,
  mesh:true, group:'all', conv:'all', peers:[], unread:{}, fp:null,
  peersHtml:null, approving:{}, view:null, gainBusy:0
};
const peerById = id => S.peers.find(p => p.id === id);

const stream = $('stream'), col = $('col'), logBox = $('log');

// --------------------------------------------------------------- transcript
function atBottom(){
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60;
}
function scrollDown(force){
  if(force || S.stick){
    stream.scrollTop = stream.scrollHeight;
    if($('jump')) $('jump').classList.remove('show');
  } else if($('jump')) $('jump').classList.add('show');
}
stream.addEventListener('scroll', () => {
  S.stick = atBottom();
  if(S.stick && $('jump')) $('jump').classList.remove('show');
});
if($('jump')) $('jump').onclick = () => { S.stick = true; scrollDown(true); };

const STATE = {
  queued:   {icon:'&#9675;',          cls:'pending', title:'queued for transmission'},
  sent:     {icon:'&#10003;',         cls:'pending', title:'transmitted, waiting for the acknowledgement'},
  sending:  {icon:'&#8942;',          cls:'pending', title:'transmitting'},
  delivered:{icon:'&#10003;&#10003;', cls:'ok',      title:'acknowledged by the receiver'},
  partial:  {icon:'&#10003;&#9888;',  cls:'bad',     title:'acknowledged by some receivers, not all'},
  waiting:  {icon:'&#8987;',          cls:'pending', title:'waiting for the node to come into range'},
  received: {icon:'&#8595;',          cls:'ok',      title:'received'},
  receiving:{icon:'&#8942;',          cls:'pending', title:'receiving'},
  failed:   {icon:'&#9888;',          cls:'bad',     title:'no acknowledgement after every retry'},
  incomplete:{icon:'&#9888;',         cls:'bad',     title:'the sender stopped before the file was complete'},
  corrupt:  {icon:'&#9888;',          cls:'bad',     title:'reassembled but the CRC did not match'}
};

// The static half of a bubble. Rebuilt only when the author, the text or the
// attachment itself changes - rewriting it on every fragment would restart the
// image download several times a second.
function headHTML(m){
  const out = m.dir === 'out';
  let h = '';
  if(m.kind === 'system') return '<div class="text">' + esc(m.text) + '</div>';
  h += '<div class="who-line"><span class="n">' +
       esc(out ? (m.nick || S.nick) : (m.nick || 'peer')) +
       '</span><span class="a">addr ' + esc(m.addr) + '</span>' +
       (m.conv === S.group && S.mesh ? '<span class="a grp">to everyone</span>' : '') +
       '</div>';
  if(m.kind === 'text'){
    h += '<div class="text">' + esc(m.text) + '</div>';
    return h;
  }
  const f = m.file || {};
  if(isImage(f) && f.url){
    h += '<figure class="shot" data-full="' + esc(f.url) + '">' +
           '<div class="shot-img"><img src="' + esc(f.url) + '" alt="' +
             esc(f.name) + '" loading="lazy"></div>' +
           '<figcaption><span class="fn">' + esc(f.name) + '</span>' +
             '<span class="fs">' + bytes(f.size) + '</span>' +
             '<a class="dl" href="' + esc(f.url) + '" download title="save">' +
               '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ' +
               'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
               'stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4"/>' +
               '<path d="M5 19h14"/></svg></a>' +
           '</figcaption></figure>';
    return h;
  }
  if(isImage(f)){
    // Still arriving: a framed placeholder rather than a broken image.
    h += '<figure class="shot pending-shot"><div class="shot-img skel">' +
         '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" ' +
         'stroke="currentColor" stroke-width="1.6" stroke-linecap="round">' +
         '<rect x="3" y="4" width="18" height="16" rx="3"/>' +
         '<circle cx="8.5" cy="9.5" r="1.6"/><path d="m4 17 5-5 4 4 3-2 4 4"/>' +
         '</svg></div><figcaption><span class="fn">' + esc(f.name) +
         '</span><span class="fs">' + bytes(f.size) + '</span></figcaption>' +
         '</figure>';
    return h;
  }
  h += '<div class="filecard"><span class="chip">' + esc(ext(f.name)) +
       '</span><span class="fmeta"><span class="fn">' + esc(f.name) +
       '</span><span class="fs">' + bytes(f.size) +
       (f.url ? ' &middot; <a href="' + esc(f.url) + '" download>save</a>' : '') +
       '</span></span></div>';
  return h;
}

function headSig(m){
  const f = m.file || {};
  return [m.kind, m.dir, m.nick, m.addr, m.text, f.name, f.mime, f.url, f.size,
          m.conv].join(' ');
}

// The live half: transfer progress, the current note, and the receipt line.
function tailHTML(m){
  let h = '';
  if(m.kind === 'file'){
    const p = m.progress || {};
    const busy = m.state === 'sending' || m.state === 'receiving'
                 || m.state === 'queued';
    if(p.total && busy){
      const pct = clamp(100 * (p.done||0) / p.total, 0, 100);
      h += '<div class="prog"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
           '<div class="progtxt"><span>' + (p.done||0) + ' / ' + p.total +
           ' fragments</span><span>' + pct.toFixed(0) + '%</span></div>';
    }
  }
  if(m.note) h += '<div class="note">' + esc(m.note) + '</div>';
  if(m.kind === 'system') return h;

  const st = STATE[m.state] || {icon:'', cls:'pending', title:m.state||''};
  h += '<div class="meta">';
  if(m.secure) h += '<span class="badge sec" title="sealed end to end: ChaCha20-Poly1305, Noise KK session">E2E</span>';
  if(m.frames > 1) h += '<span class="badge">' + m.frames + ' frames</span>';
  if(m.tries > 1) h += '<span class="badge warn">retry &times;' + (m.tries-1) + '</span>';
  if(m.crc) h += '<span class="badge ' + (m.state === 'corrupt' ? 'bad' : 'ok') +
                 '">CRC ' + esc(m.crc) + '</span>';
  h += '<span class="time">' + clock(m.ts) + '</span>';
  h += '<span class="st ' + st.cls + '" title="' + esc(st.title) + '">' +
       st.icon + '</span></div>';
  // A group message: one receipt per receiver.
  const rc = m.rcpt ? Object.entries(m.rcpt) : [];
  if(m.dir === 'out' && rc.length > 1){
    h += '<div class="rcpts">' + rc.map(([id, r]) => {
      const x = STATE[r.state] || {icon:'', cls:'pending', title:r.state};
      return '<span class="rc ' + x.cls + '" title="' + esc(x.title) +
             (r.note ? ' - ' + esc(r.note) : '') + '">' + esc(r.nick || id) +
             ' ' + x.icon + '</span>';
    }).join('') + '</div>';
  }
  return h;
}

function avatarHTML(m){
  if(m.kind === 'system') return '';
  return '<div class="avatar ' + (m.dir === 'out' ? 'me' : 'them') + '">' +
         esc(initials(m.dir === 'out' ? (m.nick || S.nick) : m.nick)) + '</div>';
}

// Which conversation a message belongs to. System lines without one (channel
// changes, key errors) show in every conversation.
function convOf(m){
  if(m.conv) return m.conv;
  return m.kind === 'system' ? null : S.group;
}
function visible(m){
  const c = convOf(m);
  return c === null || c === S.conv || !S.mesh;
}
function render(m){
  const key = String(m.dir) + ':' + String(m.id);
  let row = S.rows.get(key);
  const wasBottom = atBottom();
  const fresh = !row;
  S.msgs.set(key, m);
  if(fresh && m.dir === 'in' && convOf(m) && convOf(m) !== S.conv){
    S.unread[convOf(m)] = (S.unread[convOf(m)] || 0) + 1;
    renderPeers();
  }
  if(!row){
    row = document.createElement('div');
    row.className = 'row ' + (m.kind === 'system'
      ? 'sys ' + (m.level||'info') : (m.dir === 'out' ? 'out' : 'in'));
    row.innerHTML = avatarHTML(m) +
      '<div class="bubble"><div class="head"></div><div class="tail"></div></div>';
    col.appendChild(row);
    S.rows.set(key, row);
  }
  const sig = headSig(m);
  if(row.dataset.sig !== sig){
    row.dataset.sig = sig;
    row.querySelector('.head').innerHTML = headHTML(m);
  }
  row.querySelector('.tail').innerHTML = tailHTML(m);
  row.hidden = !visible(m);
  // An image only takes up space once it has decoded, so a transcript that was
  // scrolled to the bottom would drift up under it.
  row.querySelectorAll('.shot img').forEach(img => {
    if(!img.complete) img.addEventListener('load', () => scrollDown(false), {once:true});
  });
  S.stick = wasBottom;
  scrollDown(false);
}

function selectConv(id){
  S.conv = id;
  delete S.unread[id];
  S.rows.forEach((row, key) => {
    const m = S.msgs.get(key);
    row.hidden = !(m && visible(m));
  });
  renderPeers();
  renderHeader();
  if(S.stats && S.stats.mac) applyStats(S.stats, true);
  const p = peerById(id);
  input.placeholder = id === S.group
    ? 'Message everyone in range\u2026   (/help for commands)'
    : 'Message ' + ((p && p.nick) || 'this node') + '\u2026' +
      (p && !p.trusted && p.is_key ? '   (approve it first)' : '');
  S.stick = true; scrollDown(true);
}

// ------------------------------------------------------------------ peers
const LOCK = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" ' +
  'stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/>' +
  '<path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
function secClass(p){
  if(!p) return 'off';
  if(p.security === 'off') return 'off';
  if(p.is_key && !p.trusted) return 'bad';
  return p.security === 'secure' ? 'on' : 'wait';
}
function secLabel(p){
  if(!p) return 'not encrypted';
  if(p.security === 'off') return 'not encrypted';
  if(!p.is_key) return 'no encryption on that node';
  if(!p.trusted) return 'not approved';
  return SEC_TEXT[p.security] || p.security;
}
function peerRow(p){
  const sel = p.id === S.conv ? ' sel' : '', away = p.present ? '' : ' away';
  const unread = S.unread[p.id] ? '<span class="unread">' + S.unread[p.id] + '</span>' : '';
  let h = '<button class="prow' + sel + away + '" data-id="' + esc(p.id) + '">' +
    '<span class="pav">' + esc(initials(p.nick)) + '</span>' +
    '<span class="pmain"><span class="pname">' + esc(p.nick || ('node ' + p.addr)) +
    '</span><span class="psub"><span class="led' + (p.present ? ' up' : '') +
    '"></span>' + (p.present ? '' : 'away &middot; ') +
    (p.addr == null ? '' : 'addr ' + esc(p.addr)) +
    (p.channel == null ? '' : ' &middot; ch ' + esc(p.channel)) +
    (p.present && LSTATE[p.state] && p.state !== 'ok' && p.state !== 'away'
      ? ' &middot; <span class="nohear" title="' + esc(p.reason || '') + '">' +
        LSTATE[p.state][0] + '</span>'
      : (p.present && p.hears_me === false ? ' &middot; <span class="nohear" title="its beacons do not list ' +
      'this node: it cannot decode you">doesn\'t hear you</span>' : '')) + '</span></span>' +
    '<span class="pside"><span class="lock ' + secClass(p) + '" title="' +
    esc(secLabel(p)) + '">' + LOCK + '</span>' + unread + '</span></button>';
  if(p.is_key && !p.trusted && p.security !== 'off'){
    const busy = S.approving[p.id];
    h += '<div class="trustbox"><div>New node. Its key fingerprint:</div>' +
         '<code>' + esc(p.fp) + '</code><div>Compare it with the one shown on ' +
         'that node\'s own screen, then:</div><button class="trust" data-id="' +
         esc(p.id) + '"' + (busy ? ' disabled' : '') + '>' +
         (busy ? 'approving\u2026' : 'Trust ' + esc(p.nick || 'this node')) +
         '</button></div>';
  }
  return h;
}
function renderPeers(){
  const box = $('plist'); if(!box) return;
  let html;
  if(!S.mesh){
    html = S.peers.map(peerRow).join('');
  } else {
    const n = S.peers.filter(p => p.present && (p.trusted || p.security === 'off')).length;
    const all = '<button class="prow grp' + (S.conv === S.group ? ' sel' : '') +
      '" data-id="' + S.group + '"><span class="pav">&#8727;</span>' +
      '<span class="pmain"><span class="pname">Everyone in range</span>' +
      '<span class="psub">' + n + ' approved node' + (n === 1 ? '' : 's') +
      ' &middot; group</span></span><span class="pside">' +
      (S.unread[S.group] ? '<span class="unread">' + S.unread[S.group] + '</span>' : '') +
      '</span></button>';
    const order = [...S.peers].sort((a, b) =>
      (b.present - a.present) || String(a.nick).localeCompare(String(b.nick)));
    html = all + order.map(peerRow).join('');
  }
  // Only touch the DOM when something visible changed: stats arrive four
  // times a second, and replacing the rows under the pointer would swallow a
  // click (mousedown and mouseup on different elements) - on Trust, too.
  if(html !== S.peersHtml){ S.peersHtml = html; box.innerHTML = html; }
  const hint = $('phint');
  if(hint) hint.style.display = S.peers.length ? 'none' : '';
}
document.addEventListener('click', e => {
  const t = e.target.closest && e.target.closest('.trust');
  if(t){
    e.stopPropagation();
    const id = t.dataset.id;
    if(S.demo){ S.approving[id] = true; demoTrust(id); return; }
    post('/api/trust', JSON.stringify({peer:id, trust:true}),
         {'Content-Type':'application/json'})
      .catch(() => addLog('approval failed - is the flowgraph running?'));
    S.approving[id] = true; S.peersHtml = null; renderPeers();
    return;
  }
  const r = e.target.closest && e.target.closest('.prow');
  if(r && r.dataset.id) selectConv(r.dataset.id);
});
function applyPeers(list){
  S.peers = list || [];
  if(S.conv !== S.group && S.mesh && !peerById(S.conv) && S.peers.length === 0)
    S.conv = S.group;
  renderPeers();
  renderHeader();
}

function addLog(line, ts){
  if(!logBox) return;
  const d = document.createElement('div');
  d.innerHTML = '<span class="t">' + clock(ts) + '</span>' + esc(line);
  logBox.appendChild(d);
  while(logBox.children.length > 300) logBox.removeChild(logBox.firstChild);
  logBox.scrollTop = logBox.scrollHeight;
}

// ----------------------------------------------------------------- lightbox
const lb = document.createElement('div');
lb.className = 'lightbox';
lb.innerHTML = '<img alt="attachment"><button class="lbx" aria-label="close">&times;</button>';
document.body.appendChild(lb);
const closeLb = () => lb.classList.remove('show');
lb.addEventListener('click', closeLb);
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeLb(); });
document.addEventListener('click', e => {
  const fig = e.target.closest && e.target.closest('.shot[data-full]');
  if(!fig || (e.target.closest && e.target.closest('.dl'))) return;
  lb.querySelector('img').src = fig.dataset.full;
  lb.classList.add('show');
});

// ---------------------------------------------------------------- telemetry
function tile(id, value, unit, cls){
  const el = $(id); if(!el) return;
  const v = el.querySelector('.v'); if(!v) return;
  v.innerHTML = value + (unit ? '<small>' + unit + '</small>' : '');
  el.classList.remove('good','warn','bad');
  if(cls) el.classList.add(cls);
}

function applyStats(s, again){
  S.stats = s;
  if(s.nick) S.nick = s.nick;
  if(s.peer_nick) S.peerNick = s.peer_nick;
  if(s.peers && !again) applyPeers(s.peers);
  if(s.my_addr != null){ S.myAddr = s.my_addr; S.peerAddr = s.peer_addr; }
  if(s.mac && s.mac.fp) S.fp = s.mac.fp;
  renderHeader();
  applyMac(s);

  // Signal tiles: the channel of the selected node, or the best one.
  let r = s.radio || {};
  const sel = peerById(S.conv);
  if(sel && sel.channel != null && s.mac && s.mac.radio && s.mac.radio[sel.channel])
    r = s.mac.radio[sel.channel];
  const snr = r.snr_db;
  tile('tSnr', snr == null ? '--' : snr.toFixed(1), 'dB',
       snr == null ? null : snr > 12 ? 'good' : snr > 6 ? 'warn' : 'bad');
  tile('tLvl', r.level_db == null ? '--' : r.level_db.toFixed(0), 'dB');
  tile('tTx', (s.tx_bps/1000).toFixed(1), 'kb/s');
  tile('tRx', (s.rx_bps/1000).toFixed(1), 'kb/s');
  const per = Number(s.per)||0;
  tile('tPer', per.toFixed(1), '%', per < 2 ? 'good' : per < 10 ? 'warn' : 'bad');
  tile('tQ', (1000*(s.backlog||0)).toFixed(0), 'ms');

  set('cTxF', s.tx_frames||0);
  set('cRxOk', s.rx_valid||0);
  set('cRetx', s.retx||0);
  set('cRxBad', s.rx_bad||0);
  set('cAck', (s.ack_tx||0) + ' / ' + (s.ack_rx||0));
  set('cDup', s.rx_dup||0);
  set('cDrop', s.dropped||0);
  set('cFrag', s.frag_lost||0);
  set('kPer', per.toFixed(2) + ' %');
  set('kQ', s.queued||0);
  set('kBk', (1000*(s.backlog||0)).toFixed(0) + ' ms');
  set('kIdle', (s.idle_tx||0) + ' / ' + (s.idle_rx||0));
  set('kRounds', s.file_rounds||0);
  set('kPend', s.pending || 'idle');
  set('kWin', (s.window == null ? '--' : (s.window > 1 ? 'SR ' + s.window : 'stop-and-wait'))
              + ' / ' + (s.inflight || 0));
  set('kFast', (s.fast_retx || 0) + ' / ' + (s.rx_skipped || 0));
  set('kBytes', bytes(s.tx_payload_bytes) + ' / ' + bytes(s.rx_payload_bytes));
  set('kUp', Math.floor(s.uptime||0) + ' s');
  set('kRev', s.revision || '--');
  set('thNow', rate(s.tx_bps) + ' / ' + rate(s.rx_bps));
  set('snrNow', snr == null ? '-- dB' : snr.toFixed(1) + ' dB');
  if(s.mac && s.mac.channels){
    const m = s.mac;
    set('subtitle', (s.tx_freq ? (s.tx_freq/1e6).toFixed(1) + ' MHz \u00b7 ' : '') +
        m.channels + ' \u00d7 ' + ((m.spacing||0)/1000).toFixed(0) + ' kHz FDMA \u00b7 ' +
        (m.mode === 'static' ? 'static two-node mode' : 'mesh') +
        ' \u00b7 differential BPSK');
  }
  if(s.frag_size)
    set('cap', 'frag ' + s.frag_size + ' B · ACK ' +
        (s.ack_timeout||0).toFixed(2) + ' s · ' + (s.max_retries||0) +
        ' retries · ' + ((s.sym_rate||0)/1000).toFixed(0) + ' ksym/s');

  applySecurity(s.security || {});
  applyRadio(s);
  if(again) return;

  S.tp.push([s.tx_bps||0, s.rx_bps||0]); if(S.tp.length > 150) S.tp.shift();
  S.snr.push(snr == null ? null : snr);   if(S.snr.length > 150) S.snr.shift();
  drawSparks();
}

const MAC_TEXT = {
  idle:'starting', listen:'listening', active:'transmitting',
  silent:'checking channel', full:'every channel busy'
};
function applyMac(s){
  const m = s.mac; if(!m) return;
  set('meChan', m.channel == null ? (MAC_TEXT[m.state] || m.state)
                                  : 'channel ' + m.channel);
  set('macNow', MAC_TEXT[m.state] || m.state);
  set('mChan', m.channel == null ? '--' : 'ch ' + m.channel + ' (' +
      ((m.offsets[m.channel]||0) >= 0 ? '+' : '') +
      ((m.offsets[m.channel]||0)/1000).toFixed(0) + ' kHz)');
  set('mMoves', (m.claims||0) + ' / ' + (m.collisions||0));
  set('mChecks', m.checks || 0);
  set('mEcho', m.echo == null ? '--' : (1000*m.echo).toFixed(0) + ' ms' +
      (m.lagging ? ' - host too slow' : ''));
  const a = m.adc || {};
  set('mAdc', a.peak_dbfs == null ? '--' : a.peak_dbfs.toFixed(1) + ' dBFS / ' +
      (100*(a.clip||0)).toFixed(2) + ' %');
  tile('tAdc', a.peak_dbfs == null ? '--' : a.peak_dbfs.toFixed(1), 'dBFS',
       a.peak_dbfs == null ? null : (a.clip||0) >= 0.001 || a.peak_dbfs > -1 ? 'bad'
       : a.peak_dbfs > -6 ? 'warn' : 'good');
  const box = $('chmap');
  if(box){
    const lv = m.levels || [], floor = m.floor, rows = [];
    const lo = floor == null ? -80 : floor, hi = lo + 60;
    for(let k = 0; k < m.channels; k++){
      const mine = k === m.channel;
      const who = S.peers.filter(p => p.channel === k && p.present);
      const occ = mine ? 'this node' + (who.length ? ' + ' + who.map(p => p.nick).join(', ') : '')
                : who.length ? who.map(p => p.nick || ('node ' + p.addr)).join(', ')
                : (m.busy && m.busy[k]) ? 'busy' : 'free';
      const r = (m.radio || [])[k] || {};
      const pct = lv[k] == null ? 0 : clamp(100*(lv[k]-lo)/(hi-lo), 2, 100);
      rows.push('<div class="ch' + (mine ? ' mine' : '') + (who.length > 1 || (mine && who.length) ? ' clash' : '') +
        '"><span class="chk">' + k + '</span><span class="cho">' +
        (((m.offsets||[])[k]||0) >= 0 ? '+' : '') + (((m.offsets||[])[k]||0)/1000).toFixed(0) +
        '</span><span class="chbar"><i style="width:' + pct.toFixed(0) + '%"></i></span>' +
        '<span class="chwho">' + esc(occ) + '</span><span class="chsnr">' +
        (r.snr_db == null || mine ? '' : r.snr_db.toFixed(0) + ' dB') + '</span></div>');
    }
    box.innerHTML = rows.join('');
  }
  const lk = $('links');
  if(lk){
    const db = v => v == null ? '--' : Number(v).toFixed(0) + ' dB';
    const cls = v => v == null ? '' : v >= 15 ? ' ok' : v >= 10 ? ' warn' : ' bad';
    const rows = S.peers.map(p => {
      const st = LSTATE[p.state] || [p.state || '--', ''];
      const l = p.link || {};
      const why = p.state && p.state !== 'ok' && p.reason ? p.reason
        : (l.retx ? l.retx + ' retransmissions, ' : '') +
          (p.link ? bytes(l.tx_bytes) + ' sent / ' + bytes(l.rx_bytes) + ' received' : '');
      return '<div class="lk2"><span class="lkn">' + esc(p.nick || ('node ' + p.addr)) +
        '</span><span class="num lstate' + cls(p.snr_in) + '">' + db(p.snr_in) +
        '</span><span class="num lstate' + cls(p.snr_out) + '">' + db(p.snr_out) +
        '</span><span class="lstate ' + st[1] + '">' + esc(st[0]) + '</span>' +
        (why ? '<span class="why">' + esc(why) + '</span>' : '') + '</div>';
    });
    lk.innerHTML = rows.join('') || '<div class="lk none">no node in range yet</div>';
    set('linksNow', S.peers.length + ' node' + (S.peers.length === 1 ? '' : 's'));
  }
}

// r6.1 link states (bpsk_app.link_state): short label, colour class
const LSTATE = {
  ok:['ok','ok'], away:['away','bad'], 'not-approved':['not approved','warn'],
  'no-encryption':['no encryption','warn'], 'cant-hear-you':['can\'t hear you','bad'],
  'no-response':['no response','bad'], securing:['securing\u2026','warn']
};
const SEC_TEXT = {
  secure:'end-to-end encrypted', handshaking:'securing link\u2026',
  idle:'securing link\u2026', 'no-key':'peer key missing'
};
function dur(sec){
  if(sec == null) return '--';
  sec = Math.max(0, Math.floor(sec));
  return sec < 120 ? sec + ' s' : Math.floor(sec/60) + ' min ' + (sec%60) + ' s';
}
function applySecurity(x){
  const st = x.stats || {};
  const p = peerById(S.conv);
  const secured = S.peers.filter(q => q.security === 'secure').length;
  set('secNow', x.error ? 'key error' : !x.enabled ? 'off'
      : S.mesh ? secured + ' secure session' + (secured === 1 ? '' : 's')
      : (SEC_TEXT[x.state] || x.state));
  set('sSuite', x.enabled ? 'Noise KK · X25519 · ChaCha20-Poly1305' : '--');
  set('sBackend', x.backend || '--');
  set('sMine', x.my_fp || S.fp || '--');
  set('sApproved', x.approved == null ? '--' : x.approved);
  set('sPeer', !x.enabled ? '--' : p ? (p.fp || 'no key') + ' \u00b7 ' + secLabel(p)
               : (x.peer_fp || (S.mesh ? 'select a node' : 'not in roster')));
  set('sAge', dur(x.session_age));
  set('sRekey', dur(x.rekey_in));
  set('sCount', x.sessions || 0);
  set('sFrames', (st.sealed_tx||0) + ' / ' + (st.opened_rx||0));
  set('sBad', (st.auth_fail||0) + ' / ' + (st.replay||0));
  set('sPlain', st.plain_dropped||0);
}

// r6.1: gain control, overflows, receive-chain liveness, CPU, AFC
function applyRadio(s){
  const g = s.gain, phy = s.phy || {};
  if(g){
    set('gRx', g.rx_gain); set('gTx', g.tx_atten);
    const a = $('gAuto');
    if(a && Date.now() > S.gainBusy) a.checked = !!g.auto;
    set('gainNow', g.auto ? 'automatic' : 'manual');
    set('gReason', g.reason ? 'last change: ' + g.reason : '');
  } else set('gainNow', 'no gain control');
  const ou = phy.ou || {};
  const o = ou.overflows, u = ou.underflows;
  const ouTxt = o == null ? '--' : o + ' / ' + u;
  set('rOu', ou.tap === false && o == null ? 'not counted' : ouTxt);
  const recent = S.ouPrev && o != null && (o > S.ouPrev[0] || u > S.ouPrev[1]);
  if(o != null){
    if(recent) S.ouAt = Date.now();
    S.ouPrev = [o, u];
  }
  tile('tOu', o == null ? '--' : o + '<small>/</small>' + u, '',
       o == null ? null : (S.ouAt && Date.now() - S.ouAt < 10000) ? 'bad'
       : (o || u) ? 'warn' : 'good');
  const live = phy.live || {};
  const lb = $('live');
  if(lb && live.channels){
    lb.innerHTML = 'chains ' + live.channels.map((ok, k) =>
      '<i class="' + (ok ? '' : 'dead') + '">' + k + '</i>').join('');
  }
  if(live.fault) banner('lost', 'receiver fault: ' + live.fault);
  else if(S.connected && !S.demo && $('banner') &&
          /receiver fault/.test($('bannerText').textContent)) banner(null);
  const cpu = phy.cpu || {};
  set('rCpu', cpu.process == null ? '--' : cpu.process.toFixed(2) + ' cores');
  set('rTop', (cpu.top || []).slice(0, 3).map(t => t[0] + ' ' +
      Number(t[1]).toFixed(2)).join(', ') || '--');
  set('rAfc', (phy.afc || []).map(a => a && a.cfo != null
      ? (a.cfo >= 0 ? '+' : '') + (a.cfo/1000).toFixed(1) : '--').join(' / ') +
      ((phy.afc || []).length ? ' kHz' : ''));
  const rej = (phy.deframer || []).map(d => d ? (d.hdr_bad_len||0) + (d.hdr_mismatch||0) : null);
  set('rRej', rej.length ? rej.map(x => x == null ? '--' : x).join(' / ') : '--');
  const q = phy.txq || {};
  set('rTxq', q.backlog == null ? '--' : (1000*q.backlog).toFixed(0) + ' ms / ' +
      (q.dropped||0) + (q.enabled === false ? ' (silent)' : ''));
}

// Constellation (one channel) and spectrum (the whole receiver), ~1/s
function drawView(){
  const v = S.view; if(!v) return;
  const m = (S.stats && S.stats.mac) || {};
  // the selected node's channel, else the first node in range, else own
  let k = null;
  const sel = peerById(S.conv);
  if(sel && sel.channel != null) k = sel.channel;
  if(k == null || !(v.const||[])[k]){
    const p = S.peers.find(q => q.present && q.channel != null &&
                                (v.const||[])[q.channel]);
    k = p ? p.channel : m.channel;
  }
  const pts = k == null ? null : (v.const||[])[k];
  const a = $('viewConst');
  if(a){
    const fit = fitCanvas(a);
    if(fit){
      const ctx = fit[0], w = fit[1], h = fit[2];
      ctx.strokeStyle = 'rgb(' + cssv('--c-grid') + ')';
      ctx.beginPath(); ctx.moveTo(w/2,0); ctx.lineTo(w/2,h); ctx.moveTo(0,h/2);
      ctx.lineTo(w,h/2); ctx.stroke();
      if(pts){
        const sc = Math.max(1e-6, ...pts.map(p => Math.max(Math.abs(p[0]), Math.abs(p[1])))) * 1.15;
        ctx.fillStyle = 'rgb(' + cssv('--c-rx') + ')';
        pts.forEach(p => ctx.fillRect(w/2 + p[0]/sc*w/2 - 1, h/2 - p[1]/sc*h/2 - 1, 2.4, 2.4));
      }
    }
  }
  set('constCap', k == null ? 'constellation' : 'constellation ch ' + k +
      (k === m.channel ? ' (own)' : ''));
  const b = $('viewSpec');
  if(b && v.spectrum){
    const fit = fitCanvas(b);
    if(fit){
      const ctx = fit[0], w = fit[1], h = fit[2], sp = v.spectrum, n = sp.length;
      const top = Math.max(...sp), lo = top - 70;
      gridlines(ctx, w, h);
      ctx.beginPath();
      sp.forEach((d, i) => {
        const x = w*i/(n-1), y = h - (h-4)*clamp((d-lo)/70, 0, 1) - 2;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.strokeStyle = 'rgb(' + cssv('--c-tx') + ')'; ctx.lineWidth = 1.3; ctx.stroke();
      ctx.strokeStyle = 'rgb(' + cssv('--c-grid') + ')';
      ctx.beginPath(); ctx.moveTo(w/2+.5, 0); ctx.lineTo(w/2+.5, h); ctx.stroke();
    }
  }
}

document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('.gb');
  if(!b) return;
  const g = (S.stats && S.stats.gain) || {};
  const key = b.dataset.g, cur = key === 'rx' ? g.rx_gain : g.tx_atten;
  if(cur == null) return;
  const body = {auto:false}; body[key] = cur + Number(b.dataset.d);
  S.gainBusy = Date.now() + 2000;
  if($('gAuto')) $('gAuto').checked = false;
  if(S.demo){ g.auto = false; g[key === 'rx' ? 'rx_gain' : 'tx_atten'] = body[key];
              g.reason = 'set by hand'; return applyRadio(S.stats); }
  post('/api/gain', JSON.stringify(body), {'Content-Type':'application/json'})
    .catch(() => addLog('gain change failed - is the flowgraph running?'));
});
if($('gAuto')) $('gAuto').addEventListener('change', e => {
  S.gainBusy = Date.now() + 2000;
  if(S.demo){ if(S.stats.gain) S.stats.gain.auto = e.target.checked; return; }
  post('/api/gain', JSON.stringify({auto: e.target.checked}),
       {'Content-Type':'application/json'})
    .catch(() => addLog('gain change failed - is the flowgraph running?'));
});

function setPeer(up, nick, mine, theirs){
  if(up != null) S.peerUp = !!up;
  if(mine != null){ S.myAddr = mine; S.peerAddr = theirs; }
  renderHeader();
}
// The header describes the selected conversation.
function renderHeader(){
  set('title', S.nick);
  set('meName', S.nick);
  set('meAddr', 'addr ' + S.myAddr);
  if(S.fp) set('meFp', S.fp);
  const led = $('led'), av = $('peerAvatar'), sec = $('sec');
  let name, up, state, addrs, cls, label, title = '';
  if(S.mesh && S.conv === S.group){
    const here = S.peers.filter(p => p.present);
    name = 'Everyone in range';
    up = here.length > 0;
    state = here.length + ' node' + (here.length === 1 ? '' : 's') + ' in range';
    addrs = 'group';
    const x = S.stats.security || {};
    cls = x.error ? 'bad' : (x.enabled ? 'on' : 'off');
    label = x.error ? 'key error' : (x.enabled ? 'each copy sealed per node' : 'not encrypted');
    title = x.error || 'a group message goes to every approved node as a separate, separately encrypted copy';
  } else {
    const p = peerById(S.conv) || {nick:S.peerNick, addr:S.peerAddr, present:S.peerUp};
    name = p.nick || ('node ' + p.addr);
    up = !!p.present;
    state = up ? 'online' : 'away';
    addrs = S.myAddr + ' \u2192 ' + (p.addr == null ? '?' : p.addr) +
            (p.channel == null ? '' : ' \u00b7 ch ' + p.channel) +
            (p.state && LSTATE[p.state] && p.state !== 'ok'
              ? ' \u00b7 ' + LSTATE[p.state][0]
              : p.hears_me === true ? ' \u00b7 hears you' :
                p.hears_me === false ? ' \u00b7 does NOT hear you' : '') +
            (p.snr_in != null || p.snr_out != null
              ? ' \u00b7 ' + (p.snr_in == null ? '--' : p.snr_in.toFixed(0)) + ' / ' +
                (p.snr_out == null ? '--' : p.snr_out.toFixed(0)) + ' dB' : '');
    cls = secClass(p.id ? p : null);
    label = p.id ? secLabel(p) : 'not encrypted';
    title = p.fp ? 'key ' + p.fp : '';
    const x = S.stats.security || {};
    if(!S.mesh && x.enabled){
      cls = x.state === 'secure' ? 'on' : (x.state === 'no-key' ? 'bad' : 'wait');
      label = SEC_TEXT[x.state] || x.state;
    }
    if(x.error){ cls = 'bad'; label = 'key error'; title = x.error; }
  }
  if(led) led.className = 'led' + (up ? ' up' : '');
  set('peerName', name);
  set('peerState', state);
  set('addrs', addrs);
  if(av) av.textContent = S.mesh && S.conv === S.group ? '\u2217' : initials(name);
  if(sec){ sec.className = 'sec ' + cls; sec.title = title; }
  set('secText', label);
}

function fitCanvas(c){
  const d = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if(!w || !h) return null;
  if(c.width !== w*d || c.height !== h*d){ c.width = w*d; c.height = h*d; }
  const ctx = c.getContext('2d');
  ctx.setTransform(d,0,0,d,0,0);
  ctx.clearRect(0,0,w,h);
  return [ctx,w,h];
}
function series(ctx, w, h, data, pick, rgb, max){
  const n = data.length; if(n < 2) return;
  ctx.beginPath();
  for(let i=0;i<n;i++){
    const v = pick(data[i]);
    const x = w*i/(n-1), y = h - 3 - (h-6)*clamp((v||0)/max,0,1);
    i ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
  }
  ctx.strokeStyle = 'rgb(' + rgb + ')';
  ctx.lineWidth = 1.7; ctx.lineJoin = 'round'; ctx.stroke();
  ctx.lineTo(w,h); ctx.lineTo(0,h); ctx.closePath();
  const g = ctx.createLinearGradient(0,0,0,h);
  g.addColorStop(0, 'rgba(' + rgb + ',.20)');
  g.addColorStop(1, 'rgba(' + rgb + ',0)');
  ctx.fillStyle = g; ctx.fill();
}
function gridlines(ctx,w,h){
  ctx.strokeStyle = 'rgb(' + cssv('--c-grid') + ')';
  ctx.lineWidth = 1;
  for(let i=1;i<3;i++){
    const y = Math.round(h*i/3)+.5;
    ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke();
  }
}
function drawSparks(){
  const a = $('sparkTp');
  if(a){
    const fit = fitCanvas(a);
    if(fit){
      const ctx = fit[0], w = fit[1], h = fit[2];
      gridlines(ctx,w,h);
      const peak = Math.max(1000, ...S.tp.map(p => Math.max(p[0],p[1])));
      series(ctx,w,h,S.tp,p=>p[0],cssv('--c-tx'),peak);
      series(ctx,w,h,S.tp,p=>p[1],cssv('--c-rx'),peak);
    }
  }
  const b = $('sparkSnr');
  if(b){
    const fit = fitCanvas(b);
    if(fit){
      const ctx = fit[0], w = fit[1], h = fit[2];
      gridlines(ctx,w,h);
      series(ctx,w,h,S.snr,v=>v,cssv('--c-snr'),30);
    }
  }
}
window.addEventListener('resize', () => { drawSparks(); drawView(); });

// ------------------------------------------------------------------- banner
function banner(kind, text){
  const b = $('banner');
  if(!b) return;
  if(!kind){ b.className = 'banner'; return; }
  b.className = 'banner show ' + kind;
  set('bannerText', text);
}

// ------------------------------------------------------------------- events
function handle(ev){
  switch(ev.type){
    case 'hello':    loadState(ev.state); break;
    case 'message':  render(ev.message); break;
    case 'stats':    applyStats(ev.stats); break;
    case 'log':      addLog(ev.line, ev.t); break;
    case 'peer':     setPeer(ev.up); break;
    case 'peers':    applyPeers(ev.peers); break;
    case 'security': break;
    case 'view':     S.view = ev.view; drawView(); break;
    case 'identity': S.nick = ev.nick; S.peerNick = ev.peer_nick;
                     if(ev.my_addr != null) S.myAddr = ev.my_addr;
                     renderHeader(); break;
    case 'reset':    col.innerHTML=''; S.rows.clear(); S.msgs.clear(); break;
  }
}

function loadState(st){
  if(!st) return;
  S.nick = st.nick; S.peerNick = st.peer_nick;
  S.myAddr = st.my_addr; S.peerAddr = st.peer_addr;
  S.mesh = st.mesh !== false; S.group = st.group || 'all'; S.fp = st.fp;
  S.conv = st.default_conv || S.group;
  S.peers = st.peers || [];
  document.body.classList.toggle('static', !S.mesh);
  col.innerHTML=''; S.rows.clear(); S.msgs.clear(); S.unread = {};
  (st.messages||[]).forEach(render);
  S.unread = {};
  (st.logs||[]).forEach(l => addLog(l[1], l[0]));
  if(st.stats) applyStats(st.stats);
  selectConv(S.conv);
  scrollDown(true);
}

let es = null, sawHello = false;
function connect(){
  try{ es = new EventSource('/events'); }catch(e){ return startDemo(); }
  es.onopen = () => { S.connected = true; if(!S.demo) banner(null); };
  es.onmessage = e => {
    sawHello = true; S.connected = true; S.demo = false; banner(null);
    try{ handle(JSON.parse(e.data)); }catch(err){}
  };
  es.onerror = () => {
    S.connected = false;
    if(!sawHello) return;              // the demo watchdog takes over instead
    banner('lost', 'radio process not reachable - reconnecting...');
  };
}

// ------------------------------------------------------------------ sending
async function post(url, body, headers){
  const r = await fetch(url, {method:'POST', body: body, headers: headers||{}});
  return r.json().catch(() => ({}));
}
function sendText(){
  const box = $('input');
  const t = box.value.trim();
  if(!t) return;
  box.value=''; resize(); if($('send')) $('send').disabled = true;
  if(S.demo) return demoSend(t);
  post('/api/send', JSON.stringify({text:t, to:S.conv}), {'Content-Type':'application/json'})
    .catch(() => addLog('send failed - is the flowgraph running?'));
}
const MAX_UPLOAD = 8 * 1024 * 1024;      // bpsk_app.MAX_UPLOAD
let localId = 0;
function localNote(text, level){
  render({id:'local' + (++localId), dir:'sys', kind:'system',
          level: level || 'warn', text: text, ts: Date.now()/1000});
}
function sendFiles(list){
  [...list].forEach(f => {
    if(S.mesh && S.conv === S.group){
      localNote('files go to one node at a time - pick a node on the left, then attach ' + f.name);
      return;
    }
    if(S.demo) return demoFile(f);
    if(f.size > MAX_UPLOAD){
      localNote(f.name + ' is ' + bytes(f.size) + ' - attachments are limited to ' +
                bytes(MAX_UPLOAD));
      return;
    }
    // Header values must be Latin-1, so the name is percent-encoded; without
    // this, a Sinhala, Tamil or emoji filename made fetch() throw.
    f.arrayBuffer().then(buf => post('/api/upload', buf, {
      'X-Filename': encodeURIComponent(f.name),
      'X-Filetype': f.type || 'application/octet-stream',
      'X-To': encodeURIComponent(S.conv),
      'Content-Type': 'application/octet-stream'
    })).then(r => {
      if(r && r.error) localNote('could not attach ' + f.name + ': ' + r.error);
    }).catch(() => localNote('upload of ' + f.name + ' failed - is the flowgraph running?'));
  });
}

const input = $('input');
function resize(){
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 150) + 'px';
}
input.addEventListener('input', () => {
  resize();
  if($('send')) $('send').disabled = !input.value.trim();
});
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendText(); }
});
if($('send')) $('send').onclick = sendText;
if($('attach')) $('attach').onclick = () => $('file').click();
if($('file')) $('file').onchange = e => { sendFiles(e.target.files); e.target.value=''; };
if($('togglePanel')) $('togglePanel').onclick = () => {
  const p = $('panel');
  const open = p.classList.toggle('open');
  $('togglePanel').classList.toggle('on', open);
  document.body.classList.toggle('panel-open', open);
  drawSparks();
};
document.addEventListener('paste', e => {
  const items = [...((e.clipboardData && e.clipboardData.files) || [])];
  if(items.length){ e.preventDefault(); sendFiles(items); }
});
['dragenter','dragover'].forEach(t => stream.addEventListener(t, e => {
  e.preventDefault(); stream.classList.add('drag');
}));
['dragleave','drop'].forEach(t => stream.addEventListener(t, e => {
  e.preventDefault();
  if(t === 'drop' && e.dataTransfer) sendFiles(e.dataTransfer.files);
  stream.classList.remove('drag');
}));

// ---------------------------------------------------------------- demo mode
// With no radio answering, the page still has to be worth looking at: it runs a
// self-contained mock of a four-node mesh so the layout can be reviewed and
// demonstrated with no hardware. The banner makes it unmistakable.
let demoId = 100;
const DEMO = {
  peers: [
    {id:'pb7e1', addr:59, nick:'Ben', channel:0, present:true, trusted:true,
     is_key:true, fp:'c07e 2b91 55a0 d3f6', security:'secure', age:0.4, hears_me:true,
     state:'ok', snr_in:21.4, snr_out:19.0,
     link:{up:true, inflight:0, queued:0, retx:3, file:false, rx_bytes:30600, tx_bytes:68200}},
    {id:'p42aa', addr:179, nick:'Cat', channel:3, present:true, trusted:true,
     is_key:true, fp:'42aa 90c2 17de 8b03', security:'secure', age:0.7, hears_me:true,
     state:'ok', snr_in:17.9, snr_out:16.5,
     link:{up:true, inflight:2, queued:0, retx:0, file:false, rx_bytes:4100, tx_bytes:2900}},
    {id:'p9d1e', addr:121, nick:'Dan', channel:1, present:true, trusted:false,
     is_key:true, fp:'9d1e 2c9a 01e5 efca', security:'idle', age:0.2, link:null,
     state:'not-approved', snr_in:11.2, snr_out:null,
     reason:'not approved here - compare its fingerprint, then press Trust'}
  ]
};
function demoStats(k){
  const snr = 14.6 + 2.5*Math.sin(k/9) + (Math.random()-.5)*0.9;
  const lv = [-41 + Math.sin(k/7), -52 + Math.sin(k/5), -6.1, -47 + Math.sin(k/4)];
  return {
    nick:'Ann', peer_nick:'', peer_up:true, my_addr:7, peer_addr:null,
    radio:{snr_db:snr, level_db:-34 + 2*Math.sin(k/13), locked:true},
    tx_bps: 26000 + 9000*Math.sin(k/7), rx_bps: 17000 + 7000*Math.cos(k/5),
    per: clamp(1.6 + 1.3*Math.sin(k/11), 0, 100),
    tx_frames: 1902+k*3, rx_valid: 1744+k*3, retx: 24+((k/6)|0),
    rx_bad: 31+((k/9)|0), ack_tx: 1731+k*3, ack_rx: 1802+k*3, rx_dup: 6,
    dropped: 1, frag_lost: 3, idle_tx: 902+k, idle_rx: 880+k, file_rounds: 2,
    queued: (k%7===0)?3:0, backlog: 0.021 + 0.012*Math.abs(Math.sin(k/4)),
    pending: (k%7===0)?'file-frag 78 -> 59':null,
    tx_payload_bytes: 68200+k*400, rx_payload_bytes: 30600+k*260,
    uptime: 243+k, revision:'r6.1-mesh', window: 32, inflight: (k%7===0)?9:3,
    fast_retx: 20+((k/6)|0), rx_skipped: 0, frag_size:256, ack_timeout:0.5,
    max_retries:5, sym_rate:250000, tx_freq:910.2e6, rx_freq:910.2e6,
    peers: DEMO.peers,
    mac:{mode:'mesh', state:(k%31===0)?'silent':'active', channel:2, channels:4,
         spacing:600e3, offsets:[450e3,1050e3,1650e3,2250e3],
         busy:[true,true,false,true], levels:lv, floor:-52, claims:1,
         collisions:0, checks:6+((k/30)|0), echo:0.062, lagging:false,
         adc:{peak_dbfs:-9.4 + Math.sin(k/6), rms_dbfs:-21.0, clip:0},
         fp:'3f1a 90c2 7d4e b815', my_addr:7,
         radio:[{snr_db:snr, level_db:lv[0]}, {snr_db:11.2, level_db:lv[1]},
                {snr_db:null, level_db:lv[2]}, {snr_db:17.9, level_db:lv[3]}]},
    gain:{rx_gain:18, tx_atten:52, auto:true, reason:'every neighbour hears this node above 23.0 dB'},
    phy:{ou:{overflows:0, underflows:0, tap:true},
         live:{channels:[true,true,true,true], fault:null},
         cpu:{process:1.21, top:[['freq_xlating_', 0.28], ['symbol_sync_cc', 0.17], ['fft_filter_ccc', 0.12]]},
         afc:[{cfo:-3410}, {cfo:12800}, {cfo:0}, {cfo:-18050}],
         deframer:[{hdr_bad_len:0, hdr_mismatch:2}, {hdr_bad_len:1, hdr_mismatch:0}, null, {hdr_bad_len:0, hdr_mismatch:1}],
         txq:{backlog:0.012, dropped:0, enabled:true}},
    security:{enabled:true, state:'secure', backend:'cryptography',
      suite:'Noise_KK_25519_ChaChaPoly_SHA256', my_fp:'3f1a 90c2 7d4e b815',
      approved:2, sessions:2, stats:{sealed_tx: 1902+k*3, opened_rx: 1744+k*3,
      auth_fail: 0, replay: 0, plain_dropped: 0}}
  };
}
function startDemo(){
  if(S.demo) return;
  S.demo = true;
  banner('demo', 'Demo mode - no radio process on this port. Start the flowgraph and reload to go live.');
  const t = Date.now()/1000;
  loadState({nick:'Ann', peer_nick:'', my_addr:7, peer_addr:null, mesh:true,
    group:'all', default_conv:'all', fp:'3f1a 90c2 7d4e b815',
    peers: DEMO.peers, logs:[], stats: demoStats(0), messages: [
    {id:'d1',dir:'sys',kind:'system',level:'info',ts:t-300,
     text:'mesh: address 7 (derived from this node\'s key), 4 channels at 600 kHz spacing - transmitting on channel 2 (+1650 kHz)'},
    {id:'d2',dir:'sys',kind:'system',level:'info',ts:t-296,conv:'pb7e1',
     text:'Ben is in range (address 59, key c07e 2b91 55a0 d3f6, approved)'},
    {id:'d2b',dir:'sys',kind:'system',level:'info',ts:t-295,conv:'pb7e1',
     text:'end-to-end encrypted with node 59 - its key fingerprint is c07e 2b91 55a0 d3f6. Compare it with /secure on that node.'},
    {id:'d2c',dir:'sys',kind:'system',level:'warn',ts:t-40,conv:'p9d1e',
     text:'NEW node Dan in range - key fingerprint 9d1e 2c9a 01e5 efca. Compare it with that node\'s own screen, then press Trust.'},
    {id:'g1',dir:'in',kind:'text',nick:'Ben',addr:59,conv:'all',state:'received',ts:t-250,secure:true,
     text:'All four boards are up - I see three other carriers on the waterfall.'},
    {id:'g2',dir:'out',kind:'text',nick:'Ann',addr:7,conv:'all',state:'delivered',ts:t-240,secure:true,
     text:'Same here. Group chat goes to each of you as its own encrypted copy.',
     rcpt:{pb7e1:{state:'delivered',nick:'Ben'}, p42aa:{state:'delivered',nick:'Cat'}}},
    {id:'g3',dir:'in',kind:'text',nick:'Cat',addr:179,conv:'all',state:'received',ts:t-230,secure:true,
     text:'Copy. I am on channel 3, SNR 18 dB.'},
    {id:'d3',dir:'in',kind:'text',nick:'Ben',addr:59,conv:'pb7e1',state:'received',ts:t-208,secure:true,
     text:'Receiver is locked. Constellation is two tight blobs at 45 dB Rx gain.'},
    {id:'d4',dir:'out',kind:'text',nick:'Ann',addr:7,conv:'pb7e1',state:'delivered',tries:2,ts:t-190,secure:true,
     text:'Copy. Dropping Tx attenuation to 20 dB and sending the sweep from the bench run.',
     rcpt:{pb7e1:{state:'delivered',nick:'Ben'}}},
    {id:'d5',dir:'out',kind:'file',nick:'Ann',addr:7,conv:'pb7e1',state:'delivered',ts:t-150,secure:true,
     file:{name:'rx_spectrum_910MHz.png',mime:'image/png',size:41210,url:DEMO_IMAGE},
     progress:{done:162,total:162}},
    {id:'d6',dir:'in',kind:'text',nick:'Ben',addr:59,conv:'pb7e1',state:'received',ts:t-96,secure:true,
     text:'Got it - CRC matched on the first pass, three fragments needed a repair round.'},
    {id:'d7',dir:'in',kind:'file',nick:'Cat',addr:179,conv:'p42aa',state:'received',ts:t-60,secure:true,
     crc:'2820d377', file:{name:'link_budget.csv',mime:'text/csv',size:2841,url:'#'},
     progress:{done:12,total:12}},
    {id:'d8',dir:'out',kind:'file',nick:'Ann',addr:7,conv:'pb7e1',state:'sending',tries:2,ts:t-8,secure:true,
     note:'retransmitting fragment 76',
     file:{name:'constellation_20dB.png',mime:'image/png',size:25027,url:null},
     progress:{done:78,total:99}}
  ]});
  [[t-300,'transmitting on channel 2 (+1650 kHz)'],
   [t-296,'secure session with 59 established (new, peer key c07e 2b91 55a0 d3f6)'],
   [t-150,'sending rx_spectrum_910MHz.png: 41210 bytes in 162 fragments'],
   [t-140,'peer is missing 3 fragments, resending them (round 2)'],
   [t-132,'peer confirmed rx_spectrum_910MHz.png received complete and CRC-correct'],
   [t-40,'handshake from 121 refused: its key 9d1e 2c9a 01e5 efca has not been approved on this node'],
   [t-8,'sending constellation_20dB.png: 25027 bytes in 99 fragments']
  ].forEach(l => addLog(l[1], l[0]));
  let k = 0;
  setInterval(() => { k++; applyStats(demoStats(k)); }, 900);
  setInterval(() => {
    const blob = (n, sig) => Array.from({length:96}, () =>
      [(Math.random() < .5 ? -1 : 1) + (Math.random()-.5)*sig, (Math.random()-.5)*sig]);
    const sp = Array.from({length:128}, (_, i) => {
      let d = -78 + Math.random()*3;
      [[-0.15,-41],[0.35,-6],[0.55,-52],[0.75,-47]].forEach(([f, l]) => {
        const x = (i/127 - 0.5) - f/2;
        if(Math.abs(x) < 0.03) d = Math.max(d, l + Math.random());
      });
      return d;
    });
    S.view = {const:[blob(96,.35), blob(96,.6), blob(96,.15), blob(96,.45)], spectrum:sp};
    drawView();
  }, 1000);
}
function demoTrust(id){
  const p = DEMO.peers.find(q => q.id === id);
  if(!p) return;
  p.trusted = true; p.security = 'handshaking';
  applyPeers(DEMO.peers);
  render({id:'d'+(++demoId), dir:'sys', kind:'system', level:'info', conv:id,
          ts:Date.now()/1000, text:'approved ' + p.nick + ' (key ' + p.fp +
          '). A secure session is being set up.'});
  setTimeout(() => {
    p.security = 'secure';
    p.link = {up:true, inflight:0, queued:0, retx:0, file:false, rx_bytes:0, tx_bytes:0};
    applyPeers(DEMO.peers);
    render({id:'d'+(++demoId), dir:'sys', kind:'system', level:'info', conv:id,
            ts:Date.now()/1000, text:'end-to-end encrypted with node ' + p.addr +
            ' - its key fingerprint is ' + p.fp + '.'});
  }, 900);
}
function demoSend(text){
  const id = 'd' + (++demoId);
  const group = S.conv === S.group;
  const to = group ? DEMO.peers.filter(p => p.trusted && p.present)
                   : DEMO.peers.filter(p => p.id === S.conv);
  const rcpt = {};
  to.forEach(p => { rcpt[p.id] = {state:'queued', nick:p.nick}; });
  const m = {id:id, dir:'out', kind:'text', nick:S.nick, addr:7, text:text,
             conv:S.conv, state:'queued', ts:Date.now()/1000, secure:true, rcpt:rcpt};
  if(!group && to[0] && !to[0].trusted){
    m.state = 'failed'; m.rcpt = {};
    m.note = 'its key has not been approved - compare the fingerprint and press Trust';
    return render(m);
  }
  render(m);
  setTimeout(() => { m.state='sent'; Object.values(m.rcpt).forEach(r => r.state='sent'); render(m); }, 260);
  to.forEach((p, i) => setTimeout(() => {
    m.rcpt[p.id].state = 'delivered';
    m.state = Object.values(m.rcpt).every(r => r.state === 'delivered') ? 'delivered' : 'sent';
    render(m);
  }, 700 + 350*i));
  const from = to[0];
  if(from) setTimeout(() => render({id:'d'+(++demoId), dir:'in', kind:'text',
    nick:from.nick, addr:from.addr, conv:S.conv, state:'received', ts:Date.now()/1000,
    secure:true, text:'(demo) ' + from.nick + ' here - got "' + text.slice(0,40) + '"'}), 1900);
}
function demoFile(f){
  const id = 'd' + (++demoId), url = URL.createObjectURL(f);
  const m = {id:id, dir:'out', kind:'file', nick:S.nick, addr:7, state:'sending',
             conv:S.conv, secure:true,
             file:{name:f.name, mime:f.type, size:f.size, url:url},
             progress:{done:0, total:Math.max(1, Math.ceil(f.size/254))},
             ts:Date.now()/1000};
  render(m);
  const step = () => {
    m.progress.done = Math.min(m.progress.total,
                               m.progress.done + Math.ceil(m.progress.total/12));
    if(m.progress.done >= m.progress.total){ m.state='delivered'; render(m); }
    else { render(m); setTimeout(step, 240); }
  };
  setTimeout(step, 300);
}

// -------------------------------------------------------------------- start
// On a narrow screen the telemetry panel would cover the conversation.
if(window.innerWidth < 900 && $('panel') && $('panel').classList.contains('open')){
  $('panel').classList.remove('open');
  if($('togglePanel')) $('togglePanel').classList.remove('on');
}
connect();
setTimeout(() => { if(!sawHello) startDemo(); }, 1200);
resize(); drawSparks();
})();
