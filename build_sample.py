#!/usr/bin/env python3
"""build_sample.py - regenerate sample-output.txt from a real run, redacting only identity.

Runs kilo_doctor.py three times (report --predict, --account, --list-archives), redacts the home
path, project folder names and session ids, and writes the sample.

The redaction is not best-effort: every rule is counted, each rule must match at least once anywhere
in the file, and a forbidden-pattern scan runs on the finished text. A rule that stops matching
therefore fails the build instead of publishing a sample that leaks identity.

The sample is a snapshot of a live store, so re-running this changes the numbers (and adds rows as
sessions appear). Only the format is meant to be stable.
"""
import argparse
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOCTOR = os.path.join(HERE, "kilo_doctor.py")

RULES = [
    (re.compile(r"C:[\\/]Users[\\/][^\\/\"]+"), "<home>", "home path"),
    (re.compile(r"\.\.\./Documents/"), "<home>/Documents/", "tail-truncated home + Documents"),
    (re.compile(r"\.\.\./AppData/"), "<home>/AppData/", "tail-truncated home + AppData"),
    (re.compile(r"VSCode_[A-Za-z0-9_]+"), "VSCode_<project>", "project folder names"),
    (re.compile(r"ses_[A-Za-z0-9]{8}[A-Za-z0-9]+"), lambda m: m.group(0)[:12] + "...", "session ids"),
]

FORBIDDEN = [
    (re.compile(r"C:[\\/]Users[\\/](?!<home>)"), "an unredacted home path survived"),
    (re.compile(r"ses_[A-Za-z0-9]{8}[A-Za-z0-9]+"), "an untruncated session id survived"),
    (re.compile(r"VSCode_(?!<project>)[A-Za-z0-9_]+"), "an unredacted project name survived"),
]

counts = {}


def run(args):
    r = subprocess.run([sys.executable, DOCTOR] + args, capture_output=True)
    out = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0 or not out.strip():
        sys.stderr.write((r.stderr or b"").decode("utf-8", "replace")[:600])
        raise SystemExit("kilo_doctor.py %s failed rc=%s" % (" ".join(args), r.returncode))
    return out.replace("\r\n", "\n").strip("\n")


def redact(text, label):
    for pat, rep, what in RULES:
        text, n = pat.subn(rep, text)
        counts[what] = counts.get(what, 0) + n
        print("  %-10s %-38s %3d" % (label, what, n))
    for pat, msg in FORBIDDEN:
        if pat.search(text):
            raise SystemExit("redaction failed in %s: %s" % (label, msg))
    return text


def excerpt(archives, keep=3):
    head, body = archives.split("\n")[:2], archives.split("\n")[2:]
    kept = body[:keep]
    tail = [l for l in body if l.startswith("TOTAL") or l.startswith("restore:")]
    rest = len(body) - len(kept) - len(tail)
    return "\n".join(head + kept + (["... (%d more rows) ..." % rest] if rest > 0 else []) + tail)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(HERE, "sample-output.txt"))
    ap.add_argument("--hours", type=int, default=12, help="burn-rate window for --account")
    ap.add_argument("--keep", type=int, default=3, help="archive rows to show before the ellipsis")
    args = ap.parse_args()

    report = redact(run(["--predict"]), "report")
    account = redact(run(["--account", "--hours", str(args.hours)]), "account")
    archives = redact(excerpt(run(["--list-archives"]), args.keep), "archives")

    missing = [what for _, _, what in RULES if not counts.get(what)]
    if missing:
        raise SystemExit("redaction rule(s) matched nothing anywhere: %s" % ", ".join(missing))

    header = """\
# Real run on Kilo Code 7.8.15, Windows. Redacted: home path -> <home>, project folder names ->
# VSCode_<project>, session ids truncated to 8 chars. Numbers and timestamps are untouched.
#
# Three modes, each a separate run of the same script: the report with the trajectory block, the
# price-free account view, and the archive index the retirement path maintains.
#
# Regenerate with: python build_sample.py
"""

    parts = [
        header,
        "# python kilo_doctor.py --predict",
        report,
        "",
        "# python kilo_doctor.py --account --hours %d" % args.hours,
        account,
        "",
        "# python kilo_doctor.py --list-archives",
        archives,
    ]

    text = "\n".join(parts).rstrip("\n") + "\n"
    with io.open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    crlf = sum(1 for i in range(1, len(text)) if text[i] == "\n" and text[i - 1] == "\r")
    print("redaction counts: %s" % counts)
    print("wrote %s  %d bytes, %d lines, CRLF=%d"
          % (args.out, len(text.encode("utf-8")), text.count("\n"), crlf))


if __name__ == "__main__":
    main()
