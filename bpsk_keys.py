#!/usr/bin/env python3
"""Key management for the encrypted BPSK network.

r6 (mesh mode, the default)
---------------------------
Each node has ONE private key, keys/node.key, that never leaves that machine.
Its address is derived from the key, so there is nothing else to configure:

    on every node:  python3 bpsk_keys.py init

Trust is then given in the chat UI: a new node appears with its key
fingerprint, the operators compare fingerprints (read them aloud, or look at
both screens) and press Trust. Approvals are stored in keys/trusted.json.
To pre-approve without the UI:

    python3 bpsk_keys.py trust --key <public key hex> --name "Bench B"
    python3 bpsk_keys.py revoke --key <public key hex>

`show` prints this node's fingerprint and every approved key.

A node that already has an r5 key (a single node_<addr>.key) keeps using it
as its identity - same fingerprint - so an r5 lab needs no new keys. Every
key in an r5 roster.json is trusted too.

r5.x (static mode: my_addr set in the flowgraph)
-------------------------------------------------
Each node has one private key (keys/node_<addr>.key) and every node has the
same keys/roster.json: address -> PUBLIC key. Setting up two nodes, A (addr 1)
and B (addr 2):

    on A:  python3 bpsk_keys.py init --addr 1 --name "Node A"
    on B:  python3 bpsk_keys.py init --addr 2 --name "Bench B"

Each prints an `add` line. Run A's line on B and B's line on A:

    on B:  python3 bpsk_keys.py add --addr 1 --key <A's public key> --name "Node A"
    on A:  python3 bpsk_keys.py add --addr 2 --key <B's public key> --name "Bench B"

Or merge a copied roster instead:  python3 bpsk_keys.py merge other_roster.json

Then read the fingerprints aloud across the bench (`show` on both) - if they
match, nobody substituted a key in transit. A new node later (r6) is one more
`init` on that node and one more `add` everywhere else.

Commands:  init | trust | revoke | show            (r6)
           init --addr | add | remove | merge     (r5.x roster)
Options:   --keys DIR   key directory (default: ./keys, as the flowgraph uses)
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_crypto as C                                         # noqa: E402
import bpsk_secure as S                                         # noqa: E402


def _roster(keys):
    path = os.path.join(keys, 'roster.json')
    network, nodes = S.load_roster(path)
    return path, network, nodes


def cmd_init(a):
    if a.addr is None:
        return cmd_init_mesh(a)
    os.makedirs(a.keys, exist_ok=True)
    path, network, nodes = _roster(a.keys)
    if a.network:
        network = a.network
    key_path = os.path.join(a.keys, 'node_%d.key' % a.addr)
    if os.path.exists(key_path):
        priv = S.read_private(key_path)
        print('existing key for address %d kept (%s)' % (a.addr, key_path))
    else:
        priv = C.generate_private()
        S.write_private(key_path, priv)
        print('new private key for address %d written to %s (mode 600)'
              % (a.addr, key_path))
    pub = C.x25519_base(priv)
    old = nodes.get(a.addr)
    if old is not None and old['public_key'] != pub:
        print('roster entry for %d replaced (it held a different key)' % a.addr)
    nodes[a.addr] = {'public_key': pub,
                     'name': a.name or (old or {}).get('name', '')}
    S.save_roster(path, network, nodes)
    print('\npublic key   %s\nfingerprint  %s\nnetwork      %s\n'
          % (pub.hex(), S.fingerprint(pub), network))
    print('on every other node, run:\n  python3 bpsk_keys.py add --addr %d '
          '--key %s%s' % (a.addr, pub.hex(),
                          (' --name "%s"' % a.name) if a.name else ''))
    return 0


def cmd_init_mesh(a):
    os.makedirs(a.keys, exist_ok=True)
    store = S.MeshKeyStore(a.keys, create=True)
    if store.created:
        print('new private key written to %s (mode 600)' % store.key_path)
    elif store.adopted:
        print('using the existing r5 key %s as this node\'s identity'
              % store.adopted)
    else:
        print('existing key kept (%s)' % store.key_path)
    if a.network:
        store.network = a.network
        store._save_trust()
    import bpsk_mac as M
    print('\npublic key   %s\nfingerprint  %s\naddress      %d (derived; '
          'moves automatically on a collision)\nnetwork      %s\n'
          % (store.public.hex(), S.fingerprint(store.public),
             M.derive_address(store.public, store.get_state('addr_n', 0)),
             store.network))
    print('Start the flowgraph with my_addr = 0. Other nodes will show this '
          'fingerprint; approve them the same way.')
    return 0


def _pubkey(text):
    try:
        pub = bytes.fromhex((text or '').strip())
    except ValueError:
        pub = b''
    return pub if len(pub) == 32 else None


def cmd_trust(a):
    pub = _pubkey(a.key)
    if pub is None:
        print('error: --key must be 64 hex characters (a 32-byte X25519 '
              'public key)')
        return 2
    store = S.MeshKeyStore(a.keys, create=False)
    store.trust(pub, a.name)
    print('approved %s, fingerprint %s' % (a.name or 'key', S.fingerprint(pub)))
    print('confirm that fingerprint with the operator of that node')
    return 0


def cmd_revoke(a):
    pub = _pubkey(a.key)
    if pub is None:
        print('error: --key must be 64 hex characters')
        return 2
    store = S.MeshKeyStore(a.keys, create=False)
    if pub not in store.trusted and pub not in store.roster_keys:
        print('that key is not approved here')
        return 1
    if not store.revoke(pub):
        print('removed from trusted.json, but roster.json still lists it - '
              'remove it there too (bpsk_keys.py remove --addr N)')
        return 1
    print('approval withdrawn: %s' % S.fingerprint(pub))
    return 0


def cmd_add(a):
    try:
        pub = bytes.fromhex(a.key.strip())
    except ValueError:
        pub = b''
    if len(pub) != 32:
        print('error: --key must be 64 hex characters (a 32-byte X25519 '
              'public key)')
        return 2
    path, network, nodes = _roster(a.keys)
    if a.network:
        network = a.network
    nodes[a.addr] = {'public_key': pub,
                     'name': a.name or nodes.get(a.addr, {}).get('name', '')}
    S.save_roster(path, network, nodes)
    print('address %d added, fingerprint %s' % (a.addr, S.fingerprint(pub)))
    print('confirm that fingerprint with the operator of node %d' % a.addr)
    return 0


def cmd_remove(a):
    path, network, nodes = _roster(a.keys)
    if nodes.pop(a.addr, None) is None:
        print('address %d is not in the roster' % a.addr)
        return 1
    S.save_roster(path, network, nodes)
    print('address %d removed - it can no longer open a session with this '
          'node' % a.addr)
    return 0


def cmd_merge(a):
    path, network, nodes = _roster(a.keys)
    _, other = S.load_roster(a.file)
    changed = 0
    for addr, entry in sorted(other.items()):
        cur = nodes.get(addr)
        if cur is not None and cur['public_key'] != entry['public_key']:
            print('address %d: CONFLICT - keeping the existing key %s, the '
                  'file has %s. Remove it first if the new one is right.'
                  % (addr, S.fingerprint(cur['public_key']),
                     S.fingerprint(entry['public_key'])))
            continue
        if cur is None:
            nodes[addr] = entry
            changed += 1
            print('address %d added, fingerprint %s'
                  % (addr, S.fingerprint(entry['public_key'])))
    S.save_roster(path, network, nodes)
    print('%d entries added' % changed)
    return 0


def cmd_show(a):
    try:
        store = S.MeshKeyStore(a.keys, create=False)
    except S.KeyError_:
        store = None
    if store is not None:
        print('this node  %s  (%s)%s\n' % (
            S.fingerprint(store.public), store.public.hex(),
            '  [r5 key %s]' % os.path.basename(store.adopted)
            if store.adopted else ''))
        print('approved keys (%s):' % store.trusted_path)
        for pub, entry in sorted(store.trusted.items(),
                                 key=lambda kv: kv[1].get('name') or ''):
            print('  %s  %s' % (S.fingerprint(pub), entry.get('name') or '-'))
        if not store.trusted:
            print('  none yet - approve nodes in the chat UI')
        print('')
    path, network, nodes = _roster(a.keys)
    if not nodes:
        print('crypto backend: %s' % C.BACKEND)
        return 0
    own = {int(n[5:-4]) for n in os.listdir(a.keys)
           if n.startswith('node_') and n.endswith('.key')} \
        if os.path.isdir(a.keys) else set()
    print('roster %s  (network "%s")\n' % (path, network))
    print('  addr  fingerprint           name')
    for addr, entry in sorted(nodes.items()):
        print('  %4d  %s   %s%s' % (addr, S.fingerprint(entry['public_key']),
                                    entry.get('name') or '-',
                                    '   <- private key here' if addr in own
                                    else ''))
    print('\ncrypto backend: %s' % C.BACKEND)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--keys', default='keys', help='key directory')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init', help='create this node\'s key (r6); with '
                                    '--addr, an r5 key plus roster entry')
    p.add_argument('--addr', type=int, default=None)
    p.add_argument('--name', default='')
    p.add_argument('--network', default='')
    p = sub.add_parser('add', help='add another node\'s public key')
    p.add_argument('--addr', type=int, required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--name', default='')
    p.add_argument('--network', default='')
    p = sub.add_parser('remove', help='revoke a node')
    p.add_argument('--addr', type=int, required=True)
    p = sub.add_parser('merge', help='add every node from another roster.json')
    p.add_argument('file')
    sub.add_parser('show', help='this node\'s fingerprint, approved keys, '
                                'roster')
    p = sub.add_parser('trust', help='approve a node\'s public key (r6)')
    p.add_argument('--key', required=True)
    p.add_argument('--name', default='')
    p = sub.add_parser('revoke', help='withdraw an approval (r6)')
    p.add_argument('--key', required=True)
    a = ap.parse_args(argv)
    if getattr(a, 'addr', None) is not None and not 0 <= a.addr <= 254:
        ap.error('--addr must be 0..254 (255 is broadcast)')
    try:
        return {'init': cmd_init, 'add': cmd_add, 'remove': cmd_remove,
                'merge': cmd_merge, 'show': cmd_show, 'trust': cmd_trust,
                'revoke': cmd_revoke}[a.cmd](a)
    except S.KeyError_ as exc:
        print('error: %s' % exc)
        return 2


def provision(nodes, network='bpsk-lab'):
    """Create keys for several nodes and give each the full roster.

    nodes: {addr: keys_dir}. For the offline demo and the tests - on real
    hardware each node runs `init` itself so no private key is ever copied.
    """
    pubs = {}
    for addr, keys in nodes.items():
        store = S.KeyStore(keys, addr, create=True)
        pubs[addr] = store.public
    for addr, keys in nodes.items():
        S.save_roster(os.path.join(keys, 'roster.json'), network,
                      {a: {'public_key': p, 'name': 'Node %d' % a}
                       for a, p in pubs.items()})
    return pubs


def provision_mesh(dirs, network='bpsk-lab', trust=True):
    """r6: a node.key in every directory and, with trust=True, every node
    approving every other. dirs: {name: keys_dir}. For the offline demo and
    the tests only - on hardware each operator approves in the UI."""
    stores = {}
    for name, keys in dirs.items():
        stores[name] = S.MeshKeyStore(keys, create=True)
    if trust:
        for name, st in stores.items():
            st.network = network
            for other, ot in stores.items():
                if other != name:
                    st.trust(ot.public, str(other))
    return {name: st.public for name, st in stores.items()}


if __name__ == '__main__':
    sys.exit(main())
