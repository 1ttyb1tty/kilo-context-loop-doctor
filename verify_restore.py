#!/usr/bin/env python3
"""
verify_restore.py - prove that archived sessions can be restored.

`kilo_doctor.py` archives a session (and its child sessions) with `kilo export` before deleting
anything. This checks the other end of that promise: that each archive imports cleanly and lands
with the same message and part counts, in an ISOLATED scratch store, so the real store is untouched.

    python verify_restore.py                                  # a couple of archives, auto-discovered
    python verify_restore.py D:\\backups\\ses_a.json ses_b.json
    python verify_restore.py --all                            # every archive (slow on big sets)

Nothing is deleted here, and nothing is imported into the real store.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kilo_doctor import find_kilo
except Exception:
    def find_kilo():
        env = os.environ.get("KILO_BIN")
        if env and os.path.isfile(env):
            return env
        raise SystemExit("could not import find_kilo from kilo_doctor.py; set KILO_BIN")


def default_archive_dir():
    return os.path.join(os.path.expanduser("~"), ".local", "share", "kilo", "retired-sessions")


def counts_in(store_env, cwd):
    r = subprocess.run([KILO, "db",
                        "SELECT id, (SELECT COUNT(*) FROM message m WHERE m.session_id=s.id), "
                        "(SELECT COUNT(*) FROM part p WHERE p.session_id=s.id) FROM session s"],
                       capture_output=True, env=store_env, cwd=cwd)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    rows = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3:
            rows[parts[0]] = (int(parts[1] or 0), int(parts[2] or 0))
    return rows


def main():
    global KILO
    ap = argparse.ArgumentParser()
    ap.add_argument("archives", nargs="*", help="archive JSON files (default: discover them)")
    ap.add_argument("--archive-dir", default=default_archive_dir())
    ap.add_argument("--all", action="store_true", help="check every archive, not just two")
    args = ap.parse_args()
    KILO = find_kilo()

    paths = list(args.archives)
    if not paths:
        found = sorted(glob.glob(os.path.join(args.archive_dir, "*.json")))
        if not found:
            sys.exit("no archives in %s - pass paths explicitly" % args.archive_dir)
        paths = found if args.all else [found[0], found[-1]]
    print("checking %d archive(s)" % len(paths))

    scratch = tempfile.mkdtemp(prefix="kilo-restore-")
    env = dict(os.environ)
    for key, sub in (("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
        os.makedirs(os.path.join(scratch, sub), exist_ok=True)
        env[key] = os.path.join(scratch, sub)
    print("scratch store: %s" % scratch)

    ok = True
    expected = {}
    for path in paths:
        name = os.path.basename(path)
        try:
            j = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print("  %-52s UNREADABLE: %s" % (name, e))
            ok = False
            continue
        msgs = len(j.get("messages", []))
        parts = sum(len(m.get("parts", [])) for m in j.get("messages", []))
        sid = (j.get("info") or {}).get("id")
        r = subprocess.run([KILO, "import", path], capture_output=True, env=env, cwd=scratch)
        status = "rc=%d" % r.returncode
        if r.returncode != 0:
            status = "IMPORT FAILED " + status
            ok = False
        if sid:
            expected[sid] = (msgs, parts)
        print("  %-52s expected msgs=%-5d parts=%-5d  %s" % (name, msgs, parts, status))

    got = counts_in(env, scratch)
    print("\nrestored in the scratch store:")
    for sid, (m, p) in expected.items():
        gm, gp = got.get(sid, (None, None))
        match = (gm == m and gp == p)
        if not match:
            ok = False
        print("  %s  messages %s/%s  parts %s/%s  %s" % (
            sid, gm, m, gp, p, "OK" if match else "MISMATCH"))
    if not expected:
        print("  (no sessions found after import)")

    shutil.rmtree(scratch, ignore_errors=True)
    print("\n%s" % ("all archives restored with matching counts" if ok else "SOME ARCHIVES FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
