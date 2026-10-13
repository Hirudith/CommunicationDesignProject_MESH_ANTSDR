"""Run the whole r6 network with no radio attached: several nodes, one browser
tab each.

Every node is a complete ChatApp - MAC (channel and address self-assignment),
Selective Repeat ARQ per peer, Noise KK encryption per pair, gain control,
the UI server - exactly the code the flowgraph runs. They share the
bpsk_meshsim air with its r6.1 radio model: four channels, continuous
carriers, each node's own transmitter far above the others at its ADC (the
gain control has to settle it), IQ images, per-pair signal levels, decoding
by SINR, optional frame loss. So this is a faithful rehearsal for a lab demo, and a way to develop the
front end without occupying the hardware.

    python3 bpsk_offline_demo.py                  # 4 nodes, 3% frame loss
    python3 bpsk_offline_demo.py --nodes 3 --loss 0.1
    python3 bpsk_offline_demo.py --trust none     # approve every node by hand
    python3 bpsk_offline_demo.py --insecure       # no encryption
    python3 bpsk_offline_demo.py --no-rf          # r6.0 air model (no gains)

Then open http://127.0.0.1:8088/ , :8089/ , :8090/ , :8091/ side by side.

--trust  all       every node already trusts every other
         but-last  (default) all except the last node, so the Trust button can
                   be demonstrated on it from any other node's page
         none      every approval by hand, as on a fresh bench

Keys are created under <store>/<name>/keys (on hardware each node creates its
own with `bpsk_keys.py init` - a private key is never copied).
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpsk_app as app                                          # noqa: E402
import bpsk_meshsim as meshsim                                  # noqa: E402

NAMES = ['Ann', 'Ben', 'Cat', 'Dan', 'Eve', 'Fay', 'Gus', 'Hal']
NOISE_DB = -85.0            # the simulator's receiver noise level


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--nodes', type=int, default=4, help='2..8 (the air has '
                    '4 channels: a fifth node waits for a free one)')
    ap.add_argument('--loss', type=float, default=0.03,
                    help='fraction of frames each receiver loses')
    ap.add_argument('--port', type=int, default=8088,
                    help='UI port of the first node; the others follow')
    ap.add_argument('--store', default='demo_files',
                    help='where keys and attachments are kept')
    ap.add_argument('--trust', choices=('all', 'but-last', 'none'),
                    default='but-last')
    ap.add_argument('--insecure', action='store_true',
                    help='no encryption')
    ap.add_argument('--no-rf', action='store_true',
                    help='without the r6.1 radio model (own-signal coupling, '
                         'ADC, gain control, IQ images, SINR)')
    args = ap.parse_args()
    n = max(2, min(args.nodes, len(NAMES)))
    names = NAMES[:n]

    rf = None if args.no_rf else meshsim.RfModel()
    sim = meshsim.MeshSim(os.path.abspath(args.store), loss=args.loss,
                          garble=0.3, rf=rf)
    # simulated time runs on the wall clock, so what the UI server computes
    # from time.time() and what the nodes stamped agree
    sim.t = round(time.time(), 3)
    for i, name in enumerate(names):
        sim.add(name, security=not args.insecure, start=sim.t + 0.6 * i,
                tx_freq=910.2e6, rx_freq=910.2e6)
    if not args.insecure and args.trust != 'none':
        sim.trust_all(names if args.trust == 'all' else names[:-1])

    servers = [app.serve(sim.app(name), args.port + i)[0]
               for i, name in enumerate(names)]
    stop = threading.Event()
    lock = threading.Lock()

    def air():
        """Advance the simulated air to the wall clock."""
        while not stop.is_set():
            with lock:
                ahead = time.time() - sim.t
                if ahead > 0:
                    sim.run(min(ahead, 0.2))
            time.sleep(0.004)

    def telemetry():
        """Per-channel SNR from the simulated levels, for the UI tiles."""
        while not stop.is_set():
            for name in names:
                node = sim.app(name)
                lv = node.ch_levels
                for k, level in enumerate(lv):
                    if level is None or k == node.chan.channel:
                        continue
                    snr = level - NOISE_DB
                    node.set_radio_metrics(
                        snr_db=round(min(snr, 28.0), 1) if snr > 8 else None,
                        level_db=round(level, 1),
                        evm=round(100 * 10 ** (-min(snr, 28) / 20), 2)
                        if snr > 8 else None,
                        locked=snr > 8, channel=k)
            time.sleep(0.5)

    threading.Thread(target=air, daemon=True, name='air').start()
    if rf is None:          # the radio model measures the channels itself
        threading.Thread(target=telemetry, daemon=True).start()

    for i, name in enumerate(names):
        print('%-4s http://127.0.0.1:%d/' % (name, args.port + i))
    print('air: 4 channels, %.0f%% frame loss. Nodes start 0.6 s apart, listen '
          '2-4 s, then pick a channel. Ctrl-C to stop.' % (args.loss * 100))
    if not args.insecure:
        print('trust: %s' % {'all': 'every node trusts every other',
                             'but-last': '%s is new to everyone - approve it '
                                         'with its Trust button' % names[-1],
                             'none': 'approve each node by hand'}[args.trust])
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for srv in servers:
            app.shutdown(srv)
        for name in names:
            sim.app(name).close()


if __name__ == '__main__':
    main()
