"""Closed-loop gain staging for the r6.1 node: RX gain and TX attenuation.

Standard library only (bpsk_app uses it; so do the simulator and tests).

The law, in one place, because the README documents it from here:

RX loop, every RX_PERIOD (0.5 s), from the ADC peak and clipped fraction
over the last second of raw-receiver snapshots:
  * any clipping (> CLIP_MAX) or peak above ADC_MAX (-6 dBFS): RX gain
    drops at once by the overshoot above ADC_TARGET (-12 dBFS), 3..12 dB
    (12 dB if more than 1 % of samples clip);
  * peak in (ADC_HIGH, ADC_MAX] (-9..-6 dBFS): RX gain -1 dB;
  * peak in [ADC_LOW, ADC_HIGH] (-20..-9 dBFS): nothing - the hysteresis;
  * peak below ADC_LOW for LOW_HOLD (2 s) in a row: RX gain +RX_UP (2 dB),
    then another LOW_HOLD before the next step - but only on readings taken
    while this node's own transmitter was on: the own signal sets the
    level, so headroom measured while listening or during a silence check
    is not headroom (gain raised then would clip the moment the
    transmitter returns; on the slowed-down bench a check's silence reached
    the ADC late enough to slip past the gate, and the gain went 2 dB up
    and back down at every check until the hold was added);
  * clipping with RX gain already at rx_min: this node's own transmitter
    is too strong for its own receiver, TX attenuation +3 dB.
  Every node hears its own transmitter far above everyone else; this keeps
  that signal clear of the ADC's full scale while the others stay above
  its noise.

TX loop, every TX_PERIOD (2 s), from the neighbours' beacons (each reports
the SNR at which it hears this node):
  * some neighbour below target - 2 dB (13 dB) and the ADC has 2 dB of
    headroom below ADC_HIGH: TX attenuation -2 dB (more power);
  * every neighbour above target + 8 dB (23 dB): TX attenuation +1 dB;
  * in between: nothing (hysteresis).
  A neighbour whose beacon says its receiver is overloaded or impaired is
  left out - more power would not help it - and one that does not list
  this node at all counts as 0 dB. With no reports nothing changes.
  More power is never bought with this node's own sensitivity: it needs
  RX gain RX_RESERVE (10 dB) above rx_min (each dB more puts the own
  signal 1 dB higher on the ADC, and the RX loop answers with 1 dB less
  gain for everyone). And when NO neighbour lists this node at all, power
  goes up at most DEAF_STEP (12 dB) above where it started: past that the
  fault is not path loss (a broken antenna or cable, a deaf neighbour) and
  more power would only deafen this node's own receiver.

Bounds rx_min..rx_max and tx_min..tx_max (defaults 0..70, 10..89 dB),
whole dB, at most one change per period; readings taken within SETTLE
(0.25 s) of a change are ignored. auto=False freezes both (manual
override); set_manual() moves either one by hand.
"""

import math
from collections import deque


class GainControl(object):
    """See the module docstring for the law."""

    ADC_MAX = -6.0
    ADC_HIGH = -9.0
    ADC_LOW = -20.0
    ADC_TARGET = -12.0
    CLIP_MAX = 0.0005
    RX_PERIOD = 0.5
    TX_PERIOD = 2.0
    RX_UP = 2
    SETTLE = 0.25
    RX_RESERVE = 10
    DEAF_STEP = 12
    LOW_HOLD = 2.0

    def __init__(self, rx_gain=30, tx_atten=50, auto=True, rx_min=0,
                 rx_max=70, tx_min=10, tx_max=89, target=15.0):
        self.rx_min, self.rx_max = int(rx_min), int(rx_max)
        self.tx_min, self.tx_max = int(tx_min), int(tx_max)
        self.rx_gain = self._b(rx_gain, self.rx_min, self.rx_max)
        self.tx_atten = self._b(tx_atten, self.tx_min, self.tx_max)
        self.tx_start = self.tx_atten
        self.auto = bool(auto)
        self.target = float(target)
        self._adc = deque()
        self._rx_next = 0.0
        self._tx_next = 0.0
        self._changed = -1e9
        self._low_since = None
        self.changes = 0
        self.last_reason = ''

    @staticmethod
    def _b(v, lo, hi):
        return max(lo, min(hi, int(round(float(v)))))

    def snapshot(self):
        peak, clip = self.adc_window()
        return {'rx_gain': self.rx_gain, 'tx_atten': self.tx_atten,
                'auto': self.auto, 'reason': self.last_reason,
                'changes': self.changes, 'adc_peak': peak, 'adc_clip': clip,
                'target': self.target,
                'bounds': [self.rx_min, self.rx_max, self.tx_min,
                           self.tx_max]}

    def observe_adc(self, now, peak_dbfs, clip, tx_on=True):
        if peak_dbfs is None or now - self._changed < self.SETTLE:
            return
        self._adc.append((now, float(peak_dbfs), float(clip or 0.0),
                          bool(tx_on)))
        while self._adc and now - self._adc[0][0] > 1.0:
            self._adc.popleft()

    def adc_window(self):
        if not self._adc:
            return None, None
        return (max(a[1] for a in self._adc),
                max(a[2] for a in self._adc))

    def _tx_on(self):
        return bool(self._adc) and all(a[3] for a in self._adc)

    def set_manual(self, rx_gain=None, tx_atten=None):
        out = []
        if rx_gain is not None:
            self.rx_gain = self._b(rx_gain, self.rx_min, self.rx_max)
            out.append(('rx', self.rx_gain, 'set by hand'))
        if tx_atten is not None:
            self.tx_atten = self._b(tx_atten, self.tx_min, self.tx_max)
            self.tx_start = self.tx_atten
            out.append(('tx', self.tx_atten, 'set by hand'))
        if out:
            self.last_reason = 'set by hand'
            self._changed = -1e9
            self._adc.clear()
            self.changes += 1
        return out

    def step(self, now, reports=()):
        """reports: one dict per neighbour heard recently: {'snr': dB at
        which it hears this node, or None if it does not list this node;
        'impaired': its receiver is overloaded or impaired}. Returns a list
        of (what, value, reason), what = 'rx' or 'tx'."""
        out = []
        if not self.auto:
            return out
        peak, clip = self.adc_window()
        if now >= self._rx_next and peak is not None:
            self._rx_next = now + self.RX_PERIOD
            over = clip > self.CLIP_MAX or peak > self.ADC_MAX
            if over and self.rx_gain > self.rx_min:
                drop = 12 if clip > 0.01 else int(min(12, max(
                    3, math.ceil(peak - self.ADC_TARGET))))
                out.append(('rx', max(self.rx_min, self.rx_gain - drop),
                            'ADC peak %.1f dBFS, %.2f %% clipped'
                            % (peak, 100.0 * clip)))
            elif over and self.tx_atten < self.tx_max:
                out.append(('tx', min(self.tx_max, self.tx_atten + 3),
                            'own transmitter overloads the ADC even at '
                            'minimum RX gain (%.1f dBFS)' % peak))
            elif peak > self.ADC_HIGH and self.rx_gain > self.rx_min:
                out.append(('rx', self.rx_gain - 1, 'ADC peak %.1f dBFS'
                            % peak))
            elif peak < self.ADC_LOW and self.rx_gain < self.rx_max \
                    and self._tx_on():
                if self._low_since is None:
                    self._low_since = now
                elif now - self._low_since >= self.LOW_HOLD:
                    self._low_since = None
                    out.append(('rx', min(self.rx_max,
                                          self.rx_gain + self.RX_UP),
                                'ADC peak %.1f dBFS: headroom' % peak))
            if not (peak < self.ADC_LOW and self._tx_on()):
                self._low_since = None
        if not out and now >= self._tx_next:
            self._tx_next = now + self.TX_PERIOD
            fine = [r for r in reports if not r.get('impaired')]
            if fine:
                smin = min(r['snr'] if r.get('snr') is not None else 0.0
                           for r in fine)
                room = (peak is None or peak < self.ADC_HIGH - 2) and \
                    self.rx_gain >= self.rx_min + self.RX_RESERVE
                floor = self.tx_min
                if all(r.get('snr') is None for r in fine):
                    floor = max(floor, self.tx_start - self.DEAF_STEP)
                if smin < self.target - 2 and self.tx_atten > floor \
                        and room:
                    out.append(('tx', max(floor, self.tx_atten - 2),
                                'weakest neighbour hears this node at '
                                '%.1f dB' % smin))
                elif smin > self.target + 8 and self.tx_atten < self.tx_max:
                    out.append(('tx', self.tx_atten + 1,
                                'every neighbour hears this node above '
                                '%.1f dB' % smin))
        for what, value, reason in out:
            if what == 'rx':
                self.rx_gain = value
            else:
                self.tx_atten = value
            self.last_reason = reason
        if out:
            self._changed = now
            self._adc.clear()
            self.changes += 1
        return out
