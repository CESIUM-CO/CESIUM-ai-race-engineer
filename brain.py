import json
import re
import threading
import time
from collections import deque

import config
from gameinfo import ZONE_NAMES, weather_line
from intent_router import IntentRouter
from local_llm import LocalLLM
from ollama_client import OllamaClient
import persona
from strategy import TYRE_NAMES, FUEL_COLOURS

UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def parse_number(text):
    whole, frac, after_point, found = 0, "", False, False
    for tok in text.replace("-", " ").split():
        if tok == "point":
            after_point = True
            continue
        if tok.isdigit():
            val = int(tok)
        elif tok in UNITS:
            val = UNITS[tok]
        elif tok in TENS:
            val = TENS[tok]
        else:
            continue
        found = True
        if after_point:
            frac += str(val)
        else:
            whole += val
    return float(f"{whole}.{frac or 0}") if found else None


def spoken_time(sec):
    m, s = divmod(sec, 60)
    return f"{int(m)} minute {s:.1f}" if m else f"{s:.1f}"


# Each intent needs ONE fuzzy hit from EVERY word-group. Fuzzy = tolerates mishearing ("weathr").
INTENTS = [
    ("tyre_temps", [["temperature", "temperatures", "temp", "temps", "hot", "heat", "warm", "hotter"],
                    ["tyre", "tyres", "tire", "tires", "rubber"]]),
    ("another_lap", [["another", "extra", "more"], ["lap"]]),
    ("pit", [["should", "can"], ["pit", "box", "stay"]]),
    ("car", [["car", "cart", "card", "vehicle"], ["what", "which", "name", "model", "driving", "using", "am"]]),
    ("track", [["track", "circuit", "where"], ["what", "which", "name", "where", "am"]]),
    ("weather", [["weather", "rain", "raining", "temperature", "wind", "cloud", "forecast", "sunny"]]),
    ("grip", [["grip", "slippery", "damp", "wet", "surface", "conditions"]]),
    ("damage", [["damage", "damaged", "broken"]]),
    ("engine", [["engine", "revs", "rpm", "motor"]]),
    ("position", [["position", "place", "standing", "running"]]),
    ("status", [["status", "report", "update", "situation"]]),
    ("fuel", [["fuel", "petrol"]]),
    ("tyres", [["tyre", "tyres", "tire", "tires", "rubber"]]),
    ("last_lap", [["last", "previous"], ["lap", "time"]]),
    ("best_lap", [["best", "fastest", "quickest"], ["lap", "time"]]),
]
# Questions like "what pressure should I run" need thinking/knowledge, not a status readout.
KNOWLEDGE_HINT = re.compile(r"\b(should|recommend|best|why|how do|how can|how to|setup|set up|pressures?|explain|tell me about)\b")
GATE_EXEMPT = ("another_lap", "pit", "best_lap")


def _difflib():
    import difflib                           # only loaded the first time a voice command is matched
    return difflib


def _hit(group, tokens):
    for tok in tokens:
        for w in group:
            if tok == w:
                return True
            if len(tok) >= 4 and len(w) >= 4 and _difflib().SequenceMatcher(None, tok, w).ratio() >= 0.84:
                return True
    return False


def _rnd(x):
    return None if x is None else round(x, 1)


class Brain:
    """Order: instant rules (meaning, fuzzy) -> memory -> fast local AI -> trusted web in the background."""

    def __init__(self, radio, memory):
        self.radio, self.memory = radio, memory
        self.state = {}
        self.history = deque(maxlen=4)
        self.last_question = None
        self.llm = LocalLLM()
        self.ollama = OllamaClient()           # optional: a local Ollama server (config.USE_OLLAMA)
        self.router = IntentRouter(self._matches_telemetry)
        self.research_lock = threading.Lock()
        self.llm.start()                       # built-in AI loads in the background, race calls never wait for it
        self.ollama.start()                    # checks / warms up Ollama in the background; no server = silently skipped

    @staticmethod
    def _matches_telemetry(tokens):
        """True if the instant rules would understand this sentence (used by the router)."""
        return any(all(_hit(g, tokens) for g in groups) for _, groups in INTENTS)

    def ai_state(self):
        """For the control window: ready if ANY AI (Ollama or the built-in model) can answer."""
        states = {self.llm.state, self.ollama.state if self.ollama.enabled() else "idle"}
        for s in ("ready", "loading", "failed"):
            if s in states:
                return s
        return "idle"

    def say(self, text, prio=1):
        self.radio.speak(text, prio, kind="answer")      # a reply to the driver, not one of his own alerts

    def handle(self, text):
        t = text.lower().strip()
        print(f"\n🎧 [DRIVER]: {t}")
        try:
            if not self._quick(t):
                self._ask_ai(t)
        except Exception:                      # one bad answer must never disturb the engineer
            import traceback
            traceback.print_exc()
            self.say("Sorry, I couldn't work that out. Say it again?")

    # ---------------- instant answers built from live data ----------------
    def _h_car(self):
        return f"You're in the {self.state.get('car_name')}."

    def _h_track(self):
        return f"We're at {self.state.get('track_name')}."

    def _h_weather(self):
        return weather_line(self.state)

    def _h_grip(self):
        g = self.state.get("grip")
        if g is None:
            return "No grip data yet."
        return f"Track grip is {g * 100:.0f} percent." + (" That's low, could be damp." if g < 0.93 else "")

    def _h_damage(self):
        zones = self.state.get("damage") or []
        parts = [f"{ZONE_NAMES[i]} {v:.0f}" for i, v in enumerate(zones) if v >= config.DAMAGE_MIN_STEP]
        return "Damage: " + ", ".join(parts) + "." if parts else "No damage. Car's clean."

    def _h_engine(self):
        st = self.state
        if st.get("engine_dead"):
            return "The engine's gone."
        load = st.get("engine_load") or 0
        tail = "You've been working it hard, short-shift." if load > 0.3 else "Load is fine."
        return f"Revs {st.get('rpm', 0)} of {st.get('max_rpm', 0)}. {tail}"

    def _h_position(self):
        return f"You're P{self.state.get('position')}."

    def _fuel_line(self):
        fl, s = self.state.get("fuel_laps"), self.state.get("strategy")
        if fl is None:                                   # no trustworthy fuel-per-lap number: use the fuel light instead
            pct = self.state.get("fuel_pct")
            if pct is None:
                return "Still learning your fuel use."
            colour = FUEL_COLOURS[self.state.get("fuel_warning") or 0]
            return f"Still learning your fuel use. The tank is at {pct * 100:.0f} percent and the fuel light is {colour}."
        return f"{fl:.1f} laps of fuel, {s.burn():.2f} litres a lap."

    def _tyre_line(self):
        st = self.state
        each = st.get("tyre_each")
        if not each:
            return "No tyre data yet."
        w = min(range(4), key=lambda i: each[i])
        blown = [i for i in (st.get("tyre_blown") or []) if 0 <= i < 4]
        line = ""
        if blown:
            names = " and ".join(TYRE_NAMES[i] for i in blown)
            line = f"The {names} {'is' if len(blown) == 1 else 'are'} blown. Box. "
        line += f"Tyres average {sum(each) / 4 * 100:.0f} percent. Worst is the {TYRE_NAMES[w]} at {each[w] * 100:.0f}."
        g = st.get("tyre_laps")
        if not blown and g is not None:                      # "laps to the cliff" means nothing with a blown tyre
            line += f" {max(g, 0):.1f} laps to the cliff."
        temps = [(t, i) for i, t in enumerate(st.get("tyre_temp") or []) if t is not None]
        if temps:
            t, i = max(temps)
            line += f" Hottest is the {TYRE_NAMES[i]} at {t} degrees."
        return line

    def _h_tyre_temps(self):
        temps = self.state.get("tyre_temp") or []
        if not temps or any(t is None for t in temps):
            return "I don't have tyre temperatures yet."
        hot = max(range(4), key=lambda i: temps[i])
        return (f"Front left {temps[0]}, front right {temps[1]}, rear left {temps[2]}, rear right {temps[3]} degrees. "
                f"Hottest is the {TYRE_NAMES[hot]}.")

    def _h_tyre_pressure(self):
        psi = self.state.get("tyre_psi") or []
        if not psi or any(p is None for p in psi):
            return "I don't have tyre pressures yet."
        return (f"Front left {psi[0]:.0f}, front right {psi[1]:.0f}, rear left {psi[2]:.0f}, rear right {psi[3]:.0f} psi. "
                f"Highest is {max(psi):.0f}. I warn you at {config.TYRE_PSI_WARN}.")

    def _h_fuel(self):
        return self._fuel_line()

    def _h_tyres(self):
        return self._tyre_line()

    def _h_status(self):
        return f"{self._fuel_line()} {self._tyre_line()}"

    def _h_last_lap(self):
        v = self.state.get("last_lap")
        return f"Last lap {spoken_time(v)}." if v else "No lap time yet."

    def _h_best_lap(self):
        v = self.state.get("best_lap")
        return f"Your best is {spoken_time(v)}." if v else "No best lap yet."

    def _h_another_lap(self):
        st = self.state
        fl, tl, wl = st.get("fuel_laps"), st.get("tyre_laps"), st.get("tyre_worst_laps")
        if fl is None and (st.get("fuel_warning") or 0) >= 2:      # no fuel numbers, but the fuel light is orange or red
            return f"Negative. The fuel light is {FUEL_COLOURS[st['fuel_warning']]}. Box this lap."
        if fl is None or tl is None:
            return "Still learning your numbers. Give me another lap."
        # your comment: 0.6 = fuel (laps) that must remain after the extra lap
        if fl - 1 >= 0.6 and tl - 1 >= 0 and (wl is None or wl - 1 >= 0):
            return f"Affirm. Fuel for {fl:.1f} laps, tyres {tl:.1f} to the cliff."
        return "Negative. Box this lap."

    _h_pit = _h_another_lap

    # ---------------- understanding the driver ----------------
    def _quick(self, t):
        st = self.state
        s = st.get("strategy")
        tokens = re.findall(r"[a-z0-9']+", t)
        num = parse_number(t)

        if "set" in tokens and _hit(["cliff"], tokens) and num is not None and s:
            s.set_cliff(num / 100 if num > 1 else num)
            self.say(f"Tyre cliff set to {s.cliff * 100:.0f} percent.")
            return True
        if "set" in tokens and "margin" in tokens and num is not None and s:
            s.set_margin(num)
            self.say(f"Fuel call set to {s.fuel_margin:.1f} laps.")
            return True
        if _hit(["quiet", "silence"], tokens):
            self.say("Copy. Radio silence, critical calls only.", 0)
            self.radio.quiet = True
            return True
        if re.search(r"talk to me|unmute|radio on|chatty", t):
            self.radio.quiet = False
            self.say("Copy. Radio is open.")
            return True
        if re.search(r"\b(repeat|say again)\b", t):
            self.say(self.radio.last_text or "Nothing to repeat.")
            return True
        m = re.match(r"(?:remember that|the answer is|note that)\s+(.*)", t)
        if m:
            if self.last_question:
                self.memory.save_knowledge(self.last_question, m.group(1), "driver")
                self.say("Noted. I'll remember that.")
            else:
                self.say("Remember what? Ask me something first.")
            return True
        if s and st.get("live") and re.match(
                r"(my |the )?(tyres?|tires?) (are|is|feel|have gone)\s+(completely |totally |really |now )?"
                r"(done|gone|dead|finished|cooked|worn out|destroyed|shot)", t):
            each = st.get("tyre_each") or []
            if each:
                avg = sum(each) / 4
                s.set_cliff(avg)
                self.say(f"Noted. Tyres at {avg * 100:.0f} percent are finished for you. "
                         "I'll call the pit before that from now on.")
                return True
        if len(tokens) <= 3 and _hit(["thanks", "thank", "copy", "roger", "okay"], tokens):
            self.say("Copy.", 2)
            return True

        if self.router.prefers_chat(t, tokens):      # banter must not trip a one-word telemetry trigger
            return False

        if (re.search(r"\b(pressures?|psi)\b", t) and _hit(["tyre", "tyres", "tire", "tires"], tokens)
                and not re.search(r"\b(should|recommend|best|setup|set up|run|target|ideal|optimal|why|how do|how can)\b", t)):
            self.say(self._h_tyre_pressure() if st.get("live") else "Telemetry isn't live yet.")
            return True

        gated = bool(KNOWLEDGE_HINT.search(t))
        for name, groups in INTENTS:
            if all(_hit(g, tokens) for g in groups):
                if gated and name not in GATE_EXEMPT:
                    continue
                if not st.get("live"):
                    self.say("Telemetry isn't live yet.")
                else:
                    self.say(getattr(self, "_h_" + name)())
                return True
        return False

    # ---------------- thinking: memory -> local AI -> web ----------------
    def _system_prompt(self, route="knowledge"):
        """The pit-wall persona (persona.py) with the live telemetry attached."""
        return persona.build_system_prompt(
            config.ENGINEER_NAME, self._context(),
            allow_search=(route == "knowledge" and config.USE_WEB),
            lore=getattr(config, "TEAM_LORE", ()), driver=getattr(config, "DRIVER_NAME", ""))

    def _context(self):
        st = self.state
        each = st.get("tyre_each")
        grip = st.get("grip")
        data = {
            "car": st.get("car_name"), "track": st.get("track_name"), "position": st.get("position"),
            "laps_done": st.get("laps"), "fuel_laps_left": _rnd(st.get("fuel_laps")),
            "tyre_health_percent_FL_FR_RL_RR": [round(x * 100) for x in each] if each else None,
            "laps_to_tyre_cliff": _rnd(st.get("tyre_laps")),
            "tyre_core_temp_c_FL_FR_RL_RR": st.get("tyre_temp") or None,
            "tyre_pressure_psi_FL_FR_RL_RR": st.get("tyre_psi") or None,
            "air_temp_c": _rnd(st.get("air_temp")), "road_temp_c": _rnd(st.get("road_temp")),
            "track_grip_percent": None if grip is None else round(grip * 100),
            "damage_front_rear_left_right_centre": [round(v) for v in st.get("damage", [])] or None,
            "engine_load_0_to_1": _rnd(st.get("engine_load")),
            "last_lap_seconds": _rnd(st.get("last_lap")), "best_lap_seconds": _rnd(st.get("best_lap")),
            "coach_notes": st.get("lessons") or None,
        }
        return json.dumps({k: v for k, v in data.items() if v is not None})

    def _chat(self, system, user, history=False):
        """Ask the AI. Order: Ollama (if it is running) -> the built-in model. None = neither answered in time."""
        hist = list(self.history) if history else []
        text = self.ollama.chat(system, user, hist, max_tokens=getattr(config, "OLLAMA_MAX_TOKENS", 80))
        if text is None and config.USE_LLM and self.llm.ready:
            text = self.llm.chat(system, user, hist, max_tokens=70)
        return persona.tidy_radio(text, config.ENGINEER_NAME) if text else None

    def _ask_ai(self, t):
        if len(t.split()) < 3:
            self.say("Say again?")
            return
        self.last_question = t
        known = self.memory.get_knowledge(t)
        if known:
            self.say(known[0])
            return
        route = self.router.classify(t, re.findall(r"[a-z0-9']+", t))      # "chat" or "knowledge"
        print(f"[ROUTER] {route} -> {t!r}")
        answer = self._chat(self._system_prompt(route), t, history=True)
        if answer is None:                     # no AI answered (still loading, down, or too slow)
            if route == "chat":
                self.say(self.llm.spoken_status() or persona.fallback_chat())
            elif KNOWLEDGE_HINT.search(t) or "?" in t or len(t.split()) >= 5:
                self._research_async(t, t)
            else:
                self.say(self.llm.spoken_status() or "Sorry, say that again?")
        elif answer.upper().startswith("SEARCH"):
            if route == "knowledge":
                self._research_async(t, answer.split(":", 1)[-1].strip() or t)
            else:                              # banter must never turn into a web search
                self.say(persona.fallback_chat())
        else:
            self.history.append((t, answer))
            self.say(answer)

    # ---------------- trusted web, always in the background ----------------
    def _research_async(self, question, query):
        if not config.USE_WEB:
            return self._unknown(question)
        self.say(["Let me check that.", "Give me a moment, checking."], 2)
        threading.Thread(target=self.research, args=(question, query, True), daemon=True).start()

    def _web(self, query):
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            from urllib.parse import urlparse
            sites = " OR ".join(f"site:{s}" for s in config.TRUSTED_SITES)
            hits = []
            try:
                ddgs = DDGS(timeout=config.WEB_TIMEOUT)
            except TypeError:
                ddgs = DDGS()
            for r in ddgs.text(f"{query} Assetto Corsa ({sites})", max_results=15):
                domain = urlparse(r.get("href", "")).netloc.lower().replace("www.", "")
                if any(domain.endswith(s) for s in config.TRUSTED_SITES):
                    hits.append({"domain": domain, "body": r.get("body", "")})
                if len(hits) == 3:
                    break
            return hits
        except Exception as e:
            print(f"[WEB] {e}")
            return []

    def research(self, question, query, report):
        """Search trusted sites, summarise, SAVE. report=True speaks it; False = silent background study."""
        with self.research_lock:
            hits = self._web(query)
            if not hits:
                if report:
                    self._unknown(question)
                return None
            snippets = "\n".join(f"- ({h['domain']}) {h['body']}" for h in hits)
            answer = self._chat("Using ONLY these snippets, answer the driver's question in max two short "
                                "radio-style sentences. If they don't answer it, reply exactly: NONE.\n" + snippets,
                                question)
            if answer is None:
                answer = hits[0]["body"][:180].strip()
            if not answer or answer.upper().startswith("NONE"):
                if report:
                    self._unknown(question)
                return None
            self.memory.save_knowledge(question, answer, hits[0]["domain"])
            if report:
                self.say(f"{answer} Source, {hits[0]['domain']}.")
            return answer

    def _unknown(self, question):
        self.memory.log_unknown(question)
        self.say("I don't know that one and found nothing I trust. I'll study it and get back to you. "
                 "Or teach me: say remember that, then the answer.", 2)
