from diffs import map_diff, map_get_multi_diff
from model import Torrent

HASH_A = 'A' * 40
HASH_B = 'B' * 40


def make_torrent(**attrs):
    t = Torrent()
    t.__dict__.update(attrs)
    return t


def test_map_diff_reports_changed_keys_only():
    assert map_diff({'a': 1, 'b': 2}, {'a': 1, 'b': 3}) == {'b': 3}
    assert map_diff({'a': 1}, {'a': 1}) == {}


def test_map_diff_tolerates_removed_key():
    # a poll may return an object missing a key the previous one had; this used
    # to KeyError, permanently stalling every subsequent updater push
    assert map_diff({'a': 1, 'gone': 2}, {'a': 1}) == {}
    assert map_diff({'a': 1, 'gone': 2}, {'a': 5}) == {'a': 5}


def test_map_diff_reports_added_key():
    assert map_diff({'a': 1}, {'a': 1, 'b': 2}) == {'b': 2}


def test_map_get_multi_diff_tolerates_removed_key():
    old = [make_torrent(hash=HASH_A, name='x', extra=1)]
    new = [make_torrent(hash=HASH_A, name='y')]
    diff = map_get_multi_diff(old, new)
    assert diff['changed'] == [{'name': 'y', 'hash': HASH_A}]
    assert 'new' not in diff and 'del' not in diff


def test_map_get_multi_diff_removed_key_alone_is_silent():
    old = [make_torrent(hash=HASH_A, name='x', extra=1)]
    new = [make_torrent(hash=HASH_A, name='x')]
    assert map_get_multi_diff(old, new) == {}


def test_map_get_multi_diff_new_and_del():
    old = [make_torrent(hash=HASH_A, name='x')]
    new = [make_torrent(hash=HASH_B, name='y')]
    diff = map_get_multi_diff(old, new)
    assert diff['del'] == [HASH_A]
    assert diff['new'] == [new[0].__dict__]
    assert 'changed' not in diff
