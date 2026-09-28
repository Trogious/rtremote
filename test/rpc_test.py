"""Unit tests for rpc.classify_fault: which component an rtorrent fault is blamed on."""
from rpc import classify_fault
from utils import WHERE_APP, WHERE_RTORRENT, WHERE_RTREMOTE


def test_unknown_hash_is_the_apps_stale_torrent_in_both_wordings():
    for text in ('invalid parameters: info-hash not found',  # rtorrent 0.16, rpc/rpc_manager.cc
                 'Could not find info-hash.'):               # rtorrent 0.9, rpc/xmlrpc.cc
        e = classify_fault(-500, text, 'f.multicall')
        assert e.where == WHERE_APP, text
        assert str(e) == 'rtorrent has no torrent with that hash (f.multicall)'
        assert 'refreshes on the next push' in e.hint


def test_stale_file_tracker_or_peer_is_the_apps_problem():
    for text, command in (('invalid parameters: index not found', 'f.priority.set'),
                          ('invalid parameters: hash not found', 'p.banned.set')):
        e = classify_fault(-500, text, command)
        assert e.where == WHERE_APP, text
        assert str(e) == 'rtorrent has no such file, tracker or peer on that torrent (%s)' % command


def test_untrusted_rejection_is_an_rtremote_bug():
    e = classify_fault(-501, 'Command "d.open" is not allowed for untrusted connections.', 'd.open')
    assert e.where == WHERE_RTREMOTE
    assert 'd.open' in str(e) and 'rtremote bug' in e.hint


def test_unknown_command_means_rtorrent_is_too_old():
    e = classify_fault(-506, "Method 'system.sockets.size' not defined", 'system.sockets.size')
    assert e.where == WHERE_RTORRENT
    assert '0.16' in e.hint


def test_anything_else_is_rtorrents_own_explanation():
    text = 'Cannot change the directory of an open download after the files have been moved.'
    e = classify_fault(-500, text, 'd.directory.base.set')
    assert e.where == WHERE_RTORRENT
    assert str(e) == 'rtorrent fault -500 (d.directory.base.set): ' + text
    assert e.code == -500 and e.command == 'd.directory.base.set'
