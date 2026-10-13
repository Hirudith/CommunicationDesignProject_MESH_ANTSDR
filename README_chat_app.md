# BPSK Mesh Terminal · `r6.1-mesh`

r6.1 hardens the r6 mesh for real hardware. It answers every failure seen on
the ANT-SDR E200s with r6.0.1 (the report is
[`R6.1_FAILURE_REPORT.md`](R6.1_FAILURE_REPORT.md)): the receiver no longer
overloads on its own transmitter, no channel lands on another's IQ image, a
corrupted header costs one frame instead of freezing the receiver, the node
block can no longer hold the radio back, weak neighbours are tracked, and a
link that works one way only is reported, not "fixed" by hopping.

> **Not yet verified on hardware.** Everything below was measured in
> simulation, on a GNU Radio bench (`bpsk_zmq_bench.py`: the real flowgraph,
> the Pluto blocks replaced by models of the radio), or by running the real
> flowgraph on recorded samples (`dev/grc_rx_check.py`). The hardware
> checklist [below](#r61-hardware-acceptance-checklist) is the first thing to
> run on the E200s.

r6.1 does **not** interoperate on the air with r6.0.x (different channel
plan and sample rate) — upgrade every node. Keys, approvals (`trusted.json`)
and the UI carry over.

## r6.1 — what changed and why

| Area | r6.0.1 | r6.1 | Why |
|---|---|---|---|
| Channel plan | 3 MS/s, two-sided: −900, −300, +300, +900 kHz | **6 MS/s, one-sided: +450, +1050, +1650, +2250 kHz** | every channel's IQ image fell on another channel (0↔3, 1↔2); now all images land at negative frequencies, where there is none |
| Gain | manual sliders, fixed | **closed loop**: RX gain keeps the ADC peak below −6 dBFS; TX attenuation keeps every neighbour near 15 dB SNR | the own transmitter clipped the ADC (61.8 % of samples at full scale on node A) |
| Receive chain | FLL band-edge → RRC → symbol sync → Costas, no AGC | AFC (Python estimate, rotator) → **AGC** (bounded) → RRC → symbol sync → Costas | the loops' gains scale with signal amplitude: r6.0.1 decodes **nothing** from a neighbour 30 dB below full scale; FLLs cost 0.54 core |
| Deframing | `correlate_access_code_bb_ts` → `repack_bits_bb` → `tagged_stream_to_pdu` | numpy deframer, 8192 bits per Python call | GNU Radio 3.10.12 reads all 16 length bits: one symbol error turned 13 bytes into 12301 and the channel waited for 98408 bits |
| Node block | read 4 symbol streams + the ADC (stream inputs) | **message-only**: snapshots arrive as PDUs from C++ taps | independently clocked streams read together throttle the fastest one until the radio overflows |
| Transmit | `pdu_to_tagged_stream` queue, ~200 IDLE frames/s, open-loop airtime model | a framer block **pulls** frames, PRBS-15 between them, reports its real backlog every 50 ms | the queue overflowed (`pdu_to_tagged_stream … Message discarded`) and every receiver decoded 200 IDLE frames/s in Python |
| One-way links | "nobody hears this node" → move channel | silence checks first (spread out, repeated, every other one extended, so two nodes on one channel cannot hide each other); clear channel → a **link fault**: reported with the reason, no move | hopping cannot fix an overloaded receiver |
| Channel choice | random free channel | among free channels, the one where the neighbours' receivers report the least interference (within 3 dB: random) | — |
| Timers | fixed ACK (0.5 s) and handshake (1 s) timers | follow the measured echo (own beacons): ACK wait + 2 × echo, handshake retry ≥ 2 × round trip | a 4.6 s echo (node B) retransmitted every frame and never finished a handshake |
| Sessions | — | a node that keeps receiving frames for a session it does not hold re-keys | crossed handshakes on a slow link left two nodes on mismatched sessions until the 10-minute rekey |
| Instrumentation | ADC tile | **O/U counters** (gr-iio's `O`/`U`), two-way SNR table in beacons, receive-chain liveness, per-thread CPU, AFC offsets, rejected headers, per-second CSV telemetry, constellation and spectrum in the browser | measure first |

### Channel plan math

```
radio_rate fs = 6 MS/s (Pluto RX and TX), RF bandwidth 5.4 MHz (0.9 fs)
channel k at  f_k = ch_first + k * ch_spacing = 450 + 600 k  kHz   (k = 0..3)
              = +450, +1050, +1650, +2250 kHz above center_freq
occupied      250 ksym/s * (1 + 0.35) = 337.5 kHz;  guard 262.5 kHz
IQ image of k at -f_k: -450 ... -2250 kHz  -> no channel there
LO / DC leakage at 0 Hz: 450 - 168.75 = 281 kHz from channel 0's edge
band edge     2250 + 168.75 = 2418.75 kHz = 81 % of fs/2 (3 MHz)
receive       freq_xlating FIR, 71 taps (pass +-175 kHz, stop from 425 kHz,
              65 dB), decimation 6 -> 1 MS/s = 4 samples/symbol per channel
transmit      RRC, 16-symbol span, interpolation 24 straight to 6 MS/s,
              digital back-off 0.35 (symbol peaks about -7 dBFS), then a
              rotator to f_k (tx_ctl message)
```

Measured (numpy, the shipped taps): adjacent-channel leakage of the
transmitter at ±600 kHz **−67.6 dB**, alternate −74.1 dB, PAPR 4.1 dB —
before the Pluto's own analog stages, whose compression adds regrowth
(−52.7 dB at 1 dB compression in the same model). After the receiver's
channel filter and matched filter, a transmitter one channel away is
68.7 dB down.

### Control laws

**RX gain and TX attenuation** (`bpsk_gain.py`; standard library only):

```
RX loop, every 0.5 s, from the ADC peak and clipped fraction of the last
1 s of raw-receiver snapshots (8192 samples every 50 ms):
  clipping (> 0.05 % of samples) or peak > -6 dBFS -> RX gain down at once by
      the overshoot above -12 dBFS, 3..12 dB (12 dB if > 1 % clips)
  peak in (-9, -6] dBFS                               -> RX gain -1 dB
  peak in [-20, -9] dBFS                              -> nothing (hysteresis)
  peak < -20 dBFS for 2 s, readings taken with the own
      TX on (for more than 2 x echo + 0.5 s)          -> RX gain +2 dB
  clipping with RX gain at its minimum                -> TX attenuation +3 dB
TX loop, every 2 s, from the neighbours' beacons (each reports the SNR at
which it hears this node; the median of its last three reports is used):
  weakest neighbour < 13 dB, ADC peak < -11 dBFS,
      RX gain >= rx_min + 10 dB                       -> TX attenuation -2 dB
  every neighbour > 23 dB                             -> TX attenuation +1 dB
  in between                                          -> nothing
  a neighbour reporting itself overloaded/impaired is left out; one that
  does not list this node at all counts as 0 dB, but then power goes up at
  most 12 dB above the start (a broken antenna is not path loss)
bounds RX 0..70 dB, TX attenuation 10..89 dB; at most one change per
period; readings within 0.25 s of a change are ignored
```

Start values: `rx_gain` 30, `tx_atten` 50 (`--rx-gain`, `--tx-atten`). Manual
override: the ± buttons in the *Radio* pane, `/gain rx 24`, `/gain tx 55`,
`/agc off` (`/agc on` resumes), or `POST /api/gain {"rx":24,"tx":55,"auto":false}`;
`--agc 0` starts with the loop off. On the radio the values become
`hardwaregain` on `ad9361-phy` `voltage0` (RX: `+gain`, TX: `-attenuation`)
through two IIO Attribute Sinks.

**Per-channel AGC** (`analog.agc3_cc`, attack 1.0, decay 1e-4, max gain 1e5).
With attack 1.0 the gain is updated from each sample *before* it is scaled,
so no output sample can exceed 1.0. The usual `agc2_cc` scales with the old
gain first: on the bench, a transmitter switching on after silence produced
samples of ~2500 and wedged `symbol_sync_cc` (its instantaneous period is not
limited; it then spins without consuming), which back-pressured all four
channels into a receiver stall. Symbol sync: loop bandwidth 0.02 (was 0.045),
maximum deviation 0.01 samples (was 1.5: noise walked the clock to the limit).

**AFC** (`bpsk_phy.Afc`, per channel, on 4096-sample snapshots every 0.1 s):
the squared signal has a line at twice the carrier offset; an update needs two
consecutive estimates within 0.15 % of fs, a residual above 0.015 % of fs, a
result inside ±6 % of fs (±60 kHz; an estimate outside is refused, not
clipped), and 0.45 s since the last update. A channel without a BPSK line for
10 s drops its correction. Estimates are within 1 Hz from 0 to ±55 kHz
(two ±25 ppm crystals at 910 MHz are up to 45 kHz apart).

**Link faults.** A neighbour "hears" this node while its beacons have listed
it within the last 5 s, plus twice the extra length of a check on a lagging
host (5 s at a normal echo, 8 s at a 3 s echo). When **no** neighbour has
listed it for 4 s — and for at least a round trip, 2 × echo + 2 s — the node
checks its own channel for another transmitter before concluding anything:

```
check length   silent until the own signal has drained (echo + 0.1 s), then
               0.35 s of listening; 0.5 s at least, echo + 0.65 s (>= 2 s,
               <= 8 s) at most   (r6.0: 2 s at most - on a slow host it
               never heard a clean moment)
first check    after a random 0 .. (1 s + check length)
repeats        while the node stays unheard, every (1..2) x (1 s + check
               length), x1, x2, then x4
extended       check n stays silent (1 s + check length) + echo + 2.5 s
               longer when bit n of the node's address is 1: two nodes that
               claimed one channel at the same moment are unheard from the
               same moment and would otherwise check together, each hearing
               the other one silent; their addresses differ in some bit, so
               within a few checks one of them outlasts the other's check and
               hears it come back
seen           a check counts only if it saw the channel: two level readings
               of it once the own signal had drained (decoded frames on a
               lagging host)
another transmitter heard -> a collision: move, as before
link fault     after 2 clear checks that saw the channel, one of them
               extended (or 4 plain ones): the node stays, reports
               "LINK FAULT", keeps checking (backed off) while unheard
alone          a node that has heard no neighbour for 3 s, 10 s + 2 x echo
               after taking its channel, has no link to lose: it runs the
               same checks (spread, repeated, extended by the address bits),
               and they last until they have seen the channel (up to 8 s
               more) - two nodes alone on one channel decode nothing from
               each other, so nobody is "deaf" and only the checks can part
               them; no link fault is reported (there is nobody to report)
```

The node then says, per neighbour, why it is not heard (its receiver is
overloaded / reports a fault or slow host / sees no signal on this channel —
check this node's antenna / sees the carrier but cannot decode it), at most
every 30 s, and handshakes to a node that cannot hear it are retried slowly.
The own channel's level while transmitting (to tell the own signal's tail
from a second node during a check) is learnt only from readings that can
show it: taken after the echo, and clearly above the quietest channel.

### Instrumentation

- **Header tiles**: SNR, RX level, ADC peak, **O / U** (gr-iio's overflow and
  underflow marks, counted from the process's stderr), throughput, frame
  errors, backlog.
- **Radio pane**: RX gain and TX attenuation with ± buttons and the
  *automatic* switch, the last change and its reason, receive-chain lights
  (red = that channel's chain delivered nothing for 3 s), overflows,
  DSP CPU and the busiest threads, AFC offset per channel, rejected headers
  per channel, transmit queue, a constellation (the selected node's channel)
  and the receiver spectrum.
- **Links pane — the two-way table**: per node, *I hear* (SNR on its
  channel, here) and *hears me* (the SNR it reports for this node in its
  beacons), and the link state: `ok`, `not approved`, `no encryption`,
  `can't hear you` (with the reason), `no response` (handshakes unanswered),
  `securing…`, `away`.
- **Beacons** carry an r6.1 extension after the r6.0 fields (r6.0 parsers
  ignore it): flags (gain control on, overloaded, impaired), ADC peak, RX
  gain, TX attenuation, the four channel levels, and per neighbour its
  address, SNR (0.5 dB steps) and level. Version byte 1; an unknown version
  is ignored.
- **Telemetry**: `telemetry/node_<nick>_<time>.csv`, one row a second
  (state, channel, gains, ADC, O/U totals and per second, transmit backlog,
  airtime-model error, echo, CPU and top threads, frame counts, per channel
  level / SNR / EVM / AFC offset / frames / CRC fails / rejected headers /
  liveness, fault, memory) and `…_peers.csv` (per neighbour: state, both
  SNRs and levels, frame error rate, retransmissions, bytes, reason).
  `--telemetry 0` turns it off.
- **Liveness**: a receive chain that delivers nothing for 3 s (or four
  snapshot intervals, on the slowed-down bench) is named in the log, the
  chat and a red banner; "no samples from the radio" when nothing arrives
  at all.

### Parameters new in r6.1

| Parameter | Default | |
|---|---|---|
| `radio_rate` | 6e6 | must be a multiple of `ch_spacing`; the plan scales with it |
| `ch_first` | 450e3 | offset of channel 0 above `center_freq` |
| `rx_gain` / `tx_atten` | 30 / 50 | start values for the gain control (dB) |
| `agc` | 1 | 0 = gain control off (the start values stay) |
| `rx_buffer` / `tx_buffer` | 65536 / 49152 | Pluto buffers (11 / 8 ms at 6 MS/s) |
| `telemetry` | 1 | per-second CSV |
| `plan_file` | empty | optional static plan, `{"channels": {"<fingerprint or nickname>": k}}` — a listed node takes channel k and keeps it, as with `--channel k` (no channel checks); an unlisted node chooses automatically |

### Measured (bench and simulation — not hardware)

| What | r6.0.1 | r6.1 | How |
|---|---|---|---|
| CPU, whole node process | 1.80 cores at **3** MS/s (FLLs 0.56, TX resampler 0.28, node block 0.19) | **1.42 cores at 6 MS/s** (DSP blocks + deframers 1.25, node block 0.17; 1.52 in an earlier run) | the real flowgraph on a recorded 4-carrier signal, Pluto blocks replaced by a paced file (65536-sample buffers) and a paced null sink, whose own 0.09 / 0.19 core is subtracted; this cloud machine (Xeon, 2 vCPUs), `dev/cpu_check.py`, output in `dev/results/cpu_*.txt` |
| Memory | grows (444 MB/h over 30 s) | 60 MB/h over 30 s, i.e. +0.5 MB, within noise; 2-h soak below | same harness |
| Neighbour 40 / 55 dB below full scale, +18 kHz offset | 0 / 0 of 79 frames | 78 / 78 of 79 | `test_grc.py` G7 |
| Corrupted length field | 15 of 30 following frames lost | 0 lost | `test_grc.py` G4 |
| Whole flowgraph on recorded samples: own TX at −8 dBFS with a −45 dBc image, neighbours at −38 dBFS/+18 kHz and −53 dBFS/−21 kHz, a corrupted header, 40 ms of samples missing | — | 302/307 and 303/308 frames (the 5 lost are inside the 40 ms gap), AFC +18000 / −21000 Hz, no fault | `dev/grc_rx_check.py` (`test_grc.py` G6) |
| 2 nodes, ±25 ppm, 300 s, bench at 1/8 speed | — | 21.5–22.5 dB both ways, FER 0, 34/34 group messages, 0 overflows, 0 channel moves | `bpsk_zmq_bench.py`, `dev/results/bench_two_ppm.json` |
| 4 nodes, 300 s, bench at 1/32 speed (code before the channel-check fixes; see the long runs below) | — | all 12 links 19–32 dB, FER 0, 22/22 group messages with 3/3 receipts, 0 overflows, 0 channel moves | same |
| Faults (2 nodes, 1/8 speed): 3 % corrupted headers / 2 % buffer drops / one receiver stalled 12 s | — | headers: 30 rejected, 14/14 messages, FER 0. Drops: counted as overflows (105), FER 0.7 %, 14/14. Stall: no freeze; the stalled node reports it and recovers, the other reports a link fault and keeps its channel; 16/17 messages (the one sent into the stall fails) | same, `dev/results/bench_{badhdr,drops,stall}.json` |
| **2-h soak**, 4 nodes, ±25/±20 ppm, 1/32 speed, code as shipped | — | 0 overflows / underflows; 0 channel moves after start-up (two start-up collisions resolved in the first minute); all 12 links 20–29.5 dB, FER 0, `ok` every second after the first 2 minutes (one second of `away` in 2 h); **1163/1163 group messages delivered with all receipts**; 0 transmit-queue drops; 0.27 core per node; memory 157 → 160–161 MB per node (1.5–2 MB/h). Earlier versions: 111 min (memory growth slowing, ~3 → ~1 MB/h) and 27 min | `dev/results/bench_soak_4node_2h.json`, `…_111min_prefix.json`, `…_final_27min.json` |

Bench timing is not hardware timing: at `--scale S` every buffer holds S
times as much time, so echo, session set-up and receipt times are S times
longer than they will be on the radios, and the snapshot taps (levels, AFC,
constellations) deliver S times less often — at 1/32 a channel check sees a
level reading only every 3.2 s, where the radio gives ten a second. Every
node on the bench therefore runs as a "slow host" (echo ~3 s).

## r6.1 hardware acceptance checklist

Run on the E200s, antennas 1–3 m apart, every node on r6.1.

1. **Keys and start.** As r6 (`bpsk_keys.py init` once per node). Start each
   node: `python3 bpsk_duplex_pluto.py --uri ip:192.168.1.10 --nickname Ann
   --center-freq 910.2e6`. Do not touch the gain sliders.
2. **Console in the first 10 s**: no `O`/`U` after the first few seconds; the
   *O / U* tile stays green. `RX gain -> … (ADC peak …)` lines are the gain
   control settling — expect a few.
3. **Radio pane**: ADC peak between −20 and −9 dBFS, clipped 0 %; chains 0–3
   green; DSP CPU ≤ 1.5 cores; the AFC offsets of the other nodes' channels
   within ±50 kHz and steady.
4. **Approve** each other (fingerprints, Trust). Session up < 2 s after both
   sides pressed Trust (the lock turns green).
5. **Links pane, two nodes**: *I hear* and *hears me* ≥ 15 dB both ways,
   state `ok`; frame error tile < 1 %.
6. **Four nodes**: all 12 directed links ≥ 12 dB (each node's Links pane
   shows its 3 + 3); a group message every few seconds for 10 minutes gets
   ✓✓ from all three receivers; no channel changes in the log after the
   first minute; no `O` over 30 minutes after the first 10 s; the UI and the
   console stay responsive.
7. **Faults**: disconnect one node's TX antenna — that node shows *can't
   hear you* for every neighbour ("… sees no signal on channel k"), logs a
   LINK FAULT and keeps its channel. Pull one E200's USB/Ethernet for 5 s —
   that node shows a red "no samples from the radio" banner; the others
   report a link fault for it and keep their channels. (Whether gr-iio
   resumes by itself after the cable is back is untested — restart that
   flowgraph if it does not.) Nobody else is affected.
8. **Keep** each node's `telemetry/` folder and the console logs of a 30-min
   run: they are the evidence (and what to send if something fails).

## r6.1 troubleshooting

| You see | It means | Do |
|---|---|---|
| *O / U* tile red, `O` on the console | samples lost: the computer did not keep up | check the DSP CPU in the Radio pane and `top`; plug the laptop in, performance governor, close other programs; on a weak machine, close the GRC Qt window (the browser UI shows the same) |
| ADC tile red / "receiver overloaded" | own transmitter too strong for this receiver | normally the gain control fixes it within seconds; if RX gain is already 0 dB it raises TX attenuation. If it persists: more TX–RX antenna separation |
| *can't hear you* with "sees no signal on channel k" | the neighbour does not see this node's carrier | this node's TX antenna/cable, TX attenuation (Radio pane), or `--tx-atten` too high |
| *can't hear you* with "receiver is overloaded" | the neighbour's ADC clips | look at **that** node's Radio pane |
| *can't hear you* with "sees the carrier but cannot decode it" | signal there, decoding fails | carrier offset (AFC values), interference, or a host too slow on that node |
| *no response* | handshakes unanswered | has that node approved this one? |
| red banner "receive chain of channel k stalled" | that channel's demodulator delivered nothing for 3 s | send the console log; the other channels keep working |
| red banner "no samples from the radio" | nothing from the E200 | USB/Ethernet, `iio_info -u ip:…` |
| "hears its own transmission X s late" | the host lags the sample stream | as for `O` |
| rejected headers counting up on one channel | corrupted frames are being discarded (harmless) | only a sign of a poor link if FER rises too |

## r6.1 files

| File | |
|---|---|
| `bpsk_phy.py` | **new**: deframer, TX framer/PRBS filler, channel plan, CFO estimate and AFC, EVM/level/ADC/spectrum measures, O/U counting (numpy only) |
| `bpsk_gain.py` | **new**: the gain control law (standard library only) |
| `bpsk_duplex_pluto.grc` / `.py` | regenerated (`dev/make_grc.py`), 3.10.12 layout; embedded blocks `bpsk_duplex_pluto_deframer_ch0..3.py`, `…_tx_framer.py`, `…_link_layer.py` are byte-identical to the sources in the `.grc` |
| `bpsk_duplex_pluto_link_layer.py` | the node block, message-only; stderr O/U tap, liveness, CPU, AFC, views |
| `bpsk_app.py`, `bpsk_mac.py`, `bpsk_link.py`, `bpsk_secure.py` | link report, link faults, channel scoring, gain/telemetry, latency-aware timers, session resync |
| `bpsk_meshsim.py` | radio front-end model (self-coupling, ADC clipping, gain, IQ image per plan, ACLR, SINR-based decoding, receive and transmit latency, slow transmitter, level-reading cadence) |
| `bpsk_zmq_bench.py` | the bench: per-receiver air model, ±ppm, ADC/DAC models, fault injection, summary |
| `test_phy.py`, `test_r61.py`, `test_grc.py` | **new** suites (below) |
| `dev/` | `make_grc.py` (generator), `epy_*.py` (embedded block sources), `grc_rx_check.py`, `cpu_check.py` |

### Tests

```
python3 test_phy.py      # 53 checks: deframer, PRBS, TX framer, plan, AFC, measures, O/U, gain law
python3 test_r61.py      # 25 checks: one or more per hardware failure (F1-F7), wire format, telemetry, rules
BPSK_TREE=/path/to/r6.0.1 python3 test_r61.py   # the same checks against r6.0.1: they fail there
python3 test_grc.py      # 24 checks, GNU Radio: embedded sources, compiled .py, old vs new blocks, the whole flowgraph on samples
```

All 11 suites (the 8 from r6 and these 3) pass with both crypto backends
(`BPSK_CRYPTO=pure`): 360 checks each (`dev/results/suites_both_backends.txt`).
On r6.0.1, `test_r61` fails 19 of its checks (every F, W and T check) and
passes the two rule checks R1/R2, which hold in both
(`dev/results/test_r61_on_r6.0.1.txt`).

### Phase 3 (star FDD fallback)

Not started. It is the fallback if in-band full duplex still fails the
acceptance criteria on the E200s after this release, and it starts only on
your go-ahead.

---

# r6.0 / r6.0.1 — the mesh as first released

> **Background.** The r6.1 section above supersedes this one for the RF plan,
> gain staging, the receive chain, CPU figures, and how a node reacts when
> nobody hears it. Keys, approval, the MAC, the link layer, security and the
> UI basics below still apply.


r6 turns the two-node link into a network: **any number of nodes in range,
four of them transmitting at once**, each able to chat 1:1 with any other or
with everyone, every pair end-to-end encrypted with its own keys. Nothing is
configured per node except its nickname: addresses come from the node's key,
channels are chosen automatically, and a new node is admitted by comparing a
key fingerprint and pressing **Trust**.

```
                      one centre frequency, 3 MS/s, 4 channels
      ch 0            ch 1            ch 2            ch 3
   -900 kHz        -300 kHz        +300 kHz        +900 kHz
  [ node A ]      [ node B ]      [ node C ]      [ node D ]     <- each node TRANSMITS on one
  <------------------ every node RECEIVES all four ----------------->
```

| Layer | r6 | File |
|---|---|---|
| PHY | the r5 DBPSK modem, 250 ksym/s, ×4: channelised receive (four channel filters + four demodulators), transmitter moved onto its channel digitally | `bpsk_duplex_pluto.grc/.py` |
| MAC | FDMA with self-assigned channels (listen, claim, check) and self-assigned addresses; 1 Hz beacons | `bpsk_mac.py` **new** |
| Link | the r5.4 Selective Repeat engine, one per peer (`LinkMux`), sharing the node's channel round-robin | `bpsk_link.py` |
| Security | Noise KK per pair, as r5.3; trust is now attached to keys (trust on first use in the UI) | `bpsk_secure.py`, `bpsk_keys.py` |
| App | peers, 1:1 and group conversations, per-recipient receipts, trust API | `bpsk_app.py`, `chat_ui.html` |

r6 does not interoperate with r5.x on the air (different PHY plan). Setting
`my_addr` **and** `peer_addr` gives a two-node *static mode* that behaves like
r5.4 on the r6 flowgraph (see [Static mode](#static-mode)).

## r6.0.1 — fixes from the first hardware run

Same wire format as r6.0 (they interoperate), but upgrade every node: two of
these are receiver freezes.

| Symptom on hardware | Cause | Fix |
|---|---|---|
| `block_executor :error: sched: <block repack_bits_bb> is requesting more input data than we can provide`, then the node hears nothing | A corrupted header announces up to 65535 bytes; `repack_bits` waits for that many bits, more than its 64 KiB buffer. One branch that stops consuming **freezes all four channels and the radio source** | 1 MiB output buffer on each Access Code correlator, 128 KiB on each repack. (r5.4 on GNU Radio 3.10.12 has the same latent bug: set *Min Output Buffer* of its Correlate Access Code block to 1048576.) |
| Streams of `O` on the console, watchdog "no tick" warnings | The node block and the 4-input constellation sink took equal amounts from all four channels. Each channel's symbol clock follows its transmitter's crystal, so the fastest channel was throttled to the slowest; the backlog reached the radio source, which overflowed and dropped samples on every channel | The node block consumes each input on its own; one constellation sink per channel |
| A node hears the other, the other never hears it; endless "nobody hears this node" channel changes | The other node's receiver is overloaded or frozen; hopping cannot fix that | After 2 moves the node stays and says which node cannot hear it. Each node in the sidebar shows **doesn't hear you** when its beacons do not list you |
| Other node's SNR near 0 dB; better when TX attenuation is raised | The node's own transmitter overloads its own receiver (it is inside the 2.4 MHz receive band) | New **ADC peak** tile and an "receiver overloaded" warning; follow *Gain staging* below |

### Gain staging (do this once per bench layout)

Every node receives its own transmitter far stronger than anyone else, in
the same band. The ADC must not clip on it.

1. Per node: separate TX and RX antennas by ≥ 50 cm (an SMA extension on the
   RX antenna); nodes 1–2 m apart.
2. Start every node at `tx_atten` 60, `rx_gain` 30.
3. Watch the **ADC peak** tile: keep it below −6 dBFS (green). Red means
   clipping: raise `tx_atten` or lower `rx_gain`.
4. Raise `rx_gain` in 5 dB steps while the other nodes' SNR (Channels panel)
   improves; stop when it no longer does, or when the ADC tile turns amber.
5. Lower `tx_atten` on all nodes in 5 dB steps while every node's SNR of the
   others improves and its ADC tile stays green; back off 5 dB.
6. Target: every node's SNR of every other above 15 dB, nobody marked
   *doesn't hear you*.

**Cabled alternative** (removes the antenna-coupling question entirely):
every TX1 into a 4-way combiner, 30–40 dB attenuator, 4-way splitter into
every RX1. Each node then hears itself at the same level as the others.

### If `O` still appears

`O` = the computer did not take the receiver's samples in time (r6.0.1 no
longer causes this itself). One node needs about 2 cores at 3 MS/s
(measured: four receive chains 1.5, transmitter 0.5; the FLL is the largest
single block). Check `top`; set the CPU governor to performance and plug the
laptop in; close other programs. A few `O`s at start-up are harmless.

## Quick start (lab)

**1. A key per node** (once; the private key never leaves the machine):

```
python3 bpsk_keys.py init
```

It prints this node's **fingerprint** and its derived address. A node that
already has exactly one r5 key (`keys/node_<addr>.key`) keeps it as its
identity — nothing to do. Every key listed in an r5 `roster.json` counts as
already approved.

**2. Start every node with the same centre frequency:**

```
python3 bpsk_duplex_pluto.py --uri ip:192.168.1.10 --nickname Ann --center-freq 910.2e6
```

In GRC: leave `my_addr` and `peer_addr` at `0`, set `center_freq`, `uri` and
`nickname`. Start the nodes in any order.

**3. Approve each other.** Each page shows *this node* with its key
fingerprint at the top left, and every node in range below it within ~5 s. A
new node carries a dashed box with its fingerprint: check it against the
fingerprint on that node's own screen (read it out), then press **Trust** —
on both nodes. The lock turns green when the session is up (well under a
second). Approvals are stored in `keys/trusted.json` and survive restarts.
Pre-approving without the UI: `bpsk_keys.py trust --key <hex> --name Ben`.

**4. Chat.** Pick a node on the left for a private conversation, or
*Everyone in range* for the group. Files go 1:1 (pick a node, then attach).

Without hardware: `python3 bpsk_offline_demo.py` runs four complete nodes on a
simulated air at `http://127.0.0.1:8088/ … :8091/` (see
[Testing](#testing-r6)).

## RF plan and hardware notes

| | |
|---|---|
| Pluto sample rate | 3 MS/s, TX and RX (`radio_rate`); RF bandwidth 0.8 × that |
| Channels | 4 at (k − 1.5) × 600 kHz: −900, −300, +300, +900 kHz (`ch_spacing`) |
| Per channel | 250 ksym/s, RRC α = 0.35 → 337.5 kHz occupied, 262.5 kHz guard |
| Channel filter | 49-tap low-pass, decimation 3 → 1 MS/s = the r5 receive chain unchanged |
| Transmitter | modulator at 12 samples/symbol, multiplied by a Signal Source at the channel offset (`tx_ctl` message port) |
| Both LOs | `center_freq`, identical on every node |

- **In-band full duplex.** A node transmits on its channel while receiving the
  other three a few hundred kHz away, on the same LO. Use separate TX and RX
  antennas, follow *Gain staging* (r6.0.1 starts at `tx_atten` 50, `rx_gain` 30) so that the own
  channel does not clip (the *Channels* panel shows each channel's level; the
  constellation sink shows all four). Keep the gain **manual**: the MAC
  compares channel powers, which an AGC would falsify.
- **DC and LO leakage** fall between channels (an even channel count puts
  none at 0 Hz); `bbdc`/`rfdc` stay on.
- **IQ image.** A strong signal on channel k leaves a −40…−50 dBc image on
  the mirror channel (0↔3, 1↔2). The MAC ignores any level more than 30 dB
  below the strongest channel, so images and adjacent-channel leakage do not
  count as occupancy.
- **CPU.** Measured here: the four-channel receiver needs 1.3 cores, the
  transmitter 0.4 cores at 3 MS/s — plan on two free cores per node, plus the
  Qt sinks. If the host falls behind, the node notices (its own beacons come
  back late) and says so in the chat: *"this node hears its own transmission
  X s late"*. Close the scopes, or give it a faster machine.
- **USB.** 3 MS/s each way is 24 MB/s of 16-bit samples, within USB 2.0; the
  E200's Ethernet has room to spare.
- `preamble_size` defaults to 64 (tested at r5.4); the buffers default to
  65536 RX / 24576 TX samples (22 ms / 8 ms at 3 MS/s).

## How the MAC works

**Channels.** A starting node transmits nothing for a random 2–4 s and notes
which channels are in use. It then claims a free one at random, starts its
carrier, and beacons once a second. A channel counts as *in use* if, within
the last 1.5 s,

- a frame from another node was decoded on it, or
- three frames on it failed their CRC (what overlapping carriers produce), or
- it carried **energy**: two readings in a row 10 dB above the quietest
  channel of the same reading and within 30 dB of the strongest.

Two nodes can still claim the same channel if they finish listening within
the ~0.1 s it takes to hear each other. That is caught four ways:

| Check | When | Catches |
|---|---|---|
| Silence | 3 times at 1.5–5 s after a claim, then every 20–40 s: stop for 0.35–0.5 s and listen to the own channel | a decoded frame, CRC failures, or energy on it |
| Capture | while transmitting | a frame from another node decoded over the own carrier |
| Beacon | on every beacon | another node announcing the same channel — the larger key moves |
| Deafness | continuously, 4 s after a claim | every beaconing node omits us from its neighbour list → we are not being heard |

A silence check starts judging only once the node's own signal has drained
out of the buffers: the node measures that continuously, as the time its own
beacons take to come back to it. The node then relistens (0.3–1.5 s) and
claims a different channel. Four nodes that claimed the *same* channel at the
*same* instant sort themselves out in 3–4 s in simulation (every one of 18
seeded pile-ups). A fifth node finds every channel busy, keeps receiving, and
takes the first channel that frees up (within ~3 s).

**Addresses.** `address = 1 + SHA-256("bpsk-addr" ‖ public key ‖ n) mod 254`,
n = 0 normally. When a beacon shows another key using our address, a node
that is still listening yields; between two transmitting nodes the larger key
yields. The yielding node increments n, keeps it in `keys/node.state`, and
rebuilds its sessions from the new address. Other nodes follow automatically
(a beacon from a known key at a new address).

**Beacon** (link type `0xB`, broadcast, never sealed):

| Bytes | Field |
|---|---|
| 0 | version (1) |
| 1 | channel |
| 2 | n (address derivation counter) |
| 3 | flags: bit 0 = identity is an X25519 public key |
| 4–35 | identity: the public key (a random persistent id with security off) |
| 36, … | nickname length, nickname (UTF-8, ≤ 32 bytes) |
| then | M, then M addresses decoded in the last 3 s (the neighbour list) |

## Link, security, application

- **Per-peer ARQ.** `LinkMux` keeps one r5.4 Selective Repeat engine per peer
  (own window, SACKs, file transfer both ways, receive window, epoch) and
  serves them round-robin on the node's one channel. Transfers to different
  peers run side by side.
- **Group messages** are sent to every approved node in range as separate,
  separately encrypted 1:1 copies (app type 3), so the group has exactly the
  privacy of its pairs, and each copy has its own receipt: the sender sees
  `Ben ✓✓ Cat ✓✓ Dan ⚠`, and *partial* if not everyone acknowledged.
- **Messages to a node not yet in range** wait up to 60 s for it instead of
  failing.
- **Trust.** Every key is trusted by fingerprint, not by address:
  `trusted.json` (UI approvals), plus any `roster.json`. The address book
  (address → key) is learnt from beacons; SecureLink starts or accepts a
  handshake only for a key that is approved **and** currently bound to that
  address. The Noise KK prologue carries both addresses, so a session is
  valid only between those two addresses and those two keys; the HS\_INIT
  replay guard now follows the key, not the address.
- **Carrier filler** tops the transmit queue up to `queue_ahead` every tick.
  r5.4 sent at most one idle frame per 10 ms tick; with a 64-byte preamble an
  idle frame lasts ~5 ms, so the carrier was off half the time.

### Threat model: what r6 adds

Everything in [the r5.3 threat model](#threat-model) still holds per pair.
New, because nodes now announce themselves:

- Beacons are public and unauthenticated: anyone in range learns which nodes
  are present, their nicknames, keys, addresses and channels.
- A forged beacon can claim a nickname or an address. It cannot claim a key it
  does not hold: a session needs the private key, and **only the fingerprint
  identifies a node** — which is why approval compares fingerprints, never
  nicknames. A forger can disrupt (make an address look taken, rebind an
  address for a moment), which is denial of service, as jamming always was.
- An approved node cannot read or forge traffic between two other nodes
  (tested: `test_mesh.py isolation`, and `test_secure.py` for the pair level).

### UI

- **Left**: this node (address, channel, key fingerprint), *Everyone in
  range*, and every node heard: address, channel, presence, a lock (green =
  session up, amber = keying, red = not approved), unread counts, and the
  Trust box for new nodes.
- **Header**: the selected conversation — its node, channel, security state;
  SNR and level tiles show that node's channel.
- **Transcript**: per conversation; group entries show one receipt per node.
- **Panel**: *Channels* (level bar, occupant and SNR of each channel, claims,
  moves, silence checks, echo latency), *Links* (per peer: in flight,
  retransmissions, bytes), then the r5 panes.
- API additions: `POST /api/send {"text", "to"}` (`to` = a node id or `all`),
  `POST /api/upload` with `X-To`, `POST /api/trust {"peer", "trust"}`,
  `GET /api/peers`. Console commands: `/peers`, `/trust <node>`,
  `/untrust <node>`, `/msg <node|all> <text>`.

## Parameters (GRC and command line)

| Parameter | Default | |
|---|---|---|
| `center_freq` | 910.2e6 | the same on every node (replaces `tx_freq`/`rx_freq`) |
| `my_addr` | 0 | 0 = derived from the key; a number fixes it |
| `peer_addr` | 0 | 0 = mesh; set it with `my_addr` for static two-node mode |
| `channel` | −1 | −1 = automatic; 0–3 fixes the transmit channel (no checks) |
| `ch_spacing` | 600e3 | channel spacing, Hz |
| `radio_rate` | 3e6 | Pluto sample rate; each channel gets `radio_rate/3` |
| `nickname` | empty | shown to the other nodes; empty = "Node ‹address›" |
| `preamble_size` / `postamble_size` | 64 / 64 | bytes |
| `rx_buffer` / `tx_buffer` | 65536 / 24576 | Pluto buffers, samples |
| `security`, `keys_dir`, `arq_window`, `http_port`, `uri` | as r5.4 | |

## Static mode

`--my-addr 1 --peer-addr 2` (and `2`/`1` on the other node) gives r5.4
behaviour on the r6 flowgraph: fixed addresses, one peer, keys from
`roster.json`, no beacons, no channel changes. The transmit channel is
`my_addr − 1` (mod 4) unless `channel` is set. Both nodes still use one
`center_freq`. All r5.x test suites run against this mode unchanged.

## Measured

Simulation (`bpsk_meshsim`, 64-byte preamble, 256-byte fragments, encryption
on), file goodput after the channels have settled:

| Traffic | Per transfer | Total |
|---|---|---|
| one pair, one direction | 131 kb/s | 131 kb/s |
| … with 5 % frame loss | 121 kb/s | 121 kb/s |
| two pairs at once | 131 kb/s | 263 kb/s |
| four transfers at once (two pairs both ways, or a ring) | 104 kb/s | 417 kb/s |
| three nodes sending to one | 130 kb/s | 389 kb/s |
| one node sending to three | 44 kb/s | 133 kb/s |

The last two rows are the shape of FDMA: a node receives all four channels at
once, but has only its own to send on.

Start-up: four nodes started 0.3 s apart are on four channels with full peer
lists after ~4 s and have all six encrypted sessions ~0.3 s later.

Real GNU Radio (this flowgraph, Pluto blocks replaced by a DAC emulator and a
shared ZMQ "air", `bpsk_zmq_bench.py`), run at 1/8 real time on a 2-core
machine: two nodes and three nodes discover each other, take separate
channels, are approved over the HTTP API, set up every session, exchange 1:1
and group messages, and move a file byte-identically. With three nodes the
machine was overloaded (load 34, own echo up to 3 s): the lag warning fired
and channel checks fell back to decoded frames, as designed; one genuine
start-up channel race was detected and resolved.

## Testing (r6)

```
python3 test_mesh.py            # r6: 14 scenarios, 62 checks (below)
python3 test_arq.py             # r5.4 suites, now on LinkMux / static mode
python3 test_secure.py
python3 test_regressions.py
python3 test_bpsk_link.py
python3 test_crypto.py
python3 test_shim.py            # the GRC block: 4 channels, tx_ctl, mesh start
python3 test_shutdown.py
BPSK_CRYPTO=pure python3 test_mesh.py      # same, pure-Python crypto
```

`test_mesh.py` scenarios: MAC units, discovery, simultaneous channel claim,
address collision (listening and both active), trust on first use (one-sided
approval refused, persistence, withdrawal), group chat with partial delivery,
two pairs moving files concurrently with loss, fairness between two peers,
privacy against the other nodes and an eavesdropper (no plaintext on air), a
fifth node waiting for a channel, adjacent-channel leakage, restart, security
off, static mode. 246 checks across all suites pass with both crypto backends.

Multi-node GNU Radio bench:

```
python3 bpsk_zmq_bench.py air --nodes 3 --scale 8
cd n1 && python3 ../bpsk_zmq_bench.py node --scale 8 --tx tcp://127.0.0.1:5601 -- --nickname Ann --http-port 8081
cd n2 && python3 ../bpsk_zmq_bench.py node --scale 8 --tx tcp://127.0.0.1:5602 -- --nickname Ben --http-port 8082
cd n3 && python3 ../bpsk_zmq_bench.py node --scale 8 --tx tcp://127.0.0.1:5603 -- --nickname Cat --http-port 8083
```

`--scale S` runs everything S times slower (rates and spacing ÷ S) so several
nodes fit on one PC; protocol timing scales with it. GNU Radio's buffers are
sized in samples, so at a reduced rate they hold S times more time: the
bench's own-echo latency is far longer than a Pluto's, and the lag warning
can appear there without meaning anything on hardware.

## Bench checklist (hardware)

1. One node alone: it listens 2–4 s, then *Channels* shows "transmitting" on
   one channel; the spectrum shows one carrier at centre ± 300/900 kHz.
2. Second node: both list each other within ~5 s, on different channels.
   Compare fingerprints, Trust on both, lock goes green.
3. Set gains: other nodes' channels should show SNR > 12 dB; raise
   `tx_atten` if the own channel's level is near clipping.
4. Nodes 3 and 4: four distinct channels; group message delivered to all
   (three ✓✓ receipts).
5. Two files at once between disjoint pairs: neither slows the other.
6. Stop one node: it turns *away* within 5 s; a group message then goes only
   to those in range. Restart it: same address, sessions return by themselves.
7. Watch *channel claims / moves* for a few minutes: moves should stay at 0
   after start-up. Frequent moves mean energy or leakage is being misread —
   note the levels in the Channels panel and the log line, which records them.

## Limits, and what r7 could add

- One broadcast domain: every node must hear every other (no relaying). A
  node that cannot hear the others' beacons cannot resolve an address clash
  with them.
- Four transmitters at a time (the channel count); more nodes wait for a free
  channel. More channels need more receive chains (CPU) or a polyphase
  channeliser.
- A node's outbound capacity is its one channel: one-to-many traffic (group
  messages, one node serving files to several) shares it.
- Relaying (multi-hop) and a shared group key are the natural next steps; the
  sealed header already carries origin and final destination separately for
  the former.

## Files (r6)

| File | r6 |
|---|---|
| `bpsk_mac.py` | **new**: channel manager, address manager, beacon format |
| `bpsk_meshsim.py` | **new**: K-channel air simulator (collisions, levels, leakage, latency) |
| `test_mesh.py` | **new**: 14 multi-node scenarios |
| `bpsk_link.py` | `Radio`, `LinkMux`, beacon type, filler top-up |
| `bpsk_secure.py` | `MeshKeyStore` (key-based trust, address book), dynamic peers and address |
| `bpsk_app.py` | mesh mode: MAC, peers, conversations, group, trust, waiting messages |
| `bpsk_keys.py` | `init` without an address, `trust`, `revoke`, `show` |
| `bpsk_duplex_pluto_link_layer.py` | 4 symbol inputs, `rx0`–`rx3`, `tx_ctl`, per-channel level/EVM |
| `bpsk_duplex_pluto.grc/.py` | channelised receiver, TX mixer, new parameters |
| `chat_ui.html`, `ui/` | peer list, trust, conversations, receipts, channel map (`ui/common.css` new) |
| `bpsk_offline_demo.py` | four nodes on the simulator, one UI each |
| `bpsk_zmq_bench.py` | multi-node: DAC emulator per node plus a shared air |

---

# Earlier releases (r5.x, two-node FDD link)

Everything below describes r5.0–r5.4. The chat layer, encryption and Selective
Repeat described there are all still in r6; the *Running it* instructions
apply to [static mode](#static-mode) only.


## The application layer (r5)

A chat application layer on top of the existing full-duplex FDD BPSK PlutoSDR
link. The modem, the ARQ and the file protocol are unchanged in behaviour; what
is new is a structured event interface out of the link layer, an application
envelope, and a browser front end served by the flowgraph itself.

```
chat_ui.html          browser: transcript, telemetry, composer
    |  HTTP + Server-Sent Events on 127.0.0.1:<http_port>
bpsk_app.py           envelope, chat model, delivery receipts, HTTP server
    |  ('tx', bytes) / ('log', str) / ('evt', dict)
bpsk_link.py          addressing, stop-and-wait ARQ, file segmentation
    |  plaintext link frames
bpsk_secure.py        r5.3: Noise KK handshake, seal / open every frame
    |  sealed frames (PDU)
GNU Radio flowgraph   DBPSK modem, PlutoSDR / ANT-SDR E200
```

No third-party Python packages (the `cryptography` package is used for speed
if it happens to be installed, never required). The server is `http.server` plus Server-Sent
Events, so nothing has to be installed on the lab machine.

## Files

| File | Status | Purpose |
|---|---|---|
| `bpsk_link.py` | modified | link layer, now `r5-applayer`: message ids, structured events, opaque file metadata |
| `bpsk_app.py` | new | application envelope, chat model, receipts, telemetry, HTTP + SSE server |
| `chat_ui.html` | new | the front end, single self-contained file (light "Ledger" theme) |
| `ui/` | new | the sources the front end is built from, and three alternative themes |
| `bpsk_duplex_pluto_link_layer.py` | modified | GRC embedded block: PMT bridge, EVM/SNR estimator, server lifecycle |
| `bpsk_duplex_pluto.grc` | modified | two new parameters, one new connection |
| `bpsk_duplex_pluto.py` | modified | regenerated flowgraph, in step with the `.grc` |
| `bpsk_offline_demo.py` | new | run both nodes with no radio, over a simulated channel |
| `test_bpsk_link.py` | extended | two-node loopback test over a lossy virtual channel |
| `test_shim.py` | new | tests the GRC block with `pmt`/`gr` stubbed out |
| `test_shutdown.py` | new in r5.1 | shutdown-path regression test: the watchdog and the UI server must not outlive `stop()` |
| `test_regressions.py` | new in r5.2 | one reproduction per defect fixed in r5.2; fails on r5.1, passes on r5.2 |
| `bpsk_crypto.py` | **new in r5.3** | ChaCha20-Poly1305, X25519, HKDF: pure Python, or `cryptography` if installed |
| `bpsk_secure.py` | **new in r5.3** | keys + roster, Noise KK handshake, sessions, sealing, replay window |
| `bpsk_keys.py` | **new in r5.3** | key management CLI: `init`, `add`, `remove`, `merge`, `show` |
| `bpsk_zmq_bench.py` | **new in r5.3** | runs the real flowgraph with the Pluto blocks swapped for ZMQ + a channel model |
| `test_crypto.py` | **new in r5.3** | RFC 8439 / 7748 / 5869 vectors, Noise KK reference transcript, both backends |
| `test_arq.py` | **new in r5.4** | Selective Repeat: throughput, exactly-once in-order delivery, blackouts, restarts, forged sync |
| `test_secure.py` | **new in r5.3** | confidentiality, tampering, replay, impersonation, restart, rekey, forward secrecy |

All of them must sit in the same directory.

## Running it

First time only, on each node, create its key and exchange public keys (details
in [Encryption](#encryption-r53)):

```
python3 bpsk_keys.py init --addr 1 --name "Rivija"      # on node A
python3 bpsk_keys.py init --addr 2 --name "Bench B"     # on node B
# each prints an `add ...` line: run A's line on B and B's line on A
```

Node A:

```
python3 bpsk_duplex_pluto.py --my-addr 1 --peer-addr 2 \
    --tx-freq 905.2e6 --rx-freq 910.2e6 --uri ip:192.168.1.10 \
    --nickname "Rivija" --http-port 8088
```

Node B:

```
python3 bpsk_duplex_pluto.py --my-addr 2 --peer-addr 1 \
    --tx-freq 910.2e6 --rx-freq 905.2e6 --uri ip:192.168.1.10 \
    --nickname "Bench B" --http-port 8088
```

The chat opens in the default browser a moment after the flowgraph starts, at
`http://127.0.0.1:8088/`. The GNU Radio window stays up beside it with the
constellation, spectrum and time-domain scopes; its edit box is now a fallback
console for `/stats` and `/help`.

Set `open_ui` to `False` in the *BPSK Chat Node* block to stop it opening a
browser tab on every restart. The server binds `127.0.0.1` only.

Both nodes must speak the same wire format: with encryption on, both need
r5.3 and each other's keys; with `--security 0`, any r5.x works. The revision
and the security state are printed in the banner and shown in the telemetry
panel; a mismatched pair is otherwise indistinguishable from a bad channel. Text from an r4 node still appears in the transcript,
marked as coming from an unknown sender.

### Without hardware

```
python3 bpsk_offline_demo.py --loss 0.10
```

Two complete nodes on a simulated channel, at `http://127.0.0.1:8088/` and
`:8089/`. Same code path as the radio, so it is a fair rehearsal for a demo —
and useful for developing the UI without occupying the E200s.

Opening `chat_ui.html` directly as a file, with nothing serving it, puts the
page into a clearly marked demo mode with mock traffic. That is for looking at
the layout, not for testing the link.

### Tests

```
python3 test_bpsk_link.py     # two nodes, 15% frame loss, text + image + receipts
python3 test_shim.py          # GRC block, EVM estimator, server lifecycle
python3 test_shutdown.py      # teardown must be clean
python3 test_regressions.py   # the r5.2 fixes, one scenario each
python3 test_crypto.py        # r5.3: primitives against the RFC vectors
python3 test_secure.py        # r5.3: security properties, end to end
python3 test_arq.py           # r5.4: Selective Repeat
```

## What the UI shows

The transcript is set as a ruled log rather than as chat bubbles: each entry
carries the sender, the address, the time and the receipt in a fixed gutter, so
a screenshot of it reads as evidence rather than as a messenger.

- **Status bar** — peer reachability, SNR estimated from the recovered
  constellation, RX level, TX/RX goodput, frame error rate, transmit backlog.
- **Entries** — per message: `queued → sent → delivered`, retry count,
  fragment progress for attachments, and the receiver's CRC verdict.
- **Attachments** — an image is framed as a plate with a caption line giving
  its name, size and a save button; click it for the full-size view. A file
  still arriving shows a proportioned placeholder with the fragment counter
  rather than a broken image, and anything that is not an image becomes a card
  with an extension chip.
- **ARQ panel** — the whole `stats` dict live: frames, retransmissions, CRC
  failures, duplicates, drops, idle filler, repair rounds, queue depth.
- **Link log** — the same lines the GNU Radio console prints.

### Changing the theme

`chat_ui.html` is generated. Everything it does lives in `ui/core.js` and
`ui/body.html`; only the stylesheet distinguishes one theme from another, so
switching cannot change the behaviour or the API.

```
python3 ui/build.py d     # Ledger     - editorial, ruled entries   (current)
python3 ui/build.py a     # Studio     - modern product UI, indigo
python3 ui/build.py b     # Instrument - flat and dense, teal
python3 ui/build.py c     # Aurora     - airy, telemetry in a drawer
python3 ui/build.py       # write all four side by side, to compare
```

Each writes `chat_ui.html` next to the flowgraph. To adjust the current theme,
edit `ui/d_ledger.css` and run the build again.

Composer: Enter sends, Shift+Enter is a newline, files go in by drag-and-drop,
paste, or the clip button. `/nick <name>`, `/stats`, `/ping`, `/help`,
`/sendfile <path>`, `/clear`.

Attachments are kept in `chat_files/` next to the flowgraph — `tx_<id>_<name>`
for what you sent, `rx_<name>` for what arrived.

## Protocol additions

Nothing in the frame header changed. Two payloads gained structure.

**Application envelope**, inside the payload of a `DATA` frame:

| Bytes | Field |
|---|---|
| 0 | `0x9A` magic |
| 1 | app version (4b) \| app type (4b) — 1 = text, 2 = presence |
| 2–5 | message id, uint32 BE, sender-scoped |
| 6 | part index |
| 7 | number of parts |
| 8 | nickname length *N* |
| 9…9+N−1 | nickname, UTF-8 |
| rest | this part's payload |

A payload that does not start with `0x9A` is delivered as plain text from an
unknown sender, which is what keeps an un-upgraded node visible. Messages
longer than one fragment are split across parts and reassembled per
(source, message id).

**FILE\_START payload**, extended:

| Bytes | Field |
|---|---|
| 0–5 | file size (uint32 BE), fragment count (uint16 BE) |
| 6 | `0xA5` marker — absent on r4, which is how the old layout is detected |
| 7 | filename length |
| 8… | filename, UTF-8 |
| then | metadata length (uint16 BE) + JSON `{"n":nick,"m":mime,"i":id,"t":ts}` |

The link layer never parses the metadata blob; it hands it up verbatim.

## Signal quality

Input port 0 of the chat block takes the Costas loop output. Every 200 ms it
takes 4096 symbols, estimates the amplitude as `mean(|Re x|)`, forms the error
against the hard decision on the real axis, and reports

```
EVM = sqrt(mean|x - sgn(Re x)·A|²) / A          SNR ≈ −20·log₁₀(EVM)
```

Against synthetic AWGN this tracks the true SNR to within about 0.1 dB from
6 to 20 dB (`test_shim.py`). It is a receiver-side estimate that includes
timing jitter and residual phase error, not just thermal noise — which is the
number worth showing, since it is what the decoder actually sees. When the
symbol amplitude collapses the reading is cleared rather than left stale.

## Flowgraph changes

- *BPSK Link Layer* → *BPSK Chat Node*: new parameters `nickname`,
  `http_port`, `open_ui`, `tx_freq`, `rx_freq`, and a complex input port.
- New connection: `Costas Loop → BPSK Chat Node` (measurement only; the data
  path is untouched).
- New GRC parameters `nickname` and `http_port`.
- The Edit Box label now says it is a console.

The server and the watchdog thread still start on the first tick, not in
`__init__`, so GRC's validation pass never binds the port. `stop()` releases
it, so the flowgraph can be restarted without an "address already in use".


---

# r5.1-stable — what changed and why

A stabilisation release over `r5-applayer`. **No wire-format change**: `WIRE_COMPAT`
is still `r5`, so an r5.1 node interoperates with an r5 node. The banner will show
different `REVISION` strings — that is expected and is not a mismatch.

## The crash

The symptom was an intermittent GNU Radio crash with the browser showing
`radio process not reachable - reconnecting...` in red. That banner is
`EventSource.onerror` in the UI — it means the HTTP server stopped answering,
which means the flowgraph process died. It was never a radio fault.

**Cause 1 — the watchdog outlived `stop()`.** `_watchdog_loop` ran `while True:`
and `stop()` never signalled it, so it kept calling `message_port_pub` while the
runtime destroyed the block. Worse than a race: `stop()` freezes `_last_strobe`,
so the watchdog *engages* 250 ms into teardown and then publishes at 100 Hz.
Measured before the fix: **38 port publishes in the 600 ms after `stop()`**.
After: **0**.

**Cause 2 — a late message resurrected the block.** `stop()` set
`_server = None`, which `_ensure_running()` could not distinguish from "never
started", so any in-flight `rx_frame`, `tick` or `chat_in` re-bound the TCP port
and restarted the watchdog mid-teardown.

Both are fixed by a single `threading.Event` (`_halt`) that `stop()` sets first
and that every publish, handler and resource acquisition checks. `stop()` now
joins the watchdog before releasing anything, and is idempotent.

## Diagnosis was impossible before

`catch_exceptions` was `True` on the Options block, so **any** Python fault
stopped the flowgraph silently — no traceback, window still open but dead, HTTP
server gone, red banner. Every distinct bug presented identically. It is now
`False`: a fault prints a traceback naming the file and line.

## Full fix list

| # | Fix | Evidence |
|---|---|---|
| A | Watchdog exits on `stop()` and is joined | 38 → 0 publishes after stop |
| B | `_halt` guards `_ensure_running` and every handler | late message no longer re-binds the port |
| C | `catch_exceptions=False` | faults now produce a traceback |
| D | Attachment staging, fragmenting and queue building moved off the GNU Radio thread | 8 MiB file: **38.45 ms → 0.81 ms** on the radio thread |
| E | Incomplete multi-part text expires after `PART_TIMEOUT` (60 s) | `_rx_parts` 1 → 0 entries, operator told |
| F | Attachment token map bounded at `MAX_FILE_TOKENS` (1000) | 1250 registered → 1000 held |
| G | `rx_payload_bytes` counted after the duplicate check | duplicate frame: 200 B → 100 B |
| H | `pmt.intern` results cached; `_dispatch` runs up to 100 Hz | one allocation instead of one per publish |
| I | Inbound transfers keyed by source (`_rx_msgs`) | progress cannot be attributed to the wrong entry |

### D in detail

`send_file()` used to fragment the blob and build every transmit-queue entry on
the GNU Radio thread, inside the block lock. For an 8 MiB attachment that is
33 028 entries and ~38 ms — against a `queue_ahead` cushion of 30 ms and a Pluto
sink buffer of 8.19 ms, so the transmitter starved and the far end lost lock.

Now `ChatApp.submit()` stages to disk, calls `bpsk_link.segment()` and
`bpsk_link.build_file_items()` **on the calling thread** (an HTTP worker), and
hands the radio thread a finished list that goes in with one `deque.extend()`.
`/sendfile` from the console does the same on a one-shot worker thread.
Radio-thread cost is now flat in file size.

| Attachment | r5 radio thread | r5.1 radio thread |
|---|---|---|
| 1 MiB | 5.22 ms | 0.14 ms |
| 4 MiB | 19.33 ms | 0.40 ms |
| 8 MiB | 38.45 ms — **underrun** | 0.81 ms |

## Verification

```
python3 test_bpsk_link.py     # 15 checks - protocol, text, image, receipts
python3 test_shim.py          # 13 checks - GRC block, EVM, server lifecycle
python3 test_shutdown.py      # 10 checks - NEW: teardown must be clean
```

All 38 pass. `test_bpsk_link.py` produces results bit-identical to r5
(25.77 s simulated, 171 fragments, 73 retransmissions, 0 drops), which is the
evidence that none of these fixes changed protocol behaviour.

## Not changed

The modem, the framing, the scrambler, the CRC, the ARQ and the addressing are
untouched. Preamble sizing, multi-node support and windowing are r6 work.


---

# r5.2-stable — what changed and why

A second stabilisation release. **No wire-format change**: `WIRE_COMPAT` is still
`r5`, so r5.2 interoperates with r5 and r5.1. Every r5.1 fix (A–I above) was
re-verified and holds. The defects below were found by driving scenarios the
existing tests do not cover; each one is reproduced by `test_regressions.py`,
which fails on r5.1 and passes on r5.2.

| # | Defect in r5.1 | Seen as | Fix |
|---|---|---|---|
| J | FILE_NACK / FILE_DONE went to the **back** of the single TX queue | With a file going each way, the smaller transfer's verdict waited behind the peer's whole upload; the sender gave up after 8 × 3 s and marked a file it had delivered as **failed** — on a perfect channel | Two queues: replies and chat in `txq_hi` (replies at the front), bulk fragments in `txq` |
| K | Chat text queued behind file fragments | A line typed during a 200 kB upload was delivered **114 s** later | Chat/presence go through `txq_hi` → **0.2 s** |
| L | Receiver ignored a repeated FILE_END once the file was complete | FILE_DONE lost 6× in a row → sender reports **failed**, file is intact at the receiver | Receiver keeps the last verdict and repeats it |
| M | FILE_DONE names no transfer | A late verdict could complete the **next** file while its fragments were still queued | OK verdict is accepted only if its CRC matches the current transfer |
| N | `_purge_file_queue` removed every `file*` label | Our restart/give-up also deleted the NACK/DONE we owed the peer, stalling the other direction | Purge only our own bulk queue |
| O | In-progress transfer matched on (name, fragment count) only | A new `image.png` after an abandoned one inherited its fragments; one lost fragment became a **CRC failure** instead of a repair round | Match on the metadata blob too (it carries the sender's message id); the abandoned entry is shown as *incomplete* |
| P | Received files always written to `rx_<name>` | Two pasted screenshots (both `image.png`): the first entry silently showed the **second** picture | `rx_<name>`, then `rx_<name>-2`, … |
| Q | A failed middle part of a multi-frame text was overwritten by later ACKs | Entry stuck at a single tick for ever | `failed` is sticky for text |
| R | Untrusted filename / metadata used as-is | A CRC-valid FILE_START with a NUL in the name raised `ValueError` out of `on_rx`; JSON metadata that is not an object raised `AttributeError` | `clean_name()` in the link layer; metadata type-checked |
| S | Any exception escaping a handler now aborts the process | `catch_exceptions=False` turns one malformed frame into `std::terminate` | Handlers catch at the block boundary, print the full traceback, drop that input, keep running (tracebacks rate-limited) |
| T | `fetch()` rejects non-Latin-1 header values | Files named in Sinhala, Tamil, CJK or with an emoji could not be attached; the sanitiser also reduced `ඡායාරූපය.png` to `png` | Name percent-encoded by the page, decoded by the server; sanitiser keeps any script |
| U | Local HTTP server accepted any origin and any Host | Any web page open in the same browser could transmit on air (text/plain POST, no preflight); a DNS-rebinding page could read `/api/state` | Host must be loopback; POSTs with a foreign `Origin` refused; attachments served with `Content-Security-Policy: sandbox` and an RFC 5987 filename |
| V | `/stats` in the GNU Radio edit box printed nothing on the terminal | Fallback console silent when the browser is unavailable | Printed on the terminal as well as in the transcript |

### Notes

- **`catch_exceptions=False` is kept.** It is what surfaces faults outside this
  block. Inside it, the boundary guard (S) gives the same traceback without
  taking the radio down.
- **Queue priority changes behaviour, not protocol.** Stop-and-wait still has
  one frame in flight; the receiver never depended on chat and file frames
  being in order, and the sequence space is shared as before. File throughput
  with no chat traffic is unchanged (the 200 kB transfer in the bidirectional
  test takes the same 116 s on r5.1 and r5.2).
- `test_bpsk_link.py` is **not** bit-reproducible run to run: the FILE_START
  metadata carries a wall-clock timestamp, whose JSON length varies, which
  shifts the modelled airtime slightly. The r5.1 note claiming bit-identical
  results (25.77 s, 73 retransmissions) holds only for some runs.
- Not a defect, noted for r6: after a node restarts, its sequence numbers begin
  again at 0, so if the peer's last-seen sequence from it happened to be 0 the
  first new frame is ACKed and discarded as a duplicate. In practice that frame
  is the presence announcement. A per-boot session identifier removes this;
  the encryption work provides one.


---

# Encryption (r5.3)

`r5.3-secure` seals every frame end to end. Only the two nodes of a pair can
read or forge their traffic, and a third node on the channel can't, even one
holding a valid key of its own. Recording the air and stealing a node's key
later does not decrypt past sessions.

| Property | How |
|---|---|
| Confidentiality of content **and frame type** | ChaCha20-Poly1305 over the whole inner frame (type, seq, ack, flags, payload) |
| Integrity + authenticity of every frame, **ACKs included** | Poly1305 tag; forged or altered frames are dropped before the link layer sees them |
| Isolation between nodes | a separate session per pair, keyed from that pair's X25519 keys |
| Forward secrecy | Noise KK handshake with ephemeral keys; new session every 10 min |
| Replay protection | fresh 64-bit counter per transmission; 2048-frame window; replays dropped, **not ACKed** |
| Fails closed | no session or no keys → nothing is sent except the carrier filler |

## Setting up keys

Each node has one private key that never leaves it, and every node shares the
same **roster** of public keys. The roster holds nothing secret.

```
# on node A                                   # on node B
python3 bpsk_keys.py init --addr 1 --name A    python3 bpsk_keys.py init --addr 2 --name B
```

Each `init` prints the node's public key, a fingerprint, and an `add` line for
the other nodes:

```
# on node B (A's line)                        # on node A (B's line)
python3 bpsk_keys.py add --addr 1 --key <64 hex> --name A
                                              python3 bpsk_keys.py add --addr 2 --key <64 hex> --name B
```

**Then compare fingerprints across the bench.** Run `python3 bpsk_keys.py show`
on both nodes, or `/secure` in the chat, and read the four-group codes aloud.
If they match, nobody substituted a key in transit. A roster file can also be
copied and merged (`bpsk_keys.py merge roster.json`); conflicting keys are
reported, never silently overwritten.

Files, in `keys/` next to the flowgraph (`--keys-dir` to change it):

| File | Contents |
|---|---|
| `node_<addr>.key` | this node's X25519 private key, hex, mode 600: **never copy it** |
| `roster.json` | `{"network": ..., "nodes": {"<addr>": {"public_key": hex, "name": ...}}}` |
| `node_<addr>.state` | last handshake timestamp, so it keeps increasing across restarts |

The `network` name is bound into every handshake, so two labs using the same
addresses can't cross-connect. Keys are never created automatically: GRC
instantiates the block on every validation, and that must not leave key files
behind. Restart the flowgraph after editing the roster.

## Running

Encryption is on by default (`--security 1`). Use `--security 0` to talk to an
r5 / r5.1 / r5.2 node: r5.3 is then byte-identical on air to r5. In GRC these are
the *Encryption (1/0)* and *Key directory* parameters.

What you see:

- **Banner**: `security: ON  Noise_KK_25519_ChaChaPoly_SHA256 [backend]  my key …  peer 2 key …`
- **Header**: a lock under the peer's name, reading *securing link…* (amber), *end-to-end encrypted* (green) or *peer key missing* / *key error* (red).
- **Transcript**: a line when the session comes up, with the peer's fingerprint, and an **E2E** badge on each message.
- **Encryption pane**: suite, backend, both fingerprints, session age, next rekey, sealed/opened counts, authentication failures, replays, refused plaintext.
- **`/secure`**: the same, in the transcript and on the terminal.

Messages typed before the session is up wait in the queue and go out sealed
once it is. Nothing is ever sent in the clear. From power-on, the session
normally comes up within about 2 s.

## Design

```
LinkState  --plaintext frame-->  SecureLink.protect  --FT_SEALED-->  radio
LinkState  <--plaintext frame--  SecureLink.accept   <--any frame--  radio
```

`LinkState` is unchanged in what it sends and expects. It gained one gate: it
pumps nothing while there is no session. It never sees a key.

**Handshake: `Noise_KK_25519_ChaChaPoly_SHA256`** (Noise spec rev. 34). KK
means both sides already know each other's static key from the roster:

```
-> e, es, ss     HS_INIT  (0x9)   initiator's timestamp, session index, boot id   67 B
<- e, ee, se     HS_RESP  (0xA)   responder's session index, boot id              63 B
```

Why KK:

- It needs one round trip.
- It authenticates both sides.
- Static keys are never transmitted.
- The transport keys come from the ephemeral-ephemeral DH, which gives forward secrecy.

The Noise prologue binds the protocol version, the network name and both
addresses.

**Transport frame** (outer link type `FT_SEALED` = 0x8):

| Bytes | Field | |
|---|---|---|
| 0 | version | clear, authenticated (AAD) |
| 1 / 2 | origin / final destination | clear, authenticated: end-to-end, a relay may not change them |
| 3–6 | receiver's session index | clear, authenticated |
| 7–14 | counter, uint64 BE | clear, authenticated: the nonce |
| 15… | ChaCha20(type, seq, ack, flags, payload) + 16-byte tag | encrypted |

That is 35 bytes of overhead per frame. The outer link header carries only
addresses, with seq, ack and flags zeroed. An observer can't tell a chat line
from a file fragment or an ACK except by length.

**Session rules:**

- **Responder confirmation.** The responder does not transmit on a new session until the initiator has used it; the initiator sends a sealed keep-alive at once. So a replayed HS_INIT can't move the responder onto a session nobody can finish. HS_INIT timestamps must also increase per initiator.
- **Rekeying.** The initiator rekeys every 10 min, make-before-break. The replaced session still decrypts for 30 s, and no session is ever used after 30 min.
- **Simultaneous initiation.** The lower address wins; the other node drops its attempt and answers.
- **Restarts.** A restart is detected from the boot id carried in the handshake. The peer then resets its per-peer receive state, which also fixes the old duplicate-suppression issue where a restarted node's first frame, seq 0, could be discarded.
- **Handshake retries.** Handshake frames aren't ARQ'd. SecureLink retries them itself with a fresh ephemeral each time, backing off 1 → 2 → 4 → 5 s.
- **Held retransmissions.** While there is no session, an in-flight frame is held for up to 15 s without spending retries.

## Threat model

| Protected against | Not protected against |
|---|---|
| Eavesdropping on any frame | **Traffic analysis**: addresses, frame lengths and timing are visible |
| Forged or modified frames, forged ACKs | **Jamming** / denial of service |
| Replay of frames or handshakes | A **compromised node PC**: plaintext is on its screen, `chat_files/` on its disk, the key file in `keys/` |
| A third node, even one in the roster, impersonating another | A wrong public key in the roster, if fingerprints are never compared |
| Later theft of a static key (past sessions stay sealed) | Timing side channels in the pure-Python backend (it is not constant-time) |
| Web pages in the same browser reading or sending through the local UI (Host/Origin checks, r5.2) | — |

The IDLE filler stays broadcast and unauthenticated. It carries no data and
only keeps the carrier up, so a spoofed one can at most make *peer reachable*
light up. The lock indicator is driven by the authenticated session, not by
that.

## Performance (measured)

| | pure Python | `cryptography` |
|---|---|---|
| Seal / open a 275-byte frame | 0.32 ms | 0.003 ms |
| One X25519 operation | 1.1 ms | < 0.1 ms |
| Radio-thread call, p99, during a 60 kB transfer | 0.6 ms | 0.1 ms |
| Radio-thread call, worst (answering a handshake) | 7.4 ms | 1.0 ms |

Even the worst case is well inside the 30 ms `queue_ahead` cushion, so no
worker thread is needed. Airtime: +35 bytes per frame, i.e. +4.8 % on a
256-byte fragment and +7.4 % on an ACK. In the simulated stop-and-wait
transfers, end-to-end time stayed within ±5 % of plaintext, because the cycle
is dominated by ARQ and filler timing rather than frame length.

## Verification

| Suite | Checks | What it establishes |
|---|---|---|
| `test_crypto.py` | 38 | RFC 8439 (block, encryption, Poly1305, AEAD), RFC 7748 (both §5.2 vectors, 1 and 1000 iterations, §6.1), RFC 5869; a Noise KK transcript from the independent `noiseprotocol` reference; pure Python == `cryptography` on 350 random cases |
| `test_secure.py` | 31 | Staggered start-ups converge on one session. Nothing readable on air (text, filename, MIME, nickname, file bytes). Tampered frames rejected; replays dropped, not ACKed; handshake replay refused. A third node with a valid key can neither read nor impersonate; a node missing from the roster is refused. Restart recovers. Rekeying mid-transfer (13 sessions, file intact). Static keys plus a full recording open nothing. Lossy handshake recovers. Plaintext refused. |
| `test_bpsk_link.py` | 31 | The original scenario, run twice: plaintext and encrypted |
| `test_shim.py` | 17 | Adds: encryption on by default; no key → nothing sent in the clear; handshake goes out once keyed |
| `test_regressions.py`, `test_shutdown.py` | 29 + 10 | Unchanged r5.2 behaviour |

All suites pass with `BPSK_CRYPTO=pure` and with `BPSK_CRYPTO=native`.

**In GNU Radio itself (3.10.9 here):**

- `grcc` compiles `bpsk_duplex_pluto.grc` cleanly.
- Two copies of the generated flowgraph ran as separate processes through `bpsk_zmq_bench.py`: the real DBPSK modem, a channel with a 2 kHz carrier offset, 20 ppm clock offset and AWGN, one node on the pure-Python backend and one on `cryptography`.
- Results: secure session in 2 s, matching fingerprints, text both ways, a Sinhala-named file byte-identical, SNR read as 24 dB from the Costas output, and clean exits on SIGTERM.
- At higher noise (1.6 % frame errors, 17 retransmissions), everything still arrived intact.

**Not verified here:**

- Over the air on the E200s.
- GNU Radio 3.10.12 specifically. The shipped `bpsk_duplex_pluto.py` follows the 3.10.12 generator layout; if in doubt, regenerate it from the `.grc` in your GRC.

### Bench checklist

1. Run r5.2 or r5.3 with `--security 0` on both nodes first, to confirm the RF side is unchanged.
2. Run `bpsk_keys.py init` on each node, exchange the `add` lines, and compare fingerprints.
3. Start both nodes with encryption on. The lock should turn green within a few seconds.
4. Send text and an image. Check `/secure`: *auth failures* and *replays* should be 0.
5. Restart one node. The session should re-key by itself, and the next message should arrive.
6. Run one node with `--security 0`. Its messages must be refused, and *unencrypted refused* should count them.

## Notes for r6 (multi-node)

- `SecureLink` already keeps sessions **per peer address** and takes a list of peers. The roster scales to 254 nodes, and adding a node is one `init` on it and one `add` everywhere else. The two-node assumption lives in `LinkState` (one `peer_addr`), not in the security layer.
- **Relays (hub / mesh).**
  - Origin and final destination are inside the authenticated header, and the outer link header is free for hop addressing. A relay can forward an `FT_SEALED` frame untouched without holding its keys.
  - The receiver checks the sealed origin against the session, not the outer source, so no receiver change is needed.
  - ARQ per hop would need hop-level ACKs sealed with the relay's own pair session; that's an r6 design decision.
- **Broadcast / CSMA control traffic.** Pairwise sessions can't authenticate a frame to everyone. If r6 needs broadcast data or authenticated control frames (RTS/CTS, beacons), add a group key or signatures. Not implemented here.
- **CSMA airtime.** Sealed frames are 35 bytes longer and handshakes add two short frames per pair every 10 minutes. Size backoff slots and the airtime model accordingly; `LinkState.seal_overhead` already feeds the airtime model.

## Files changed in r5.3

- `bpsk_link.py`:
  - `on_frame()` split out of `on_rx()`.
  - Transmit gate (`tx_gate`) and `seal_overhead` in the airtime model.
  - `note_tx()` for handshake frames, `kick()`, and `reset_peer()`.
  - Frame types 0x8–0xA reserved; `HOLD_LIMIT`.
- `bpsk_app.py`:
  - `security` / `keys_dir` parameters; fails closed on a key error.
  - Seals in `_consume()`, opens in `on_rx()`.
  - Security events and system lines, `/secure`, `security` in the telemetry, `secure` flag on messages.
- `bpsk_duplex_pluto_link_layer.py`: `security` and `keys_dir` block parameters, and the security line in the banner.
- `bpsk_duplex_pluto.grc` / `.py`: two new flowgraph parameters, `security` (1/0) and `keys_dir`, passed to the block.
- `bpsk_offline_demo.py`: provisions demo keys; `--insecure` to disable encryption.
- `ui/`: lock indicator, Encryption pane, E2E badge, `/secure` hint; `chat_ui.html` rebuilt.


---

# Selective Repeat (r5.4)

`r5.4-sr` keeps several frames in flight instead of one. The receiver buffers
anything that arrives out of order, and only lost frames are resent. Delivery
to the application stays exactly-once and in order. Nothing else changed: the
file protocol, the chat envelope and the encryption behave as in r5.3.

## Why Selective Repeat and not Go-Back-N

Take the bench's measured 9.3 % frame loss and a window of 8 frames:

- Go-Back-N resends the whole window after every loss, so it is about 55 % efficient.
- Selective Repeat resends only the lost frame, so it is about 91 % efficient.

The difference grows with the window.

## Measured in simulation

Setup: encrypted, 100 kB file, 256-byte fragments, and +41 ms one-way latency
to model the Pluto buffers (32768-sample RX and 8192-sample TX at 1 MSPS).

| Configuration | 0 % loss | 9.3 % loss |
|---|---|---|
| Stop-and-wait, 384-byte preamble (r5.3) | 10.5 kb/s | 6.4 kb/s |
| Selective Repeat W = 32, 384-byte preamble | **75.1 kb/s** | **66.4 kb/s** |
| Stop-and-wait, 64-byte preamble | 18.9 kb/s | 9.3 kb/s |
| Selective Repeat W = 32, 64-byte preamble | **148.1 kb/s** | **110.3 kb/s** |

Two things stand out:

- **The window gives about 7×.** Under stop-and-wait each frame waits a full round trip, about 200 ms on hardware.
- **The short preamble only pays off once the window is on.** Under stop-and-wait it saves about 25 ms of a ~200 ms cycle. With the channel kept full, it doubles efficiency.

## How it works

**Sender:**

- Up to `window` frames are unacknowledged at once (1–64; 1 = stop-and-wait). Sequence numbers are still 8-bit.
- Each frame has its own timer, as in r5.3: `ack_timeout` after it leaves the antenna.
- **Fast retransmit.** The channel delivers frames in order, so a frame still unacknowledged when a later frame *has* been acknowledged was lost. It is resent at once rather than after the timeout. In tests, 85–100 % of recoveries were fast.
- **Pacing.** At most two frames' worth of airtime is queued ahead of the radio. That keeps the channel full without leaving the ACKs this node owes the peer stuck behind a whole window of its own data.

**Receiver:**

- Frames within 128 of the next expected sequence number are new and are buffered. Anything else is old: a duplicate, whose ACK was lost.
- Buffered frames are delivered in order as gaps fill, so the file protocol and multi-part text work unchanged.

**SACK (selective ACK):**

- One ACK carries the receiver's next expected sequence number, which covers everything before it cumulatively. It also carries a 127-bit bitmap of what is buffered beyond that point.
- Receivers send at most one SACK per `ack_gap` (30 ms) and cover everything received so far. A lost SACK is therefore covered by the next one: in the test, half of all ACKs were dropped and **nothing** was retransmitted.

**Window base in every frame:**

- Each data frame carries the sender's oldest unacknowledged sequence number, in the header byte stop-and-wait left unused.
- If the sender gives up on a frame after `max_retries`, the receiver sees the base move past it. It delivers what it holds, skips the gap and carries on, where it would otherwise wait for ever.
- If the sender has nothing further to send, three unicast *sync* frames carry the new base.
- Sync frames are sealed with encryption on. A broadcast frame can never move the window, because a forged move would make the receiver skip frames and then acknowledge them as old.

**Restarts:**

- The header's spare flag bits carry a 6-bit sender epoch, random at each start-up.
- A new epoch means the peer restarted and its sequence numbers began again at 0, so the receiver resets its window for that peer.
- With encryption on, the handshake's boot id does the same (r5.3), independently.
- Frames with epoch 0 come from r5.3 or older. They are ignored, not allowed to disturb a live window.

**Head-of-line blocking.** In-order delivery means messages behind a lost frame wait until it is recovered. A fast retransmit takes one round trip. An abandoned frame takes up to `(max_retries+1) × ack_timeout`, 3 s by default, and the later messages are then released.

## Parameters (GRC and command line)

| Parameter | Default | Notes |
|---|---|---|
| `arq_window` / `--arq-window` | 32 | 1 = stop-and-wait. It should cover about 2 round trips of frames (~200 ms ÷ 24 ms ≈ 8 per round trip on hardware). Named `arq_window` because a GRC variable called `window` would shadow `gnuradio.fft.window`, which the spectrum sink uses. |
| `preamble_size` / `--preamble-size` | 384 | Bytes before every frame. The carrier is continuous (idle filler), so the loops stay locked and **64** decoded cleanly in the GNU Radio test. Lower it step by step on the bench while watching the frame error rate. |
| `postamble_size` / `--postamble-size` | 64 | 16 worked in the GNU Radio test. |
| `rx_buffer` / `--rx-buffer` | 32768 | Pluto RX buffer in samples; 33 ms of latency at 1 MSPS. Smaller cuts the round trip, but watch the terminal for `O` (overflow). |
| `tx_buffer` / `--tx-buffer` | 8192 | Pluto TX buffer; watch for `U` (underflow). |

The airtime model follows `preamble_size` and `postamble_size` automatically,
because `overhead = preamble + postamble + 12`.

The ARQ panel gained *ARQ window / in flight*, *oldest unacked*, and *fast retx / skipped*.

## Verification

- `test_arq.py`: 22 checks.
  - Throughput is 4.6× over stop-and-wait on a clean channel and 8.8× at 9.3 % loss.
  - At 20 % loss both ways, with files and 50 chat messages in each direction, every message arrived exactly once and in order, and sequence numbers wrapped past 255.
  - Half of all ACKs lost caused no retransmissions.
  - After a 6 s blackout, frames were abandoned and skipped, chat resumed, and the file completed by file-level repair.
  - Held frames were released when nothing else was sent.
  - A plaintext restart was detected by its epoch.
  - A forged broadcast sync was ignored.
  - Window 1 behaves as stop-and-wait, and encryption shows no authentication failures or replays.
- All earlier suites still pass, with both crypto backends. Two tests that reached into the removed stop-and-wait internals were updated. One check changed meaning: frames from r5.3-or-older nodes are now ignored rather than shown.
- In real GNU Radio (3.10.9, `grcc`-compiled `.grc`, two processes over `bpsk_zmq_bench.py`), a 60 kB file arrived intact with window 1, with window 32, and with window 32 plus a 64-byte preamble and 16-byte postamble. Both processes shut down cleanly.
  - On a lossy channel in the same setup (SNR ≈ 6.8 dB, most data frames needing a resend), the 60 kB file took 9.8 s with window 32 and 242.7 s with window 1, both intact. 268 of the 273 recoveries were fast retransmits. Stop-and-wait pays the full 0.5 s `ack_timeout` for every loss; Selective Repeat pays about one round trip.
  - The bench is a functional test, not a timing one. Its throttle lets samples through faster than real time after idle spells, and it has no Pluto buffer latency. Measure throughput on the hardware.

## Bench checklist

1. Tune RX gain and TX attenuation first. The frame error rate limits every ARQ scheme.
2. Run `--arq-window 1` and then `--arq-window 32` with the same file. Compare the *TX* tile and the transfer time.
3. Lower `--preamble-size` in steps, 384 → 192 → 128 → 64, on both nodes. Stop where the frame error rate starts to rise.
4. Optionally try `--rx-buffer 8192 --tx-buffer 4096`. Keep them only if the terminal shows no `O` or `U`.
