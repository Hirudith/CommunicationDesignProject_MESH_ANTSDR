#!/usr/bin/env python3
"""Run the generated flowgraph on synthesized radio samples (test_grc G6).

The Pluto blocks are replaced (before the flowgraph module is imported) by a
file of 6 MS/s samples paced at the radio rate and a paced null sink, so the
receiver, the measurement taps, the AFC, the gain control and the node block
run exactly as generated. The recording:

  channel 0  this node's own transmitter, -8 dBFS peaks, with a -45 dBc
             IQ image (it lands at -450 kHz: no channel there)
  channel 1  a neighbour 30 dB below it, carrier offset +18 kHz
  channel 2  a neighbour 45 dB below it, carrier offset -21 kHz
  channel 3  empty
  noise      -78 dBFS
  faults     a frame on channel 1 whose length field reads 12301 bytes at
             2.0 s, and 40 ms of samples missing at 2.6 s (an overflow)

Prints one line 'RESULT {json}': frames sent / received per channel after
the AFC has settled (from 1.2 s), AFC estimates, the node's fault state.

    python3 dev/grc_rx_check.py
"""

import json
import os
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['BROWSER'] = 'true'

import numpy as np                                          # noqa: E402

FS = 6e6
SECONDS = 3.6
SETTLE = 1.2
CH = [450e3, 1050e3, 1650e3, 2250e3]


def make_recording(path):
    from gnuradio import analog, blocks, filter, gr
    from gnuradio.filter import firdes
    import bpsk_link as L
    import bpsk_phy as P
    rng = np.random.default_rng(11)
    nsym = int(SECONDS * 250e3)
    sent = {}
    streams = []
    for k, dbfs, cfo in ((0, -8.0, 0.0), (1, -38.0, 18e3), (2, -53.0, -21e3)):
        tx = P.TxFramer(max_backlog=1e9)
        starts = []
        i = 0
        bad_at = None
        while tx.backlog_bits() < nsym:
            t = tx.backlog_bits() / 250e3
            payload = struct.pack('!HI', k, i) + bytes(rng.integers(
                0, 256, 80, dtype=np.uint8))
            frame = L.build(L.FT_DATA, 99, 10 + k, i & 0xFF, 0, payload,
                            0, 0)
            if k == 1 and bad_at is None and t >= 2.0:
                raw = bytearray(P.frame_bytes(frame))
                raw[72] ^= 0x30                  # length 0x300D, both copies
                raw[74] ^= 0x30
                b = np.unpackbits(np.frombuffer(bytes(raw), np.uint8))
                tx.queue.append(b)
                tx.queued_bits += len(b)
                bad_at = t
                starts.append((i, t, 'bad'))
            else:
                tx.push(frame)
                starts.append((i, t, 'ok'))
            i += 1
        sym = tx.next_symbols(nsym)
        sent[k] = starts
        streams.append((k, sym, dbfs, cfo))
    tb = gr.top_block()
    add = blocks.add_cc(1)
    taps = firdes.root_raised_cosine(24, 24, 1.0, 0.35, 16 * 24 + 1)
    n_in = 0
    for k, sym, dbfs, cfo in streams:
        src = blocks.vector_source_c(sym.tolist(), False)
        interp = filter.interp_fir_filter_ccf(24, taps)
        # RRC peaks ~1.25 x the symbol amplitude after this filter
        amp = blocks.multiply_const_cc(10 ** (dbfs / 20.0) / 1.25)
        rot = blocks.rotator_cc(2 * np.pi * (CH[k] + cfo) / FS, False)
        tb.connect(src, interp, amp, rot)
        if k == 0:
            # TX IQ imbalance: y = x + b conj(x), image -45 dBc
            cj = blocks.conjugate_cc()
            bb = blocks.multiply_const_cc(10 ** (-45 / 20.0))
            tb.connect(rot, cj, bb, (add, n_in))
            n_in += 1
        tb.connect(rot, (add, n_in))
        n_in += 1
    noise = analog.fastnoise_source_c(analog.GR_GAUSSIAN,
                                      10 ** (-78 / 20.0), 3, 1 << 16)
    tb.connect(noise, (add, n_in))
    head = blocks.head(gr.sizeof_gr_complex, int(SECONDS * FS))
    sink = blocks.file_sink(gr.sizeof_gr_complex, path, False)
    tb.connect(add, head, sink)
    tb.run()
    sink.close()
    # 40 ms of samples lost at 2.6 s (an overflow)
    x = np.fromfile(path, np.complex64)
    a = int(2.6 * FS)
    x = np.concatenate([x[:a], x[a + int(0.04 * FS):]])
    x.tofile(path)
    return sent, len(x)


def main():
    work = tempfile.mkdtemp(prefix='bpsk_grc_rx_')
    path = os.path.join(work, 'rx.c64')
    t0 = time.time()
    sent, nsamp = make_recording(path)
    gen_s = time.time() - t0

    from gnuradio import blocks, gr, iio
    import pmt

    class FileRadio(gr.hier_block2):
        def __init__(self, uri, ch, buffer_size, *a):
            gr.hier_block2.__init__(self, 'file_radio',
                                    gr.io_signature(0, 0, 0),
                                    gr.io_signature(1, 1, 8))
            self.src = blocks.file_source(8, path, False)
            self.thr = blocks.throttle(8, FS, True)
            self.connect(self.src, self.thr, self)
            FileRadio.last = self

        def __getattr__(self, name):
            if name.startswith('set_'):
                return lambda *a, **k: None
            raise AttributeError(name)

    class NullRadio(gr.hier_block2):
        def __init__(self, uri, ch, buffer_size, *a):
            gr.hier_block2.__init__(self, 'null_radio',
                                    gr.io_signature(1, 1, 8),
                                    gr.io_signature(0, 0, 0))
            self.thr = blocks.throttle(8, FS, True)
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

    iio.fmcomms2_source_fc32 = FileRadio
    iio.fmcomms2_sink_fc32 = NullRadio
    iio.attr_sink = Attr
    from PyQt5 import Qt
    qapp = Qt.QApplication(sys.argv)
    os.chdir(work)
    import bpsk_duplex_pluto as fg
    tb = fg.bpsk_duplex_pluto(
        agc=1, channel=0, http_port=8297, my_addr=1, peer_addr=2,
        nickname='grc-check', security=0, telemetry=0, uri='ip:file')
    got = {0: set(), 1: set(), 2: set(), 3: set()}
    app = tb.link_layer.app
    orig = app.on_rx

    def on_rx(raw, now, channel=None):
        try:
            import bpsk_link as L
            f = L.parse(raw)
            if f['type'] == L.FT_DATA and len(f['payload']) >= 6:
                k, i = struct.unpack('!HI', f['payload'][:6])
                if channel is not None:
                    got[channel].add((k, i))
        except Exception:                               # noqa: BLE001
            pass
        return orig(raw, now, channel)
    app.on_rx = on_rx
    tb.start()
    t1 = time.time()
    src = FileRadio.last.src
    done = False
    while time.time() - t1 < SECONDS * 3 + 30:
        time.sleep(0.2)
        qapp.processEvents()
        if src.nitems_written(0) >= nsamp:
            done = True
            break
    time.sleep(1.0)
    fault = tb.link_layer._fault
    afc = [round(a.cfo, 1) for a in tb.link_layer._afc]
    tb.stop()
    tb.wait()
    res = {'sent': {}, 'got': {}, 'afc': afc, 'fault': fault, 'done': done,
           'gen_s': round(gen_s, 1), 'run_s': round(time.time() - t1, 1)}
    lost = 0
    for k in range(4):
        exp = [(i, t) for i, t, kind in sent.get(k, [])
               if kind == 'ok' and SETTLE <= t <= SECONDS - 0.05]
        rx = {i for kk, i in got[k] if kk == k}
        res['sent'][k] = len(exp)
        res['got'][k] = sum(1 for i, t in exp if i in rx)
        if k == 1:
            # the frames that follow each fault (not the ones it hit)
            bad_t = [t for i, t, kind in sent[1] if kind == 'bad'][0]
            near = [i for i, t in exp if bad_t < t <= bad_t + 0.1
                    or 2.65 <= t <= 2.75]
            lost = sum(1 for i in near if i not in rx)
    res['lost_after_faults'] = lost
    # channel 3 carries nothing: anything decoded there is wrong
    res['got'][3] = len(got[3])
    print('RESULT ' + json.dumps(res), flush=True)
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    os._exit(0)


if __name__ == '__main__':
    main()
