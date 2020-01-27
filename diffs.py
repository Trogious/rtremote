from utils import Logger


def map_diff(old, new):
    diff = {}
    for key in old.keys():
        if old[key] != new[key]:
            diff[key] = new[key]
    return diff


def map_get_multi_diff(old, new):
    map_old = {t.hash: t.__dict__ for t in old}
    map_new = {t.hash: t.__dict__ for t in new}
    common_hashes = set(map_old.keys()) & set(map_new.keys())
    diff = {}
    deleted_hashes = [t.hash for t in old if t.hash not in common_hashes]
    if len(deleted_hashes) > 0:
        diff['del'] = deleted_hashes
    new_hashes = [t.__dict__ for t in new if t.hash not in common_hashes]
    if len(new_hashes) > 0:
        diff['new'] = new_hashes
    changed_hashes = []
    for ch in common_hashes:
        t_diff = map_diff(map_old[ch], map_new[ch])
        if len(t_diff.keys()) > 0:
            t_diff['hash'] = ch
            changed_hashes.append(t_diff)
    if len(changed_hashes) > 0:
        diff['changed'] = changed_hashes
    return diff
