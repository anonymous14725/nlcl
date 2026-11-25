# NLCL — Natural Language Computational Language

**NLCL is an open-source prototype exploring natural language as a computational language.** It turns English instructions into semantic operations, applies them to a persistent intermediate representation (IR), and generates code for multiple domains.

> Natural language is not merely an interface: it can be the programming language itself. Sentences become semantic tokens, tokens become computational structures, and those structures become a language-independent IR that can be compiled into cooperating modules.

This project is an independent research prototype shared to invite feedback. It is not affiliated with or endorsed by Anthropic.

## Screenshots

A look at NLCL’s interface and workflow:

<table>
  <tr>
    <td align="center"><img src="screenshots/Screenshot%202026-11-26%20024554.png" alt="NLCL screenshot 1" width="100%"></td>
    <td align="center"><img src="screenshots/Screenshot%202026-11-26%20024937.png" alt="NLCL screenshot 2" width="100%"></td>
  </tr>
  <tr>
    <td align="center"><img src="screenshots/Screenshot%202026-11-26%20025356.png" alt="NLCL screenshot 3" width="100%"></td>
    <td align="center"><img src="screenshots/Screenshot%202026-11-26%20025433.png" alt="NLCL screenshot 4" width="100%"></td>
  </tr>
</table>

## What NLCL does

```text
Natural-language instruction
  → semantic tokens and candidate interpretations
  → dry-run against a persistent state
  → universal intermediate representation (IR)
  → domain-specific code generation
  → build, run, or simulate
```

The core runs offline using only the Python standard library. English instructions are handled by the offline parser. Optional language-model backends can help with non-Latin languages, interpretation, and code repair.

## Quick start

```bash
python -m nlcl
python -m nlcl examples/game.nl --run
python -m nlcl examples/smart_greenhouse.nl --build
python -m unittest discover -s tests
```

A short interactive example:

```text
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

The same state can be represented in NLCL's IR:

```text
CREATE(domain=GAME, type=object, properties={"health": 100, "movable": true, "collision": true})
BEHAVIOR(target=hero, behavior={"on": "update", "if": "health < 1", "do": [{"op": "destroy"}]})
CONNECT(target=temp, dst=monitor, via=http, properties={"fields": ["temperature"], "interval_ms": 2000})
```

## Architecture

NLCL combines three vocabulary mechanisms:

| Mechanism | Description |
|---|---|
| Atomic tokens | Core operations such as `CREATE`, `DELETE`, and `CONNECT` (`nlcl/tokens.py`) |
| Composite tokens | Ordered patterns that combine tokens into higher-level operations |
| Adaptive vocabulary | Teach aliases and define commands; definitions are dry-run against a copy of the state before acceptance |

The interpreter applies operations to a persistent state and asks for clarification when it cannot confidently determine the intended domain or operation. The knowledge base stores ontology, learned reliability, safety rules, and learned vocabulary. The weighting model uses measurable interpretation features to estimate confidence; `:why` explains a score.

| Component | Location | Role |
|---|---|---|
| Parser | `nlcl/parser.py` | Offline English rule parser |
| IR and interpreter | `nlcl/ir.py`, `nlcl/interpreter.py` | State, operations, and interpretation |
| Knowledge base | `nlcl/kb/`, `nlcl/knowledge.py` | Ontology, reliability, and learned facts |
| Learning and weighting | `nlcl/learn.py`, `nlcl/weights.py` | Learn rewrite rules and calibrate confidence |
| Modules | `nlcl/modules/` | Domain-specific generators and tools |
| Terminal | `nlcl/shell.py` | Interpret terminal requests and verify command effects in a sandbox |
| Benchmark | `nlcl/bench/` | Reproducible evaluation suites |

## Code-generation domains

| Domain | Example output | Free tools and runtimes |
|---|---|---|
| `app` | Tkinter desktop app, packaging scripts, installable PWA | CPython, PyInstaller, browser |
| `python` | Objects, services, hubs, and bridges | CPython |
| `game` | Unity C#, Godot GDScript, HTML5 preview | Unity Personal, Godot, browser |
| `embedded` | Arduino C++, MicroPython, PlatformIO projects | Arduino IDE, PlatformIO, Wokwi |
| `hardware` | Verilog RTL, testbench, synthesis scripts | Icarus Verilog, GTKWave, Yosys |
| `cpp` | C++17 classes, main loop, channels, CMake | g++, Clang, MSVC Build Tools |
| `3d` | Three.js digital twin and Blender script | Browser, Blender |
| `cad` | OpenSCAD parts and CadQuery models | OpenSCAD, CadQuery, FreeCAD |

Modules are plugins: a module in `nlcl/modules/` that provides a `MODULE` can add a domain. Different domain entities can be connected with channel contracts such as HTTP, MQTT, serial, SPI, or I²C. A composed system can include `system.json` and a `SYSTEM.md` guide.

## Example instructions

The offline parser supports common instruction patterns, including:

```text
create 3 enemies with health 30 and color red
create a platform called floor at position 0, 0
create a state machine called door with states closed, open and locked
make wheel a child of robot
hero's speed should be 9
when hero touches gold, increase hero score by 10 and destroy gold
every 3 seconds, if state is red, set state to green
```

It is a rule-based prototype, not a general natural-language understanding system. It supports common sentence forms rather than all of English.

## Optional language-model backends

Backends are optional. Without a configured backend, the offline English parser still works. A backend can be used for non-Latin input, online interpretation, and assisted code fixes.

| Backend | Environment variable |
|---|---|
| Ollama | Local configuration |
| Groq | `GROQ_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |
| Cerebras | `CEREBRAS_API_KEY` |
| Mistral | `MISTRAL_API_KEY` |
| Hugging Face | `HF_TOKEN` |
| GitHub Models | `GITHUB_TOKEN` |

The first available backend is selected automatically. Set a model with `NLCL_<NAME>_MODEL` (for example, `NLCL_GROQ_MODEL`) or inspect the configuration with `:backend status`. Backend availability and free-tier limits depend on the provider.

## Terminal assistance and safety

NLCL can interpret requests such as listing files, copying or renaming files, and searching project contents. The terminal suite compares generated command effects with a reference operation in a sandbox. File-changing operations are designed to require confirmation and show proposed changes before they are applied.

Code diagnostics can use installed tools such as Python, Node.js, C/C++ compilers, Icarus Verilog, Verilator, OpenSCAD, and Godot. Online code repair requires a configured language-model backend; without one, `:check` can still diagnose issues.

## Benchmark results

The latest supplied offline report is dated **2026-11-24 16:23** and evaluates held-out sentences. A result counts as correct when it produces the same resulting system state as the canonical instruction. The benchmark used knowledge-base version `9d1d36fffb91`.

| Metric | Untrained | Trained | Trained, unseen intents only |
|---|---:|---:|---:|
| Test sentences | 184 | 184 | 24 |
| Interpretation accuracy | 58.08% (95% CI: 50.5–65.3%) | 59.88% (95% CI: 52.3–67.0%) | 62.5% (95% CI: 42.7–78.8%) |
| Silent error rate | 34.24% (95% CI: 27.8–41.4%) | 14.67% (95% CI: 10.3–20.5%) | 16.67% (95% CI: 6.7–35.9%) |
| Precision when executing | 60.62% (95% CI: 52.9–67.9%) | 78.74% (95% CI: 70.8–85.0%) | 78.95% (95% CI: 56.7–91.5%) |
| Out-of-scope inputs refused | 1/17 | 13/17 | — |
| Asked instead of executing | 24 | 57 | 5 |
| Calibration error (ECE; lower is better) | 0.302 | 0.1112 | 0.2023 |
| AUROC for separating right from wrong | 0.9252 | 0.9469 | 0.8778 |
| Median latency per sentence | 0.66 ms | 1.05 ms | 1.22 ms |
| Area under risk-coverage curve (lower is better) | 0.1733 | 0.1577 | 0.1696 |

Wilson 95% confidence intervals are shown for accuracy, silent error rate, and precision. The unseen-intent subset contains no out-of-scope examples, so refusal is not applicable (`0/0` in the raw report).

### Learning curve

| Training paraphrases per intent | Rules learned | Accuracy | Silent errors | Out-of-scope refused | ECE |
|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 58.08% | 34.24% | 1/17 | 0.302 |
| 1 | 1 | 58.08% | 23.37% | 12/17 | 0.2119 |
| 2 | 1 | 58.08% | 18.48% | 13/17 | 0.1844 |
| 3 | 3 | 59.88% | 14.67% | 13/17 | 0.1292 |

### Terminal benchmark

| Metric | Result |
|---|---:|
| Same file-system effect as the intended command | 59.46% (22/37 sentences) |
| Executed with the wrong effect | 0/37 |
| Generated Bash and PowerShell checks passed in a sandbox | 24/44 |
| Unconfirmed destructive file actions | 0 |

The `24/44` figure is the number of shell checks that passed, not a 100% verification rate. The report notes that `llm-direct` and `nlcl+teacher` were **not run** because no language-model backend was configured. NLCL produced identical interpretations on repeated runs for all **184/184** benchmark sentences across five runs. Offline token cost was zero; trained median latency was **1.05 ms** per sentence.

These are prototype benchmark results on a small, author-created corpus. They show lower silent error rates and improved refusal and calibration after training in this run, but do not establish broad real-world performance. See the [pre-registered hypotheses](bench/HYPOTHESES.md), [latest benchmark report](bench/results/report.md), and [machine-readable results](bench/results/results.json) for methodology and full details.

Run the offline benchmark with:

```bash
python -m nlcl.bench --backend off
```

To attempt the language-model comparisons, configure a supported backend and run:

```bash
python -m nlcl.bench --backend auto
```

## REPL commands

Useful commands include `:help`, `:show`, `:ir`, `:trace on`, `:build`, `:run`, `:undo`, `:domains`, `:vocab`, `:teach <word> <TOKEN>`, `:backend status`, and `:quit`.

## Limitations

- The offline parser covers common English patterns, not unrestricted natural language.
- Persian and other non-Latin input require an online backend.
- Generated code is a starting point, not a verified production product. Toolchains may be needed to build or simulate non-Python targets.
- Online interpretation and code repair depend on the selected model, provider availability, and API limits.
- The benchmark is small and was created by the project author; its results should be treated as preliminary.
- Dedicated hardware for semantic operations is outside the scope of this software prototype.

## Credits and license

The concept and design goals are by M. M. Shamshiri (2019); see [`PROPOSAL.md`](PROPOSAL.md). The prototype was developed with assistance from Claude through Claude Code. This does not imply endorsement by Anthropic.

Released under the [MIT License](LICENSE).
