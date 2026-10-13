# Communication Design Project — BPSK mesh chat on ANT-SDR E200

An encrypted text/file chat mesh for ANT-SDR E200 radios (AD9361, Pluto
firmware) built on GNU Radio 3.10. Up to four nodes share one 6 MS/s band,
each transmitting on its own BPSK channel while receiving all four, with
Noise_KK end-to-end encryption, selective-repeat ARQ, automatic channel
choice and closed-loop gain control. The operator interface is a local web
page.

This is release **r6.1** (`r6.1-mesh`), which hardens r6.0.1 for real
hardware: one-sided channel plan (no IQ-image self-interference), RX gain /
TX attenuation control, a deframer that cannot freeze on a corrupted
header, a message-only node block (no lockstep overflows), latency-aware
timers, one-way link reporting instead of channel hopping, and per-second
telemetry.

| Document | |
|---|---|
| [`README_chat_app.md`](README_chat_app.md) | full manual: what changed in r6.1 and why, channel-plan math, control laws, parameters, measurements, **hardware acceptance checklist**, troubleshooting, and the r6/r5 history |
| [`R6.1_FAILURE_REPORT.md`](R6.1_FAILURE_REPORT.md) | every failure seen on the E200s with r6.0.1: root cause, fix, regression test, measured result — and what is not yet verified on hardware |
| [`dev/results/`](dev/results) | raw test, CPU and bench outputs behind the numbers |

## Quick start

Needs GNU Radio 3.10 with gr-iio (the `.py` is in the 3.10.12 layout),
Python 3, numpy, and `cryptography` (a pure-Python fallback is included).

```
python3 bpsk_keys.py init                      # once per node: key pair
python3 bpsk_duplex_pluto.py --uri ip:192.168.1.10 --nickname Ann --center-freq 910.2e6
```

The chat page opens in the browser; approve the other nodes by fingerprint
(Trust). See the README for keys, approvals and the checklist.

## Tests

```
python3 test_phy.py; python3 test_r61.py; python3 test_grc.py   # r6.1 suites
python3 test_mesh.py                                              # and the 8 older suites
BPSK_CRYPTO=pure python3 test_r61.py                              # pure-Python crypto backend
python3 bpsk_zmq_bench.py run --nodes 4 --scale 32 --seconds 600  # multi-node bench, no radios
```

All 11 suites pass with both crypto backends (360 checks each). `test_r61`
holds one or more regression checks per hardware failure; run against the
r6.0.1 tree (`BPSK_TREE=...`) they fail there.

## Status

Verified in simulation, on a GNU Radio bench (the real flowgraph with the
Pluto blocks replaced by radio models, slowed 8–32×), and on recorded
samples — including a 2-hour, 4-node, ±25 ppm soak. **Not yet verified on
the E200s**: run the hardware acceptance checklist in the README first.
