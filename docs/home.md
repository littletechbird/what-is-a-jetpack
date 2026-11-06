# Where the files live

This is a written rule we use with a jetpack. It is not something every agent comes with.

A jetpack is useful only if the files actually live somewhere you can find again. Chat is a working window. Scratch space on a machine that can be wiped is also a working window. Neither one is home.

Home is the storage you chose for the library: a local disk, a networked drive, cloud storage, a phone, or a connected thumb drive. Those choices are described in the main [readme](../README.md). If a file matters, a copy of it belongs there before the work counts as done.

In practice that means:

- Create or edit in scratch if you need to.
- Land a lasting copy on the storage you chose.
- Record where that copy lives (a path, or an id in cloud storage).
- Only then call the work done, attach it, or send it.
- Do not delete a file you worked on unless that lasting copy already exists.

An automatic summary of a chat is not the archive, and it is not the memory of record for a locked decision. Write the decision into the seed or the handoff, and keep the real files in the jetpack.

If disk space on a scratch machine starts to run out, treat that as a warning that the lasting copies were not made, not as a reason to start throwing away the only copy of something useful.

## Commands

tools/keep.py is the command for this rule. It copies a file into kept/ under JETPACK_HOME. That folder is the lasting copy.

```bash
python tools/keep.py path/to/file
python tools/keep.py --check path/to/file
```

The first command lands the file in kept/. `--check` looks for a copy that is already inside the jetpack home and fails when the only copy is still outside it. A destination name that would leave the keep folder, such as `--as ../escape`, is refused. Links are resolved first: a link inside kept/ that points somewhere else is refused as a destination, and a link inside the home that points outside it does not count as a lasting copy. kept/ itself may be a link to another drive.
