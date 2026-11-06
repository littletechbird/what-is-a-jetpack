#!/usr/bin/env python3
"""Write and check the sleep files: a handoff and a seed.

The write-up is in docs/sleep.md. These files live under state/ in the
jetpack home so a fresh session can start from them instead of from the chat.

  python tools/sleep.py init
  python tools/sleep.py write-handoff notes.md
  python tools/sleep.py write-seed rules.md
  python tools/sleep.py twin /path/to/durable
  python tools/sleep.py wake
"""
import argparse
import datetime
import os
import shutil
import sys

from home import jetpack_home

HANDOFF_NAME = "handoff.md"
SEED_NAME = "seed.md"

HANDOFF_TEMPLATE = """# Handoff

Written for someone who was not in the room.

## Done today

## Open

## Blockers

## Decisions locked today

## Next-wake actions

## Pointers
"""

SEED_TEMPLATE = """# Seed

The standing picture a brand-new copy has to know on day one.

## Preferences

## Standing rules

## How to work
"""


def state_dir():
    path = os.path.join(jetpack_home(), "state")
    os.makedirs(path, exist_ok=True)
    return path


def handoff_path():
    return os.path.join(state_dir(), HANDOFF_NAME)


def seed_path():
    return os.path.join(state_dir(), SEED_NAME)


def backup_file(path):
    if not os.path.isfile(path):
        return None
    stamp = datetime.date.today().isoformat()
    dest = f"{path}.bak-{stamp}"
    if os.path.exists(dest):
        dest = f"{path}.bak-{stamp}-{datetime.datetime.now().strftime('%H%M%S')}"
    shutil.copy2(path, dest)
    return dest


def write_text(path, text, do_backup=True):
    if do_backup:
        backup_file(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read_input(source):
    if source is None or source == "-":
        return sys.stdin.read()
    with open(source, encoding="utf-8") as handle:
        return handle.read()


def pointer_paths(text):
    found = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if heading == "pointers":
                in_section = True
                continue
            if in_section:
                break
            continue
        if in_section:
            item = line.strip().lstrip("-").strip()
            if item:
                found.append(item)
    return found


def looks_like_path(item):
    if item.startswith("/") or item.startswith("~") or ":\\" in item or item.startswith("\\\\"):
        return True
    if os.sep in item or (os.altsep and os.altsep in item):
        return True
    return os.path.exists(os.path.expanduser(item))


def cmd_init(_args):
    for path, template in ((handoff_path(), HANDOFF_TEMPLATE), (seed_path(), SEED_TEMPLATE)):
        if not os.path.exists(path):
            write_text(path, template, do_backup=False)
    print(handoff_path())
    print(seed_path())
    return 0


def cmd_backup(_args):
    paths = []
    for path in (handoff_path(), seed_path()):
        dest = backup_file(path)
        if dest:
            paths.append(dest)
    if not paths:
        sys.exit("nothing to back up: write the handoff and seed first")
    for path in paths:
        print(path)
    return 0


def cmd_write_handoff(args):
    text = read_input(args.file)
    if not text.strip():
        sys.exit("handoff is empty")
    print(write_text(handoff_path(), text))
    return 0


def cmd_write_seed(args):
    text = read_input(args.file)
    if not text.strip():
        sys.exit("seed is empty")
    print(write_text(seed_path(), text))
    return 0


def cmd_twin(args):
    dest_root = os.path.abspath(args.dest)
    os.makedirs(dest_root, exist_ok=True)
    copied = []
    for path in (handoff_path(), seed_path()):
        if not os.path.isfile(path):
            sys.exit(f"missing: {os.path.basename(path)}")
        dest = os.path.join(dest_root, os.path.basename(path))
        shutil.copy2(path, dest)
        copied.append(dest)
    for path in copied:
        print(path)
    return 0


def cmd_wake(_args):
    missing = []
    for label, path in (("handoff", handoff_path()), ("seed", seed_path())):
        if os.path.isfile(path):
            print(f"{label}: {path}")
        else:
            print(f"{label}: missing")
            missing.append(path)
    if os.path.isfile(handoff_path()):
        with open(handoff_path(), encoding="utf-8") as handle:
            pointers = pointer_paths(handle.read())
        if pointers:
            print("pointers:")
            for item in pointers:
                if looks_like_path(item):
                    resolved = os.path.abspath(os.path.expanduser(item))
                    if os.path.exists(resolved):
                        print(f"  ok  {resolved}")
                    else:
                        print(f"  missing  {item}")
                        missing.append(item)
                else:
                    print(f"  note  {item}")
    return 1 if missing else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Write and check the sleep handoff and seed in the jetpack."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create empty handoff and seed templates if they are missing").set_defaults(func=cmd_init)
    sub.add_parser("backup", help="save a dated backup of the current handoff and seed").set_defaults(func=cmd_backup)

    write_h = sub.add_parser("write-handoff", help="replace the handoff from a file or stdin")
    write_h.add_argument("file", nargs="?", help="markdown file, or omit to read stdin")
    write_h.set_defaults(func=cmd_write_handoff)

    write_s = sub.add_parser("write-seed", help="replace the seed from a file or stdin")
    write_s.add_argument("file", nargs="?", help="markdown file, or omit to read stdin")
    write_s.set_defaults(func=cmd_write_seed)

    twin = sub.add_parser("twin", help="copy the handoff and seed to another folder")
    twin.add_argument("dest", help="folder that should receive the copies")
    twin.set_defaults(func=cmd_twin)

    sub.add_parser("wake", help="check that the handoff, seed, and listed pointers still exist").set_defaults(func=cmd_wake)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
