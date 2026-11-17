# NLCL — Natural Language Computational Language

**A computational language based on natural language** — a prototype implementation of the idea by M. M. Shamshiri (2019).

> Natural language is not merely an interface; it is the programming language itself. Sentences become semantic tokens, tokens become computational structures, structures become a language-independent intermediate representation (IR), and the IR generates code for multiple modules (games, Python, embedded systems, hardware/Verilog, C++, 3D scenes, and CAD) that work together.

---

## Project overview

### What has been built?

```text
Human text/speech
 → Semantic tokenization       (atomic: CREATE, OBJECT, CONNECT ...)
 → Composite tokens            (CREATE + OBJECT → CREATE_OBJECT)
 → Computational structure    (IR operations on a “persistent state”)
 → Universal intermediate representation (IR)
                                 CREATE(domain=GAME, type=object, properties={health: 100})
 → Multi-module composition   (communication channels between modules: serial / http / mqtt / spi / i2c ...)
 → Code generation             (each module targets its own environment)
 → Execution / simulation      (using free tools)
```

- **The core is fully offline and free:** it uses only the Python standard library; no `pip install` is required.
- **Free online language-model support:** English sentences are processed offline. Persian and other non-Latin languages require an online backend: the agent rewrites a sentence as English NLCL commands, then runs them through the tested pipeline. Without an API key, Persian input prompts you to use an online agent. Smart bug fixing also requires an online backend.
- **Modules are plugins:** any file in `nlcl/modules/` that provides a `MODULE` adds a new domain.

### Three tokenization mechanisms (section 2 of the proposal)

| Mechanism | Implementation |
|---|---|
| A. Atomic tokens | `nlcl/tokens.py → ATOMIC` (create, delete, connect ...) |
| B. Composite tokens | `COMPOSITE`: ordered patterns such as `CREATE + ARTICLE + OBJECT → CREATE_OBJECT`; extensible at runtime |
| C. Adaptive vocabulary | `:teach spawn create`; define new commands with `define patrol(d) as ...`. Each definition is tested before it is accepted. |

### The universal concept of an “object” (section 3)

`create an object` → the system does **not** guess the domain. It asks: *What kind of object would you like to create?* The same object has a different implementation in each domain (a Unity GameObject, a Python class, a Verilog register, an OpenSCAD model, and so on).

### Installation and usage

```bash
cd nlcl
python -m nlcl                      # interactive environment
python -m nlcl examples/game.nl --run
python -m nlcl examples/smart_greenhouse.nl --build
python -m unittest discover -s tests
```

### Free APIs (all optional)

| Backend | Environment variable | Description |
|---|---|---|
| `ollama` | — | Fully local and free (ollama.com) — `ollama pull llama3.2` |
| `groq` | `GROQ_API_KEY` | Free tier, very fast |
| `gemini` | `GEMINI_API_KEY` | Google AI Studio free tier |
| `openrouter` | `OPENROUTER_API_KEY` | `:free` models |
| `cerebras` | `CEREBRAS_API_KEY` | Free tier |
| `mistral` | `MISTRAL_API_KEY` | Free Experiment plan |
| `huggingface` | `HF_TOKEN` | Free monthly allowance |
| `github` | `GITHUB_TOKEN` | GitHub Models (free with limits) |

The first available backend is selected automatically. Change model names with `NLCL_<NAME>_MODEL` (for example, `NLCL_GROQ_MODEL`). Check status with `:backend status`. Without any API keys, everything works with the offline parser.

---

## Quick start

```bash
python -m nlcl                         # interactive natural-language runtime
python -m nlcl -c "create an object" -c "a game object" -c "give it 100 health" --build
python -m nlcl examples/smart_greenhouse.nl --trace --build
```

The interaction from the proposal works literally:

```
> create an object
System: What kind of object would you like to create? (game / python / embedded / ...)
? a game object
  created game object 'object'
System: What properties should object have? (e.g. health, speed, movable, ...)
> give it 100 health, make it movable, and add collision detection
  object: {health: 100}
  object: {movable: True}
  object: {collision: True}
> when health drops below 1, destroy it
> build it
```

### Universal IR

```
CREATE(domain=GAME, type=object, properties={"health": 100, "movable": true, "collision": true})
BEHAVIOR(target=hero, behavior={"on": "update", "if": "health < 1", "do": [{"op": "destroy"}]})
CONNECT(target=temp, dst=monitor, via=http, properties={"fields": ["temperature"], "interval_ms": 2000})
```

Ops: `CREATE SET UNSET DELETE CONNECT DISCONNECT RENAME BEHAVIOR DOMAIN FOCUS` + meta `SHOW BUILD RUN UNDO DEFINE`.
Behaviour triggers: `start update collision input:<key> timer:<ms> change:<prop> message:<src>`;
actions: `set add toggle destroy log move send call`; conditions use a tiny C-like expression language that every module renders into its own target (Python, C, C#, GDScript, JS, Verilog ...).

### Multi-module systems

Entities from different domains are connected with `connect A to B via <protocol>`. The composer turns every link into a **channel contract** (protocol, fields, interval, port/topic/baud) that both modules implement, then writes `system.json` (IR) and `SYSTEM.md` (architecture diagram + how to run each part with free tools). When two non-Python modules talk over HTTP, the Python module automatically emits a small **hub** with a live web dashboard (`http://localhost:8765`) and serial→HTTP bridges.

| domain | output | free tools |
|---|---|---|
| `app` | desktop GUI (Tkinter) + native packaging scripts + installable PWA | CPython, PyInstaller, any browser |
| `python` | objects, services, hub, bridges | CPython |
| `game` | Unity C# + Godot 4 GDScript + zero-install HTML5 preview | Godot, Unity Personal, browser |
| `embedded` | Arduino C++ per board, MicroPython, PlatformIO, Wokwi simulation | Arduino IDE, PlatformIO, wokwi.com |
| `hardware` | Verilog RTL + top + testbench + Yosys script | Icarus Verilog, GTKWave, Yosys, EDA Playground |
| `cpp` | C++17 classes + main loop + channels + CMake | g++/MinGW, clang, MSVC Build Tools |
| `3d` | three.js digital twin (live hub data) + Blender script | browser, Blender |
| `cad` | OpenSCAD parametric parts/enclosures + CadQuery | OpenSCAD, CadQuery, FreeCAD |

### REPL commands

`:help :show :ir :trace on|off :build [dir] :run [domain] :files :save :load :new :undo :domains :vocab :teach <word> <TOKEN> :forget <word> :accept :backend [status|auto|off|name] :mode rules|hybrid|llm :quit`

### Writing a new module

Create `nlcl/modules/my_mod.py`:

```python
from .base import DomainModule, BuildContext

class MyModule(DomainModule):
    name = "robotics"
    title = "ROS 2 nodes"
    keywords = ("robot", "lidar", "ros")
    kinds = {"object": {}, "robot": {"speed": 0}}
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        return {f"{e.ident}.py": "..." for e in ctx.entities}

MODULE = MyModule()
```

It is discovered automatically; its keywords join domain inference and its kinds join the vocabulary.

### Layout

```
nlcl/
  tokens.py       atomic / composite / adaptive vocabulary
  parser.py       offline rule parser (English)
  fixer.py        diagnostics for every file type + online bug-fix agent
  llm.py          free LLM backends (OpenAI-compatible, stdlib HTTP) + LLM parser
  ir.py           universal IR: Op, Entity, Link, SystemState, behaviours, expressions
  interpreter.py  applies IR to the persistent state, asks clarification questions
  composer.py     multi-module composition, channel contracts, SYSTEM.md
  runtime.py      interactive session / REPL
  modules/        domain modules (plug-ins) + _assets
examples/         .nl scripts
tests/            unittest suite
```

### What the offline parser understands (examples)

```
create 3 enemies with health 30 and color red        # groups: following "make them blue" applies to all
create a platform called floor at position 0, 0      # "called X at ...", "12 by 3 by 10", "25mm", "2 cm"
create a state machine called door with states closed, open and locked
r1 has a pwm called fan  /  put arm inside robot  /  make wheel a child of robot
hero's speed should be 9  /  the speed of hero is 4  /  make it a counter with width 8
select hero  /  change its name to knight  /  undo the last 2 changes  /  delete all enemies
when ghost touches bob, decrease bob lives by 1      # collision rule on bob, filtered "with ghost"
when hero touches gold, increase hero score by 10 and destroy gold
when the player presses space, jump  /  when I type reset, set speed to 0
when eye detects motion, turn on the led  /  when motion stops, turn off the led
when level of tank is between 20 and 80, log ok
every 3 seconds, if state is red, set state to green
when health is below 1 destroy it                    # no comma needed
add 1 to score every 3 seconds  /  make the led blink every 500 ms
when bell is pressed, increase visits count by 1 and set door angle to 90   # split across 2 entities
```

Rules are attached to the entity whose property they change (not just the last one created), names like `bob lives` / `temperature of temp` resolve to `bob.lives` / `temp.temperature`, and renaming an entity updates every rule and link that mentions it.

### Other languages (Persian, ...) — online only

There is no offline parser for non-Latin scripts. With a free backend configured, the online agent rewrites the sentence as English NLCL commands (shown to you) and the normal pipeline runs them:

```
> Create a player named julia with 100 health; destroy it when its health reaches zero.
  [groq] create a game player called julia with health 100
  created game player 'julia' {health: 100, ...}
  [groq] when health of julia is below 1, destroy julia
  julia: behaviour on update if health < 1 -> 1 action(s)
```

Without a backend the runtime answers that the online agent is needed (`:backend status`).

### Applications for every operating system (`app` domain)

```
create an app called tally with title "Tally Counter" and size 360 by 320
add a label called display to tally with value 0
add a button called plus to tally with text "+1"
add an input called step to tally with value 1 and label "Step"
when plus is pressed, increase value of display by value of step
when value of display is above 19, print 'limit reached: {value}'
build it
```

Produces, from the same description:

| target | files | how |
|---|---|---|
| Windows / macOS / Linux desktop | `app.py`, `nlcl_app.py` | Python + Tkinter (standard library); `python app.py` |
| native executables | `build_windows.bat`, `build_macos.sh`, `build_linux.sh` | free PyInstaller: `.exe`, `.app`, Linux binary + `.desktop` |
| Android / iOS / ChromeOS / browser | `web/` (installable PWA, offline cache) | serve the folder, "Add to Home screen" |

Widgets: app/window, form/panel, label, button, input, password, textarea, checkbox, switch, slider, progress, dropdown, list, canvas. Events: `when <button> is pressed`, `when <key> is pressed`, `every N seconds`, `when <prop> of <widget> changes`, `when <condition>`. Apps talk to other modules (firmware, FPGA, Python services) with `connect ... via http`. `run it` executes a self-test of both the desktop and the web build.

### Smart bug fixing (every module, and code you already have)

```
fix main.py: it crashes when the list is empty          # any file or folder on disk
fix "C:\work\old_project": the total is always zero
fix the python module: the alert never prints            # generated code of any module
fix hero: it should lose 20 health, not 10               # the module that contains an entity
check the game code                                      # diagnostics only (works offline)
:fix undo                                                # restore the files from the last fix
```

1. **Diagnose (offline, real tools):** Python compile, `node --check` for JS and for scripts inside HTML, JSON, C/C++ (`g++`/`clang++` if installed), Verilog (`iverilog`/`verilator`), OpenSCAD, GDScript (`godot`), bracket/structure checks for C#, Arduino, Verilog and every C-like file, and an optional run of the program (tracebacks become diagnostics).
2. **Fix (online agent):** the code, the diagnostics and your description go to the LLM, which returns small search/replace edits. Edits are applied with a backup (`.nlcl_backup/`), everything is re-checked, and a round that makes things worse is rolled back and reported to the agent (up to 3 rounds).
3. **Generated code:** fixes are remembered in the system state and re-applied after every rebuild.

Without a backend, `fix` only locates problems; changing code needs the online agent.

### Honest limits

- The offline parser covers common sentence shapes, not all of English. Other languages and bug fixing need a (free) online backend; their quality depends on the model you choose.
- Generated code is a solid starting point, not a verified product; only Python (and JS syntax) can be executed here without installing toolchains. Hardware/embedded output is meant for free simulators (Icarus, Wokwi).
- Dedicated hardware for semantic operations (section 6 of the proposal) is out of scope; the software prototype is the measurement baseline the proposal asks for first.

## Semantic architecture (v2): knowledge base, weighting, learning, terminal, benchmark

```
sentence ─► semantic tokens ─► candidate interpretations (rules / terminal / online teacher)
                                   │  each one is dry-run on a copy of the state
                                   ▼
                         weight = P(right)  ── learned, calibrated, stored in Prolog
                                   │
          ≥ 0.60 execute   0.35–0.60 execute and say how it was understood   < 0.35 ask (or repair / teacher)
                                   │
            yes / no answers, training and the teacher feed evidence and new rules back into the KB
```

| part | file | what it does |
|---|---|---|
| Prolog engine | `nlcl/prolog.py` | dependency-free ISO subset: resolution, cut, if-then-else, negation, findall/setof, assert |
| knowledge base | `nlcl/kb/core.pl`, `~/.nlcl/learned.pl` | domain ontology, reliability of every token/frame/intent, safety rules, terminal meanings, languages; learned facts are readable Prolog. `:kb export kb.pl` creates one file that SWI-Prolog can load |
| weighting | `nlcl/weights.py` | 17 measurable features (coverage, unknown words, invented properties, missing verb, dry-run errors, learned reliability...) → logistic weight; `:why` shows them |
| learning | `nlcl/learn.py` | correctness is semantic (same resulting state). `:train` induces rewrite rules (accepted only if they fix examples and break none, never erasing a core verb or content word), records evidence and fits weights. `:test` scores held-out sentences |
| discovery | `nlcl/discover.py` | unknown word → closest known form, asked once then remembered; online teacher's answer distilled into an offline rule; a language with no module (for example, a Kotlin program) grows a new module, persisted in the KB |
| terminal | `nlcl/shell.py` | sentence → intent + slots → exact effect (reference implementation) + bash and PowerShell text. File-changing commands run in a sandbox copy first and show the diff; destructive commands require confirmation; protected paths are refused by Prolog rules |
| benchmark | `python -m nlcl.bench` | `bench/HYPOTHESES.md` (pre-registered) → `bench/results/report.md` |

### What the benchmark measured (held-out sentences, offline, this machine)

| | untrained | trained (rules + evidence + weights in Prolog) |
|---|---|---|
| interpretation accuracy on unseen paraphrases | 45% | 47% |
| executed something that was not meant | 41% | 24% |
| nonsense / out-of-scope refused | 1/12 | 9/12 |
| calibration error of the weight (ECE) | 0.38 | 0.19 |
| identical result on repeated runs | 117/117 | 117/117 |
| terminal: generated bash + PowerShell verified in a sandbox | – | 38/38 |
| terminal: files changed without showing the diff first | – | 0 |
| cost | 0 tokens, ~1 ms | 0 tokens, ~1 ms |

Honest reading: the measurable advantages are **refusing what it does not understand, calibrated confidence, determinism, verified terminal commands, safety and zero cost**. Raw accuracy on free paraphrases is low offline (H4 in the hypotheses predicts that a language model wins there). The comparison with a language model (`llm-direct`, `nlcl+teacher`) needs an API key: `python -m nlcl.bench --backend auto`.

## Credits and licence

The idea and design goals are by M. M. Shamshiri (concept from 2019); see `PROPOSAL.md`. The prototype was written with the help of Claude (Anthropic) through Claude Code: most of the code by Claude Opus 5.5, later features and fixes by Claude Fable 5.2. This does not imply endorsement by Anthropic.

Released under the MIT licence (`LICENSE`).
