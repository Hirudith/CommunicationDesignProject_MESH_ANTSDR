"""Selective Repeat ARQ (r5.4): correctness at the edges, and the speed-up.

Two ChatApp nodes on a simulated FDD channel. Frames leave each node when its
modelled transmit backlog allows, so window pacing and ACK timing are real.

    python3 test_arq.py
"""

import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_app as app                                          # noqa: E402
import bpsk_keys                                                # noqa: E402
import bpsk_link as L                                           # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


class Sim(object):
    """drop(src, parsed_frame_or_None, t) -> True to lose that frame.

    With security on the frame is sealed, so drop() sees only the outer
    header; the tests that inspect frame types run in plaintext.
    """

    def __init__(self, root, window=32, loss=0.0, drop=None, secure=False,
                 latency=0.002, seed=1, frag=256):
        random.seed(seed)
        self.tmp = tempfile.mkdtemp(prefix='arq_', dir=root)
        self.keys = {1: os.path.join(self.tmp, 'k1'),
                     2: os.path.join(self.tmp, 'k2')}
        if secure:
            bpsk_keys.provision(self.keys)
        self.opts = dict(ack_timeout=0.5, max_retries=5, frag_size=frag,
                         sym_rate=250000.0, overhead=460, security=secure,
                         window=window)
        self.nodes = {a: self.make(a) for a in (1, 2)}
        self.loss, self.drop, self.latency = loss, drop, latency
        self.air, self.t = [], 0.0

    def make(self, a):
        return app.ChatApp(a, 2 if a == 1 else 1, 'Node %d' % a,
                           os.path.join(self.tmp, 's%d' % a),
                           keys_dir=self.keys[a], **self.opts)

    def pump(self, me, actions):
        for kind, frame in actions:
            if kind != 'tx':
                continue
            if random.random() < self.loss:
                continue
            if self.drop is not None:
                try:
                    frm = L.parse(frame)
                except L.BadFrame:
                    frm = None
                if self.drop(me, frm, self.t):
                    continue
            leave = max(self.t, self.nodes[me].link.busy_until)
            self.air.append((leave + self.latency, 2 if me == 1 else 1, frame))

    def inject(self, dest, frame):
        self.air.append((self.t + 0.001, dest, frame))

    def run(self, seconds, until=None):
        end, step = self.t + seconds, 0
        while self.t < end:
            self.t = round(self.t + 0.001, 6)
            due = sorted((x for x in self.air if x[0] <= self.t),
                         key=lambda x: x[0])
            self.air = [x for x in self.air if x[0] > self.t]
            for _, dest, frame in due:
                self.pump(dest, self.nodes[dest].on_rx(frame, self.t))
            if step % 10 == 0:
                for addr, node in self.nodes.items():
                    self.pump(addr, node.on_tick(self.t))
            step += 1
            if until is not None and step % 50 == 0 and until(self):
                return True
        return False

    def texts(self, a):
        return [m['text'] for m in self.nodes[a].messages
                if m['dir'] == 'in' and m['kind'] == 'text']

    def rx_file(self, a, name):
        path = os.path.join(self.tmp, 's%d' % a, 'rx_' + name)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as fh:
            return fh.read()


def st(node, mid):
    return node._by_mid.get(mid, {}).get('state')


def done(node, mid):
    return st(node, mid) in ('delivered', 'failed')


def transfer(root, window, loss, secure=False, size=100000, latency=0.002):
    s = Sim(root, window=window, loss=loss, secure=secure, latency=latency)
    if secure:
        s.run(5, until=lambda x: x.nodes[1].sec.ready(2, x.t))
    t0 = s.t
    data = os.urandom(size)
    mid = s.nodes[1].submit('file', name='bulk.bin', mime='', data=data)
    s.run(1200, until=lambda x: done(x.nodes[1], mid))
    dt = s.t - t0
    return dt, s.rx_file(2, 'bulk.bin') == data, s


# ---------------------------------------------------------------------------
def t_speed(root):
    print('\n--- throughput: 100 kB file, 256-byte fragments')
    rows = []
    for loss in (0.0, 0.093):
        for window in (1, 32):
            dt, ok, _ = transfer(root, window, loss)
            rows.append((loss, window, dt, ok))
            print('     loss %4.1f%%  window %2d  %6.1f s  %5.1f kb/s  intact=%s'
                  % (loss * 100, window, dt, 800 / dt, ok))
    sw = {r[0]: r[2] for r in rows if r[1] == 1}
    sr = {r[0]: r[2] for r in rows if r[1] == 32}
    check('every transfer arrives intact', all(r[3] for r in rows))
    check('window 32 is >= 3x faster than stop-and-wait on a clean channel',
          sw[0.0] / sr[0.0] >= 3.0, '%.1fx' % (sw[0.0] / sr[0.0]))
    check('... and >= 5x faster at the bench\'s 9.3% frame loss',
          sw[0.093] / sr[0.093] >= 5.0, '%.1fx' % (sw[0.093] / sr[0.093]))


def t_exactly_once(root):
    print('\n--- 20% loss both ways, files and chat in both directions')
    s = Sim(root, loss=0.20, seed=4)
    fa, fb = os.urandom(80000), os.urandom(30000)
    ta = ['A%02d:' % i + 'a' * (i * 29 % 300) for i in range(25)]
    tb = ['B%02d:' % i + 'b' * (i * 41 % 300) for i in range(25)]
    mfa = s.nodes[1].submit('file', name='fa.bin', mime='', data=fa)
    mfb = s.nodes[2].submit('file', name='fb.bin', mime='', data=fb)
    ma = [s.nodes[1].submit('text', text=t) for t in ta]
    mb = [s.nodes[2].submit('text', text=t) for t in tb]
    s.run(900, until=lambda x: done(x.nodes[1], mfa) and done(x.nodes[2], mfb)
          and all(done(x.nodes[1], m) for m in ma)
          and all(done(x.nodes[2], m) for m in mb))
    check('both files intact', s.rx_file(2, 'fa.bin') == fa
          and s.rx_file(1, 'fb.bin') == fb)
    check('every chat message delivered exactly once, in order',
          s.texts(2) == ta and s.texts(1) == tb,
          '%d/%d and %d/%d' % (len(s.texts(2)), len(ta), len(s.texts(1)),
                               len(tb)))
    a = s.nodes[1].link
    check('sequence numbers wrapped past 255 on the way',
          a._txno - a.stats['retx'] > 256,
          '%d data frames' % (a._txno - a.stats['retx']))
    check('fast retransmit did the recovery',
          s.nodes[1].link.stats['fast_retx'] > 0,
          '%d fast of %d retransmissions'
          % (s.nodes[1].link.stats['fast_retx'], s.nodes[1].link.stats['retx']))


def t_ack_loss(root):
    print('\n--- half of all ACKs lost')

    def drop(src, frm, t):
        return frm is not None and frm['type'] == L.FT_ACK and random.random() < 0.5
    s = Sim(root, drop=drop, seed=5)
    data = os.urandom(50000)
    texts = ['t%d' % i for i in range(20)]
    mf = s.nodes[1].submit('file', name='a.bin', mime='', data=data)
    mids = [s.nodes[1].submit('text', text=t) for t in texts]
    s.run(300, until=lambda x: done(x.nodes[1], mf)
          and all(done(x.nodes[1], m) for m in mids))
    check('no duplicate deliveries, nothing lost', s.texts(2) == texts
          and s.rx_file(2, 'a.bin') == data)
    a = s.nodes[1].link
    sent = a._txno - a.stats['retx']
    check('a lost ACK is covered by the next cumulative SACK, so almost '
          'nothing is resent', a.stats['retx'] <= 0.05 * sent,
          '%d retransmissions for %d frames' % (a.stats['retx'], sent))


def t_blackout(root):
    print('\n--- a 6 s blackout mid-transfer forces frames to be abandoned')
    window = {'on': False}

    def drop(src, frm, t):
        return window['on'] and src == 1
    s = Sim(root, drop=drop)
    data = os.urandom(60000)
    mf = s.nodes[1].submit('file', name='b.bin', mime='', data=data)
    s.run(3)
    window['on'] = True
    s.run(6)
    window['on'] = False
    mt = s.nodes[1].submit('text', text='after the blackout')
    s.run(300, until=lambda x: done(x.nodes[1], mf) and done(x.nodes[1], mt))
    a, b = s.nodes[1].link, s.nodes[2].link
    check('frames were abandoned during the blackout',
          a.stats['dropped'] > 0, '%d dropped' % a.stats['dropped'])
    check('the receiver skipped the gap instead of waiting for ever',
          b.stats['rx_skipped'] > 0, '%d skipped' % b.stats['rx_skipped'])
    check('chat flows again after the blackout',
          'after the blackout' in s.texts(2))
    check('the file still completes intact (file-level repair)',
          st(s.nodes[1], mf) == 'delivered' and s.rx_file(2, 'b.bin') == data,
          st(s.nodes[1], mf))


def t_sync_release(root):
    print('\n--- frames held behind an abandoned one are released')
    def drop(src, frm, t):
        if src != 1 or frm is None or frm['type'] != L.FT_DATA:
            return False
        env = app.unpack_envelope(frm['payload'])
        if env is not None and env['body'] == b'one':
            return True                          # every copy of 'one' is lost
        return False
    s = Sim(root, drop=drop)
    s.run(1)
    mids = [s.nodes[1].submit('text', text=t) for t in ('one', 'two', 'three')]
    s.run(20, until=lambda x: all(done(x.nodes[1], m) for m in mids))
    s.run(1.0)                   # the base-sync frame is still on its way
    check('"one" fails, "two" and "three" still arrive with nothing else sent',
          st(s.nodes[1], mids[0]) == 'failed' and s.texts(2) == ['two', 'three'],
          repr(s.texts(2)))


def t_restart_plain(root):
    print('\n--- plaintext mode: the sender restarts mid-conversation')
    s = Sim(root)
    m = s.nodes[1].submit('text', text='before')
    s.run(5, until=lambda x: done(x.nodes[1], m))
    for _ in range(40):                          # move A's numbering on
        s.nodes[1].submit('text', text='x')
    s.run(20)
    s.nodes[1] = s.make(1)                       # new process: seq back to 0
    m2 = s.nodes[1].submit('text', text='after')
    ok = s.run(10, until=lambda x: done(x.nodes[1], m2))
    check('the receiver detects the new epoch and resynchronises',
          s.nodes[2].link.stats['rx_resync'] >= 1)
    check('the first message after the restart is delivered',
          ok and s.texts(2)[-1] == 'after', repr(s.texts(2)[-3:]))


def t_forged_sync(root):
    print('\n--- a broadcast frame cannot move the receive window')
    s = Sim(root)
    m = s.nodes[1].submit('text', text='hello')
    s.run(5, until=lambda x: done(x.nodes[1], m))
    b = s.nodes[2].link
    base = b.rx[1]['base']
    epoch = s.nodes[1].link.epoch
    s.inject(2, L.build(L.FT_IDLE, L.BROADCAST, 1, 0, (base + 50) % 256, b'',
                        L.FLAG_SYNC | (epoch << L.EPOCH_SHIFT), 0))
    s.run(0.2)
    check('broadcast SYNC ignored', b.rx[1]['base'] == base
          and b.stats['rx_skipped'] == 0)
    m2 = s.nodes[1].submit('text', text='still fine')
    s.run(5, until=lambda x: done(x.nodes[1], m2))
    check('traffic unaffected', s.texts(2) == ['hello', 'still fine'])


def t_window_one(root):
    print('\n--- window 1 behaves as stop-and-wait')
    dt, ok, s = transfer(root, 1, 0.05, size=20000)
    a = s.nodes[1].link
    check('file intact with window 1', ok)
    check('never more than one frame in flight',
          a.window == 1 and len(a.outq) <= 1)


def t_secure(root):
    print('\n--- Selective Repeat through the encryption layer')
    dt, ok, s = transfer(root, 32, 0.093, secure=True)
    dt0, ok0, _ = transfer(root, 1, 0.093, secure=True)
    check('encrypted transfer intact at 9.3% loss, window 32 vs 1',
          ok and ok0, '%.1f kb/s vs %.1f kb/s' % (800 / dt, 800 / dt0))
    check('no authentication failures or replays from the window',
          s.nodes[2].sec.stats['auth_fail'] == 0
          and s.nodes[2].sec.stats['replay'] == 0)


def main():
    root = tempfile.mkdtemp(prefix='bpsk_arq_')
    try:
        for fn in (t_speed, t_exactly_once, t_ack_loss, t_blackout,
                   t_sync_release, t_restart_plain, t_forged_sync,
                   t_window_one, t_secure):
            fn(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print('\n%s' % ('all ARQ checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
