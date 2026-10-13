"""Regression tests for the failures seen on hardware with r6.0.1.

One group per failure in section 2 of the r6.1 task (and the two reported
from the bench afterwards), each written so that it FAILS on r6.0.1 and
passes on r6.1:

  F1 mirror-image self-interference      F4 overflows: channels in lockstep
  F2 own transmitter overloads receiver  F5 lag, slow host, one-way links
  F3 receiver freeze on a bogus header   F6 CPU (structure of the flowgraph)
  F7 'pdu_to_tagged_stream ... Message discarded' (transmit queue)
  W  wire format, T telemetry, R rules for the embedded block

Run against another source tree to see the r6.0.1 results:

    python3 test_r61.py                       # this tree
    BPSK_TREE=/path/to/r6.0.1 python3 test_r61.py

The simulator (bpsk_meshsim.py, with its r6.1 radio model) and the GNU
Radio stubs always come from this directory; the code under test from
BPSK_TREE. GNU Radio itself is not needed (test_grc.py covers that level).
"""

import importlib.util
import inspect
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.abspath(os.environ.get('BPSK_TREE') or HERE)
sys.path.insert(0, TREE)

FAILURES = []
SCRATCH = tempfile.mkdtemp(prefix='bpsk_r61_')


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def guarded(label):
    """Run one check function; an exception (e.g. an API r6.0.1 does not
    have) is that check's failure, not the suite's."""
    def wrap(fn):
        def run():
            try:
                fn()
            except Exception as exc:                    # noqa: BLE001
                check(label, False, '%s: %s' % (type(exc).__name__, exc))
        run.__name__ = fn.__name__
        return run
    return wrap


def section(title):
    print('\n%s' % title)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sim_module():
    return load('bpsk_meshsim_r61', os.path.join(HERE, 'bpsk_meshsim.py'))


# --------------------------------------------------------- shim with stubs
def install_stubs():
    if 'pmt' in sys.modules and getattr(sys.modules['pmt'], '_stub', False):
        return
    pmt = types.ModuleType('pmt')
    pmt._stub = True

    class Sym(str):
        pass

    class Pair(object):
        def __init__(self, car, cdr):
            self.car, self.cdr = car, cdr

    class U8(object):
        def __init__(self, data):
            self.data = bytes(data)

    pmt.PMT_NIL = None
    pmt.intern = pmt.string_to_symbol = lambda s: Sym(s)
    pmt.is_symbol = lambda o: isinstance(o, Sym)
    pmt.symbol_to_string = str
    pmt.is_pair = lambda o: isinstance(o, Pair)
    pmt.car = lambda o: o.car
    pmt.cdr = lambda o: o.cdr
    pmt.cons = Pair
    pmt.is_u8vector = lambda o: isinstance(o, U8)
    pmt.u8vector_elements = lambda o: list(o.data)
    pmt.init_u8vector = lambda n, lst: U8(bytes(lst))
    pmt.is_c32vector = lambda o: False
    pmt.is_dict = lambda o: isinstance(o, dict)
    pmt.make_dict = lambda: {}
    pmt.dict_add = lambda d, k, v: dict(d, **{str(k): v})
    pmt.to_python = lambda o: o
    pmt.from_double = float
    pmt.from_bool = bool
    sys.modules['pmt'] = pmt
    gr = types.ModuleType('gnuradio.gr')

    class basic_block(object):
        def __init__(self, name='', in_sig=None, out_sig=None):
            self.published = []
            self.sigs = (in_sig, out_sig)

        def message_port_register_in(self, port):
            pass

        def message_port_register_out(self, port):
            pass

        def set_msg_handler(self, port, fn):
            # GNU Radio's gateway finds the handler again BY NAME
            if getattr(self, getattr(fn, '__name__', '<lambda>'), None) \
                    is None:
                raise AttributeError('handler for %s is not a named method '
                                     'of the block' % port)

        def message_port_pub(self, port, msg):
            self.published.append((str(port), msg))

        def consume(self, *a):
            pass

        def set_output_multiple(self, *a):
            pass

    gr.basic_block = gr.sync_block = basic_block
    pkg = types.ModuleType('gnuradio')
    pkg.gr = gr
    sys.modules['gnuradio'] = pkg
    sys.modules['gnuradio.gr'] = gr


def tree_shim():
    install_stubs()
    return load('tree_shim', os.path.join(
        TREE, 'bpsk_duplex_pluto_link_layer.py'))


def make_blk(shim, **kw):
    params = inspect.signature(shim.blk.__init__).parameters
    kw = {k: v for k, v in kw.items() if k in params}
    if 'count_ou' in params:
        kw.setdefault('count_ou', False)
    if 'telemetry' in params:
        kw.setdefault('telemetry', False)
    return shim.blk(**kw)


def grc_data():
    import yaml
    with open(os.path.join(TREE, 'bpsk_duplex_pluto.grc')) as fh:
        return yaml.safe_load(fh)


def new_sim(**kw):
    sim = sim_module()
    root = tempfile.mkdtemp(prefix='sim_', dir=SCRATCH)
    if kw.pop('rf', False):
        kw['rf'] = sim.RfModel(**kw.pop('rf_kw', {}))
    return sim, sim.MeshSim(root, **kw)


def run_secure_message(m, a, b, budget):
    """Wait for a session a<->b, then send a->b and b->a. Returns
    (session time or None, state a->b, state b->a)."""
    t0 = m.t
    ok = m.run(budget, until=lambda s: s.nodes[a].app.sec is not None and
               s.secure(a, b))
    t_sess = (m.t - t0) if ok else None
    pa, pb = m.pid(a), m.pid(b)
    mab = m.app(a).submit('text', text='a to b', to=pb)
    mba = m.app(b).submit('text', text='b to a', to=pa)
    m.run(budget, until=lambda s: s.state(a, mab) in ('delivered', 'failed')
          and s.state(b, mba) in ('delivered', 'failed'))
    return t_sess, m.state(a, mab), m.state(b, mba)


# =================================================================== F1
@guarded('F1a the node block\'s channel plan puts no channel on another '
         'channel\'s IQ image')
def f1a():
    shim = tree_shim()
    blk = make_blk(shim, my_addr=1, peer_addr=2, rx_dir=SCRATCH,
                   open_ui=False, security=False, http_port=8291)
    offs = [blk.app.offset(k) for k in range(4)]
    blk.stop()
    hits = [(k, j) for k, f in enumerate(offs) for j, g in enumerate(offs)
            if abs(-f - g) < 337.5e3]
    check('F1a the node block\'s channel plan puts no channel on another '
          'channel\'s IQ image', not hits,
          'offsets %s kHz, image hits %s' % (
              [round(f / 1e3) for f in offs], hits))


@guarded('F1b two nodes on channels 0 and 3 (mirror channels in r6.0.1) '
         'secure the link and exchange messages')
def f1b():
    sim, m = new_sim(rf=True, seed=11)
    m.add('A', channel=0, self_db=-1.0)
    m.add('B', channel=3, self_db=-1.0)
    m.trust_all()
    t_s, s1, s2 = run_secure_message(m, 'A', 'B', 40.0)
    snr = (m.link_snr('A', 'B'), m.link_snr('B', 'A'))
    check('F1b two nodes on channels 0 and 3 (mirror channels in r6.0.1) '
          'secure the link and exchange messages',
          t_s is not None and s1 == s2 == 'delivered'
          and min(x or -99 for x in snr) >= 15,
          'session %s s, messages %s/%s, SINR A->B %s B->A %s dB'
          % (None if t_s is None else round(t_s, 1), s1, s2,
             *[None if x is None else round(x, 1) for x in snr]))


# =================================================================== F2
@guarded('F2 own transmitter at full scale: gain control clears the ADC and '
         'both directions reach 15 dB')
def f2():
    sim, m = new_sim(rf=True, seed=5)
    m.add('A', self_db=4.0)          # its own signal clips its ADC
    m.add('B', self_db=-3.0)
    m.trust_all()
    t_s, s1, s2 = run_secure_message(m, 'A', 'B', 60.0)
    m.run(10.0)
    clip = m.app('A').adc.get('clip')
    snr = (m.link_snr('A', 'B'), m.link_snr('B', 'A'))
    check('F2 own transmitter at full scale: gain control clears the ADC and '
          'both directions reach 15 dB',
          clip == 0.0 and min(x or -99 for x in snr) >= 15
          and s1 == s2 == 'delivered',
          'A clipped %s, SINR A->B %s B->A %s dB, messages %s/%s, A RX gain '
          '%s TX atten %s' % (clip, *[None if x is None else round(x, 1)
                                      for x in snr], s1, s2,
                              m.nodes['A'].rx_gain, m.nodes['A'].tx_atten))


# =================================================================== F3
@guarded('F3 a header announcing 12301 bytes costs only its own frame')
def f3():
    import numpy as np
    import bpsk_phy as P
    rng = np.random.default_rng(1)
    good = [bytes(rng.integers(0, 256, 40, dtype=np.uint8)) for _ in range(5)]
    bad = bytearray(P.frame_bytes(bytes(13)))
    bad[72] ^= 0x30
    bad[74] ^= 0x30
    bits = np.concatenate([np.unpackbits(np.frombuffer(bytes(bad), np.uint8))]
                          + [P.frame_bits(g) for g in good])
    got = P.Deframer().feed(bits)
    check('F3 a header announcing 12301 bytes costs only its own frame '
          '(r6.0.1 waited for 98408 bits on that channel)', got == good,
          '%d of 5 following frames' % len(got))


# =================================================================== F4
@guarded('F4a the node block has no stream inputs')
def f4a():
    shim = tree_shim()
    blk = make_blk(shim, my_addr=1, peer_addr=2, rx_dir=SCRATCH,
                   open_ui=False, security=False, http_port=8292)
    ins = blk.sigs[0]
    blk.stop()
    check('F4a the node block has no stream inputs (r6.0.1 read four '
          'independently clocked symbol streams + the ADC in one block)',
          not ins, 'in_sig %r' % (ins,))


@guarded('F4b no block in the flowgraph takes stream inputs from two '
         'receive channels')
def f4b():
    d = grc_data()
    blocks = {b['name']: b for b in d['blocks']}
    ups = {}
    for c in d['connections']:
        src, sp, dst, dp = c
        if str(sp).isdigit() and str(dp).isdigit():
            ups.setdefault(dst, set()).add(src)

    def chans(name, seen=None):
        seen = seen or set()
        if name in seen:
            return set()
        seen.add(name)
        out = set()
        for tag in ('_ch0', '_ch1', '_ch2', '_ch3'):
            if name.endswith(tag) or tag + '_' in name:
                out.add(tag)
        if out:
            return out
        b = blocks.get(name, {})
        if b.get('id') in ('freq_xlating_fir_filter_xxx',) or \
                name.startswith('chan_filter'):
            return {name[-1]}
        for u in ups.get(name, ()):
            out |= chans(u, seen)
        return out
    bad = []
    for name, srcs in ups.items():
        if len(srcs) < 2:
            continue
        lineage = set()
        for s in srcs:
            lineage |= chans(s)
        if len(lineage) > 1:
            bad.append((name, sorted(lineage)))
    check('F4b no block in the flowgraph takes stream inputs from two '
          'receive channels (each symbol clock follows its own crystal)',
          not bad, repr(bad[:3]))


# =================================================================== F5
@guarded('F5a an idle node decodes at most a few frames a second per '
         'channel')
def f5a():
    shim = tree_shim()
    blk = make_blk(shim, my_addr=1, peer_addr=2, rx_dir=SCRATCH,
                   open_ui=False, security=False, http_port=8293)
    app = blk.app
    blk.stop()
    n = 0
    t = 1000.0
    for i in range(1000):                    # 10 s of 10 ms ticks
        for kind, _ in app.on_tick(t + 0.01 * i):
            n += kind == 'tx'
    check('F5a an idle node transmits at most a few frames a second (r6.0.1: '
          'an IDLE frame back to back, ~200/s, each decoded in Python by '
          'every receiver)', n / 10.0 <= 5, '%.1f frames/s' % (n / 10.0))


@guarded('F5b a slow host (2.5 s receive latency) still secures the link '
         'and delivers')
def f5b():
    sim, m = new_sim(seed=3)
    m.add('A', rx_latency=2.5)
    m.add('B', rx_latency=2.5)
    m.add('C', rx_latency=2.5)
    m.trust_all()
    m.run(10.0)
    res = []
    for a, b in (('A', 'B'), ('A', 'C'), ('B', 'C')):
        res.append(run_secure_message(m, a, b, 90.0))
    ok = all(r[0] is not None and r[1] == r[2] == 'delivered' for r in res)
    check('F5b a slow host (2.5 s receive latency, node B: echo 4.6 s) '
          'still secures every link and delivers both ways', ok,
          '; '.join('session %s, %s/%s' % (
              None if r[0] is None else round(r[0], 1), r[1], r[2])
              for r in res))


@guarded('F5c a node nobody can hear reports a link fault and stays on its '
         'channel')
def f5c():
    sim, m = new_sim(rf=True, seed=9)
    m.add('A', tx_loss=100.0)       # broken TX path: nobody decodes A
    m.add('B')
    m.add('C')
    m.trust_all()
    a = m.app('A')
    chans, states = [], []
    for i in range(110):
        m.run(1.0)
        chans.append(a.chan.channel)
        if i == 19 and hasattr(a, 'link_state'):   # what the UI shows
            states = [a.link_state(e, m.t)[0] for e in a.peers.values()]
    chans = [c for c in chans if c is not None]
    moves = sum(1 for x, y in zip(chans, chans[1:]) if x != y)
    texts = [x.get('text', '') for x in a.messages]
    fault = any('LINK FAULT' in t for t in texts)
    check('F5c a node nobody can hear reports a link fault and stays on its '
          'channel (r6.0.1 treated it as a collision and hopped)',
          moves == 0 and fault and len(states) == 2 and all(
              s == 'cant-hear-you' for s in states),
          '%d channel moves in 110 s, fault reported %s, peer states %s'
          % (moves, fault, states))


@guarded('F5d crossed handshakes on a slow link converge on one session')
def f5d():
    """The order of events the 4-node bench produced (1.7 s one way): each
    side ends with its current session paired with the other side's
    'previous' one, which expires after PREV_GRACE; r6.0.1 then dropped
    every frame as 'unknown session' until the 10-minute rekey."""
    import bpsk_keys
    import bpsk_link as L
    import bpsk_secure as S
    d = tempfile.mkdtemp(prefix='cross_', dir=SCRATCH)
    dirs = {10: os.path.join(d, 'a'), 20: os.path.join(d, 'b')}
    bpsk_keys.provision(dirs)
    node = {10: S.SecureLink(10, S.KeyStore(dirs[10], 10), peers=[20]),
            20: S.SecureLink(20, S.KeyStore(dirs[20], 20), peers=[10])}
    A, B = node[10], node[20]
    D = 1.7

    def txs(acts):
        return [v for k, v in acts if k == 'tx']

    def acc(x, raw, t):
        return x.accept(L.parse(raw), t)
    init_a = txs(A.on_tick(0.0))[0]                     # A initiates
    resp_b = txs(acc(B, init_a, D))[0]                  # B answers
    idle_a = txs(acc(A, resp_b, 2 * D))[0]              # A: session, confirm
    init_b = txs(B.on_tick(2 * D + 0.5))                # B gave up waiting
    if not init_b:
        check('F5d crossed handshakes on a slow link converge on one session',
              True, 'B waits for the confirmation (no crossing at all)')
        return
    resp_a = txs(acc(A, init_b[0], 3 * D + 0.5))[0]     # A answers B
    idle_b = txs(acc(B, resp_a, 4 * D + 0.5))[0]        # B: session, confirm
    acc(B, idle_a, 4 * D + 0.6)                         # A's late confirm
    acc(A, idle_b, 5 * D + 0.5)
    # from here: ticks every 0.1 s, a data frame each way every second,
    # every frame delivered after D
    air, opened = [], {10: [], 20: []}
    t = 5 * D + 0.6
    seq = 0
    while t < 120.0:
        t = round(t + 0.1, 3)
        due = [x for x in air if x[0] <= t]
        air[:] = [x for x in air if x[0] > t]
        for _, dst, raw in due:
            for k, v in acc(node[dst], raw, t):
                if k == 'tx':
                    air.append((t + D, 30 - dst, v))
                elif k == 'rx' and v.get('sealed'):
                    opened[dst].append(t)
        for a in (10, 20):
            for v in txs(node[a].on_tick(t)):
                air.append((t + D, 30 - a, v))
        if abs(t - round(t)) < 1e-6:
            seq += 1
            for a in (10, 20):
                raw = L.build(L.FT_DATA, 30 - a, a, seq & 0xFF, 0, b'x' * 20,
                              0, 0)
                sealed = node[a].protect(raw, t)
                if sealed is not None:
                    air.append((t + D, 30 - a, sealed))
    late = [sum(1 for x in opened[a] if x > 90.0) for a in (10, 20)]
    unk = A.stats['unknown_session'] + B.stats['unknown_session']
    check('F5d crossed handshakes on a slow link (1.7 s each way) converge '
          'on one session: traffic flows after the old sessions expire',
          min(late) >= 25,
          'frames opened in the last 30 s: A %d, B %d; unknown-session '
          'drops %d' % (late[0], late[1], unk))


@guarded('F5e two nodes that claimed one channel separate on a slow host')
def f5e():
    """The 2-hour bench soak (1/32 speed: seconds of pipeline latency) had
    two nodes share channel 0 for ten minutes: a channel check never saw
    the own carrier drain away, so it could not hear the other one."""
    sim, m = new_sim(rf=True, seed=12)

    class Scripted(random.Random):
        def __init__(self, seed, pick):
            random.Random.__init__(self, seed)
            self._u, self._c = [2.5], [pick]

        def uniform(self, a, b):
            return self._u.pop(0) if self._u else \
                random.Random.uniform(self, a, b)

        def choice(self, seq):
            if self._c and self._c[0] in seq:
                return self._c.pop(0)
            return random.Random.choice(self, seq)
    for name, seed in (('A', 1), ('B', 2)):
        m.add(name, start=0.0, rng=Scripted(seed, 1), rx_latency=0.5,
              tx_latency=2.5)
    m.add('C', start=0.1, rx_latency=0.5, tx_latency=2.5)
    m.trust_all()
    m.run(2.6)
    same = m.app('A').chan.channel == m.app('B').chan.channel == 1
    ok = m.run(90.0, until=lambda x: x.settled())
    check('F5e two nodes that claimed one channel separate on a slow host '
          '(2.5 s transmit pipeline; r6.0.1 kept them together)',
          same and ok, 'both on channel 1 at start: %s; settled after '
          '%.0f s: %s' % (same, m.t - 2.6, ok))


@guarded('F5g two nodes that start together on one channel part')
def f5g():
    """The final bench soak: two pairs of nodes, each pair on one channel,
    stayed together for minutes. Both nodes of a pair are deaf from the same
    moment, so they ran their silence checks together, each heard the other
    one silent, and each reported a link fault instead of moving. Here the
    two nodes share their random stream (same listen time, same channel,
    same check times) - the worst case; keys fixed, so it is repeatable."""
    import hashlib

    def key(i, nm):
        return hashlib.sha256(('F5g-%d-%s' % (i, nm)).encode()).digest()
    res = []
    for i in (1, 7, 11):
        sim, m = new_sim(rf=True, seed=i)
        for nm, start, rng in (('A', 0.0, random.Random(i)),
                               ('B', 0.0, random.Random(i)),
                               ('C', 0.05, None), ('D', 0.07, None)):
            m.add(nm, start=start, key=key(i, nm), rng=rng)
        m.trust_all()
        m.run(8.0)
        same = m.app('A').chan.channel == m.app('B').chan.channel
        t0 = m.t
        ok = m.run(40.0, until=lambda x: x.settled())
        res.append((same, ok, m.t - t0))
    shared = [r for r in res if r[0]]
    check('F5g two nodes that start on one channel at the same instant, '
          'with identical random choices, part within 40 s (r6.1 before the '
          'fix: both reported a link fault and stayed)',
          len(shared) >= 2 and all(r[1] for r in shared),
          '; '.join('%s, %s after %.0f s' % (
              'shared' if r[0] else 'apart at start',
              'parted' if r[1] else 'NOT parted', r[2]) for r in res))


@guarded('F5h two nodes alone on one channel part')
def f5h():
    """The final 2-node bench run (1/8 speed): the only two nodes both
    started on channel 0 and stayed there for the whole run. Neither could
    decode the other, so neither knew a neighbour to be deaf to; and a plain
    check on that slowed-down host ended before it had seen a level reading.
    Two nodes with identical random streams (their checks coincide), fixed
    keys, 0.3 / 0.35 s receive / transmit latency, a level reading every
    0.8 s - as the bench."""
    import hashlib

    def key(i, nm):
        return hashlib.sha256(('F5g-%d-%s' % (i, nm)).encode()).digest()
    res = []
    for i in (3, 4, 6):
        sim, m = new_sim(rf=True, seed=i, meas_period=0.8)
        for nm in ('A', 'B'):
            m.add(nm, start=0.0, key=key(i, nm), rng=random.Random(i),
                  rx_latency=0.3, tx_latency=0.35)
        m.trust_all()
        m.run(8.0)
        same = m.app('A').chan.channel == m.app('B').chan.channel
        t0 = m.t
        ok = m.run(40.0, until=lambda x: x.settled())
        res.append((same, ok, m.t - t0))
    shared = [r for r in res if r[0]]
    check('F5h the only two nodes, on one channel and unable to decode each '
          'other, part within 40 s (r6.1 before the fix and r6.0.1: stayed '
          'together)', len(shared) >= 2 and all(r[1] for r in shared),
          '; '.join('%s, %s after %.0f s' % (
              'shared' if r[0] else 'apart at start',
              'parted' if r[1] else 'NOT parted', r[2]) for r in res))


@guarded('F5f on a slow host the links stay "ok" through the channel checks')
def f5f():
    """The second bench soak (1/32 speed, ~3 s echo): a silence check lasts
    echo + 0.65 s on a lagging host - longer than the 3 s a neighbour keeps
    a node on its list - so a check on one side followed by one on the
    other showed "can't hear you" for a second, and logged a link fault,
    every few minutes on every node."""
    sim, m = new_sim(rf=True, seed=21)
    names = ('A', 'B', 'C', 'D')
    for name in names:
        m.add(name, rx_latency=0.5, tx_latency=2.5)
    m.trust_all()
    ok = m.run(120.0, until=lambda x: x.settled())
    m.run(20.0)

    def fault_ids():
        return {(nm, x.get('id')) for nm in names
                for x in m.app(nm).messages
                if 'cannot hear this node' in (x.get('text') or '')
                or 'LINK FAULT' in (x.get('text') or '')}
    before = fault_ids()
    checks0 = sum(m.app(nm).chan.checks for nm in names)
    bad = total = 0
    for _ in range(240):
        m.run(1.0)
        for nm in names:
            a = m.app(nm)
            for e in a.peers.values():
                total += 1
                bad += a.link_state(e, m.t)[0] == 'cant-hear-you'
    faults = len(fault_ids() - before)
    checks = sum(m.app(nm).chan.checks for nm in names) - checks0
    check('F5f on a slow host (3 s echo) every link stays "ok" through the '
          'periodic channel checks: no "can\'t hear you", no link fault',
          ok and bad == 0 and faults == 0 and checks > 0,
          'settled %s; over 240 s: %d channel checks, %d of %d link readings '
          '"can\'t hear you", %d link-fault reports'
          % (ok, checks, bad, total, faults))


# =================================================================== F6
@guarded('F6 the flowgraph has no FLL band-edge blocks and its Qt sinks '
         'are decimated')
def f6():
    d = grc_data()
    ids = [b['id'] for b in d['blocks']]
    fll = ids.count('digital_fll_band_edge_cc')
    blocks = {b['name']: b for b in d['blocks']}
    feed = {}
    for src, sp, dst, dp in d['connections']:
        if str(dp).isdigit():
            feed.setdefault(dst, []).append(src)
    undecim = []
    for name, b in blocks.items():
        if b['id'].startswith('qtgui_') and b['id'] not in (
                'qtgui_edit_box_msg', 'qtgui_msg_push_button') and \
                feed.get(name):
            src = blocks.get(feed[name][0], {})
            chain = [src]
            if src.get('id') in ('blocks_vector_to_stream',):
                chain.append(blocks.get(feed.get(src['name'], [''])[0], {}))
            if not any(c.get('id') in ('blocks_keep_one_in_n',
                                       'blocks_keep_m_in_n')
                       for c in chain):
                undecim.append(name)
    py = [b['name'] for b in d['blocks'] if b['id'] == 'epy_block']
    check('F6 the flowgraph has no FLL band-edge blocks (0.54 core) and its '
          'Qt sinks see decimated data (0.1-0.15 core each in r6.0.1)',
          fll == 0 and not undecim,
          '%d FLLs, undecimated Qt sinks %s, Python blocks %s'
          % (fll, undecim, py))


# =================================================================== F7
@guarded('F7 a transmitter slower than its model keeps a bounded queue')
def f7():
    sim, m = new_sim(seed=4)
    a = m.add('A', air_scale=1.15)    # the radio is 15 % slower
    m.add('B')
    m.trust_all()
    m.run(10.0, until=lambda s: s.secure('A', 'B'))
    data = bytes(random.Random(1).getrandbits(8) for _ in range(400000))
    mid = a.submit('file', name='blob.bin', mime='', data=data,
                   to=m.pid('B'))
    m.run(300.0, until=lambda s: s.state('A', mid) in ('delivered',
                                                       'failed'))
    q = m.nodes['A'].max_backlog
    check('F7 a transmitter 15 % slower than its airtime model keeps a '
          'bounded queue (r6.0.1: the PDU queue grew past 100 frames - '
          '"pdu_to_tagged_stream ... Message discarded")',
          q < 0.6 and m.state('A', mid) == 'delivered',
          'longest real queue %.2f s, file %s' % (q, m.state('A', mid)))


# =================================================================== W
@guarded('W beacons: the r6.1 link report is appended, the r6.0 layout '
         'unchanged')
def w():
    import bpsk_mac as M
    import bpsk_link as L
    sig = inspect.signature(M.pack_beacon).parameters
    check('W1 the wire revision moved on (r6.1)', L.REVISION.startswith(
        'r6.1'), L.REVISION)
    if 'ext' not in sig:
        check('W2 beacons carry the r6.1 link report as an extension', False,
              'pack_beacon has no ext')
        return
    ident = bytes(range(32))
    base = M.pack_beacon(1, 0, ident, 'Ann', True, [9, 11])
    ext = M.pack_ext(flags=M.EF_AUTO, adc_peak=-12, rx_gain=20, tx_atten=48,
                     levels=[-60, None, -45, -80],
                     links=[(9, 21.5, -40), (11, 14.0, -55)])
    full = M.pack_beacon(1, 0, ident, 'Ann', True, [9, 11], ext=ext)
    check('W2 an r6.1 beacon is the r6.0 beacon with the report appended '
          '(r6.0 parsers read the same fields)', full.startswith(base)
          and len(full) > len(base))
    b = M.unpack_beacon(full)
    e = b.get('ext') or {}
    lk = e.get('links') or {}
    check('W3 the report round-trips (two-way SNR table, gain state)',
          e.get('rx_gain') == 20 and e.get('tx_atten') == 48
          and sorted(lk) == [9, 11] and abs(lk[9]['snr'] - 21.5) < 0.6
          and e.get('levels') == [-60, None, -45, -80], repr(e)[:160])
    b0 = M.unpack_beacon(base)
    check('W4 a beacon without the report (an r6.0 node) parses, report '
          'absent', b0 is not None and b0['nick'] == 'Ann'
          and not b0.get('ext'))
    junk = M.unpack_beacon(base + bytes([0xE6, 99, 1, 2, 3, 4, 5, 6]))
    check('W5 an extension of an unknown version is ignored, not fatal',
          junk is not None and not junk.get('ext'))


# =================================================================== T
@guarded('T per-second telemetry CSV')
def t():
    import bpsk_app as A
    d = tempfile.mkdtemp(prefix='tele_', dir=SCRATCH)
    tele = os.path.join(d, 'telemetry')
    app = A.ChatApp(1, 2, 'T', os.path.join(d, 'files'), security=False,
                    telemetry_dir=tele, gain=dict(rx_gain=30, tx_atten=50))
    created = os.path.isdir(tele) and os.listdir(tele)
    now = 2000.0
    for i in range(350):
        app.on_tick(now + 0.01 * i)
    files = sorted(os.listdir(tele)) if os.path.isdir(tele) else []
    rows = 0
    header = ''
    for f in files:
        if 'peers' not in f:
            with open(os.path.join(tele, f)) as fh:
                lines = fh.read().splitlines()
            header, rows = lines[0], len(lines) - 1
    check('T1 telemetry opens no file in the constructor, then writes one '
          'row a second', not created and rows >= 3,
          '%d rows in %s' % (rows, files))
    check('T2 the row has the overflow, gain, ADC, CPU, echo and per-channel '
          'columns', all(c in header.split(',') for c in (
              'o_per_s', 'u_per_s', 'rx_gain', 'tx_atten', 'adc_peak_dbfs',
              'cpu_cores', 'echo_ms', 'snr0', 'lvl3', 'live_fault')))


# =================================================================== R
@guarded('R the embedded block\'s constructor opens nothing')
def r():
    shim = tree_shim()
    keys = os.path.join(SCRATCH, 'r_nokeys')
    before = threading.active_count()
    blk = make_blk(shim, rx_dir=keys, open_ui=False, http_port=8294,
                   security=True)
    after = threading.active_count()
    import socket
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 8294))
        free = True
    except OSError:
        free = False
    finally:
        s.close()
    kd = os.path.join(keys, 'keys')
    made = os.path.exists(os.path.join(kd, 'node.key'))
    blk.stop()
    check('R1 GRC can instantiate the block: no thread, no socket, no key '
          'created in __init__', after == before and free and not made,
          'threads %d->%d, port free %s, key created %s'
          % (before, after, free, made))
    check('R2 every message handler is a named method (the stub refuses '
          'lambdas and partials, as GNU Radio\'s gateway does)', True)


def main():
    print('code under test: %s' % TREE)
    section('F1 mirror-image self-interference')
    f1a()
    f1b()
    section('F2 own transmitter overloads own receiver')
    f2()
    section('F3 receiver freeze on a corrupted header')
    f3()
    section('F4 overflows: channels consumed in lockstep')
    f4a()
    f4b()
    section('F5 lag, slow host, persistent one-way link')
    f5a()
    f5b()
    f5c()
    f5d()
    f5e()
    f5f()
    f5g()
    f5h()
    section('F6 CPU')
    f6()
    section('F7 transmit queue in front of the modulator')
    f7()
    section('W wire format')
    w()
    section('T telemetry')
    t()
    section('R embedded-block rules')
    r()
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print('\n%s' % ('all r6.1 regression checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
