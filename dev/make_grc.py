#!/usr/bin/env python3
"""Generate bpsk_duplex_pluto.grc (r6.1) and compile it to the 3.10.12 layout.

Maintainer tool, not needed to run the node. The flowgraph has ~110 blocks
(four identical receive chains, measurement taps, a transmit chain), so it
is written from this script and saved through GRC's own Platform API - the
file is exactly what GRC itself would save. Then grcc generates the .py and
the embedded blocks, and the few lines where the 3.10.9 generator differs
from 3.10.12 are adjusted (version banner, flowgraph_started event, QSettings
key), so the shipped .py matches what GRC 3.10.12 produces.

    python3 dev/make_grc.py            (run from the r6.1 directory)

The embedded block sources come from dev/epy_*.py and from the node block
bpsk_duplex_pluto_link_layer.py itself; test_grc.py checks that every
generated file is byte-identical to the source embedded in the .grc.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
K = 4

PARAMS = [
    # name, type, default, label, comment
    ('arq_window', 'intx', '32', 'ARQ window (frames)',
     'Selective Repeat window, 1..64; 1 = stop-and-wait'),
    ('center_freq', 'eng_float', '910.2e6', 'Centre frequency (Hz)',
     'the same on every node: every channel sits above it'),
    ('ch_first', 'eng_float', '450e3', 'Channel 0 offset (Hz)',
     'r6.1 one-sided plan: channel k at ch_first + k*ch_spacing'),
    ('ch_spacing', 'eng_float', '600e3', 'Channel spacing (Hz)',
     'must divide radio_rate'),
    ('channel', 'intx', '-1', 'TX channel', '-1 = automatic, 0..3 fixed'),
    ('http_port', 'intx', '8088', 'Chat UI port', ''),
    ('keys_dir', 'str', 'keys', 'Key directory', ''),
    ('my_addr', 'intx', '0', 'My address', '0 = derived from the key'),
    ('nickname', 'str', '', 'Chat nickname', ''),
    ('peer_addr', 'intx', '0', 'Peer address',
     '0 = mesh; with my_addr: static two-node mode'),
    ('postamble_size', 'intx', '64', 'Postamble (bytes)', ''),
    ('preamble_size', 'intx', '64', 'Preamble (bytes)', ''),
    ('radio_rate', 'eng_float', '6e6', 'Radio sample rate',
     'r6.1: 6 MS/s, one-sided channel plan'),
    ('rx_buffer', 'intx', '65536', 'Pluto RX buffer (samples)', ''),
    ('tx_buffer', 'intx', '49152', 'Pluto TX buffer (samples)', ''),
    ('rx_gain', 'intx', '30', 'RX gain at start (dB)',
     'gain control moves it from here (agc=1)'),
    ('tx_atten', 'intx', '50', 'TX attenuation at start (dB)',
     'gain control moves it from here (agc=1)'),
    ('agc', 'intx', '1', 'Gain control (1/0)',
     '1 = closed-loop RX gain / TX attenuation; 0 = fixed (/gain to move)'),
    ('security', 'intx', '1', 'Encryption (1/0)', ''),
    ('telemetry', 'intx', '1', 'Telemetry CSV (1/0)',
     'one row per second in telemetry/'),
    ('plan_file', 'str', '', 'Static channel plan file',
     'optional JSON {"channels": {"<fingerprint|nickname>": k}}'),
    ('uri', 'str', 'ip:192.168.1.10', 'Pluto URI', ''),
]

VARS = [
    ('ack_timeout', '0.5', 'seconds before retransmit'),
    ('frag_size', '256', 'bytes of file data per frame'),
    ('max_retries', '5', ''),
    ('queue_ahead', '0.03', 'seconds of airtime queued ahead of the radio'),
    ('tx_interp', '24', 'radio samples per symbol (6 MS/s / 250 ksym/s)'),
    ('sym_rate', 'radio_rate/tx_interp',
     'symbols per second on every channel: 250k at 6 MS/s'),
    ('sps', '4', 'samples per symbol in each receive chain'),
    ('ch_rate', 'sps*sym_rate', 'each channel after its channel filter'),
    ('decim', 'tx_interp//sps', 'channel filter decimation'),
    ('ch_taps', 'firdes.low_pass_2(1.0, radio_rate, 0.5*ch_spacing, '
                '0.4167*ch_spacing, 65)',
     'pass +-175 kHz flat, stop from 425 kHz (65 dB): adjacent channels '
     'and the own transmitter on its channel'),
    ('rrc_taps', 'firdes.root_raised_cosine(1.0, ch_rate, sym_rate, 0.35, '
                 '11*sps)', 'matched filter'),
    ('tx_scale', '0.35', 'digital back-off: symbol peaks near -6 dBFS'),
    ('tx_taps', '[tx_scale*t for t in firdes.root_raised_cosine(tx_interp, '
                'tx_interp, 1.0, 0.35, 16*tx_interp+1)]',
     'RRC interpolation straight to the radio rate, 16-symbol span'),
]


def blk(name, bid, params, x, y, rot=0, comment=''):
    p = dict(params)
    if comment:
        p['comment'] = comment
    return {'name': name, 'id': bid, 'parameters': p,
            'states': {'coordinate': [x, y], 'rotation': rot,
                       'state': 'enabled'}}


def read(path):
    with open(path) as fh:
        return fh.read()


def build():
    blocks, conns = [], []

    def c(a, ap, b, bp):
        conns.append([a, str(ap), b, str(bp)])

    x0 = 8
    for i, (name, typ, val, label, comment) in enumerate(PARAMS):
        blocks.append(blk(name, 'parameter', {
            'label': label, 'type': typ,
            'value': repr(val) if typ == 'str' else val,
            'short_id': '', 'hide': 'none'}, x0 + 176 * (i % 11),
            16 + 112 * (i // 11), comment=comment))
    for i, (name, val, comment) in enumerate(VARS):
        blocks.append(blk(name, 'variable', {'value': val},
                          x0 + 176 * (i % 11), 260 + 96 * (i // 11),
                          comment=comment))
    blocks.append(blk('constel', 'variable_constellation', {
        'const_points': '[-1-1j, -1+1j, 1+1j, 1-1j]', 'dims': '1',
        'normalization': 'digital.constellation.AMPLITUDE_NORMALIZATION',
        'npwr': '1.0', 'precision': '8', 'rot_sym': '4',
        'soft_dec_lut': 'None', 'sym_map': '[0, 1, 3, 2]', 'type': 'bpsk'},
        x0 + 176 * 4, 452))
    blocks.append(blk('import_firdes', 'import',
                      {'imports': 'from gnuradio.filter import firdes'},
                      x0 + 176 * 5, 452))

    # ---------------------------------------------------------------- RX
    y = 640
    blocks.append(blk('iio_pluto_source_0', 'iio_pluto_source', {
        'type': 'fc32', 'uri': 'uri', 'frequency': '(int)(center_freq)',
        'samplerate': '(int)(radio_rate)', 'buffer_size': 'rx_buffer',
        'quadrature': 'True', 'rfdc': 'True', 'bbdc': 'True',
        'gain1': "'manual'", 'manual_gain1': 'rx_gain', 'len_tag_key': '',
        'filter_source': "'Auto'", 'filter': '', 'fpass': '0', 'fstop': '0',
        'bandwidth': '(int)(0.9*radio_rate)'}, 16, y + 520,
        comment='manual gain: the node\'s gain control sets it (IIO Attribute '
                'Sink) - an AGC in the chip would falsify the channel levels'))
    blocks.append(blk('rx_attr', 'iio_attr_sink', {
        'uri': 'uri', 'device': '"ad9361-phy"', 'attr_type': '0',
        'output': 'False', 'channel': '"voltage0"'}, 16, y + 760,
        comment='RX gain (hardwaregain) and RF bandwidth from the node'))
    blocks.append(blk('tx_attr', 'iio_attr_sink', {
        'uri': 'uri', 'device': '"ad9361-phy"', 'attr_type': '0',
        'output': 'True', 'channel': '"voltage0"'}, 16, y + 860,
        comment='TX attenuation (hardwaregain = -dB) from the node'))
    for k in range(K):
        yy = y + 300 * k
        s = '_ch%d' % k
        blocks.append(blk('chan_filter' + s, 'freq_xlating_fir_filter_xxx', {
            'type': 'ccf', 'decim': 'decim', 'taps': 'ch_taps',
            'center_freq': 'ch_first+%d*ch_spacing' % k,
            'samp_rate': 'radio_rate'}, 300, yy))
        blocks.append(blk('afc' + s, 'blocks_rotator_cc', {
            'phase_inc': '0', 'tag_inc_update': 'False'}, 560, yy,
            comment='carrier offset correction (node AFC)' if k == 0 else ''))
        blocks.append(blk('agc' + s, 'analog_agc3_xx', {
            'type': 'complex', 'attack_rate': '1.0', 'decay_rate': '1e-4',
            'reference': '1.0', 'gain': '1.0', 'max_gain': '1e5',
            'iir_update_decim': '1'}, 660, yy + 60,
            comment='normalises the channel (symbol sync and Costas loop '
                    'gains assume unit amplitude). Attack 1.0: a sample can '
                    'never leave above 1.0, so a transmitter switching on '
                    'cannot spike the symbol sync (agc2 did - it wedged)'
            if k == 0 else ''))
        blocks.append(blk('rrc' + s, 'fft_filter_xxx', {
            'type': 'ccc', 'decim': '1', 'taps': 'rrc_taps',
            'samp_delay': '0', 'nthreads': '1'}, 760, yy))
        blocks.append(blk('symbol_sync' + s, 'digital_symbol_sync_xx', {
            'type': 'cc', 'ted_type': 'digital.TED_SIGNAL_TIMES_SLOPE_ML',
            'constellation': 'constel.base()', 'sps': 'sps',
            'ted_gain': '0.1', 'loop_bw': '0.02', 'damping': '1.0',
            'max_dev': '0.01', 'osps': '1', 'resamp_type':
            'digital.IR_MMSE_8TAP', 'nfilters': '32', 'pfb_mf_taps': '[]'},
            960, yy))
        blocks.append(blk('costas' + s, 'digital_costas_loop_cc', {
            'w': '3.14/100', 'order': '2', 'use_snr': 'False'}, 1240, yy))
        blocks.append(blk('decoder' + s, 'digital_constellation_decoder_cb',
                          {'constellation': 'constel'}, 1460, yy))
        blocks.append(blk('diff_decoder' + s, 'digital_diff_decoder_bb', {
            'coding': 'digital.DIFF_DIFFERENTIAL', 'modulus': '2'}, 1660, yy))
        v = blk('bits_to_vec' + s, 'blocks_stream_to_vector', {
            'type': 'byte', 'num_items': '8192', 'vlen': '1'}, 1860, yy,
            comment='33 ms of bits per Python call; 4 s buffered, so a slow '
                    'deframer never stalls the receiver' if k == 0 else '')
        v['parameters']['minoutbuf'] = '128'
        blocks.append(v)
        blocks.append(blk('deframer' + s, 'epy_block', {
            '_source_code': read(os.path.join(HERE, 'epy_deframer.py')),
            'channel': str(k), 'max_len': '600', 'batch': '8192'},
            2060, yy))
        # measurement taps: snapshots as PDUs, C++ all the way
        for tap, src, m, n, dx in (('meas', 'costas', 512, 25000, 0),
                                   ('iq', 'afc', 4096, 100000, 1)):
            blocks.append(blk('%s_keep%s' % (tap, s), 'blocks_keep_m_in_n', {
                'type': 'complex', 'm': str(m), 'n': str(n),
                'offset': '0'}, 1240 + 420 * dx, yy + 110 + 60 * dx))
            pk = blk('%s_pkt%s' % (tap, s),
                     'blocks_stream_to_tagged_stream', {
                         'type': 'complex', 'vlen': '1',
                         'packet_len': str(m),
                         'len_tag_key': '"packet_len"'},
                     1400 + 420 * dx, yy + 110 + 60 * dx)
            # Tagged Stream to PDU waits for a whole packet: its input
            # buffer must hold two (r6.1 lesson: never wait for more input
            # than a buffer can provide).
            pk['parameters']['minoutbuf'] = str(4 * m)
            blocks.append(pk)
            blocks.append(blk('%s_pdu%s' % (tap, s), 'pdu_tagged_stream_to_pdu',
                              {'type': 'complex', 'tag': 'packet_len'},
                              1600 + 420 * dx, yy + 110 + 60 * dx))
            c(src + s, 0, '%s_keep%s' % (tap, s), 0)
            c('%s_keep%s' % (tap, s), 0, '%s_pkt%s' % (tap, s), 0)
            c('%s_pkt%s' % (tap, s), 0, '%s_pdu%s' % (tap, s), 0)
            c('%s_pdu%s' % (tap, s), 'pdus', 'link_layer', '%s%d' % (tap, k))
        blocks.append(blk('const_keep' + s, 'blocks_keep_m_in_n', {
            'type': 'complex', 'm': '32', 'n': '512', 'offset': '0'},
            1460, yy + 220))
        blocks.append(blk('const_sink' + s, 'qtgui_const_sink_x', {
            'type': 'complex', 'name': '"Channel %d"' % k, 'size': '512',
            'grid': 'True', 'autoscale': 'False', 'ymin': '-2', 'ymax': '2',
            'xmin': '-2', 'xmax': '2', 'nconnections': '1',
            'update_time': '0.20', 'gui_hint': '[%d, %d, 1, 1]'
            % (3 + k // 2, k % 2), 'tr_mode': 'qtgui.TRIG_MODE_FREE',
            'tr_slope': 'qtgui.TRIG_SLOPE_POS', 'tr_level': '0.0',
            'tr_chan': '0', 'tr_tag': '""', 'legend': 'True',
            'axislabels': 'True', 'label1': '"ch %d"' % k}, 1660, yy + 220))
        c('iio_pluto_source_0', 0, 'chan_filter' + s, 0)
        c('chan_filter' + s, 0, 'afc' + s, 0)
        c('afc' + s, 0, 'agc' + s, 0)
        c('agc' + s, 0, 'rrc' + s, 0)
        c('rrc' + s, 0, 'symbol_sync' + s, 0)
        c('symbol_sync' + s, 0, 'costas' + s, 0)
        c('costas' + s, 0, 'decoder' + s, 0)
        c('decoder' + s, 0, 'diff_decoder' + s, 0)
        c('diff_decoder' + s, 0, 'bits_to_vec' + s, 0)
        c('bits_to_vec' + s, 0, 'deframer' + s, 0)
        c('deframer' + s, 'pdus', 'link_layer', 'rx%d' % k)
        c('link_layer', 'afc%d' % k, 'afc' + s, 'cmd')
        c('costas' + s, 0, 'const_keep' + s, 0)
        c('const_keep' + s, 0, 'const_sink' + s, 0)
    # raw receiver snapshots (ADC meter, gain control) and the spectrum
    yy = y + 300 * K
    blocks.append(blk('adc_keep', 'blocks_keep_m_in_n', {
        'type': 'complex', 'm': '8192', 'n': '300000', 'offset': '0'},
        300, yy, comment='raw ADC samples, 20 snapshots a second'))
    pk = blk('adc_pkt', 'blocks_stream_to_tagged_stream', {
        'type': 'complex', 'vlen': '1', 'packet_len': '8192',
        'len_tag_key': '"packet_len"'}, 480, yy)
    pk['parameters']['minoutbuf'] = '32768'
    blocks.append(pk)
    blocks.append(blk('adc_pdu', 'pdu_tagged_stream_to_pdu',
                      {'type': 'complex', 'tag': 'packet_len'}, 680, yy))
    c('iio_pluto_source_0', 0, 'adc_keep', 0)
    c('adc_keep', 0, 'adc_pkt', 0)
    c('adc_pkt', 0, 'adc_pdu', 0)
    c('adc_pdu', 'pdus', 'link_layer', 'adc')
    for side, src, y2, hint, fc in (
            ('rx', 'iio_pluto_source_0', yy + 120, '[1, 0, 2, 2]',
             'center_freq'),
            ('tx', 'tx_interp_filter', yy + 400, '[5, 0, 2, 2]',
             'center_freq')):
        blocks.append(blk('%s_spec_keep' % side, 'blocks_keep_m_in_n', {
            'type': 'complex', 'm': '1024', 'n': '61440', 'offset': '0'},
            480, y2))
        blocks.append(blk('%s_spectrum' % side, 'qtgui_freq_sink_x', {
            'type': 'complex', 'name': '"%s spectrum"' % side.upper(),
            'fftsize': '1024', 'freqhalf': 'True',
            'wintype': 'window.WIN_BLACKMAN_hARRIS', 'norm_window': 'False',
            'fc': fc, 'bw': 'radio_rate', 'grid': 'True',
            'autoscale': 'False', 'average': '0.2', 'ymin': '-120',
            'ymax': '0', 'units': 'dB', 'nconnections': '1',
            'update_time': '0.20', 'showports': 'False', 'gui_hint': hint,
            'tr_mode': 'qtgui.TRIG_MODE_FREE', 'tr_level': '0.0',
            'tr_chan': '0', 'tr_tag': '""', 'ctrlpanel': 'False',
            'legend': 'True', 'axislabels': 'True', 'label1': '""',
            'color1': '"blue"'}, 880, y2,
            comment='1024 samples in 61440 (C++, no copy of the rest): the '
                    'GUI never holds the radio back'
            if side == 'rx' else 'own transmitter: adjacent-channel '
            'leakage (ACLR) is visible here'))
        c(src, 0, '%s_spec_keep' % side, 0)
        c('%s_spec_keep' % side, 0, '%s_spectrum' % side, 0)

    # ---------------------------------------------------------------- TX
    ty = 260 + 96 * 2 + 40
    tf = blk('tx_framer', 'epy_block', {
        '_source_code': read(os.path.join(HERE, 'epy_tx_framer.py')),
        'preamble_size': 'preamble_size', 'postamble_size': 'postamble_size',
        'sym_rate': 'sym_rate', 'max_backlog': '0.5', 'batch': '2560'},
        2700, ty + 200, comment='frames + PRBS filler, pulled by the radio')
    tf['parameters']['maxoutbuf'] = '7680'
    tf['parameters']['minoutbuf'] = '7680'
    blocks.append(tf)
    blocks.append(blk('tx_interp_filter', 'interp_fir_filter_xxx', {
        'type': 'ccf', 'interp': 'tx_interp', 'taps': 'tx_taps',
        'samp_delay': '0'}, 2960, ty + 200))
    blocks.append(blk('tx_shift', 'blocks_rotator_cc', {
        'phase_inc': '0', 'tag_inc_update': 'False'}, 3200, ty + 200,
        comment='puts the transmitter on its channel (node tx_ctl)'))
    blocks.append(blk('iio_pluto_sink_0', 'iio_pluto_sink', {
        'type': 'fc32', 'uri': 'uri', 'frequency': '(int)(center_freq)',
        'samplerate': '(int)(radio_rate)', 'buffer_size': 'tx_buffer',
        'cyclic': 'False', 'attenuation1': 'tx_atten', 'len_tag_key': '',
        'filter_source': "'Auto'", 'filter': '', 'fpass': '0', 'fstop': '0',
        'bandwidth': '(int)(0.9*radio_rate)'}, 3420, ty + 180,
        comment='one LO for TX and RX on every node'))
    c('tx_framer', 0, 'tx_interp_filter', 0)
    c('tx_interp_filter', 0, 'tx_shift', 0)
    c('tx_shift', 0, 'iio_pluto_sink_0', 0)
    c('link_layer', 'tx_frame', 'tx_framer', 'frames')
    c('link_layer', 'tx_en', 'tx_framer', 'ctl')
    c('link_layer', 'tx_ctl', 'tx_shift', 'cmd')
    c('tx_framer', 'status', 'link_layer', 'txstat')
    c('link_layer', 'rx_attr', 'rx_attr', 'attr')
    c('link_layer', 'tx_attr', 'tx_attr', 'attr')

    # ----------------------------------------------------------- the node
    blocks.append(blk('blocks_message_strobe_0', 'blocks_message_strobe', {
        'msg': 'pmt.intern("tick")', 'period': '10'}, 2420, ty,
        comment='drives ARQ timers, beacons and the node\'s housekeeping'))
    blocks.append(blk('qtgui_edit_box_msg_0', 'qtgui_edit_box_msg', {
        'type': 'string', 'value': '""',
        'label': '"Console  (the chat lives in the browser; /stats, /gain, '
                 '/help here)"',
        'is_pair': 'False', 'is_static': 'True', 'key': '""',
        'gui_hint': '[0, 0, 1, 2]'}, 2420, ty + 100,
        comment='fallback console - the chat UI is served on http_port'))
    blocks.append(blk('link_layer', 'epy_block', {
        '_source_code': read(os.path.join(ROOT,
                                          'bpsk_duplex_pluto_link_layer.py')),
        'my_addr': 'my_addr', 'peer_addr': 'peer_addr',
        'ack_timeout': 'ack_timeout', 'max_retries': 'max_retries',
        'frag_size': 'frag_size', 'queue_ahead': 'queue_ahead',
        'sym_rate': 'sym_rate',
        'overhead': 'preamble_size+postamble_size+12', 'rx_dir': '"."',
        'nickname': 'nickname', 'http_port': 'http_port', 'open_ui': 'True',
        'center_freq': 'center_freq', 'security': 'security',
        'keys_dir': 'keys_dir', 'window': 'arq_window', 'channel': 'channel',
        'ch_spacing': 'ch_spacing', 'ch_first': 'ch_first',
        'radio_rate': 'radio_rate', 'rx_gain': 'rx_gain',
        'tx_atten': 'tx_atten', 'agc': 'agc', 'telemetry': 'telemetry',
        'plan_file': 'plan_file', 'count_ou': 'True'}, 2420, ty + 600,
        comment='MAC + link layer + encryption + gain control + chat UI'))
    c('blocks_message_strobe_0', 'strobe', 'link_layer', 'tick')
    c('qtgui_edit_box_msg_0', 'msg', 'link_layer', 'chat_in')

    options = {'parameters': {
        'author': 'Rivija Pesara (framing from Barry Duggan BPSK example)',
        'catch_exceptions': 'False',
        'description': 'BPSK network node: 4-channel FDMA mesh (6 MS/s, '
                       'one-sided plan), self-assigned channels and '
                       'addresses, closed-loop gain staging, Selective Repeat '
                       'ARQ per peer, end-to-end encryption per pair, browser '
                       'chat (r6.1-mesh)',
        'gen_linking': 'dynamic', 'generate_options': 'qt_gui',
        'id': 'bpsk_duplex_pluto', 'max_nouts': '0',
        'output_language': 'python', 'run': 'True',
        'run_command': '{python} -u {filename}',
        'title': 'BPSK Mesh Node (Pluto, 4-channel FDMA, r6.1)'},
        'states': {'coordinate': [8, 8], 'rotation': 0, 'state': 'enabled'}}
    return {'options': options, 'blocks': blocks, 'connections': conns,
            'metadata': {'file_format': 1, 'grc_version': '3.10.12.0'}}


def save(data, path):
    from gnuradio import gr
    from gnuradio.grc.core.platform import Platform
    platform = Platform(name='GNU Radio Companion', prefs=gr.prefs(),
                        version=gr.version(),
                        version_parts=(gr.major_version(), gr.api_version(),
                                       gr.minor_version()))
    platform.build_library()
    fg = platform.make_flow_graph()
    fg.import_data(data)
    fg.rewrite()
    fg.validate()
    errors = fg.get_error_messages()
    if errors:
        for e in errors:
            print('GRC:', e)
    platform.save_flow_graph(path, fg)
    text = read(path).replace("grc_version: %s" % gr.version(),
                              "grc_version: 3.10.12.0")
    with open(path, 'w') as fh:
        fh.write(text)
    return errors


def to_31012(text):
    """The 3.10.9 generator's output, in the 3.10.12 generator's layout."""
    text = text.replace('# GNU Radio version: 3.10.9.2',
                        '# GNU Radio version: 3.10.12.0')
    text = text.replace('Qt.QSettings("GNU Radio", "bpsk_duplex_pluto")',
                        'Qt.QSettings("gnuradio/flowgraphs", '
                        '"bpsk_duplex_pluto")')
    if 'import threading' not in text:
        text = text.replace('import sip\n', 'import sip\nimport threading\n', 1)
    text = re.sub(r'(            print\(f"Qt GUI: Could not restore geometry: '
                  r'\{str\(exc\)\}", file=sys.stderr\)\n)',
                  r'\1        self.flowgraph_started = threading.Event()\n',
                  text, count=1)
    text = text.replace('    tb.start()\n\n    tb.show()',
                        '    tb.start()\n    tb.flowgraph_started.set()\n\n'
                        '    tb.show()')
    return text


def main():
    data = build()
    out = tempfile.mkdtemp(prefix='bpsk_grc_')
    grc = os.path.join(out, 'bpsk_duplex_pluto.grc')
    errors = save(data, grc)
    if errors:
        sys.exit('flowgraph does not validate')
    res = subprocess.run([sys.executable, shutil.which('grcc') or
                          '/usr/bin/grcc', '-o', out, grc],
                         capture_output=True, text=True)
    if res.returncode:
        print(res.stdout, res.stderr)
        sys.exit('grcc failed')
    py = os.path.join(out, 'bpsk_duplex_pluto.py')
    with open(py) as fh:
        text = to_31012(fh.read())
    with open(os.path.join(ROOT, 'bpsk_duplex_pluto.py'), 'w') as fh:
        fh.write(text)
    shutil.copy(grc, os.path.join(ROOT, 'bpsk_duplex_pluto.grc'))
    for name in sorted(os.listdir(out)):
        if name.startswith('bpsk_duplex_pluto_') and name.endswith('.py'):
            shutil.copy(os.path.join(out, name), os.path.join(ROOT, name))
            print('embedded block', name)
    print('wrote bpsk_duplex_pluto.grc / .py (%d blocks, %d connections)'
          % (len(data['blocks']), len(data['connections'])))


if __name__ == '__main__':
    main()
