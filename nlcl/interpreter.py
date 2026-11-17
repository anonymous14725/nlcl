"""Computational structure formation: apply IR ops to the persistent state.

The interpreter never guesses the domain of a new object.  If it cannot be
determined it returns a Question carrying the pending op; the runtime asks
the user and calls ``answer()`` with the reply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import modules as modreg
from .ir import (Entity, Link, Op, SystemState, eval_expr, expr_names, expr_tokens, ident, norm_name, singular,
                 validate_behavior, validate_expr)


COLOR_WORDS = {"red", "green", "blue", "yellow", "white", "black", "orange", "purple", "pink", "cyan",
               "magenta", "gray", "grey", "brown", "gold", "silver"}
TRUE_BY_DEFAULT = {"visible", "enabled", "active", "alive", "running"}


@dataclass
class Question:
    text: str
    op: Op
    field: str                       # which op field the answer fills ("domain")
    options: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    messages: list[str] = field(default_factory=list)
    question: Question | None = None
    followup: str | None = None      # soft prompt ("What properties should it have?")
    meta: list[Op] = field(default_factory=list)   # SHOW/BUILD/RUN/... for the runtime
    changed: bool = False


class Interpreter:
    def __init__(self, state: SystemState):
        self.state = state
        self.modules = modreg.load_modules()

    # ------------------------------------------------------------------ main
    def apply_all(self, ops: list[Op]) -> Outcome:
        out = Outcome()
        snap_taken = False
        for i, op in enumerate(ops):
            if op.action in ("SHOW", "BUILD", "RUN", "UNDO", "DEFINE", "HELP", "FIX"):
                out.meta.append(op)
                continue
            if not snap_taken:
                self.state.snapshot()
                snap_taken = True
            r = self.apply(op)
            out.messages += r.messages
            out.changed |= r.changed
            if r.followup:
                out.followup = r.followup
            if r.question:
                # remaining ops wait until the question is answered
                r.question.op.props.setdefault("__rest__", [o.to_dict() for o in ops[i + 1:]])
                out.question = r.question
                break
        if snap_taken and not out.changed:
            self.state._undo.pop()
        return out

    def apply(self, op: Op) -> Outcome:
        fn = getattr(self, f"_op_{op.action.lower()}", None)
        if not fn:
            return Outcome([f"(ignored unsupported action {op.action})"])
        try:
            return fn(op)
        except ValueError as e:
            return Outcome([f"error: {e}"])

    def answer(self, q: Question, reply: str, parsed_ops: list[Op] | None = None) -> Outcome:
        """Fill the pending op with the user's reply and continue."""
        op = q.op
        rest = op.props.pop("__rest__", [])
        low = reply.lower()
        if q.field == "domain":
            dom = self._domain_from_text(low, q.options)
            if not dom:
                q.op.props["__rest__"] = rest
                return Outcome([f"Please choose one of: {', '.join(q.options)}"], question=q)
            op.domain = dom
            op.props.pop("__domain_candidates__", None)
            # the reply may also carry a kind / props ("a game object with 100 health")
            for p in parsed_ops or []:
                if p.action == "CREATE":
                    if p.kind and p.kind != "object" and (op.kind in (None, "object")):
                        op.kind = p.kind
                    if p.name and not op.name:
                        op.name = p.name
                    op.props.update({k: v for k, v in p.props.items() if not k.startswith("__")})
                elif p.action == "SET":
                    op.props.update(p.props)
        return self.apply_all([op] + [Op.from_dict(d) for d in rest])

    def _domain_from_text(self, low: str, options: list[str]) -> str | None:
        words = re.findall(r"[\w؀-ۿ+#.\-]+", low)
        for w in words:
            m = modreg.get(w)
            if m and (not options or m.name in options):
                return m.name
        for w in words:
            for m in self.modules.values():
                if w in m.keywords and (not options or m.name in options):
                    return m.name
        for i, opt in enumerate(options, 1):
            if low.strip() == str(i):
                return opt
        return None

    # ------------------------------------------------------------------ ops
    def _op_create(self, op: Op) -> Outcome:
        st = self.state
        out = Outcome()
        domain = op.domain or self._infer_domain(op)
        if domain and not modreg.get(domain):
            raise ValueError(f"unknown domain '{domain}'. Available: {', '.join(self.modules)}")
        if not domain:
            cands = op.props.get("__domain_candidates__") or self._candidates(op.kind)
            options = cands or list(self.modules)
            kind = op.kind or "object"
            q = Question(f"What kind of {kind} would you like to create? ({' / '.join(options)})",
                         op, "domain", options)
            out.question = q
            return out
        module = modreg.get(domain)
        domain = module.name
        parent = None
        if op.parent:
            pe = st.resolve(op.parent)
            if not pe:
                out.messages.append(f"note: parent '{op.parent}' not found; created at top level")
            elif pe.domain != domain:
                # cross-domain "for": e.g. a CAD enclosure for an embedded board
                op.props.setdefault("for_entity", pe.name)
                if pe.domain == "embedded":
                    op.props.setdefault("board", pe.kind)
            else:
                parent = pe.id
        kind = (op.kind or "object").lower()
        group = []
        base0 = ident(singular(op.name or kind)) if op.count > 1 else None
        nxt = 1
        if base0:
            used = [int(m.group(1)) for x in st.entities.values()
                    for m in [re.fullmatch(rf"{re.escape(base0)}(\d+)", x.name)] if m]
            nxt = max(used, default=0) + 1
        for i in range(op.count):
            base = op.name or kind
            if op.count > 1:
                base = f"{base0}{nxt + i}"         # enemy4, enemy5 ... never enemy12
            name = st.unique_name(base)
            eid = st.new_id(kind)
            props = module.default_props(kind)
            props.update({k: v for k, v in op.props.items() if not k.startswith("__")})
            e = Entity(id=eid, name=name, domain=domain, kind=kind, props=props, parent=parent)
            st.entities[eid] = e
            st.focus = eid
            group.append(eid)
            out.messages.append(f"created {domain} {kind} '{name}'" + (f" in {st.entities[parent].name}" if parent else "")
                                + (f" {self._fmt(props)}" if props else ""))
            for w in module.validate(e):
                out.messages.append(f"  warning: {w}")
        # "create 3 enemies ... and make them red": following SETs apply to the whole group
        st.focus_group = group if len(group) > 1 else []
        out.changed = True
        real = {k for k in op.props if not k.startswith("__") and k != "subtype"}
        if not real and not getattr(self, "quiet_followups", False):
            out.followup = module.prompt_for(st.entities[st.focus])
        return out

    def _infer_domain(self, op: Op) -> str | None:
        cands = self._candidates(op.kind)
        if op.parent:
            pe = self.state.resolve(op.parent)
            if pe and (not cands or pe.domain in cands):
                return pe.domain
        hints = op.props.get("__domain_hints__") or []
        # "use hardware" then "create a counter": the default domain wins when it knows the kind
        dd = self.state.default_domain
        if dd and (dd in cands or dd in hints):
            return dd
        if not cands and len(hints) == 1:
            return hints[0]
        if len(cands) > 1 and hints:
            narrowed = [c for c in cands if c in hints]
            if len(narrowed) == 1:
                return narrowed[0]
        if len(cands) == 1:
            return cands[0]
        if self.state.default_domain and (not cands or self.state.default_domain in cands):
            return self.state.default_domain
        # context: the domain of what the user is working on (focus, then most used)
        if cands:
            focus = self.state.resolve(None)
            if focus and focus.domain in cands:
                return focus.domain
            used = [e.domain for e in self.state.entities.values() if e.domain in cands]
            if used:
                return max(set(used), key=used.count)
        return None

    def _candidates(self, kind: str | None) -> list[str]:
        if not kind or kind == "object":
            return []
        k = kind.lower()
        strong = [m.name for m in self.modules.values() if k in {x.lower() for x in m.kinds}]
        weak = [m.name for m in self.modules.values() if k in {x.lower() for x in m.keywords}]
        return strong or weak

    def _target(self, ref: str | None) -> Entity:
        e = self.state.resolve(ref)
        if not e:
            if ref:
                raise ValueError(f"I can't find '{ref}'. Known: {', '.join(x.name for x in self.state.entities.values()) or 'nothing yet'}")
            raise ValueError("there is nothing in focus yet - create something first")
        return e

    def _targets(self, ref: str | None) -> list[Entity]:
        """'*enemy' -> every enemy; None right after 'create 3 enemies' -> the whole group."""
        st = self.state
        if ref and str(ref).startswith("*"):
            k = str(ref)[1:]
            hits = [e for e in st.entities.values() if e.kind == k]
            if hits:
                return hits
            return [self._target(k)]
        if (ref is None or str(ref).lower() in ("them", "they", "all of them")) and getattr(st, "focus_group", None):
            grp = [st.entities[i] for i in st.focus_group if i in st.entities]
            if grp and st.focus in st.focus_group:
                return grp
        return [self._target(ref)]

    def _op_set(self, op: Op) -> Outcome:
        targets = self._targets(op.target)
        out = Outcome(changed=True)
        for e in targets:
            r = self._set_one(e, op)
            out.messages += r.messages
        if len(targets) > 1:
            self.state.focus = targets[-1].id
        return out

    def _set_one(self, e: Entity, op: Op) -> Outcome:
        out = Outcome(changed=True)
        props = dict(op.props)
        new_kind = props.pop("__kind__", None)
        if new_kind and new_kind != e.kind:
            mod = modreg.get(e.domain)
            if mod and new_kind not in mod.kinds:
                cands = self._candidates(new_kind)
                if cands:
                    e.domain = cands[0] if e.domain not in cands else e.domain
                    out.messages.append(f"{e.name} moved to domain {e.domain}")
            out.messages.append(f"{e.name} is now a {new_kind}")
            e.kind = new_kind
            mod = modreg.get(e.domain)
            for k, v in (mod.default_props(new_kind) if mod else {}).items():
                e.props.setdefault(k, v)
        act = props.pop("__act__", None)
        if act:
            out.messages += self._do_now(e, act)
        mv = props.pop("__move__", None)
        if mv is not None:
            pos = list(e.props.get("position", [0, 0, 0]))
            pos = (pos + [0, 0, 0])[:3]
            e.props["position"] = [a + b for a, b in zip(pos, (list(mv) + [0, 0, 0])[:3])]
            out.messages.append(f"moved {e.name} to {e.props['position']}")
        if op.domain and op.domain != e.domain:
            m = modreg.get(op.domain)
            if not m:
                raise ValueError(f"unknown domain '{op.domain}'")
            e.domain = m.name
            out.messages.append(f"{e.name} now belongs to domain {m.name}")
        if op.parent:
            pe = self._target(op.parent)
            if pe.id == e.id:
                raise ValueError("an entity cannot contain itself")
            e.parent = pe.id
            out.messages.append(f"{e.name} is now inside {pe.name}")
        props = {k: v for k, v in props.items() if not k.startswith("__")}
        if props:
            e.props.update(props)
            out.messages.append(f"{e.name}: " + self._fmt(props))
        mod = modreg.get(e.domain)
        for w in (mod.validate(e) if mod else []):
            out.messages.append(f"  warning: {w}")
        self.state.focus = e.id
        return out

    def _do_now(self, e: Entity, a: dict) -> list[str]:
        """Execute a behaviour action immediately on the design-time state."""
        a = validate_behavior({"on": "start", "do": [a]})["do"][0]
        op = a["op"]
        env = self._env(e)
        if op == "set":
            e.props[a["prop"]] = eval_expr(a["value"], env)
            return [f"{e.name}: {a['prop']} = {e.props[a['prop']]!r}"]
        if op == "add":
            cur = e.props.get(a["prop"], 0)
            e.props[a["prop"]] = (cur if isinstance(cur, (int, float)) else 0) + eval_expr(a["value"], env)
            return [f"{e.name}: {a['prop']} = {e.props[a['prop']]!r}"]
        if op == "toggle":
            e.props[a["prop"]] = not bool(e.props.get(a["prop"], False))
            return [f"{e.name}: {a['prop']} = {e.props[a['prop']]!r}"]
        if op == "move":
            mv = [eval_expr(a[k], env) for k in ("x", "y", "z")]
            pos = (list(e.props.get("position", [0, 0, 0])) + [0, 0, 0])[:3]
            e.props["position"] = [p + d for p, d in zip(pos, mv)]
            return [f"moved {e.name} to {e.props['position']}"]
        if op == "log":
            return [f"[{e.name}] " + re.sub(r"\{(\w+)\}", lambda m: str(e.props.get(m.group(1), m.group(0))), a["text"])]
        raise ValueError(f"'{op}' only makes sense inside a behaviour (when/every ...)")

    def _env(self, owner: Entity) -> dict:
        env = {}
        for x in self.state.entities.values():
            for k, v in x.props.items():
                env[f"{x.ident}.{k}"] = v
        env.update(owner.props)
        return env

    def _op_unset(self, op: Op) -> Outcome:
        e = self._target(op.target)
        gone = [k for k in op.props if e.props.pop(norm_name(k), None) is not None or True]
        self.state.focus = e.id
        return Outcome([f"{e.name}: removed {', '.join(gone)}"], changed=True)

    def _op_delete(self, op: Op) -> Outcome:
        e = self._target(op.target)
        st = self.state
        doomed = {e.id}
        changed = True
        while changed:
            changed = False
            for x in st.entities.values():
                if x.parent in doomed and x.id not in doomed:
                    doomed.add(x.id)
                    changed = True
        gone_names = {st.entities[d].ident for d in doomed if d in st.entities}
        for d in doomed:
            st.entities.pop(d, None)
        for lid in [l.id for l in st.links.values() if l.src in doomed or l.dst in doomed]:
            st.links.pop(lid)
        st.focus = next(reversed(st.entities), None) if st.entities else None
        st.focus_group = [g for g in getattr(st, "focus_group", []) if g in st.entities]
        extra = f" (and {len(doomed) - 1} children)" if len(doomed) > 1 else ""
        return Outcome([f"deleted {e.name}{extra}"] + self._dangling(gone_names), changed=True)

    def _op_rename(self, op: Op) -> Outcome:
        e = self._target(op.target)
        old = e.name
        old_ident = e.ident
        e.name = self.state.unique_name(op.name or e.name) if ident(op.name or "") != e.name else e.name
        self.state.focus = e.id
        new_ident = e.ident
        n = self._rewrite_refs(old_ident, new_ident) if new_ident != old_ident else 0
        return Outcome([f"renamed {old} -> {e.name}" + (f" (updated {n} reference(s))" if n else "")], changed=True)

    def _rewrite_refs(self, old: str, new: str) -> int:
        """Rename every reference to an entity: triggers, conditions, values, send targets, links."""
        count = 0
        pat = re.compile(rf"(?<![\w.]){re.escape(old)}(?=\.)")
        for x in self.state.entities.values():
            for b in x.behaviors:
                for key in ("on",):
                    for pre in ("message:", "input:", "change:"):
                        if b[key] == f"{pre}{old}" or b[key].startswith(f"{pre}{old}."):
                            b[key] = b[key].replace(f"{pre}{old}", f"{pre}{new}", 1)
                            count += 1
                if b.get("with") == old:
                    b["with"] = new
                    count += 1
                if b.get("if"):
                    nb = pat.sub(new, b["if"])
                    count += nb != b["if"]
                    b["if"] = nb
                for a in b["do"]:
                    for k in ("value", "x", "y", "z"):
                        if isinstance(a.get(k), str):
                            nv = pat.sub(new, a[k])
                            count += nv != a[k]
                            a[k] = nv
                    if a.get("op") == "send" and a.get("to") and ident(a["to"]) == old:
                        a["to"] = new
                        count += 1
            for k, v in list(x.props.items()):
                if k in ("target", "follow", "for_entity", "spawn") and isinstance(v, str) and ident(v) == old:
                    x.props[k] = new
                    count += 1
        for lid in list(self.state.links):
            l = self.state.links[lid]
            if old in lid.split("_to_"):
                parts = lid.split("_to_")
                nid = "_to_".join(new if p == old else p for p in parts)
                if nid not in self.state.links:
                    l.id = nid
                    self.state.links[nid] = self.state.links.pop(lid)
                    count += 1
        return count

    def _dangling(self, gone: set[str]) -> list[str]:
        """Behaviours that still mention deleted entities."""
        msgs = []
        for x in self.state.entities.values():
            for b in x.behaviors:
                text = " ".join([b["on"], b.get("if") or "", b.get("with") or ""] +
                                [str(a.get("value", "")) + " " + str(a.get("to", "")) for a in b["do"]])
                hit = [g for g in gone if re.search(rf"(?<![\w]){re.escape(g)}(?![\w])", text)]
                if hit:
                    msgs.append(f"  warning: {x.name} still has a rule that mentions {', '.join(hit)} (on {b['on']})")
        return msgs

    def _op_connect(self, op: Op) -> Outcome:
        src = self._target(op.target)
        dst = self._target(op.dst)
        if src.id == dst.id:
            raise ValueError("cannot connect an entity to itself")
        st = self.state
        lid = f"{src.ident}_to_{dst.ident}"
        i = 2
        while lid in st.links:
            lid = f"{src.ident}_to_{dst.ident}_{i}"
            i += 1
        props = dict(op.props)
        if "fields" in props:
            props["fields"] = [norm_name(f) for f in props["fields"]]
        st.links[lid] = Link(lid, src.id, dst.id, op.via, props)
        msg = f"connected {src.name} -> {dst.name}" + (f" via {op.via}" if op.via else "")
        if src.domain != dst.domain:
            msg += f"  [cross-domain: {src.domain} -> {dst.domain}]"
        return Outcome([msg], changed=True)

    def _op_disconnect(self, op: Op) -> Outcome:
        a = self._target(op.target)
        b = self._target(op.dst)
        ids = [l.id for l in self.state.links.values() if {l.src, l.dst} == {a.id, b.id}]
        for i in ids:
            self.state.links.pop(i)
        return Outcome([f"removed {len(ids)} link(s) between {a.name} and {b.name}"], changed=bool(ids))

    # ------------------------------------------------------------------ behaviours
    def _split_ref(self, name: str) -> tuple[Entity | None, str]:
        """'bob_lives' / 'bob.lives' / 'visits_count' -> (bob, 'lives') when bob is an entity."""
        if "." in name:
            ent, prop = name.split(".", 1)
            e = self._by_ident(ent)
            return (e, prop) if e else (None, name)
        best = None
        for e in self.state.entities.values():
            for key in {e.ident, ident(e.kind)}:
                if name.startswith(key + "_") and len(name) > len(key) + 1:
                    if best is None or len(key) > len(best[1]):
                        best = (e, key)
        if best:
            e, key = best
            same_kind = [x for x in self.state.entities.values() if ident(x.kind) == key]
            if key == ident(e.kind) and key != e.ident and len(same_kind) != 1:
                return None, name
            return e, name[len(key) + 1:]
        return None, name

    def _by_ident(self, s: str) -> Entity | None:
        s = ident(s)
        for e in self.state.entities.values():
            if e.ident == s:
                return e
        kinds = [e for e in self.state.entities.values() if ident(e.kind) == s]
        return kinds[-1] if len(kinds) == 1 else None

    def _holder(self, prop: str, exclude: Entity | None = None, near: Entity | None = None) -> Entity | None:
        """Best entity that owns a property: subtype/name match, then a link into `near`, then non-CAD, then last."""
        hs = [x for x in self.state.entities.values() if prop in x.props and x is not exclude]
        if not hs:
            return None
        if len(hs) == 1:
            return hs[0]
        named = [x for x in hs if prop in str(x.props.get("subtype", "")) or prop in x.name or prop in x.kind]
        if len(named) == 1:
            return named[0]
        if near is not None:
            linked = [x for x in hs if any(l.src == x.id and l.dst == near.id for l in self.state.links.values())]
            if linked:
                return linked[-1]
        active = [x for x in hs if x.domain != "cad"]
        return (active or hs)[-1]

    def _op_behavior(self, op: Op) -> Outcome:
        b0 = validate_behavior(op.behavior)
        focus_targets = self._targets(op.target)
        explicit = op.target is not None
        default_owner = focus_targets[-1]
        # 1. decide the owner of every action: the entity whose property it changes
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        free: list[dict] = []
        for a in b0["do"]:
            a = dict(a)
            owner = None
            who = a.pop("__who", None)
            if who:
                for t in self._targets(who):
                    groups.setdefault(t.id, []).append(dict(a))
                    if t.id not in order:
                        order.append(t.id)
                continue
            if a["op"] in ("set", "add", "toggle"):
                ent, prop = self._split_ref(a["prop"])
                if ent is not None:
                    owner, a["prop"] = ent, prop
                elif not explicit and a["prop"] not in default_owner.props:
                    owner = self._holder(a["prop"], near=default_owner)
            elif a["op"] == "send" and not explicit:
                fields = a.get("fields") or []
                to = self.state.resolve(a["to"]) if a.get("to") else None
                srcs = [self.state.entities[l.src] for l in self.state.links.values()
                        if to is not None and l.dst == to.id and l.src in self.state.entities]
                holders = [self._holder(f) for f in fields if self._holder(f)]
                if default_owner is to or (default_owner.id not in [s.id for s in srcs] and srcs):
                    owner = (holders or srcs or [None])[0]
            if owner is None:
                free.append(a)
                continue
            groups.setdefault(owner.id, []).append(a)
            if owner.id not in order:
                order.append(owner.id)
        # actions with no property of their own (destroy, log, move, call ...) go with the first owner,
        # or with the focus when no action names a property
        # actions without a property of their own (destroy, log, move, call ...) stay with the rule's
        # explicit owner, else go with the first action's owner, else with the focus
        if free:
            homes = focus_targets if (explicit or not order) else [self.state.entities[order[0]]]
            for h in homes:
                groups.setdefault(h.id, [])
                groups[h.id] = groups[h.id] + [dict(a) for a in free]
                if h.id not in order:
                    order.insert(0, h.id)
        plan = [(self.state.entities[i], groups[i]) for i in order if i in groups]
        msgs = []
        for e, acts in plan:
            if not acts:
                continue
            b = dict(b0, do=[dict(a) for a in acts])
            # collision rule moved onto the partner: the partner's partner is the original owner
            if b.get("with") and b["with"] in (e.ident, ident(e.kind)) and e is not default_owner:
                b["with"] = default_owner.ident
            b = self._qualify(b, e)
            msgs += self._attach(e, b).messages
        return Outcome(msgs, changed=True)

    def _attach(self, e: Entity, b: dict) -> Outcome:
        e.behaviors.append(b)
        self.state.focus = e.id
        # make sure referenced own properties exist (so code generators declare them),
        # typed by how they are used
        bool_names = set()
        for expr in [b.get("if") or ""] + [a.get("value", "") for a in b["do"]]:
            bool_names |= set(re.findall(r"!\s*([A-Za-z_]\w*)", expr))
            bool_names |= set(re.findall(r"([A-Za-z_]\w*)\s*[!=]=\s*(?:true|false)\b", expr))
        others = {x.ident for x in self.state.entities.values()} | {x.name.lower() for x in self.state.entities.values()}
        for name in self._own_names(b):
            if name in e.props or name in ("on",) or "." in name or name in COLOR_WORDS or name in others:
                continue
            e.props[name] = False if name in bool_names else 0
        for a in b["do"]:
            if a["op"] == "toggle" or (a["op"] == "set" and a["prop"] == "on"):
                e.props.setdefault(a["prop"], a["prop"] in TRUE_BY_DEFAULT)
            elif a["op"] == "set":
                v = str(a["value"])
                e.props.setdefault(a["prop"], "" if v[:1] in "\"'" else (False if v in ("true", "false") else 0))
            elif a["op"] == "add":
                e.props.setdefault(a["prop"], 0)
        return Outcome([f"{e.name}: behaviour on {b['on']}" + (f" if {b['if']}" if b['if'] else "")
                        + f" -> {len(b['do'])} action(s)"], changed=True)

    def _qualify(self, b: dict, owner: Entity) -> dict:
        """Resolve every name in a behaviour relative to its owner:
        own prop -> name ; other entity's prop -> entity.prop ; 'bob_lives' -> bob.lives ;
        an unknown word compared with == / assigned (state == idle, set mode to eco) -> "string"."""
        all_props = {k for x in self.state.entities.values() for k in x.props}

        def resolve(n: str) -> str | None:
            if n in ("true", "false"):
                return n
            if "." in n:
                ent, prop = self._split_ref(n)
                if ent is None:
                    return n
                return prop if ent.id == owner.id else f"{ent.ident}.{prop}"
            if n in owner.props:
                return n
            ent, prop = self._split_ref(n)
            if ent is not None:
                return prop if ent.id == owner.id else f"{ent.ident}.{prop}"
            h = self._holder(n, exclude=owner, near=owner)
            if h is not None:
                return f"{h.ident}.{n}"
            return None

        def q(expr: str, assigned: bool = False) -> str:
            toks = expr_tokens(expr)
            out = []
            for i, (k, v) in enumerate(toks):
                if k == "id":
                    r = resolve(v)
                    prev = toks[i - 1][1] if i > 0 else ""
                    nxt = toks[i + 1][1] if i + 1 < len(toks) else ""
                    lone = len(toks) == 1
                    if r is None and v not in all_props and (prev in ("==", "!=") or nxt in ("==", "!=") and i > 0
                                                             or (assigned and lone)):
                        if prev in ("==", "!=") or (assigned and lone):
                            v = f'"{v}"'          # state == idle -> state == "idle"
                    elif r is not None:
                        v = r
                out.append(v)
            return validate_expr(" ".join(out))
        if b.get("if"):
            try:
                b["if"] = q(b["if"])
            except ValueError:
                pass
        for a in b["do"]:
            for k in ("value", "x", "y", "z"):
                if k in a and isinstance(a[k], str):
                    try:
                        a[k] = q(a[k], assigned=(k == "value" and a["op"] == "set"))
                    except ValueError:
                        pass
        if b["on"].startswith("change:"):
            ent, prop = self._split_ref(b["on"][7:])
            if ent is not None:
                b["on"] = f"change:{prop}" if ent.id == owner.id else f"change:{ent.ident}.{prop}"
            elif b["on"][7:] not in owner.props:
                h = self._holder(b["on"][7:], exclude=owner, near=owner)
                if h is not None:
                    b["on"] = f"change:{h.ident}.{b['on'][7:]}"
            if a["op"] == "send" and a.get("to"):
                t = self.state.resolve(a["to"])
                if t:
                    a["to"] = t.name
        if b["on"].startswith("input:"):
            key = b["on"].split(":", 1)[1]
            t = self.state.resolve(key.replace("_", " "))
            if t and self._exact(key):
                b["on"] = f"input:{t.ident}"
        return b

    def _exact(self, key: str) -> bool:
        k = key.lower()
        return any(k in (e.name.lower(), e.kind.lower(), e.ident) for e in self.state.entities.values())

    @staticmethod
    def _own_names(b: dict) -> list[str]:
        names = []
        if b.get("if"):
            names += expr_names(b["if"])
        for a in b["do"]:
            for k in ("value",):
                if k in a:
                    names += expr_names(a[k])
        return names

    def _op_domain(self, op: Op) -> Outcome:
        m = modreg.get(op.domain)
        if not m:
            raise ValueError(f"unknown domain '{op.domain}'. Available: {', '.join(self.modules)}")
        self.state.default_domain = m.name
        return Outcome([f"default domain: {m.name} ({m.title})"], changed=True)

    def _op_focus(self, op: Op) -> Outcome:
        e = self._target(op.target)
        self.state.focus = e.id
        self.state.focus_group = []
        return Outcome([f"now working on {e.name}"], changed=False)

    @staticmethod
    def _fmt(props: dict) -> str:
        return "{" + ", ".join(f"{k}: {v!r}" for k, v in props.items() if not str(k).startswith("__")) + "}"
