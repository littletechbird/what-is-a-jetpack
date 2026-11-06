#!/usr/bin/env python3
"""Land a lasting copy in the jetpack home.

The write-up is in docs/home.md. Chat and scratch disks are working windows.
If a file matters, a copy belongs in the storage you chose before the work
counts as done.

  python tools/keep.py path/to/file
  python tools/keep.py --check path/to/file
"""
import argparse
import os
import shutil
import sys

from home import jetpack_home


def kept_dir():
    path = os.path.join(jetpack_home(), "kept")
    os.makedirs(path, exist_ok=True)
    return path


def within(parent, path):
    parent = os.path.abspath(parent)
    path = os.path.abspath(path)
    try:
        return os.path.commonpath([parent, path]) == parent
    except ValueError:
        return False


def dest_for(source, dest_root, name):
    base = name or os.path.basename(os.path.abspath(source.rstrip(os.sep)))
    if not base or base in (".", ".."):
        sys.exit("could not pick a destination name")
    dest = os.path.abspath(os.path.join(dest_root, base))
    if not within(dest_root, dest):
        sys.exit("destination would leave the keep folder")
    return dest


def copy_into(source, dest):
    source = os.path.abspath(source)
    if not os.path.exists(source):
        sys.exit(f"not found: {os.path.basename(source)}")
    if os.path.isdir(source):
        if os.path.exists(dest):
            sys.exit(f"already exists: {os.path.basename(dest)}")
        shutil.copytree(source, dest)
    else:
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(source, dest)
    return dest


def lasting_copy(source, dest_root=None, name=None):
    source = os.path.abspath(source)
    dest_root = os.path.abspath(dest_root) if dest_root else os.path.join(jetpack_home(), "kept")
    dest = dest_for(source, dest_root, name)
    if os.path.exists(dest):
        return dest
    if within(jetpack_home(), source) and os.path.exists(source):
        return source
    return None


def cmd_keep(args):
    dest_root = os.path.abspath(args.dest) if args.dest else kept_dir()
    os.makedirs(dest_root, exist_ok=True)
    dest = dest_for(args.path, dest_root, args.name)
    print(copy_into(args.path, dest))
    return 0


def cmd_check(args):
    found = lasting_copy(args.path, args.dest, args.name)
    if found:
        print(found)
        return 0
    sys.exit("no lasting copy found")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copy a file into the jetpack home so scratch is not the only copy."
    )
    parser.add_argument("path", help="file or folder to keep")
    parser.add_argument("--as", dest="name", help="name to use inside the keep folder")
    parser.add_argument("--dest", help="folder that should receive the copy; default is kept/ in the jetpack home")
    parser.add_argument("--check", action="store_true", help="only report whether a lasting copy already exists")
    args = parser.parse_args(argv)
    if args.check:
        return cmd_check(args)
    return cmd_keep(args)


if __name__ == "__main__":
    sys.exit(main())
