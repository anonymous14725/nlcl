# NLCL — Natural Language as a Computational Language

**Author of the idea:** M. M. Shamshiri, independent researcher (concept first developed in 2019)
**Status:** working software prototype, released openly so that others can continue it

## The idea in one paragraph

Today's language models break text into statistical sub-word tokens and learn meaning afterwards from data.
This proposal starts from the other end: **meaning is the unit of computation from the first step.** A sentence
is reduced to semantic tokens, the tokens form a computational structure, and that structure — not the text —
is what gets executed, stored and translated into code. Natural language is then not an interface placed in
front of a programming language; it is the programming language. The long-term vision is that such a language
could sit at the core of an operating system, and eventually be accelerated by dedicated hardware.

## What makes it different from "natural language to code"

1. **Three kinds of semantic tokens in one vocabulary** — atomic (`CREATE`, `OBJECT`, `CONNECT`), composite
   (`CREATE + OBJECT → CREATE_OBJECT`), and adaptive (new words and commands learned at run time, each
   validated by a dry run before it is accepted).
2. **A universal, language-independent representation.** Every instruction becomes an operation on entities,
   properties, behaviours and links, e.g.
   `CREATE(domain=GAME, type=object, properties={health: 100, movable: true})`.
3. **A universal concept of "object".** "Create an object" is not guessed: the system asks which domain is
   meant, because the same object is a GameObject, a Python class, a register in hardware or a CAD part.
4. **Persistent state.** Each sentence modifies the same system; it does not generate a new unrelated program.
5. **Several domains at once.** One description can produce firmware, hardware description, a server, a 3D
   model and an application, with a shared contract for every connection between them.
6. **A deterministic path.** For supported sentences the same input always gives the same structure, with no
   model in the loop.

## What the prototype does today

- Offline pipeline for English: semantic tokens → composite tokens → IR → persistent state → code.
- Eight pluggable domain modules: Python, game (Unity / Godot / HTML5 preview), embedded (Arduino / ESP32 /
  MicroPython / Wokwi), hardware (Verilog + testbench), C++, 3D (three.js / Blender), CAD (OpenSCAD), and
  applications (desktop GUI for Windows / macOS / Linux plus an installable web app for Android / iOS).
- Multi-module systems: links such as `connect the sensor to the dashboard via http` become one channel
  contract implemented on both sides.
- Behaviour rules in plain sentences (`when ghost touches bob, decrease bob lives by 1`), attached to the
  entity whose state they change.
- Other human languages (for example Persian) through an online agent that rewrites the sentence into the
  language's own English commands.
- A bug-fix command for any file, folder or generated module: real diagnostics first, then an online agent
  that proposes small edits, with backup, re-check and rollback.
- Only free tools and free model APIs; the core needs nothing but the Python standard library.
- 111 automated tests pass.

## What it does not do yet (stated plainly)

- The offline parser covers common sentence shapes, not all of English. It still misreads some sentences.
- Modules are written by hand. The system learns new words and commands, not new modules.
- The online features (other languages, bug fixing) were tested only with a stand-in model, not a real one.
- Generated C++, Verilog and Arduino code has not been compiled; only Python, JavaScript and the desktop/web
  application output were actually executed.
- The operating-system core and the dedicated hardware from the original concept are not started. The software
  prototype is the baseline the concept asked for first.

## Open research questions

1. Can semantic tokens replace or complement statistical tokens inside a language model, rather than only in
   front of it?
2. Can a system extend its own semantic vocabulary and rules safely and consistently at scale?
3. How far can one universal object representation stretch across software, games, 3D, hardware and devices?
4. Would a natural-language runtime with persistent computational state be a sound basis for an operating
   system shell or kernel interface?
5. Is there any workload where dedicated hardware for semantic operations pays off?

## Why it is being shared

I do not have the funding or the time to build the full architecture. I am publishing the idea and this
prototype so that people and organisations with more resources can evaluate it, improve it, or take it
further. I am not claiming that every component is new; I am proposing the combined architecture and asking
for it to be judged on its merits.

## How it was built

The prototype was written with the help of **Claude** (Anthropic), through Claude Code. Most of the code was
written by **Claude Opus 5.5**; later features and fixes were written by **Claude Fable 5.2**. The idea,
its goals and the design decisions are mine; this does not imply any endorsement by Anthropic.

## Licence

Released under the MIT licence (see `LICENSE`): anyone may use, change and build on it, including commercially.

---

*M. M. Shamshiri — Independent Researcher*
