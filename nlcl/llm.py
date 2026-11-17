"""Free LLM backends (all optional) + LLM semantic parser.

Every backend speaks the OpenAI-compatible /chat/completions protocol, so a
single stdlib-only client covers them all.  Nothing here is required: with
no key and no local Ollama, NLCL runs on the offline rule parser.

  backend      env var for key        free tier / notes
  ---------    --------------------   ---------------------------------------
  ollama       (none)                 100% local & free  (ollama.com)
  groq         GROQ_API_KEY           free tier, very fast (console.groq.com)
  gemini       GEMINI_API_KEY         free tier (aistudio.google.com)
  openrouter   OPENROUTER_API_KEY     free ":free" models (openrouter.ai)
  cerebras     CEREBRAS_API_KEY       free tier (cloud.cerebras.ai)
  mistral      MISTRAL_API_KEY        free "Experiment" plan (console.mistral.ai)
  huggingface  HF_TOKEN               free monthly credits (huggingface.co)
  github       GITHUB_TOKEN           GitHub Models free tier

Model names change over time; override any of them with NLCL_<NAME>_MODEL,
e.g.  NLCL_GROQ_MODEL=llama-3.1-8b-instant
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .ir import Op, SystemState

BACKENDS = {
    #  name          base url                                                    key env               default model
    "ollama":      ("http://localhost:11434/v1",                                None,                 "llama3.2"),
    "groq":        ("https://api.groq.com/openai/v1",                           "GROQ_API_KEY",       "llama-3.3-70b-versatile"),
    "gemini":      ("https://generativelanguage.googleapis.com/v1beta/openai",  "GEMINI_API_KEY",     "gemini-2.5-flash"),
    "openrouter":  ("https://openrouter.ai/api/v1",                             "OPENROUTER_API_KEY", "cohere/north-mini-code:free"),
    "cerebras":    ("https://api.cerebras.ai/v1",                               "CEREBRAS_API_KEY",   "llama-3.3-70b"),
    "mistral":     ("https://api.mistral.ai/v1",                                "MISTRAL_API_KEY",    "mistral-small-latest"),
    "huggingface": ("https://router.huggingface.co/v1",                         "HF_TOKEN",           "meta-llama/Llama-3.1-8B-Instruct"),
    "github":      ("https://models.github.ai/inference",                       "GITHUB_TOKEN",       "openai/gpt-4.1-mini"),
}
# order used by auto-detection: local first, then fastest free clouds
AUTO_ORDER = ["ollama", "groq", "gemini", "cerebras", "openrouter", "mistral", "huggingface", "github"]


class LLMError(RuntimeError):
    pass


class QuotaExceeded(LLMError):
    """The provider's free daily limit is used up: stop asking it today."""


@dataclass
class Backend:
    name: str
    base_url: str
    key: str | None
    model: str
    timeout: int = 60

    def chat(self, messages: list[dict], temperature: float = 0.0, json_mode: bool = True) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode and self.name in ("groq", "gemini", "openrouter", "mistral", "cerebras", "ollama", "github"):
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "nlcl/0.1"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.name == "openrouter":
            headers["X-Title"] = "NLCL"
        req = urllib.request.Request(self.base_url.rstrip("/") + "/chat/completions", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if json_mode and e.code == 400 and "response_format" in detail:
                return self.chat(messages, temperature, json_mode=False)
            if e.code == 429:
                low = detail.lower()
                if "per-day" in low or "per day" in low or "daily" in low or "quota" in low:
                    raise QuotaExceeded(f"{self.name}: daily free limit reached ({detail[:160]})") from None
                self._retries = getattr(self, "_retries", 0) + 1
                if self._retries <= 4:                      # per-minute limit: wait and try again
                    import time
                    time.sleep(min(60, 8 * self._retries))
                    try:
                        return self.chat(messages, temperature, json_mode)
                    finally:
                        self._retries = 0
            raise LLMError(f"{self.name}: HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"{self.name}: {e}") from None
        try:
            return out["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{self.name}: unexpected response {str(out)[:200]}") from None

    def __str__(self):
        return f"{self.name} ({self.model})"


def make_backend(name: str) -> Backend | None:
    name = name.lower()
    if name not in BACKENDS:
        raise LLMError(f"unknown backend '{name}'. Choose from: {', '.join(BACKENDS)}")
    url, key_env, model = BACKENDS[name]
    url = os.environ.get(f"NLCL_{name.upper()}_URL", url)
    model = os.environ.get(f"NLCL_{name.upper()}_MODEL", model)
    key = os.environ.get(key_env) if key_env else None
    if key_env and not key:
        return None
    return Backend(name, url, key, model)


def ollama_alive(url: str = "http://localhost:11434") -> list[str] | None:
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=1.5) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return None


def autodetect(preferred: str | None = None) -> Backend | None:
    """Pick the first usable free backend (or the preferred one)."""
    pref = preferred or os.environ.get("NLCL_BACKEND")
    if pref and pref.lower() in ("none", "off", "rules"):
        return None
    order = [pref.lower()] if pref else AUTO_ORDER
    for name in order:
        if name == "ollama":
            models = ollama_alive()
            if models is None:
                continue
            b = make_backend("ollama")
            if not os.environ.get("NLCL_OLLAMA_MODEL") and models and b.model not in models \
                    and not any(m.startswith(b.model + ":") for m in models):
                b.model = models[0]
            return b
        b = make_backend(name)
        if b:
            return b
    return None


def status() -> str:
    lines = []
    for name in AUTO_ORDER:
        url, key_env, model = BACKENDS[name]
        if name == "ollama":
            ok = ollama_alive() is not None
            lines.append(f"  {name:<12} {'running' if ok else 'not running':<14} local, no key ({url})")
        else:
            ok = bool(os.environ.get(key_env))
            lines.append(f"  {name:<12} {'key set' if ok else 'no key':<14} set {key_env}")
    return "\n".join(lines)


def extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError("no JSON in model reply")


# --------------------------------------------------------------------------
# LLM semantic parser
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the semantic front-end of NLCL, a natural-language computational language.
Convert the user's instruction (any human language) into IR operations that modify a persistent system state.
Reply with ONLY a JSON object: {"ops": [ ... ], "question": null}
If the instruction is genuinely ambiguous (e.g. you cannot tell the target domain of a new object and there is no default), return {"ops": [], "question": "<short clarification question>"}.

Each op is an object with "action" and optional fields:
  CREATE     domain, kind, name, props{}, count, parent      new entity/entities
  SET        target, props{}, parent, domain                 change properties of an existing entity
  UNSET      target, props{"name": null}                     remove properties
  DELETE     target
  CONNECT    target (source), dst, via, props{fields:[..], baud, port, topic, interval_ms}
  DISCONNECT target, dst
  RENAME     target, name
  BEHAVIOR   target, behavior{on, if, do:[actions]}
  DOMAIN     domain                                           default domain for later CREATEs
  FOCUS      target                                           make target the current "it"
  SHOW / BUILD / RUN(domain) / UNDO                           meta commands
"target" refers to an existing entity by name or kind; omit it (null) to mean "it" (the entity in focus).
Domains available: {domains}
Protocols for "via": serial, http, mqtt, gpio, spi, i2c, udp, websocket.
Property names: snake_case English (health, speed, movable, collision, pin, color, position:[x,y,z], size:[x,y,z], width, ...).
Values: numbers, booleans, strings, or numeric lists.

Behaviour object:
  "on": "start" | "update" | "collision" | "input:<key-or-entity>" | "timer:<ms>" | "change:<prop>" | "message:<entity-or-any>"
  "if": optional condition expression or null
  "do": list of actions:
     {"op":"set","prop":p,"value":expr}  {"op":"add","prop":p,"value":expr}  {"op":"toggle","prop":p}
     {"op":"destroy"}  {"op":"log","text":"..."}  {"op":"move","x":expr,"y":expr,"z":expr}
     {"op":"send","to":entity,"fields":[..]}  {"op":"call","name":fn,"args":[expr..]}
Expressions: C-like: identifiers (property names, or entity.prop for another entity), numbers, "strings", true/false,
+ - * / % < > <= >= == != && || ! ( ).  A behaviour is attached to the entity whose state its actions change.

Current system state:
{state}
Default domain: {default_domain}
Learned vocabulary: {vocab}

Examples:
User: create a game object with 100 health that can move
{"ops":[{"action":"CREATE","domain":"game","kind":"object","props":{"health":100,"movable":true}}],"question":null}
User: create an object
{"ops":[],"question":"What kind of object would you like to create? (game, python, embedded, hardware, 3d, cad, ...)"}
User: when health drops below 1, destroy it
{"ops":[{"action":"BEHAVIOR","target":null,"behavior":{"on":"update","if":"health < 1","do":[{"op":"destroy"}]}}],"question":null}
User: add an esp32 with a temperature sensor on pin 4 and send the temperature to the dashboard over mqtt every 5 seconds
{"ops":[{"action":"CREATE","domain":"embedded","kind":"esp32","name":"esp32"},{"action":"CREATE","domain":"embedded","kind":"sensor","name":"temp_sensor","parent":"esp32","props":{"subtype":"temperature","pin":4,"temperature":0}},{"action":"CONNECT","target":"temp_sensor","dst":"dashboard","via":"mqtt","props":{"fields":["temperature"],"interval_ms":5000}}],"question":null}
"""


TRANSLATE_PROMPT = """You are the online language agent of NLCL, a natural-language programming system.
The user writes in ANY human language (Persian/Farsi, Arabic, Turkish, ...). Rewrite the instruction as simple
English NLCL commands. Reply with ONLY JSON: {"lines": ["command 1", "command 2", ...]}

Rules:
- One action per line. Keep the user's meaning exactly; do not invent objects or properties.
- Numbers as ASCII digits. Entity names: keep names that already exist in the state exactly; write new
  names in Latin letters (transliterate, e.g. علی -> ali).
- Use ONLY these sentence patterns:
  create a <domain> <kind> called <name> with <prop> <value> and <prop> <value>
  create <N> <domain> <kind>s with <prop> <value>
  add a <kind> called <name> to <parent> on pin <N>
  set <prop> of <name> to <value>            | give <name> <value> <prop>      | make <name> <color>
  remove <prop> from <name>                  | delete <name>                   | rename <old> to <new>
  put <child> inside <parent>                | select <name>                   | undo
  connect <a> to <b> via <serial|http|mqtt|spi|i2c|udp> sending <field>, <field> every <N> seconds
  when <prop> of <name> is above|below|equal to <value>, <action>
  when <name> is pressed, <action>           | when <a> touches <b>, <action>
  every <N> seconds|ms, <action>             | when <name> starts, <action>
  actions: set <prop> of <name> to <value> | increase|decrease <prop> of <name> by <N> | toggle <name>
           turn on|off <name> | destroy <name> | print '<text>' | move <name> up|down|left|right by <N>
           send <field> to <name>
  build it | run it | show
  fix <file or module>: <description of the bug in English>
- Domains: {domains}. Say the domain explicitly when the user names or implies one (game, python, embedded,
  hardware, cpp, 3d, cad, app).
- If the request is a question or cannot be expressed, return {"lines": [], "question": "<short question in the user's language>"}.

Current system state:
{state}

Example (Persian): "یه بازیکن بساز با ۱۰۰ تا جون، وقتی جونش صفر شد حذفش کن"
{"lines": ["create a game player called player with health 100", "when health of player is below 1, destroy player"]}
"""


class LLMParser:
    def __init__(self, backend: Backend):
        self.backend = backend

    def parse(self, text: str, state: SystemState, domains: list[str], vocab_desc: str = "",
              history: list[dict] | None = None) -> tuple[list[Op], str | None]:
        prompt = (SYSTEM_PROMPT
                  .replace("{domains}", ", ".join(domains))
                  .replace("{state}", state.summary())
                  .replace("{default_domain}", str(state.default_domain))
                  .replace("{vocab}", vocab_desc or "(none)"))
        messages = [{"role": "system", "content": prompt}]
        for h in (history or [])[-6:]:
            messages.append(h)
        messages.append({"role": "user", "content": text})
        last_err = None
        for attempt in range(2):
            reply = self.backend.chat(messages)
            try:
                data = extract_json(reply)
                if isinstance(data, list):
                    data = {"ops": data}
                ops = [Op.from_dict(o) for o in data.get("ops", [])]
                return ops, data.get("question")
            except (ValueError, TypeError, KeyError) as e:
                last_err = e
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"That was invalid ({e}). Reply again with valid JSON only."})
        raise LLMError(f"model returned invalid IR: {last_err}")

    def to_english(self, text: str, state: SystemState, domains: list[str]) -> tuple[list[str], str | None]:
        """Online agent for non-English input (Persian, Arabic, ...): rewrite the instruction as
        canonical English NLCL commands, one per line, which the normal pipeline then executes."""
        msg = [{"role": "system", "content": TRANSLATE_PROMPT
                .replace("{domains}", ", ".join(domains))
                .replace("{state}", state.summary())},
               {"role": "user", "content": text}]
        data = extract_json(self.backend.chat(msg))
        lines = data.get("lines") if isinstance(data, dict) else data
        if not isinstance(lines, list):
            raise LLMError("model did not return a list of commands")
        question = data.get("question") if isinstance(data, dict) else None
        return [str(x).strip() for x in lines if str(x).strip()], question

    def propose_meaning(self, word: str, context: str, tokens: list[str]) -> dict | None:
        """Adaptive vocabulary: ask the model what an unknown word maps to."""
        msg = [{"role": "system", "content":
                "You extend the vocabulary of a natural-language programming system. Atomic tokens: "
                + ", ".join(tokens) +
                '. Reply JSON {"token": <one atomic token or null>, "property": <snake_case property name or null>, '
                '"reason": "..."}. Use "property" if the word is a data attribute (like stamina), "token" if it '
                'is an operation/function word (like spawn = CREATE).'},
               {"role": "user", "content": f'Word: "{word}"\nSentence: "{context}"'}]
        try:
            return extract_json(self.backend.chat(msg))
        except (LLMError, ValueError):
            return None

    def write_body(self, language: str, description: str, context: str) -> str | None:
        """Optional: ask the model for a small function body (used by modules for 'call' actions)."""
        msg = [{"role": "system", "content": f"Write only the body of a {language} function. No explanations, "
                                             f"no markdown fences. Keep it under 15 lines."},
               {"role": "user", "content": f"Function purpose: {description}\nContext:\n{context}"}]
        try:
            out = self.backend.chat(msg, json_mode=False)
            return re.sub(r"^```\w*\s*|\s*```$", "", out.strip(), flags=re.S)
        except LLMError:
            return None
