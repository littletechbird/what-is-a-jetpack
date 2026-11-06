# What a jetpack is

![Comparison card: Jev is a hosted model you call with data; a jetpack is a library on your own storage that returns the matching page and citation. Same job, different machine.](docs/jev-and-a-jetpack.jpg)

A jetpack is a way to take a large grouping of information, store it, and then do things with that data. It is headless, so it does not need a screen or a desktop app to do its job. The library can live on a local hard drive, a networked drive, or cloud storage. That includes a connected thumb drive: the files live on the stick, and the machine it is plugged into runs the process. A thumb drive will be slower than an internal disk, so indexing and search take longer, but it still works. The same idea can live locally on a mobile device, like an Android or iPhone: the library sits on the phone, and the phone runs the process against that storage. The practical limits are whatever that device allows for space, processing, and running in the background. Those are choices, not requirements. If the library lives in cloud storage and you have an internet connection, local storage is not needed. The other way around works too. You could set up a local jetpack on each person's laptop and phone so they do not need internet to use it. A local copy is also the quickest way to demonstrate it, because it does not depend on a remote connection. The main thing is query: ask a question and get a fast, accurate answer, with the original page cited, instead of reading the whole pile yourself.

Essentially, it is the cheapest way we have found to keep a large body of data that you can still do things with, and to keep context small across time with a team of agents without losing the data or the information.

People sometimes compare this to TypeSafe AI's Jev, or to RAG. The job is similar. The machine is different.

Jev is a hosted model you call as a function. You send it data and a question. It returns a typed answer with a probability. It does not write sentences, and it does not keep your library. TypeSafe describes it as judgments for your code to use, including applying the same question across a large set of records.

A typical RAG retrieves chunks of text and then freely generates an answer.

A jetpack keeps the library with you, wherever you choose to store it. It indexes the files, by words and optionally by meaning. A query returns the matching pages and citations. If nothing fits, it should say so. Writing a normal paragraph from those passages is a separate, optional step, and it should stay faithful to what was retrieved.

We also apply the same idea to sleep. That is a written procedure we use with the jetpack, not something every agent comes with. At the end of a long day, instead of keeping the whole conversation loaded, we store a short handoff of what got done, what is still open, and where the files live, plus a seed of the standing rules a fresh start has to know. The next day begins by reading those files. The write-up is in docs/sleep.md. The companion rule, that the jetpack is home and the chat is not, is in docs/home.md. The code for those two procedures is tools/sleep.py and tools/keep.py.

Jev does not store or index your files. A jetpack does. Jev returns a judgment and a confidence. A jetpack returns the source page. Both exist so a person does not have to read a huge pile to get an answer.

TypeSafe's introduction to Jev is at https://typesafe.ai/blog/introducing-system-one-models-and-jev

TypeSafe's note about not calling it a decision model is at https://typesafe.ai/blog/dont-call-it-a-decision-model

## Run it

The code under tools/ builds a jetpack from documents you choose. Put PDF, Word, HTML, or plain text files in a folder named library. A form feed in a text file starts a new page. Ingest reads that folder, writes one text file per page under text/, and builds index/jetpack.db. SQLite FTS5 is the keyword index. Python 3.11 or newer is enough for that part, as long as the standard-library SQLite includes FTS5, which it does in normal builds.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p library
python tools/ingest.py
python tools/ask.py "your question"
```

On Windows, activate the virtual environment with `.venv\Scripts\activate`.

Each page is split into chunks of about 1000 characters, with about 150 characters of overlap. A chunk stays on its own page. Ask prints the matching documents, the page numbers, and a short passage. If the index has no fitting passage, it says so. `python tools/ask.py --list` lists what ingest stored. `python tools/ask.py --open DOC_ID PAGE` prints one stored page.

You can point ingest at a different folder: `python tools/ingest.py /path/to/your/files`. The index is still written under the jetpack home.

JETPACK_HOME is that home. It is the folder that holds library, text, index, work, state, and kept. When the variable is unset, the home is the root of this repository. Set it when the files should live somewhere else:

```bash
export JETPACK_HOME=/path/to/your/jetpack
mkdir -p "$JETPACK_HOME/library"
python tools/ingest.py
python tools/ask.py "your question"
```

An optional embedding pass ranks by meaning and blends that rank with the keyword rank. It uses the BAAI/bge-small-en-v1.5 model through fastembed, on CPU, and caches the model under work/models. There is no paid search API. Skip this step and ask still answers from keywords.

```bash
python tools/embed.py
python tools/ask.py "your question"
```

`python tools/ask.py --mode bm25` forces keyword rank even after vectors exist. A file in library/safety/ is stored with topic safety, and `--topic safety` limits the search to that folder's documents.

tools/reindex.py rebuilds chunks after you edit files in text/. tools/add_standard.py adds one PDF, Word, HTML, or text file to an index that already exists. tools/remove_docs.py removes a document once another document is indexed in its place: `python tools/remove_docs.py old-id=new-id`.

tools/redact.py replaces email addresses, URLs, phone numbers, and street-like addresses. Extra literal words and phrases, such as organization names, belong in redact_terms.local.json. Copy redact_terms.example.json to that name and edit the copy. The local file is gitignored. `python tools/ingest.py --redact` applies the same scrub while indexing.

tools/sleep.py writes state/handoff.md and state/seed.md under JETPACK_HOME. tools/keep.py copies a file into kept/ in that same home. Real handoff, seed, and kept files stay out of git.

```bash
python tools/sleep.py init
python tools/sleep.py write-handoff notes.md
python tools/sleep.py write-seed rules.md
python tools/sleep.py twin /path/to/durable
python tools/sleep.py wake
python tools/keep.py path/to/file
python tools/keep.py --check path/to/file
```

The code is under the MIT license in LICENSE.
