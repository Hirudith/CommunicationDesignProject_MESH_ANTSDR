#!/usr/bin/env python3
"""CPU of the whole generated flowgraph at the real radio rate.

The Pluto blocks are replaced by a file of samples paced at the radio rate
(a recorded-like mix: four BPSK carriers on the tree's own channel plan plus
noise) and a paced null sink, exactly as dev/grc_rx_check.py does, and the
process CPU (all threads: GNU Radio blocks, the node block, the Qt sinks)
is read from /proc while it runs. Works on the r6.1 tree and, for the
before/after comparison, on an r6.0.1 tree:

    python3 dev/cpu_check.py                 # this tree, 6 MS/s
    python3 dev/cpu_check.py /path/to/r6.0.1 # that tree, at its own rate

Prints one line 'CPU {json}': cores used by the process and by each kind of
thread (averaged over the measuring window).
"""

import json
import os
import sys
import tempfile
import time

TREE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else
                       os.path.dirname(os.path.dirname(
                           os.path.abspath(__file__))))
SECONDS = float(os.environ.get('CPU_SECONDS', '20'))
sys.path.insert(0, TREE)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['BROWSER'] = 'true'

import numpy as np                                          # noqa: E402


def threads():
    tick = float(os.sysconf('SC_CLK_TCK'))
    out = {}
    for tid in os.listdir('/proc/self/task'):
        try:
            with open('/proc/self/task/%s/stat' % tid) as fh:
                st = fh.read()
            with open('/proc/self/task/%s/comm' % tid) as fh:
                name = fh.read().strip()
        except OSError:
            continue
        f = st[st.rindex(')') + 2:].split()
        out[tid] = (name, (int(f[11]) + int(f[12])) / tick)
    return out


def main():
    from gnuradio import analog, blocks, gr, iio, filter
    from gnuradio.filter import firdes
    import pmt
    import inspect
    sys.path.insert(0, TREE)
    from gnuradio import digital
    _fll = digital.fll_band_edge_cc

    def fll(*a):                 # r6.0.1 ships the 3.10.12 (5-argument) form
        try:
            return _fll(*a)
        except TypeError:
            return _fll(*a[:4])
    digital.fll_band_edge_cc = fll
    import bpsk_duplex_pluto as fg
    params = inspect.signature(fg.bpsk_duplex_pluto.__init__).parameters
    rate = float(params['radio_rate'].default) if 'radio_rate' in params \
        else 3e6
    spacing = 600e3
    if 'ch_first' in params:
        offs = [450e3 + k * spacing for k in range(4)]
    else:
        offs = [(k - 1.5) * spacing for k in range(4)]
    interp = int(round(rate / 250e3))
    work = tempfile.mkdtemp(prefix='bpsk_cpu_')
    path = os.path.join(work, 'rx.c64')
    n = int(4.0 * rate)                        # 4 s, played in a loop
    tb = gr.top_block()
    add = blocks.add_cc(1)
    rng = np.random.default_rng(2)
    for k, f in enumerate(offs):
        bits = (rng.integers(0, 2, int(4.0 * 250e3)) * 2 - 1).astype(
            np.complex64)
        src = blocks.vector_source_c(bits.tolist(), True)
        fir = filter.interp_fir_filter_ccf(interp, firdes.root_raised_cosine(
            interp, interp, 1.0, 0.35, 11 * interp))
        amp = blocks.multiply_const_cc((0.3, 0.01, 0.003, 0.03)[k])
        rot = blocks.rotator_cc(2 * np.pi * f / rate, False)
        tb.connect(src, fir, amp, rot, (add, k))
    tb.connect(analog.fastnoise_source_c(analog.GR_GAUSSIAN, 1e-4, 3,
                                         1 << 16), (add, 4))
    head = blocks.head(8, n)
    sink = blocks.file_sink(8, path, False)
    tb.connect(add, head, sink)
    tb.run()
    sink.close()

    class FileRadio(gr.hier_block2):
        def __init__(self, *a):
            gr.hier_block2.__init__(self, 'file_radio',
                                    gr.io_signature(0, 0, 0),
                                    gr.io_signature(1, 1, 8))
            self.src = blocks.file_source(8, path, True)
            self.thr = blocks.throttle(8, rate, True)
            # gr-iio hands out whole Pluto buffers (rx_buffer samples):
            # the same chunking here, so downstream wake-ups are realistic
            bs = int(os.environ.get('CPU_CHUNK', '65536'))
            self.s2v = blocks.stream_to_vector(8, bs)
            self.v2s = blocks.vector_to_stream(8, bs)
            self.connect(self.src, self.thr, self.s2v, self.v2s, self)

        def __getattr__(self, name):
            if name.startswith('set_'):
                return lambda *a, **k: None
            raise AttributeError(name)

    class NullRadio(gr.hier_block2):
        def __init__(self, *a):
            gr.hier_block2.__init__(self, 'null_radio',
                                    gr.io_signature(1, 1, 8),
                                    gr.io_signature(0, 0, 0))
            self.thr = blocks.throttle(8, rate, True)
            self.null = blocks.null_sink(8)
            self.connect(self, self.thr, self.null)

        def __getattr__(self, name):
            if name.startswith('set_'):
                return lambda *a, **k: None
            raise AttributeError(name)

    class Attr(gr.basic_block):
        def __init__(self, *a):
            gr.basic_block.__init__(self, 'attr', None, None)
            self.message_port_register_in(pmt.intern('attr'))
            self.set_msg_handler(pmt.intern('attr'), self.handle)

        def handle(self, msg):
            pass

    if os.environ.get('CPU_NOQT'):
        # memory/CPU bisection: Qt sinks replaced by null sinks
        from gnuradio import qtgui

        class NoQt(gr.hier_block2):
            def __init__(self, *a, **k):
                gr.hier_block2.__init__(self, 'noqt',
                                        gr.io_signature(1, 1, 8),
                                        gr.io_signature(0, 0, 0))
                self.connect(self, blocks.null_sink(8))

            def qwidget(self):
                import sip
                self._w = Qt.QWidget()
                return sip.unwrapinstance(self._w)

            def __getattr__(self, name):
                return lambda *a, **k: None
        from PyQt5 import Qt
        qtgui.const_sink_c = NoQt
        qtgui.freq_sink_c = NoQt
    iio.fmcomms2_source_fc32 = FileRadio
    iio.fmcomms2_sink_fc32 = NullRadio
    iio.attr_sink = Attr
    from PyQt5 import Qt
    qapp = Qt.QApplication(sys.argv)
    os.chdir(work)
    kw = {k: v for k, v in dict(my_addr=1, peer_addr=2, nickname='cpu',
                                security=0, telemetry=0, http_port=8298,
                                channel=0, uri='ip:file').items()
          if k in params}
    top = fg.bpsk_duplex_pluto(**kw)
    counts = {}
    ll = getattr(top, 'link_layer', None)
    if os.environ.get('CPU_COUNT') and ll is not None:
        for name in ('_on_iq', '_on_meas', '_on_adc', '_on_txstat', '_on_tick',
                     '_on_rx'):
            orig = getattr(ll, name, None)
            if orig is None:
                continue

            def wrap(*a, _o=orig, _n=name, **k):
                counts[_n] = counts.get(_n, 0) + 1
                return _o(*a, **k)
            setattr(ll, name, wrap)
    for name in (os.environ.get('CPU_MUTE') or '').split(','):
        if name and ll is not None:          # bisection: drop that input
            setattr(ll, name, lambda *a, **k: None)
    top.start()
    t_end = time.time() + 6.0                  # settle
    while time.time() < t_end:
        qapp.processEvents()
        time.sleep(0.05)
    def rss():
        with open('/proc/self/statm') as fh:
            return int(fh.read().split()[1]) * os.sysconf(
                'SC_PAGE_SIZE') / 1048576.0
    a, ta = threads(), time.time()
    c0 = dict(counts)
    p0 = sum(os.times()[:2])
    t_end = ta + SECONDS
    mem = [(0.0, round(rss(), 1))]
    next_m = ta + 10.0
    while time.time() < t_end:
        qapp.processEvents()
        time.sleep(0.05)
        if time.time() >= next_m:
            next_m += 10.0
            mem.append((round(time.time() - ta), round(rss(), 1)))
    b, tb_ = threads(), time.time()
    p1 = sum(os.times()[:2])
    dt = tb_ - ta
    agg = {}
    for tid, (name, c) in b.items():
        if tid in a:
            key = name.rstrip('0123456789') or name
            agg[key] = agg.get(key, 0.0) + (c - a[tid][1]) / dt
    top.stop()
    top.wait()
    res = {'tree': TREE, 'radio_rate': rate, 'process_cores':
           round((p1 - p0) / dt, 2),
           'calls_per_s': {k: round((v - c0.get(k, 0)) / dt, 1)
                           for k, v in counts.items()},
           'rss_mb': mem,
           'rss_growth_mb_per_h': round((mem[-1][1] - mem[0][1]) / max(
               1e-6, mem[-1][0]) * 3600, 1) if len(mem) > 1 else None,
           'threads': {k: round(v, 3) for k, v in sorted(
               agg.items(), key=lambda kv: -kv[1]) if v >= 0.005}}
    print('CPU ' + json.dumps(res), flush=True)
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    os._exit(0)


if __name__ == '__main__':
    main()
