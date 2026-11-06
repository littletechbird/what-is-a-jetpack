# Sleep

Sleep is a written procedure we use with a jetpack. It is not something every agent comes with, and it is not a built-in feature of any one chat product. We wrote it because long conversations get expensive and worse at the actual work, and summarizing the chat in place is not the same as starting a new day.

It works like sleep for a person. At the end of the day, the agent packs up, writes what matters into the jetpack, and the next day starts fresh by reading those files. The jetpack is the place the notes live. The chat is not.

This is the same idea as the rest of the jetpack: keep a large body of information on storage you control, then read only what you need. Here the “library” is yesterday’s work: what got done, what is still open, the rules that still apply, and pointers to the files.

A useful public write-up of doing this kind of work overnight, while nobody is waiting, is Letta’s note on [sleep-time compute](https://www.letta.com/blog/sleep-time-compute/). Research on [context rot](https://www.trychroma.com/research/context-rot) is why we do not keep the whole day loaded on purpose.

## What to write

Two files are enough for a fresh start. Both live in the jetpack (local disk, a network drive, cloud storage, or whatever home you chose).

The handoff is written for someone who was not in the room. It should be short and readable on its own. A good shape is the one described in Anthropic’s note on [effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents):

- What got done today
- What is still open, in priority order
- Blockers, each with the exact question that would unblock them
- Decisions that locked today
- The first few moves for the next wake
- Pointers to files, not copies of those files dumped into the note

Pointers beat dumps. That is the same lesson [Manus wrote about](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) for agent context: keep the path, not a lossy paste of the artifact.

The seed is the standing picture a brand-new copy has to know on day one: preferences, hard rules, and how to work. A new copy of an agent does not inherit the old chat or the old memory. The seed and the handoff are its memory.

If the same correction keeps coming up, fold it into the seed or into a short lesson note. Keep a dated backup or a one-line changelog so nothing important disappears silently. That overnight tidy is in the same family as [Mem0’s Dream](https://mem0.ai/blog/dream-background-memory-consolidation-for-ai-agents/).

Before sleep counts as finished, copy the handoff, the seed, and any new files you would miss onto the durable storage you chose. Chat and scratch disks are not the only copy. See [Where the files live](home.md).

## A clean stop

Finish work that can be finished, or park it so someone can pick it up from the files. Do not leave an irreversible action half-done: no send, merge, purchase, or delete sitting in mid-flight. Anything waiting on a person goes under blockers, with the exact ask.

Before you overwrite yesterday’s handoff, save a dated backup next to it.

Do not treat an automatic chat summary as the nightly fix. Sleep ends with a fresh session that has not read the old transcript. That fresh start is the point.

## The next morning

Read the handoff and the seed. Check that the top file pointers still exist. Take the first open item. If nothing needs doing, stay quiet.

The agent can write the files. That part is reversible. Starting a fresh copy, pointing it at those files, and retiring the old copy are decisions for the person who owns the setup.

Skip sleep while a send, merge, or other live irreversible step is still in flight. Finish or park that first. By default, run it once when the day is done, or when someone asks for a fresh start.

Sleep is done when the handoff and seed are on disk, the durable copies exist, nothing irreversible is half-done, and a new session could wake from the files alone.
