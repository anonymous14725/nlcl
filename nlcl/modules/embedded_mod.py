"""Embedded domain: micro-controller firmware (Arduino C++ and MicroPython).

Every board entity (arduino / uno / nano / mega / esp32 / esp8266 / pico /
pico_w / stm32) becomes one firmware project in ``embedded/<board>/``:

* ``<board>.ino``      Arduino C++ sketch (Arduino IDE, arduino-cli, PlatformIO)
* ``platformio.ini``   PlatformIO project (``pio run``)
* ``main.py``          MicroPython equivalent (esp32 / pico boards)
* ``diagram.json`` + ``wokwi.toml`` + ``libraries.txt``
                       Wokwi simulation project (free, in the browser / VS Code)

Components (led, button, sensors, motor, servo, relay, buzzer, display ...)
belong to the board they are children of; orphans go to the only board, or to
an implicit default board (esp32 when a network channel exists, else Uno).

Behaviours become functions called from ``loop()`` (millis() timers, edge
detected button presses, change detection ...).  Channels become transports:
serial JSON lines, HTTP to the NLCL hub, MQTT, UDP, I2C/SPI byte frames or one
GPIO pin per field.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..ir import Entity, describe_action, ident
from .base import BuildContext, Channel, DomainModule, RunResult

# ===========================================================================
# Boards
# ===========================================================================


def _int(v: Any, default: int) -> int:
    """int() that never raises (channel config can carry odd user values)."""
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def _rng(a: int, b: int, prefix: str = "") -> list[str]:
    return [f"{prefix}{i}" for i in range(a, b + 1)]


BOARDS: dict[str, dict] = {
    "uno": dict(title="Arduino Uno", fqbn="arduino:avr:uno", pio=("atmelavr", "uno", "uno"),
                wokwi="wokwi-arduino-uno", wid="uno", arch="avr", wifi="", adc_max=1023, upy=False,
                pwm=["3", "5", "6", "9", "10", "11"], digital=["2", "4", "7", "8", "12", "13"],
                analog=_rng(0, 5, "A"), i2c=("A4", "A5"), spi=("13", "12", "11"), ss="10",
                vcc="5V", vcc5="5V", gnd="GND.1", led="13", fw="hex"),
    "nano": dict(title="Arduino Nano", fqbn="arduino:avr:nano", pio=("atmelavr", "nanoatmega328", "nano"),
                 wokwi="wokwi-arduino-nano", wid="nano", arch="avr", wifi="", adc_max=1023, upy=False,
                 pwm=["3", "5", "6", "9", "10", "11"], digital=["2", "4", "7", "8", "12", "13"],
                 analog=_rng(0, 7, "A"), i2c=("A4", "A5"), spi=("13", "12", "11"), ss="10",
                 vcc="5V", vcc5="5V", gnd="GND.1", led="13", fw="hex"),
    "mega": dict(title="Arduino Mega 2560", fqbn="arduino:avr:mega", pio=("atmelavr", "megaatmega2560", "mega"),
                 wokwi="wokwi-arduino-mega", wid="mega", arch="avr", wifi="", adc_max=1023, upy=False,
                 pwm=_rng(2, 12) + ["44", "45", "46"], digital=_rng(22, 49) + ["13"],
                 analog=_rng(0, 15, "A"), i2c=("20", "21"), spi=("52", "50", "51"), ss="53",
                 vcc="5V", vcc5="5V", gnd="GND.1", led="13", fw="hex"),
    "esp32": dict(title="ESP32 DevKit", fqbn="esp32:esp32:esp32", pio=("espressif32", "esp32dev", "esp32"),
                  wokwi="board-esp32-devkit-c-v4", wid="esp", arch="esp32", wifi="esp32", adc_max=4095, upy=True,
                  pwm=["13", "14", "27", "26", "25", "33", "32", "4", "16", "17", "5", "18", "19", "23", "15", "12", "2"],
                  digital=["13", "14", "27", "26", "25", "33", "32", "4", "16", "17", "5", "18", "19", "23", "15", "12", "2"],
                  analog=["34", "35", "32", "33", "36", "39"], i2c=("21", "22"), spi=("18", "19", "23"), ss="5",
                  vcc="3V3", vcc5="5V", gnd="GND.1", led="2", fw="bin"),
    "esp8266": dict(title="ESP8266 NodeMCU", fqbn="esp8266:esp8266:nodemcuv2",
                    pio=("espressif8266", "nodemcuv2", "esp8266"), wokwi="", wid="esp", arch="esp8266",
                    wifi="esp8266", adc_max=1023, upy=False,
                    pwm=["5", "4", "14", "12", "13", "0", "2", "15"],
                    digital=["5", "4", "14", "12", "13", "16", "0", "2", "15"],
                    analog=["A0"], i2c=("4", "5"), spi=("14", "12", "13"), ss="15",
                    vcc="3V3", vcc5="VIN", gnd="GND", led="2", fw="bin"),
    "pico": dict(title="Raspberry Pi Pico (RP2040)", fqbn="rp2040:rp2040:rpipico", pio=("raspberrypi", "pico", "pico"),
                 wokwi="wokwi-pi-pico", wid="pico", arch="rp2040", wifi="", adc_max=1023, upy=True,
                 pwm=["15", "14", "13", "12", "11", "10", "9", "8", "7", "6", "3", "2", "20", "21", "22"],
                 digital=["15", "14", "13", "12", "11", "10", "9", "8", "7", "6", "3", "2", "20", "21", "22"],
                 analog=["26", "27", "28"], i2c=("4", "5"), spi=("18", "16", "19"), ss="17",
                 vcc="3V3", vcc5="VBUS", gnd="GND.1", led="", fw="uf2"),
    "pico_w": dict(title="Raspberry Pi Pico W", fqbn="rp2040:rp2040:rpipicow", pio=("raspberrypi", "rpipicow", "pico_w"),
                   wokwi="board-pi-pico-w", wid="pico", arch="rp2040", wifi="picow", adc_max=1023, upy=True,
                   pwm=["15", "14", "13", "12", "11", "10", "9", "8", "7", "6", "3", "2", "20", "21", "22"],
                   digital=["15", "14", "13", "12", "11", "10", "9", "8", "7", "6", "3", "2", "20", "21", "22"],
                   analog=["26", "27", "28"], i2c=("4", "5"), spi=("18", "16", "19"), ss="17",
                   vcc="3V3", vcc5="VBUS", gnd="GND.1", led="", fw="uf2"),
    "stm32": dict(title="STM32 Blue Pill (F103C8)", fqbn="STMicroelectronics:stm32:GenF1:pnum=BLUEPILL_F103C8",
                  pio=("ststm32", "bluepill_f103c8", "bluepill"), wokwi="", wid="stm", arch="stm32", wifi="",
                  adc_max=1023, upy=False,
                  pwm=["PA8", "PB8", "PB9", "PA0", "PA1", "PA2", "PA3", "PA6", "PA7", "PB0", "PB1"],
                  digital=["PB12", "PB13", "PB14", "PB15", "PB5", "PB4", "PB3", "PA15", "PB10", "PB11"],
                  analog=["PA0", "PA1", "PA2", "PA3", "PB0", "PB1"], i2c=("PB7", "PB6"),
                  spi=("PA5", "PA6", "PA7"), ss="PA4", vcc="3V3", vcc5="5V", gnd="GND", led="PC13", fw="bin"),
}

BOARD_KINDS = {
    "arduino": "uno", "uno": "uno", "nano": "nano", "mega": "mega", "esp32": "esp32", "esp8266": "esp8266",
    "nodemcu": "esp8266", "wemos": "esp8266", "pico": "pico", "rp2040": "pico", "pico_w": "pico_w",
    "picow": "pico_w", "stm32": "stm32", "bluepill": "stm32", "board": "uno", "microcontroller": "uno",
    "mcu": "uno", "devboard": "uno",
}

# role -> state members every component of that role has (prop -> default)
ROLE_STATE: dict[str, dict] = {
    "led": {"on": False},
    "button": {"pressed": False},
    "switch": {"on": False},
    "pir": {"motion": False},
    "temp": {"temperature": 0.0},
    "dht": {"temperature": 0.0, "humidity": 0.0},
    "distance": {"distance": 0.0},
    "light": {"light": 0},
    "pot": {"value": 0},
    "analog": {},           # measure prop is derived from the subtype
    "motor": {"on": False, "speed": 255},
    "fan": {"on": False, "speed": 255},
    "servo": {"angle": 90},
    "relay": {"on": False},
    "buzzer": {"on": False, "frequency": 1000},
    "oled": {"text": ""},
    "lcd": {"text": ""},
    "neopixel": {"on": True, "color": "white", "brightness": 50, "pixels": 1},
    "board": {},
    "generic": {},
}
# the "main" value of a component when its name is used in an expression
PRIMARY = {"led": "on", "button": "pressed", "switch": "on", "pir": "motion", "temp": "temperature",
           "dht": "temperature", "distance": "distance", "light": "light", "pot": "value", "motor": "on",
           "fan": "on", "servo": "angle", "relay": "on", "buzzer": "on", "oled": "text", "lcd": "text",
           "neopixel": "on"}
# extra runtime props the drivers understand (beyond ROLE_STATE)
DRIVER_PROPS = {"led": ("brightness",), "neopixel": ("color", "brightness", "on"), "motor": ("speed",),
                "fan": ("speed",), "servo": ("angle",), "buzzer": ("frequency",), "oled": ("text",),
                "lcd": ("text",), "relay": ("on",)}
# PWM-style props (0..255) an analog reading has to be rescaled for
PWM_PROPS = {("led", "brightness"), ("neopixel", "brightness"), ("motor", "speed"), ("fan", "speed")}
# analog measurements users think of in percent
PERCENT_FIELDS = {"moisture", "humidity", "level", "percent", "water", "water_level", "soil", "wetness"}
# props that configure hardware and never become runtime state
SKIP_PROPS = {"pin", "pins", "trig_pin", "echo_pin", "subtype", "position", "rotation", "scale", "size",
              "address", "model", "wifi_ssid", "wifi_password", "board", "baud", "port", "hub_host",
              "collision_cm", "material", "texture", "shape", "units", "mass", "gravity"}
SENSOR_WORDS = {"sensor", "probe", "detector", "meter", "transducer"}

CPP_RESERVED = set("""alignas alignof and and_eq asm auto bitand bitor bool break case catch char class compl
const constexpr const_cast continue decltype default delete do double dynamic_cast else enum explicit export
extern false float for friend goto if inline int long mutable namespace new noexcept not not_eq nullptr operator
or or_eq private protected public register reinterpret_cast return short signed sizeof static static_assert
static_cast struct switch template this throw true try typedef typeid typename union unsigned using virtual void
volatile wchar_t while xor xor_eq setup loop delay millis micros random min max abs round map constrain sq sqrt
pow sin cos tan log exp tone word byte boolean bit isnan isinf radians degrees interrupts yield main index select
free malloc exit printf signal remove rename gamma y0 y1 j0 j1 now string serial time ceil floor trunc fabs
lround labs atoi itoa ltoa random_seed init""".split())
PY_RESERVED = set("""False None True and as assert async await break class continue def del elif else except
finally for from global if import in is lambda nonlocal not or pass raise return try while with yield time sys
json machine network Pin ADC PWM I2C SPI Obj REMOTE select dht neopixel print str int float bool len range list
dict min max abs round id type object input open setup loop main socket requests""".split())

# local / helper names used inside generated functions: an entity global with one of these names would be
# shadowed (C++) or would clobber a helper (MicroPython), so such entities get an '_obj' suffix.
LOCAL_NAMES = set("""a a0 a1 a2 args b body buf c ch changed client code col cond data e got h hi http i j k key
kw l1 last lasttry len line lo m method msg n now path payload r raw s self t t0 tc topic txt up url us v vals w was
who beep color command comms handle_line i16 to_num nlcl_log read_inputs write_outputs triggers wifi_up udp_sock
mqtt_client poll_serial math ssd1306 umqtt urequests usocket struct gc os errno""".split())
COLORS = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 200, 0),
          "white": (255, 255, 255), "orange": (255, 100, 0), "purple": (128, 0, 255), "pink": (255, 60, 120),
          "cyan": (0, 255, 255), "magenta": (255, 0, 255), "black": (0, 0, 0)}

# I2C/SPI frame (shared with the hardware module): 0xA5, N, then N x int16 big-endian in field order.
# Reads: SPI master sends 0x5A then clocks 1 + 2N bytes (0xA5 + words); I2C read returns 0xA5, N, words.
FRAME_SYNC = 0xA5
FRAME_READ = 0x5A


# ===========================================================================
# Internal model
# ===========================================================================

@dataclass
class Member:
    name: str        # identifier in generated code
    field: str       # original property / JSON key
    ctype: str       # bool | long | float | String
    value: Any


@dataclass
class Comp:
    e: Entity
    role: str
    var: str
    members: dict = field(default_factory=dict)   # field -> Member
    pins: dict = field(default_factory=dict)      # logical -> canonical pin string
    explicit: set = field(default_factory=set)    # logical pins the user chose


@dataclass
class Chan:
    ch: Channel
    out: bool
    inn: bool
    transport: str
    src: Comp | None = None
    dst: Comp | None = None
    note: str = ""
    pins: dict = field(default_factory=dict)      # gpio: field -> pin ; spi: {"cs": pin}

    @property
    def cid(self) -> str:
        return ident(self.ch.id) or "chan"

    @property
    def local(self) -> bool:
        return self.out and self.inn


@dataclass
class Beh:
    n: int
    comp: Comp
    b: dict

    @property
    def trig(self) -> str:
        return self.b["on"].split(":", 1)[0]

    @property
    def arg(self) -> str:
        return self.b["on"].split(":", 1)[1] if ":" in self.b["on"] else ""


@dataclass
class Board:
    key: str
    spec: dict
    ident: str
    entity: Entity | None
    comps: list = field(default_factory=list)
    chans: list = field(default_factory=list)
    behs: list = field(default_factory=list)
    used: set = field(default_factory=set)
    warnings: list = field(default_factory=list)
    remotes: dict = field(default_factory=dict)      # key "src.field" -> cpp var
    calls: dict = field(default_factory=dict)        # user function name -> max argc
    libs: set = field(default_factory=set)
    pwm_avoid: set = field(default_factory=set)     # pins whose PWM timer is taken by Servo / tone()

    @property
    def wifi(self) -> str:
        return self.spec["wifi"]

    def by_role(self, *roles) -> list:
        return [c for c in self.comps if c.role in roles]


# ===========================================================================
# Module
# ===========================================================================

class EmbeddedModule(DomainModule):
    name = "embedded"
    title = "Embedded firmware (Arduino C++, MicroPython, Wokwi simulation)"
    aliases = ("embedded", "firmware", "mcu", "iot", "microcontroller", "microcontrollers",
               "تعبیه‌شده", "تعبیه_شده", "میکروکنترلر")
    keywords = ("arduino", "esp32", "esp8266", "nodemcu", "wemos", "rp2040", "pico", "stm32", "bluepill",
                "micropython", "platformio", "wokwi", "sketch", "gpio", "pwm", "adc", "wifi", "bluetooth",
                "led", "leds", "button", "pushbutton", "sensor", "sensors", "thermistor", "thermometer", "dht",
                "dht11", "dht22", "hygrometer", "ultrasonic", "sonar", "ldr", "photoresistor", "potentiometer",
                "knob", "motor", "servo", "relay", "buzzer", "piezo", "fan", "oled", "lcd", "neopixel",
                "ws2812", "pir", "breadboard", "millis", "baud", "thermostat", "greenhouse", "irrigation",
                "doorbell", "datalogger", "weather station", "smart home", "esp", "nano", "uno", "mega")
    kinds = {
        "object": {},
        # boards
        "arduino": {}, "uno": {}, "nano": {}, "mega": {}, "esp32": {}, "esp8266": {}, "nodemcu": {},
        "pico": {}, "pico_w": {}, "rp2040": {}, "stm32": {}, "bluepill": {}, "board": {}, "microcontroller": {},
        # outputs
        "led": {"on": False}, "lamp": {"on": False},
        "motor": {"on": False, "speed": 255}, "fan": {"on": False, "speed": 255},
        "servo": {"angle": 90}, "relay": {"on": False},
        "buzzer": {"on": False, "frequency": 1000}, "piezo": {"on": False, "frequency": 1000},
        "display": {"text": ""}, "oled": {"text": ""}, "lcd": {"text": ""},
        "neopixel": {"on": True, "color": "white", "brightness": 50, "pixels": 1},
        "ws2812": {"on": True, "color": "white", "brightness": 50, "pixels": 1},
        # inputs
        "button": {"pressed": False}, "pushbutton": {"pressed": False}, "switch": {"on": False},
        "pir": {"motion": False},
        "sensor": {},
        "thermistor": {"temperature": 0.0}, "thermometer": {"temperature": 0.0}, "lm35": {"temperature": 0.0},
        "dht": {"temperature": 0.0, "humidity": 0.0}, "dht11": {"temperature": 0.0, "humidity": 0.0},
        "dht22": {"temperature": 0.0, "humidity": 0.0}, "hygrometer": {"temperature": 0.0, "humidity": 0.0},
        "ultrasonic": {"distance": 0.0}, "hc_sr04": {"distance": 0.0}, "sonar": {"distance": 0.0},
        "ldr": {"light": 0}, "photoresistor": {"light": 0},
        "potentiometer": {"value": 0}, "knob": {"value": 0},
        "counter": {"count": 0},
    }
    suggested_props = ("pin", "on", "interval_ms", "threshold", "brightness", "angle", "speed")
    protocols = ("serial", "http", "mqtt", "udp", "i2c", "spi", "gpio", "wire")
    toolchain = """Every board folder `embedded/<board>/` is a complete project for free tools:

- **Wokwi** (free browser simulator, no hardware): open https://wokwi.com, start a new project for the
  board, paste `<board>.ino` into `sketch.ino`, replace `diagram.json` and `libraries.txt`, press Play.
  VS Code: install the free Wokwi extension, run `pio run` once, then *Wokwi: Start Simulator* (uses `wokwi.toml`).
- **arduino-cli**: `arduino-cli core install <core>` then
  `arduino-cli compile --fqbn <fqbn> embedded/<board>` and
  `arduino-cli upload -p <PORT> --fqbn <fqbn> embedded/<board>` (fqbn/core listed in the folder README).
- **PlatformIO**: `pio run -d embedded/<board>` / `pio run -d embedded/<board> -t upload` / `pio device monitor`.
- **MicroPython** (esp32 / pico): flash MicroPython, then `mpremote cp embedded/<board>/main.py :main.py` and reset.

Serial channels are newline-delimited JSON at 115200 baud: `python main.py` in the python folder reads them."""

    # ------------------------------------------------------------------ props
    def validate(self, entity: Entity) -> list[str]:
        """Give sensors their measurement property (e.g. temperature) and check pins.  Never raises."""
        warns: list[str] = []
        try:
            role = self._role(entity)
            if role in ("temp", "dht", "distance", "light", "pot", "pir", "analog"):
                for k, v in self._state_defaults(entity, role).items():
                    entity.props.setdefault(k, v)
            pin = entity.props.get("pin")
            if pin is not None and not isinstance(pin, (list, tuple)) and self._norm_pin("uno", pin) is None \
                    and self._norm_pin("stm32", pin) is None:
                warns.append(f"pin {pin!r} is not a pin name I understand; a free pin will be chosen")
        except Exception:  # validation must never break the interpreter
            pass
        return warns

    # ------------------------------------------------------------------ roles
    @staticmethod
    def _words(e: Entity) -> set[str]:
        st = str(e.props.get("subtype", "") or "")
        return {w for w in re.split(r"[^a-z0-9]+", (e.kind + " " + st + " " + e.name).lower()) if w}

    def _role(self, e: Entity) -> str:
        k = (e.kind or "object").lower()
        st = str(e.props.get("subtype", "") or "").lower()
        sw = {w for w in re.split(r"[^a-z0-9]+", st) if w}
        allw = sw | {k}
        if k in BOARD_KINDS:
            return "board"
        if k in ("neopixel", "ws2812", "strip", "ring") or (k == "led" and sw & {"neopixel", "ws2812", "rgb_strip"}):
            return "neopixel"
        if k in ("led", "lamp", "bulb", "light", "indicator"):
            return "led"
        if k in ("button", "pushbutton", "push_button"):
            return "button"
        if k == "switch":
            return "switch"
        if k in ("motor",):
            return "motor"
        if k == "fan":
            return "fan"
        if k == "servo":
            return "servo"
        if k == "relay":
            return "relay"
        if k in ("buzzer", "piezo", "speaker", "beeper", "siren"):
            return "buzzer"
        if k == "lcd" or (k in ("display", "screen") and "lcd" in sw):
            return "lcd"
        if k in ("display", "oled", "screen", "ssd1306"):
            return "oled"
        if k in ("potentiometer", "knob", "dial", "slider", "pot"):
            return "pot"
        sensorish = k in SENSOR_WORDS or k.endswith("sensor") or k in (
            "pir", "dht", "dht11", "dht22", "hygrometer", "thermistor", "thermometer", "lm35", "ntc",
            "ultrasonic", "hc_sr04", "sonar", "ldr", "photoresistor", "photocell")
        if not sensorish:
            # "pico w" / "esp32 devkit" parse as kind 'w' / 'devkit' with the board in the subtype
            if sw & set(BOARD_KINDS) and k not in ROLE_STATE:
                return "board"
            return "generic"
        if allw & {"pir", "motion", "presence", "movement"}:
            return "pir"
        if allw & {"dht", "dht11", "dht22", "humidity", "hygrometer"}:
            return "dht"
        if allw & {"temperature", "temp", "thermo", "thermal", "thermistor", "thermometer", "lm35", "ntc", "heat"}:
            return "temp"
        if allw & {"distance", "ultrasonic", "sonar", "hc_sr04", "range", "proximity", "hcsr04"}:
            return "distance"
        if allw & {"light", "ldr", "photo", "photoresistor", "photocell", "lux", "luminosity"}:
            return "light"
        return "analog"

    def _state_defaults(self, e: Entity, role: str) -> dict:
        d = dict(ROLE_STATE.get(role, {}))
        if role == "analog":
            st = [w for w in re.split(r"[^a-z0-9]+", str(e.props.get("subtype", "") or "").lower()) if w]
            m = ident(st[-1]) if st else ""
            if not m or m in SKIP_PROPS or m in CPP_RESERVED or m in PY_RESERVED or m in ("analog", "sensor"):
                m = "value"
            d[m] = 0
        return d

    # ------------------------------------------------------------------ pins
    @staticmethod
    def _norm_pin(key: str, v: Any) -> str | None:
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, float):
            v = int(v)
        s = str(v).strip().upper().replace(" ", "")
        if key == "stm32":
            m = re.fullmatch(r"P([A-E])(\d{1,2})", s)
            return f"P{m.group(1)}{m.group(2)}" if m else None
        s = re.sub(r"^(GPIO|GP|IO|PIN)", "", s)
        if key == "esp8266":
            dmap = {"D0": "16", "D1": "5", "D2": "4", "D3": "0", "D4": "2", "D5": "14", "D6": "12", "D7": "13",
                    "D8": "15"}
            if s in dmap:
                return dmap[s]
        m = re.fullmatch(r"D(\d{1,2})", s)
        if m:
            s = m.group(1)
        m = re.fullmatch(r"A(\d{1,2})", s)
        if m:
            n = int(m.group(1))
            if key == "esp32":
                amap = {0: "36", 3: "39", 4: "32", 5: "33", 6: "34", 7: "35"}
                return amap.get(n)
            if key in ("pico", "pico_w"):
                return str(26 + n) if n <= 2 else None
            return f"A{n}"
        if re.fullmatch(r"\d{1,2}", s):
            return str(int(s))
        return None

    @staticmethod
    def _pin_needs(c: Comp) -> list[tuple[str, str]]:
        r = c.role
        if r == "led":
            return [("pin", "pwm" if "brightness" in c.e.props else "digital")]
        if r in ("button", "switch", "pir", "dht", "relay", "neopixel"):
            return [("pin", "digital")]
        if r in ("temp", "light", "pot", "analog"):
            return [("pin", "analog")]
        if r == "distance":
            return [("trig", "digital"), ("echo", "digital")]
        if r in ("motor", "fan"):
            return [("pin", "pwm")]
        if r in ("servo", "buzzer"):  # Servo / tone() work on any digital pin: keep PWM pins for dimming
            return [("pin", "digital")]
        return []

    def _alloc(self, b: Board, kind: str, prefer: str = "") -> str:
        sp = b.spec
        # analog pins double as digital I/O (except the input-only ESP32 ADC pins)
        spare = [p for p in sp["analog"] if not (b.key == "esp32" and p in ("34", "35", "36", "39"))]
        pwm = [p for p in sp["pwm"] if p not in b.pwm_avoid]
        order = {"analog": [sp["analog"]], "pwm": [pwm, sp["digital"], sp["pwm"]],
                 "digital": [sp["digital"], sp["pwm"], spare]}[kind]
        if prefer and prefer not in b.used and kind != "analog":
            b.used.add(prefer)
            return prefer
        for pi, pool in enumerate(order):
            for p in pool:
                if p not in b.used:
                    b.used.add(p)
                    if kind == "pwm" and pi > 0:
                        b.warnings.append(f"no free PWM pin left: pin {p} gives on/off only (set 'pin' explicitly)")
                    return p
        b.warnings.append(f"ran out of free {kind} pins; reusing a pin - set 'pin' explicitly")
        pool = order[0] or sp["digital"]
        return pool[0]

    def _assign_pins(self, b: Board, needs_i2c: bool, needs_spi: bool) -> None:
        sp = b.spec
        if needs_i2c:
            b.used.update(sp["i2c"])
        if needs_spi:
            b.used.update(sp["spi"])
        # AVR timers: Servo takes Timer1 (Uno/Nano pins 9, 10; Mega Timer5: 44-46), tone() takes Timer2
        # (Uno/Nano pins 3, 11; Mega 9, 10) - analogWrite() on those pins then stops working.
        if sp["arch"] == "avr":
            mega = b.key == "mega"
            if b.by_role("servo"):
                b.pwm_avoid |= {"44", "45", "46"} if mega else {"9", "10"}
            if b.by_role("buzzer"):
                b.pwm_avoid |= {"9", "10"} if mega else {"3", "11"}
        # explicit pins first
        for c in b.comps:
            needs = self._pin_needs(c)
            if not needs:
                continue
            props = c.e.props
            given: dict[str, Any] = {}
            if isinstance(props.get("pins"), (list, tuple)):
                for (logical, _), v in zip(needs, props["pins"]):
                    given[logical] = v
            if "pin" in props and not isinstance(props["pin"], (list, tuple)):
                given[needs[0][0]] = props["pin"]
            if "trig_pin" in props:
                given["trig"] = props["trig_pin"]
            if "echo_pin" in props:
                given["echo"] = props["echo_pin"]
            for logical, v in given.items():
                p = self._norm_pin(b.key, v)
                if p is None:
                    b.warnings.append(f"{c.e.name}: pin {v!r} not valid for {sp['title']}; choosing a free pin")
                    continue
                if p in b.used:
                    owner = next((o.e.name for o in b.comps if p in o.pins.values()), "a bus")
                    b.warnings.append(f"{c.e.name}: pin {p} is already used by {owner}")
                if c.role in ("temp", "light", "pot", "analog") and p not in sp["analog"]:
                    why = (" (ADC1 pins 32-39 only: ADC2 stops working while WiFi is on)"
                           if b.key == "esp32" else "")
                    b.warnings.append(f"{c.e.name}: pin {p} is not a usable analog input on {sp['title']}{why}")
                kind = dict(needs).get(logical)
                if kind == "pwm" and c.role != "servo" and c.role != "buzzer":
                    if p not in sp["pwm"]:
                        b.warnings.append(f"{c.e.name}: pin {p} has no PWM on {sp['title']}; speed/brightness "
                                          f"will be on/off only")
                    elif p in b.pwm_avoid:
                        b.warnings.append(f"{c.e.name}: PWM on pin {p} stops working while Servo/tone() use its "
                                          f"timer; pick another PWM pin")
                c.pins[logical] = p
                c.explicit.add(logical)
                b.used.add(p)
        # then free pins; scarce analog pins first
        for kind in ("analog", "pwm", "digital"):
            for c in b.comps:
                for logical, k in self._pin_needs(c):
                    if k == kind and logical not in c.pins:
                        prefer = sp["led"] if (c.role == "led" and kind == "digital"
                                               and not b.by_role("led")[0].pins.get("pin")) else ""
                        if c.role == "led" and c is not b.by_role("led")[0]:
                            prefer = ""
                        c.pins[logical] = self._alloc(b, kind, prefer)

    # ------------------------------------------------------------------ naming
    @staticmethod
    def _safe(n: str, taken: set | None = None) -> str:
        n = ident(n) or "obj"
        if n in CPP_RESERVED or n in PY_RESERVED or n in LOCAL_NAMES or n.startswith(("nlcl", "_", "user_", "remote_",
                                                                                      "publish_", "recv_", "poll_", "tx_")):
            n = n.lstrip("_") + "_obj"
        if taken is not None:
            base, i = n, 2
            while n in taken:
                n = f"{base}{i}"
                i += 1
            taken.add(n)
        return n

    @staticmethod
    def _mname(f: str) -> str:
        n = ident(f) or "value"
        if n in CPP_RESERVED or n in PY_RESERVED:
            n += "_"
        return n

    @staticmethod
    def _ctype(v: Any) -> str | None:
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "long" if -2147483648 <= v <= 2147483647 else "float"  # long is 32-bit on AVR
        if isinstance(v, float):
            return "float"
        if isinstance(v, str):
            return "String"
        return None

    def _member(self, c: Comp, f: str, ctype: str = "float", value: Any = None) -> Member:
        f = ident(f) or "value"
        m = c.members.get(f)
        if m is None:
            if value is None:
                value = {"bool": False, "long": 0, "float": 0.0, "String": ""}[ctype]
            m = Member(self._mname(f), f, ctype, value)
            c.members[f] = m
        return m

    # ------------------------------------------------------------------ c / python literals
    @staticmethod
    def _cstr(s: Any) -> str:
        s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
        return f'"{s}"'

    def _clit(self, m: Member) -> str:
        v = m.value
        if m.ctype == "bool":
            return "true" if v else "false"
        if m.ctype == "long":
            try:
                return str(max(-2147483647, min(2147483647, int(v))))
            except (TypeError, ValueError, OverflowError):
                return "0"
        if m.ctype == "float":
            try:
                f = float(v)
                return repr(f) if math.isfinite(f) else "0.0"
            except (TypeError, ValueError, OverflowError):
                return "0.0"
        return self._cstr(v)

    def _plit(self, m: Member) -> str:
        v = m.value
        try:
            return {"bool": lambda: "True" if v else "False", "long": lambda: str(int(v)),
                    "float": lambda: repr(float(v)) if math.isfinite(float(v)) else "0.0",
                    "String": lambda: repr(str(v))}[m.ctype]()
        except (TypeError, ValueError, OverflowError):
            return "0"

    # ======================================================================
    # Planning
    # ======================================================================
    def _plan(self, ctx: BuildContext) -> list[Board]:
        ents = list(ctx.entities)
        by_id = {e.id: e for e in ents}
        taken: set[str] = set()
        boards: list[Board] = []
        board_of_ent: dict[str, Board] = {}
        for e in ents:
            if self._role(e) == "board":
                sub = str(e.props.get("subtype", "") or "").lower()
                words = [w for w in re.split(r"[^a-z0-9]+", f"{sub} {e.kind}".lower()) if w]
                key = BOARD_KINDS.get(e.kind.lower()) or next(
                    (BOARD_KINDS[w] for w in words if w in BOARD_KINDS and w not in ("board", "microcontroller", "mcu")),
                    "uno")
                if key == "pico" and (set(words) & {"w", "wifi", "wireless"}):
                    key = "pico_w"
                for k2 in ("esp32", "esp8266", "nano", "mega", "stm32"):
                    if key == "uno" and k2 in sub:
                        key = k2
                b = Board(key, BOARDS[key], self._safe(e.ident, taken), e)
                boards.append(b)
                board_of_ent[e.id] = b
        netted = any(c.protocol in ("http", "mqtt", "websocket", "udp") for c in ctx.channels)
        default: Board | None = None

        def home(e: Entity) -> Board:
            nonlocal default
            p, seen = e.parent, set()
            while p in by_id and p not in seen:
                seen.add(p)
                if p in board_of_ent and self._role(by_id[p]) == "board":
                    return board_of_ent[p]
                p = by_id[p].parent
            if boards:
                return boards[0]
            if default is None:
                key = "esp32" if netted else "uno"
                default = Board(key, BOARDS[key], self._safe(key, taken | {ident(x.name) for x in ents}), None)
                taken.add(default.ident)
            return default

        for e in ents:
            if e.id in board_of_ent:
                continue
            board_of_ent[e.id] = home(e)
        if default is not None:
            boards.append(default)
        # components
        for e in ents:
            b = board_of_ent[e.id]
            role = self._role(e)
            c = Comp(e, role, b.ident if e is b.entity else self._safe(e.ident, taken))
            for k, v in self._state_defaults(e, role).items():
                self._member(c, k, self._ctype(v) or "float", v)
            for k, v in e.props.items():
                if k.startswith("_") or (k in SKIP_PROPS) or (k == "color" and role != "neopixel"):
                    continue
                t = self._ctype(v)
                if t is None:
                    continue
                old = c.members.get(ident(k))
                if old and old.ctype != t and not (old.ctype in ("long", "float") and t in ("long", "float", "bool")):
                    continue  # keep the driver's type when the user gave an odd value
                if old and old.ctype in ("long", "float") and t in ("long", "float", "bool"):
                    old.value = v
                    continue
                if old and old.ctype == "bool" and t in ("long", "float"):
                    old.value = bool(v)
                    continue
                self._member(c, k, t, v).value = v
            self._member(c, "alive", "bool", True)
            if e is b.entity:
                b.comps.insert(0, c)
            else:
                b.comps.append(c)
        comp_of = {c.e.id: (b, c) for b in boards for c in b.comps}
        self._all_comps = [(b, c) for b in boards for c in b.comps]
        # "set door angle to 90" inside another entity's behaviour reaches us as a default-valued
        # 'door_angle' prop copied onto the owner: drop that shadow so names resolve to door.angle
        for b, c in self._all_comps:
            defaults = self._state_defaults(c.e, c.role)
            for f in list(c.members):
                m = c.members[f]
                if f in defaults or f == "alive" or m.value not in (0, 0.0, "", False):
                    continue
                pre = c.e.ident + "_"
                if self._owned_prefix(c, f, [o for _, o in self._all_comps]) is not None or                         (f.startswith(pre) and self._known_prop(c, f[len(pre):])):
                    del c.members[f]
        # channels
        for ch in ctx.channels:
            sb = comp_of.get(ch.src.id)
            db = comp_of.get(ch.dst.id)
            for b in boards:
                out = sb is not None and sb[0] is b
                inn = db is not None and db[0] is b
                if not (out or inn):
                    continue
                proto = {"uart": "serial", "websocket": "http", "wire": "gpio"}.get(ch.protocol, ch.protocol)
                note = ""
                if proto in ("http", "mqtt", "udp") and not b.wifi and not (out and inn):
                    note = (f"{b.spec['title']} has no WiFi: '{ch.protocol}' channel falls back to serial JSON "
                            f"(run the python bridge, or use an esp32 / pico_w board)")
                    b.warnings.append(note)
                    proto = "serial"
                if proto not in ("serial", "http", "mqtt", "udp", "i2c", "spi", "gpio"):
                    note = f"protocol '{ch.protocol}' not supported on boards; using serial JSON"
                    b.warnings.append(note)
                    proto = "serial"
                b.chans.append(Chan(ch, out, inn, proto, sb[1] if out else None, db[1] if inn else None, note))
        # pins
        for b in boards:
            sl = self._i2c_slave(b)
            if sl is not None:
                b.warnings.append(f"'{sl.ch.id}': this board is the I2C slave at {self._i2c_addr(sl.ch)} "
                                  f"(MicroPython main.py has no portable I2C slave: use the Arduino sketch)")
                if b.by_role("oled", "lcd") or any(x.transport == "i2c" and not x.local and x is not sl
                                                   for x in b.chans):
                    b.warnings.append("this board is an I2C slave AND drives other I2C parts: use a second bus "
                                      "or a serial channel instead")
            for x in b.chans:
                if x.transport == "spi" and not x.local and x.ch.src.domain == x.ch.dst.domain == "embedded"                         and x.inn:
                    b.warnings.append(f"'{x.ch.id}': board-to-board SPI needs an SPI slave sketch (not generated); "
                                      f"prefer i2c or serial between two boards")
            needs_i2c = bool(b.by_role("oled", "lcd")) or any(x.transport == "i2c" and not x.local for x in b.chans)
            needs_spi = any(x.transport == "spi" and not x.local for x in b.chans)
            self._assign_pins(b, needs_i2c, needs_spi)
            for x in b.chans:
                if x.local:
                    continue
                if x.transport == "spi":
                    x.pins["cs"] = b.spec["ss"] if b.spec["ss"] not in b.used else self._alloc(b, "digital")
                    b.used.add(x.pins["cs"])
                if x.transport == "gpio":
                    for f in x.ch.fields:
                        x.pins[f] = self._alloc(b, "digital")
            n = 0
            for c in b.comps:
                for bh in c.e.behaviors:
                    if isinstance(bh, dict) and bh.get("on") and isinstance(bh.get("do"), list):
                        b.behs.append(Beh(n, c, bh))
                        n += 1
        return boards

    # ======================================================================
    # Name resolution (shared by C++ and MicroPython)
    # ======================================================================
    def _ref(self, b: Board, owner: Comp | None, n: str) -> tuple[str, str, str]:
        """identifier -> (cpp code, python code, ctype)."""
        n = str(n)
        if "." in n:
            ent, prop = n.split(".", 1)
            ent, prop = ident(ent), ident(prop.replace(".", "_"))
            c = next((c for c in b.comps if c.e.ident == ent or c.var == ent), None)
            if prop.startswith(ent + "_") and (c is None or prop not in c.members):
                prop = prop[len(ent) + 1:]   # "visits.visits_count" -> visits.count
            elif c is not None and prop not in c.members:
                sp = self._owned_prefix(c, prop, b.comps)   # "knob.visits_count" -> visits.count
                if sp is not None:
                    m = self._member(sp[0], sp[1])
                    return f"{sp[0].var}.{m.name}", f"{sp[0].var}.{m.name}", m.ctype
            if c is not None:
                m = self._member(c, prop)
                return f"{c.var}.{m.name}", f"{c.var}.{m.name}", m.ctype
            return self._remote(b, ent, prop)
        n = ident(n)
        sp = self._split_owned(b, owner, n) if "_of_" in n else None
        if sp is not None:
            m = self._member(sp[0], sp[1])
            return f"{sp[0].var}.{m.name}", f"{sp[0].var}.{m.name}", m.ctype
        # another component's name wins over a same-named prop (the parser copies names it sees onto the owner)
        other = next((c for c in b.comps if c is not owner and c.role != "board" and n in (c.e.ident, c.var)), None)
        if owner is not None and n in owner.members and other is None:
            m = owner.members[n]
            return f"{owner.var}.{m.name}", f"{owner.var}.{m.name}", m.ctype
        for c in b.comps:
            if c.e.ident == n or c.var == n:
                f = PRIMARY.get(c.role) or next((k for k in c.members if k != "alive"), "alive")
                m = self._member(c, f)
                return f"{c.var}.{m.name}", f"{c.var}.{m.name}", m.ctype
        for c in sorted(b.comps, key=lambda c: c.role in ("generic", "board")):
            if n in c.members:
                m = c.members[n]
                return f"{c.var}.{m.name}", f"{c.var}.{m.name}", m.ctype
        for x in b.chans:
            if x.inn and not x.local and n in x.ch.fields:
                return self._remote(b, x.ch.src.ident, n)
        sp = self._split_owned(b, owner, n)
        if sp is not None:
            m = self._member(sp[0], sp[1])
            return f"{sp[0].var}.{m.name}", f"{sp[0].var}.{m.name}", m.ctype
        far = self._owned_prefix(owner, n, [c for bb, c in getattr(self, "_all_comps", []) if bb is not b])
        if far is not None:  # a part on another board: only reachable through a channel
            w = (f"'{far[0].e.name}.{far[1]}' lives on another board; connect {far[0].e.name} to this board "
                 f"to receive it (0 until then)")
            if w not in b.warnings:
                b.warnings.append(w)
            return self._remote(b, far[0].e.ident, far[1])
        target = owner or (b.comps[0] if b.comps else None)
        if target is None:
            return self._remote(b, "local", n)
        m = self._member(target, n)
        return f"{target.var}.{m.name}", f"{target.var}.{m.name}", m.ctype

    @staticmethod
    def _split_owned(b: Board, owner: Comp | None, n: str) -> tuple[Comp, str] | None:
        """'led1.on' / 'led1_on' (dots are flattened by norm_name) -> (led1 component, 'on')."""
        n = str(n)
        if "." in n:
            ent, prop = n.split(".", 1)
            ent, prop = ident(ent), ident(prop.replace(".", "_"))
            c = next((c for c in b.comps if c.e.ident == ent or c.var == ent), None)
            return (c, prop) if c is not None and prop else None
        n = ident(n)
        mo = re.fullmatch(r"(\w+?)_of_(?:the_)?(\w+)", n)  # "angle of servo" phrasing -> servo.angle
        if mo:
            c = next((c for c in b.comps if mo.group(2) in (c.e.ident, c.var)), None)
            if c is not None:
                return c, mo.group(1)
        if owner is not None and n in owner.members:
            return None
        return EmbeddedModule._owned_prefix(owner, n, b.comps)

    @staticmethod
    def _owned_prefix(owner: Comp | None, n: str, comps: list) -> tuple[Comp, str] | None:
        """'door_angle' -> (door, 'angle') when door is a component with that state/driver prop."""
        for c in sorted(comps, key=lambda c: -len(c.e.ident)):
            for pre in {c.e.ident, c.var}:
                if c is not owner and n.startswith(pre + "_") and len(n) > len(pre) + 1:
                    prop = n[len(pre) + 1:]
                    if EmbeddedModule._known_prop(c, prop):
                        return c, prop
        return None

    @staticmethod
    def _known_prop(c: Comp, prop: str) -> bool:
        return prop in c.members or prop == PRIMARY.get(c.role) or prop in DRIVER_PROPS.get(c.role, ())

    @staticmethod
    def _baud(b: Board) -> int:
        """Baud of the board's serial channels (the USB serial port is shared), default 115200."""
        for x in b.chans:
            if x.transport == "serial" and not x.local and x.ch.protocol in ("serial", "uart"):
                v = _int(x.ch.config.get("baud"), 115200)
                if v in (300, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 74880, 115200,
                         230400, 250000, 460800, 500000, 921600, 1000000, 2000000):
                    return v
        return 115200

    def _remote(self, b: Board, ent: str, prop: str) -> tuple[str, str, str]:
        key = f"{ent}.{prop}"
        var = b.remotes.setdefault(key, f"remote_{ident(ent)}_{ident(prop)}")
        return var, f"REMOTE.get({key!r}, 0)", "float"

    def _expr(self, b: Board, owner: Comp | None, e: Any, lang: str) -> str:
        i = 0 if lang == "cpp" else 1
        if isinstance(e, str):
            # "when the led is on" arrives as `led == on`: compare with the state words, not a prop called 'on'
            e = re.sub(r"(==|!=)\s*(on|high|pressed|open|detected|triggered|active|present)\b(?!\s*[.(])", r"\1 true", e)
            e = re.sub(r"(==|!=)\s*(off|low|released|closed|inactive|absent)\b(?!\s*[.(])", r"\1 false", e)
        try:
            return self.expr(e, "cpp" if lang == "cpp" else "python", lambda n: self._ref(b, owner, n)[i])
        except Exception:
            return "0"

    # ======================================================================
    # Behaviour actions -> (cpp lines, python lines)
    # ======================================================================
    def _action(self, b: Board, c: Comp, a: dict) -> tuple[list[str], list[str]]:
        op = a.get("op")
        v = c.var
        if op in ("set", "add"):
            raw = str(a.get("value", "0"))
            is_str = bool(re.fullmatch(r"\"[^\"]*\"|'[^']*'", raw.strip()))
            is_bool = raw.strip() in ("true", "false")
            hint = "String" if is_str else "bool" if is_bool and op == "set" else "float"
            prop = ident(a.get("prop", "value")) or "value"
            pre = c.e.ident + "_"
            if prop.startswith(pre) and self._known_prop(c, prop[len(pre):]):   # "increase visits count" -> visits.count
                prop = prop[len(pre):]
                a = dict(a, prop=prop)
            sp = self._split_owned(b, c, a.get("prop", "value"))
            if sp is not None:  # "set led1.on to ..." -> the other component on this board
                c, prop = sp
                v = c.var
            m = self._member(c, prop, hint)
            if is_str:
                lit = raw.strip()[1:-1]
                ce, pe = self._cstr(lit), repr(lit)
            else:
                ce, pe = self._expr(b, c, raw, "cpp"), self._expr(b, c, raw, "py")
            if m.ctype == "String":
                if not is_str:
                    ce = f"String({ce})"
                return ([f"{v}.{m.name} {'=' if op == 'set' else '+='} {ce};"],
                        [f"{v}.{m.name} {'=' if op == 'set' else '+='} {pe if is_str else f'str({pe})'}"])
            if is_str:
                note = f"cannot store text in numeric property '{prop}'"
                return [f"// {note}"], [f"pass  # {note}"]
            if (c.role, prop) in PWM_PROPS and op == "set":
                # "set lamp brightness to knob value": rescale a raw analog reading to the 0..255 PWM range
                src = self._analog_src(b, raw)
                if src is not None:
                    ce = f"(({ce}) * 255.0f / NLCL_ADC_MAX)"
                    pe = f"(({pe}) * 255 // 4095)"
                cl, pl = [f"{v}.{m.name} = {ce};"], [f"{v}.{m.name} = {pe}"]
                if c.role != "neopixel" and "on" in c.members:
                    # a brightness / speed above 0 means "running" (0 switches it off)
                    cl.append(f"{v}.on = {v}.{m.name} > 0;")
                    pl.append(f"{v}.on = {v}.{m.name} > 0")
                return cl, pl
            if op == "set":
                return [f"{v}.{m.name} = {ce};"], [f"{v}.{m.name} = {pe}"]
            return [f"{v}.{m.name} += {ce};"], [f"{v}.{m.name} += {pe}"]
        if op == "toggle":
            prop = ident(a.get("prop", "on")) or "on"
            sp = self._split_owned(b, c, a.get("prop", "on"))
            if sp is not None:
                c, prop = sp
                v = c.var
            if prop not in c.members:
                # "toggle the led" where led is another entity name
                tgt = next((x for x in b.comps if x.e.ident == prop or x.var == prop), None)
                if tgt is not None:
                    m = self._member(tgt, PRIMARY.get(tgt.role, "on"), "bool")
                    return [f"{tgt.var}.{m.name} = !{tgt.var}.{m.name};"], \
                        [f"{tgt.var}.{m.name} = not {tgt.var}.{m.name}"]
            m = self._member(c, prop, "bool")
            if m.ctype == "String":
                return [f"// cannot toggle text property '{prop}'"], [f"pass  # cannot toggle '{prop}'"]
            return [f"{v}.{m.name} = !{v}.{m.name};"], [f"{v}.{m.name} = not {v}.{m.name}"]
        if op == "destroy":
            return [f"{v}.alive = false;  // destroyed: drivers switch it off", "return;"], \
                [f"{v}.alive = False  # destroyed: drivers switch it off", "return"]
        if op == "log":
            return self._log(b, c, str(a.get("text", "")))
        if op == "move":
            cl, pl = [], []
            for ax in ("x", "y", "z"):
                val = str(a.get(ax, "0")).strip()
                if val in ("0", "0.0", "-0", ""):
                    continue
                prop = "angle" if (c.role == "servo" and ax == "x") else ax
                m = self._member(c, prop, "float")
                cl.append(f"{v}.{m.name} += {self._expr(b, c, val, 'cpp')};")
                pl.append(f"{v}.{m.name} += {self._expr(b, c, val, 'py')}")
            return (cl or ["// move: no displacement"]), (pl or ["pass"])
        if op == "send":
            return self._send(b, c, a.get("to"), list(a.get("fields") or []))
        if op == "call":
            return self._call(b, c, ident(a.get("name", "action")) or "action", list(a.get("args") or []))
        what = self._desc(a)
        return [f"// unsupported action: {what}"], [f"pass  # unsupported action: {what}"]

    @staticmethod
    def _desc(a: Any) -> str:
        """describe_action that never raises (hand-written IR may carry odd values)."""
        try:
            what = describe_action(a) if isinstance(a, dict) and "op" in a else str(a)
        except Exception:
            what = str(a.get("op", a) if isinstance(a, dict) else a)
        return " ".join(what.split()).rstrip("\\")

    def _analog_src(self, b: Board, raw: str) -> Comp | None:
        """The pot / light / analog component whose raw ADC reading `raw` is (else None)."""
        n = ident(str(raw).strip().replace(".", "_"))
        for c in b.comps:
            if c.role not in ("pot", "light", "analog"):
                continue
            f = next((k for k in c.members if k not in ("alive", "on")), "value")
            if f in PERCENT_FIELDS:
                continue
            if n in (c.e.ident, c.var, f"{c.e.ident}_{f}", f"{c.var}_{f}"):
                return c
        return None

    def _log(self, b: Board, c: Comp, text: str) -> tuple[list[str], list[str]]:
        mo = re.fullmatch(r"\s*(['\"])(.*)\1\s+on\s+(?:the\s+)?(\w+)\s*", text, re.S)
        scr = next((x for x in b.comps if mo and x.role in ("oled", "lcd") and mo.group(3) in (x.e.ident, x.var)), None)
        if scr is not None:
            # "show 'light {ldr.light}' on screen" -> the display's text, not a serial log
            parts = re.split(r"\{([A-Za-z_][\w.]*)\}", mo.group(2))
            ce, pe = [], []
            for i, p in enumerate(parts):
                if i % 2 == 0:
                    if p:
                        ce.append(f"String({self._cstr(p)})")
                        pe.append(repr(p))
                else:
                    rc, rp, t = self._ref(b, c, p)
                    ce.append(f"String(({rc}) ? \"true\" : \"false\")" if t == "bool" else f"String({rc})")
                    pe.append(f"str({rp})")
            tm = self._member(scr, "text", "String")
            return ([f"{scr.var}.{tm.name} = {' + '.join(ce) or 'String()'};"],
                    [f"{scr.var}.{tm.name} = {' + '.join(pe) or repr('')}"])
        parts = re.split(r"\{([A-Za-z_][\w.]*)\}", text)
        cl = ["{", "  String m;"]
        pp = []
        for i, p in enumerate(parts):
            if i % 2 == 0:
                if p:
                    cl.append(f"  m += {self._cstr(p)};")
                    pp.append(repr(p))
            else:
                ce, pe, t = self._ref(b, c, p)
                cl.append(f"  m += {ce} ? \"true\" : \"false\";" if t == "bool" else f"  m += String({ce});")
                if t == "bool":
                    cl[-1] = f"  m += ({ce}) ? \"true\" : \"false\";"
                pp.append(f"str({pe})")
        cl += [f"  nlclLog({self._cstr(c.e.name)}, m);", "}"]
        return cl, [f"nlcl_log({c.e.name!r}, {' + '.join(pp) or repr('')})"]

    def _send(self, b: Board, c: Comp, to: Any, fields: list[str]) -> tuple[list[str], list[str]]:
        outs = [x for x in b.chans if x.out]
        mine = [x for x in outs if x.src is c] or outs
        if to:
            t = ident(str(to))
            mine = [x for x in mine if t in (x.ch.dst.ident, ident(x.ch.dst.name), ident(x.ch.dst.kind), x.cid)] or mine
        if not mine:
            return [f"// send: {c.e.name} has no outgoing channel (connect it to something)"], \
                ["pass  # send: no outgoing channel"]
        cl, pl = [], []
        for x in mine:
            flds = [ident(f) for f in fields if ident(f)] or None
            if flds and x.transport in ("serial", "http", "mqtt", "udp") and not x.local:
                cl += ["{"] + ["  " + s for s in self._json_build_cpp(b, x, flds)] + [f"  nlclTx_{x.cid}(j.str());", "}"]
                pl.append(f"tx_{x.cid}({self._json_build_py(b, x, flds)})")
            else:
                cl.append(f"nlclPublish_{x.cid}();")
                pl.append(f"publish_{x.cid}()")
        return cl, pl

    def _call(self, b: Board, c: Comp, name: str, args: list) -> tuple[list[str], list[str]]:
        bz = c if c.role == "buzzer" else next(iter(b.by_role("buzzer")), None)
        if name in ("beep", "alarm", "ring", "buzz") or name.startswith("play"):
            if bz is not None:
                pin = bz.pins.get("pin", "0")
                f = self._member(bz, "frequency", "long", 1000)
                return [f"tone({self._pconst(bz)}, (unsigned int){bz.var}.{f.name}, 200);  // {pin}"], \
                    [f"beep(_hw_{bz.var}, {bz.var}.{f.name}, 200)"]
            return self._log(b, c, f"{name}!")
        if name in ("stop", "close", "open"):
            if c.role == "servo":
                m = self._member(c, "angle", "long")
                val = 90 if name == "open" else 0
                return [f"{c.var}.{m.name} = {val};"], [f"{c.var}.{m.name} = {val}"]
            if "on" in c.members:
                val = name == "open"
                return [f"{c.var}.on = {'true' if val else 'false'};"], [f"{c.var}.on = {val}"]
            return self._log(b, c, f"{name}")
        if name == "reset":
            cl, pl = [], []
            for m in c.members.values():
                cl.append(f"{c.var}.{m.name} = {self._clit(m)};")
                pl.append(f"{c.var}.{m.name} = {self._plit(m)}")
            return cl, pl
        argc = len(args)
        b.calls[name] = max(argc, b.calls.get(name, 0))
        ca = [self._expr(b, c, x, "cpp") for x in args]
        pa = [self._expr(b, c, x, "py") for x in args]
        n = b.calls[name]
        ca += ["0"] * (n - len(ca))
        return [f"user_{name}({', '.join(ca)});"], [f"user_{name}({', '.join(pa)})"]

    # ======================================================================
    # Channel helpers
    # ======================================================================
    def _field_ref(self, b: Board, x: Chan, f: str) -> tuple[str, str, str]:
        src = x.src
        f = ident(f) or "value"
        if src is not None and f in src.members:
            m = src.members[f]
            return f"{src.var}.{m.name}", f"{src.var}.{m.name}", m.ctype
        return self._ref(b, src, f)

    def _json_build_cpp(self, b: Board, x: Chan, fields: list[str]) -> list[str]:
        L = [f"NlclJson j({self._cstr(x.ch.id)}, {self._cstr(x.ch.src.name)});"]
        for f in fields:
            ce, _, t = self._field_ref(b, x, f)
            L.append(f"j.add({self._cstr(f)}, {ce});")
        return L

    def _json_build_py(self, b: Board, x: Chan, fields: list[str]) -> str:
        items = ", ".join(f"{f!r}: {self._field_ref(b, x, f)[1]}" for f in fields)
        return f"json.dumps({{'channel': {x.ch.id!r}, 'from': {x.ch.src.name!r}, {items}}})"

    def _pconst(self, c: Comp, logical: str = "pin") -> str:
        return f"PIN_{c.var.upper()}" + ("" if logical == "pin" else f"_{logical.upper()}")

    @staticmethod
    def _pyint(b: Board, p: str) -> str:
        """Pin as a MicroPython GPIO number."""
        if re.fullmatch(r"\d+", p):
            return p
        return "0"

    # ======================================================================
    # generate
    # ======================================================================
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        import traceback
        files: dict[str, str] = {}
        try:
            boards = self._plan(ctx)
        except Exception:  # never break the build of the other modules
            return {"README.md": "# embedded\n\nFirmware planning failed (please report):\n\n```\n"
                    + traceback.format_exc() + "```\n"}
        for b in boards:
            d = b.ident
            try:
                sec = self._sections(ctx, b)
                files[f"{d}/{d}.ino"] = self._ino(ctx, b, sec)
                files[f"{d}/platformio.ini"] = self._pio(b)
                if b.spec["upy"]:
                    files[f"{d}/main.py"] = self._upy(ctx, b, sec)
                if b.spec["wokwi"]:
                    files[f"{d}/diagram.json"] = self._diagram(b)
                    files[f"{d}/wokwi.toml"] = self._wokwi_toml(b)
                    files[f"{d}/libraries.txt"] = "\n".join(sorted(self._wokwi_libs(b))) + "\n"
            except Exception:
                files[f"{d}/GENERATION_ERROR.txt"] = "Generating this board failed (please report):\n\n" + \
                    traceback.format_exc()
        try:
            files["README.md"] = self._readme(ctx, boards)
        except Exception:
            files["README.md"] = "# embedded\n\nREADME generation failed:\n\n```\n" + traceback.format_exc() + "```\n"
        return files

    # ----------------------------------------------------------------------
    def _sections(self, ctx: BuildContext, b: Board) -> dict:
        """Generate behaviour/trigger code first (it may add members & remotes)."""
        S: dict[str, list[str]] = {k: [] for k in (
            "beh_c", "beh_p", "trig_c", "trig_p", "glob_c", "start_c", "start_p", "chg_init_c", "chg_init_p",
            "cmd_c", "cmd_p")}
        update_ms = 50
        for bh in b.behs:
            c, rule = bh.comp, bh.b
            desc = f"{c.e.name}: on {rule['on']}" + (f" if {rule['if']}" if rule.get("if") else "") + " -> " + \
                "; ".join(self._desc(a) for a in rule["do"] if isinstance(a, dict))
            desc = " ".join(desc.split()).rstrip("\\")  # one line; no trailing backslash (C++ line splice)
            S["beh_c"] += [f"// {desc}", f"void nlcl_b{bh.n}() {{", f"  if (!{c.var}.alive) return;"]
            S["beh_p"] += [f"def nlcl_b{bh.n}():", f"    # {desc}", f"    if not {c.var}.alive:", "        return"]
            cond = rule.get("if")
            if cond:
                ce, pe = self._expr(b, c, cond, "cpp"), self._expr(b, c, cond, "py")
                edge = bh.trig == "update" and any(a.get("op") in ("log", "send", "call", "toggle", "add", "destroy")
                                                    for a in rule["do"] if isinstance(a, dict))
                if edge:  # fire once when the condition becomes true (avoid flooding)
                    S["beh_c"] += ["  static bool was = false;", f"  bool cond = ({ce});",
                                   "  if (!cond) { was = false; return; }", "  if (was) return;", "  was = true;"]
                    S["beh_p"] += [f"    if not ({pe}):", f"        _ST['was{bh.n}'] = False", "        return",
                                   f"    if _ST.get('was{bh.n}'):", "        return", f"    _ST['was{bh.n}'] = True"]
                else:
                    S["beh_c"].append(f"  if (!({ce})) return;")
                    S["beh_p"] += [f"    if not ({pe}):", "        return"]
            for a in rule["do"]:
                if not isinstance(a, dict):
                    continue
                cl, pl = self._action(b, c, a)
                S["beh_c"] += ["  " + s for s in cl]
                S["beh_p"] += ["    " + s for s in pl]
            S["beh_c"] += ["}", ""]
            S["beh_p"] += [""]
        # ---- triggers
        groups: dict[str, list[Beh]] = {}
        for bh in b.behs:
            groups.setdefault(bh.trig, []).append(bh)
        for bh in groups.get("start", []):
            S["start_c"].append(f"nlcl_b{bh.n}();")
            S["start_p"].append(f"nlcl_b{bh.n}()")
        # input: buttons / switches / serial commands
        inputs: dict[str, list[Beh]] = {}
        for bh in groups.get("input", []):
            inputs.setdefault(ident(bh.arg) or "any", []).append(bh)
        for key, lst in inputs.items():
            calls_c = " ".join(f"nlcl_b{x.n}();" for x in lst)
            calls_p = "; ".join(f"nlcl_b{x.n}()" for x in lst)
            src = next((c for c in b.comps if c.role in ("button", "switch", "pir")
                        and (c.e.ident == key or c.var == key)), None) or \
                next((c for c in b.comps if c.role in ("button", "switch", "pir")
                      and key in (ident(c.e.kind), c.role, ident(str(c.e.props.get("subtype", ""))))), None)
            if key == "any" and src is None:
                src = next(iter(b.by_role("button")), None)
            if src is not None:
                f = PRIMARY[src.role]
                S["glob_c"] += [f"bool nlclPrev_{src.var}_{key} = false;", f"unsigned long nlclDeb_{src.var}_{key} = 0;"]
                S["trig_c"] += [f"  // input:{key} -> rising edge of {src.e.name}.{f} (debounced)",
                                f"  if ({src.var}.{f} && !nlclPrev_{src.var}_{key} && now - nlclDeb_{src.var}_{key} > 40) "
                                f"{{ nlclDeb_{src.var}_{key} = now; {calls_c} }}",
                                f"  nlclPrev_{src.var}_{key} = {src.var}.{f};"]
                S["trig_p"] += [f"    # input:{key} -> rising edge of {src.e.name}.{f}",
                                f"    if {src.var}.{f} and not _ST.get('prev_{src.var}_{key}') "
                                f"and time.ticks_diff(now, _ST.get('deb_{src.var}_{key}', 0)) > 40:",
                                f"        _ST['deb_{src.var}_{key}'] = now",
                                f"        {calls_p}",
                                f"    _ST['prev_{src.var}_{key}'] = {src.var}.{f}"]
            # also reachable as a typed serial command (e.g. "space" or "button")
            words = {key, key.replace("_", " ")}
            if key == "space":
                words.add(" ")
            cond_c = " || ".join(f"cmd.equalsIgnoreCase({self._cstr(w)})" for w in sorted(words))
            S["cmd_c"].append(f"  if ({cond_c}) {{ {calls_c} return; }}")
            S["cmd_p"] += [f"    if cmd.lower() in {sorted(words)!r}:", f"        {calls_p}", "        return"]
        for i, bh in enumerate(groups.get("timer", [])):
            try:
                ms = max(1, int(bh.arg))
            except ValueError:
                ms = 1000
            S["glob_c"].append(f"unsigned long nlclT_{bh.n} = 0;")
            S["trig_c"] += [f"  if (now - nlclT_{bh.n} >= {ms}UL) {{ nlclT_{bh.n} = now; nlcl_b{bh.n}(); }}  // every {ms} ms"]
            S["trig_p"] += [f"    if time.ticks_diff(now, _ST.get('t{bh.n}', 0)) >= {ms}:  # every {ms} ms",
                            f"        _ST['t{bh.n}'] = now", f"        nlcl_b{bh.n}()"]
        if groups.get("update"):
            S["glob_c"].append("unsigned long nlclT_update = 0;")
            S["trig_c"] += [f"  if (now - nlclT_update >= {update_ms}UL) {{  // update behaviours ({1000 // update_ms} Hz)",
                            "    nlclT_update = now;"] + [f"    nlcl_b{x.n}();" for x in groups["update"]] + ["  }"]
            S["trig_p"] += [f"    if time.ticks_diff(now, _ST.get('tu', 0)) >= {update_ms}:", "        _ST['tu'] = now"] + \
                [f"        nlcl_b{x.n}()" for x in groups["update"]]
        for bh in groups.get("change", []):
            prop = str(bh.arg or "").strip() if "." in str(bh.arg or "") else (ident(bh.arg) or "value")
            ce, pe, t = self._ref(b, bh.comp, prop)
            S["glob_c"].append(f"{t} nlclChg_{bh.n};")
            S["chg_init_c"].append(f"nlclChg_{bh.n} = {ce};")
            S["chg_init_p"].append(f"_ST['chg{bh.n}'] = {pe}")
            S["trig_c"] += [f"  if ({ce} != nlclChg_{bh.n}) {{ nlclChg_{bh.n} = {ce}; nlcl_b{bh.n}(); }}  // change:{prop}"]
            S["trig_p"] += [f"    if {pe} != _ST.get('chg{bh.n}'):  # change:{prop}",
                            f"        _ST['chg{bh.n}'] = {pe}", f"        nlcl_b{bh.n}()"]
        if groups.get("collision"):
            ds = b.by_role("distance")
            calls_c = " ".join(f"nlcl_b{x.n}();" for x in groups["collision"])
            calls_p = "; ".join(f"nlcl_b{x.n}()" for x in groups["collision"])
            if ds:
                conds_c, conds_p = [], []
                for dc in ds:
                    lim = dc.e.props.get("collision_cm", 10)
                    lim = lim if isinstance(lim, (int, float)) and not isinstance(lim, bool) else 10
                    conds_c.append(f"({dc.var}.distance > 0 && {dc.var}.distance < {float(lim)})")
                    conds_p.append(f"(0 < {dc.var}.distance < {float(lim)})")
                S["glob_c"].append("bool nlclPrevCollision = false;")
                S["trig_c"] += ["  { // collision = an ultrasonic sensor sees an obstacle closer than collision_cm",
                                f"    bool hit = {' || '.join(conds_c)};",
                                f"    if (hit && !nlclPrevCollision) {{ {calls_c} }}",
                                "    nlclPrevCollision = hit;", "  }"]
                S["trig_p"] += [f"    hit = {' or '.join(conds_p)}",
                                "    if hit and not _ST.get('col'):", f"        {calls_p}", "    _ST['col'] = hit"]
            else:
                S["trig_c"].append("  // collision behaviours need a distance (ultrasonic) sensor on this board")
                S["trig_p"].append("    # collision behaviours need a distance (ultrasonic) sensor on this board")
        return S

    # ======================================================================
    # Arduino C++
    # ======================================================================
    def _ino(self, ctx: BuildContext, b: Board, S: dict) -> str:
        sp = b.spec
        arch = sp["arch"]
        roles = {c.role for c in b.comps}
        net = [x for x in b.chans if not x.local and x.transport in ("http", "mqtt", "udp")]
        uses_wifi = bool(net) and bool(b.wifi)
        uses_mqtt = any(x.transport == "mqtt" for x in net)
        uses_http = any(x.transport == "http" for x in net)
        uses_udp = any(x.transport == "udp" for x in net)
        uses_wire = bool(roles & {"oled", "lcd"}) or any(x.transport == "i2c" and not x.local for x in b.chans)
        uses_spi = any(x.transport == "spi" and not x.local for x in b.chans)
        bus_chans = [x for x in b.chans if not x.local and x.transport in ("i2c", "spi")]
        L: list[str] = []
        A = L.append
        A(f"// {ctx.system.name} - firmware for {sp['title']} ('{b.ident}')")
        A("// Generated by NLCL (Natural Language Computational Language). Edit freely.")
        A("//")
        A(f"//   arduino-cli compile --fqbn {sp['fqbn']} .")
        A("//   pio run            (platformio.ini next to this file)")
        if sp["wokwi"]:
            A("//   wokwi.com          paste this sketch + diagram.json + libraries.txt, press Play")
        A("//")
        A(f"// Serial ({self._baud(b)} baud): lines starting with '{{' are channel JSON, '#' lines are logs,")
        A("// any other line you type is a command that fires input:<command> behaviours.")
        if b.warnings:
            A("//")
            A("// NOTES:")
            for w in dict.fromkeys(b.warnings):
                A(f"//   - {w}")
        A("")
        A("#include <Arduino.h>")
        A("#include <math.h>")
        # ---- optional libraries
        opt = []
        if roles & {"dht"}:
            opt.append(("NLCL_USE_DHT", "DHT sensor library (Adafruit) - DHT11/DHT22", ["<DHT.h>"]))
            b.libs.add("DHT sensor library")
        if "servo" in roles:
            if arch == "esp32":
                opt.append(("NLCL_USE_SERVO", "ESP32Servo (Kevin Harrington)", ["<ESP32Servo.h>"]))
                b.libs.add("ESP32Servo")
            else:
                opt.append(("NLCL_USE_SERVO", "Servo (Arduino; bundled with most cores)", ["<Servo.h>"]))
                if arch in ("avr", "stm32"):
                    b.libs.add("Servo")
        if "neopixel" in roles:
            opt.append(("NLCL_USE_NEOPIXEL", "Adafruit NeoPixel", ["<Adafruit_NeoPixel.h>"]))
            b.libs.add("Adafruit NeoPixel")
        if "oled" in roles:
            opt.append(("NLCL_USE_OLED", "Adafruit SSD1306 + Adafruit GFX Library",
                        ["<Adafruit_GFX.h>", "<Adafruit_SSD1306.h>"]))
            b.libs.update({"Adafruit SSD1306", "Adafruit GFX Library"})
        if "lcd" in roles:
            opt.append(("NLCL_USE_LCD", "LiquidCrystal I2C (Frank de Brabander)", ["<LiquidCrystal_I2C.h>"]))
            b.libs.add("LiquidCrystal I2C")
        if uses_mqtt:
            opt.append(("NLCL_USE_MQTT", "PubSubClient (Nick O'Leary)", ["<PubSubClient.h>"]))
            b.libs.add("PubSubClient")
        if opt:
            A("")
            A("// ---- optional libraries: install them (see README / libraries.txt) or set the flag")
            A("// ---- to 0 to build with only the core (the part is then simulated / disabled).")
            for flag, lib, incs in opt:
                A(f"#define {flag} 1   // {lib}")
            for flag, lib, incs in opt:
                A(f"#if {flag}")
                for inc in incs:
                    A(f"#include {inc}")
                A("#endif")
        if uses_wire:
            A("#include <Wire.h>")
        if uses_spi:
            A("#include <SPI.h>")
        if uses_wifi:
            if b.wifi == "esp8266":
                A("#include <ESP8266WiFi.h>")
                if uses_http:
                    A("#include <ESP8266HTTPClient.h>")
            else:
                A("#include <WiFi.h>")
                if uses_http:
                    A("#include <HTTPClient.h>")
            if uses_udp:
                A("#include <WiFiUdp.h>")
            ssid = b.entity.props.get("wifi_ssid") if b.entity else None
            pw = b.entity.props.get("wifi_password") if b.entity else None
            host = b.entity.props.get("hub_host") if b.entity else None
            hub_port = 8765
            for x in net:
                if x.transport == "http":
                    try:
                        hub_port = int(str(x.ch.hub_url).rsplit(":", 1)[1].split("/")[0])
                    except (ValueError, IndexError):
                        pass
            A("")
            A("// ---- network (Wokwi simulator: SSID \"Wokwi-GUEST\", empty password)")
            A(f"#define WIFI_SSID {self._cstr(ssid or 'Wokwi-GUEST')}")
            A(f"#define WIFI_PASS {self._cstr(pw or '')}")
            A("// HUB_HOST = the PC running the NLCL hub (`python main.py` in the python folder).")
            A("// 'localhost' would be the board itself!  Real hardware: your PC's LAN IP (ipconfig / ip addr).")
            A("// Wokwi + private IoT gateway (VS Code): \"host.wokwi.internal\" reaches your PC.")
            A(f"#define HUB_HOST {self._cstr(host or 'host.wokwi.internal')}")
            A(f"#define HUB_PORT {hub_port}")
        A("")
        A(f"#define NLCL_ADC_MAX {sp['adc_max']}.0f   // full-scale analogRead() value on this board")
        # ---- pins
        A("")
        A("// ---- pins")
        for c in b.comps:
            for logical, p in c.pins.items():
                how = "set by you" if logical in c.explicit else "auto"
                A(f"const int {self._pconst(c, logical)} = {p};   // {c.e.name} ({c.role}{'' if logical == 'pin' else ' ' + logical}, {how})")
        for x in b.chans:
            if x.transport == "spi" and "cs" in x.pins:
                A(f"const int PIN_CS_{x.cid.upper()} = {x.pins['cs']};   // SPI chip-select for channel {x.ch.id}")
            if x.transport == "gpio":
                for f, p in x.pins.items():
                    A(f"const int PIN_{x.cid.upper()}_{ident(f).upper()} = {p};   // gpio channel {x.ch.id}: field {f}")
        if uses_wire:
            A(f"// I2C bus: SDA={sp['i2c'][0]} SCL={sp['i2c'][1]}")
        if uses_spi:
            A(f"// SPI bus: SCK={sp['spi'][0]} MISO={sp['spi'][1]} MOSI={sp['spi'][2]}")
        # ---- library objects
        lib_lines = []
        for c in b.comps:
            if c.role == "dht":
                model = "DHT11" if "dht11" in self._words(c.e) else "DHT22"
                lib_lines += ["#if NLCL_USE_DHT", f"DHT nlclDht_{c.var}({self._pconst(c)}, {model});", "#endif"]
            elif c.role == "servo":
                lib_lines += ["#if NLCL_USE_SERVO", f"Servo nlclServo_{c.var};", "#endif"]
            elif c.role == "neopixel":
                n = c.e.props.get("pixels", 1)
                n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
                lib_lines += ["#if NLCL_USE_NEOPIXEL",
                              f"Adafruit_NeoPixel nlclPix_{c.var}({n}, {self._pconst(c)}, NEO_GRB + NEO_KHZ800);", "#endif"]
            elif c.role == "oled":
                lib_lines += ["#if NLCL_USE_OLED", f"Adafruit_SSD1306 nlclOled_{c.var}(128, 64, &Wire, -1);", "#endif"]
            elif c.role == "lcd":
                lib_lines += ["#if NLCL_USE_LCD", f"LiquidCrystal_I2C nlclLcd_{c.var}(0x27, 16, 2);", "#endif"]
        if uses_mqtt:
            lib_lines += ["#if NLCL_USE_MQTT", "WiFiClient nlclMqttNet;", "PubSubClient nlclMqtt(nlclMqttNet);", "#endif"]
        if uses_udp:
            lib_lines += ["WiFiUDP nlclUdp;"]
        if lib_lines:
            A("")
            A("// ---- driver objects")
            L += lib_lines
        # channels/transport code is produced before struct emission (may add members)
        chan_c = self._chan_cpp(b)
        # ---- entity state
        A("")
        A("// ---- entity state (one struct per NLCL entity; JSON field names = member names)")
        for c in b.comps:
            mem = " ".join(f"{m.ctype} {m.name} = {self._clit(m)};" for m in c.members.values())
            A(f"struct NlclS_{c.var} {{ {mem} }} {c.var};   // {c.e.kind} '{c.e.name}'")
        if b.remotes:
            A("")
            A("// latest values received from other modules (\"entity.field\")")
            for key, var in sorted(b.remotes.items()):
                A(f"float {var} = 0;   // {key}")
        # ---- helpers
        L += self._cpp_helpers(b, uses_wifi, uses_http, uses_mqtt, uses_udp)
        # ---- prototypes
        A("")
        A("// ---- prototypes")
        for bh in b.behs:
            A(f"void nlcl_b{bh.n}();")
        for name, n in sorted(b.calls.items()):
            A(f"void user_{name}({', '.join('float a' + str(i) for i in range(n))});")
        for x in b.chans:
            if x.out:
                A(f"void nlclPublish_{x.cid}();")
            if x.inn and not x.local:
                A(f"void nlclRecv_{x.cid}(const String& msg);")
        A("void nlclCommand(const String& cmd);")
        # ---- globals for triggers
        if S["glob_c"]:
            A("")
            A("// ---- trigger state")
            L += S["glob_c"]
        L += chan_c
        # ---- behaviours
        A("")
        A("// ======================================================================")
        A("// behaviours")
        A("// ======================================================================")
        L += S["beh_c"]
        for name, n in sorted(b.calls.items()):
            args = ", ".join("float a" + str(i) for i in range(n))
            A(f"// '{name}' was requested by a behaviour: put your own code here.")
            A(f"void user_{name}({args}) {{")
            for i in range(n):
                A(f"  (void)a{i};")
            A(f"  nlclLog(\"call\", \"{name}()\");")
            A("}")
            A("")
        A("// typed serial commands -> input:<command> behaviours")
        A("void nlclCommand(const String& cmd) {")
        L += S["cmd_c"] or ["  (void)cmd;"]
        A("}")
        # ---- inputs / outputs
        L += self._io_cpp(b)
        # ---- triggers
        A("")
        A("void nlclTriggers(unsigned long now) {")
        L += S["trig_c"] or ["  (void)now;"]
        A("}")
        # ---- setup / loop
        A("")
        A("void setup() {")
        A(f"  Serial.begin({self._baud(b)});")
        if arch == "rp2040":
            A("  { unsigned long t0 = millis(); while (!Serial && millis() - t0 < 1500) delay(10); }  // USB serial")
        A(f"  Serial.println(\"# {b.ident}: NLCL firmware for {sp['title']} starting\");")
        L += ["  " + s for s in self._setup_cpp(b, uses_wire, uses_spi, uses_wifi, uses_mqtt, uses_udp)]
        if S["start_c"]:
            A("  // start behaviours")
            L += ["  " + s for s in S["start_c"]]
        L += ["  " + s for s in S["chg_init_c"]]
        A("}")
        A("")
        A("void loop() {")
        A("  unsigned long now = millis();")
        A("  nlclReadInputs(now);     // sensors, buttons")
        A("  nlclPollSerial();        // serial JSON channels + typed commands")
        A("  nlclComms(now);          // network / bus channels: receive + periodic publish")
        A("  nlclTriggers(now);       // behaviours")
        A("  nlclWriteOutputs(now);   // leds, motors, servos, displays ...")
        A("}")
        return "\n".join(L) + "\n"

    def _cpp_helpers(self, b: Board, wifi: bool, http: bool, mqtt: bool, udp: bool) -> list[str]:
        L = ["", "// ---- helpers", "String nlclLastLog;",
             "void nlclLog(const char* who, const String& msg) {",
             "  nlclLastLog = msg;",
             "  Serial.print(\"# \"); Serial.print(who); Serial.print(\": \"); Serial.println(msg);",
             "}",
             "",
             "// Flat JSON builder: {\"channel\":\"id\",\"from\":\"name\",\"field\":value,...}",
             "struct NlclJson {",
             "  String s;",
             "  NlclJson(const char* ch, const char* from) {",
             "    s = \"{\\\"channel\\\":\\\"\"; s += ch; s += \"\\\",\\\"from\\\":\\\"\"; s += from; s += \"\\\"\";",
             "  }",
             "  void key(const char* k) { s += \",\\\"\"; s += k; s += \"\\\":\"; }",
             "  void add(const char* k, bool v) { key(k); s += v ? \"true\" : \"false\"; }",
             "  void add(const char* k, int v) { key(k); s += String(v); }",
             "  void add(const char* k, long v) { key(k); s += String(v); }",
             "  void add(const char* k, float v) { key(k); if (isnan(v) || isinf(v)) s += \"null\"; else s += String(v, 3); }",
             "  void add(const char* k, const String& v) {",
             "    key(k); s += '\"';",
             "    for (unsigned i = 0; i < v.length(); i++) { char c = v[i]; if (c == '\"' || c == '\\\\') s += '\\\\'; s += c; }",
             "    s += '\"';",
             "  }",
             "  String str() const { return s + \"}\"; }",
             "};",
             "",
             "// Minimal flat-JSON reader (no ArduinoJson needed): raw value of \"key\" (strings unquoted).",
             "bool nlclJsonGet(const String& m, const char* key, String& out) {",
             "  String k = String(\"\\\"\") + key + \"\\\"\";",
             "  int i = m.indexOf(k);",
             "  while (i >= 0) {",
             "    int j = i + k.length();",
             "    int n = m.length();",
             "    while (j < n && m[j] == ' ') j++;",
             "    if (j < n && m[j] == ':') {",
             "      j++;",
             "      while (j < n && m[j] == ' ') j++;",
             "      if (j < n && m[j] == '\"') {",
             "        out = \"\";",
             "        for (int e = j + 1; e < n && m[e] != '\"'; e++) { if (m[e] == '\\\\' && e + 1 < n) e++; out += m[e]; }",
             "        return true;",
             "      }",
             "      int e = j;",
             "      while (e < n && m[e] != ',' && m[e] != '}') e++;",
             "      out = m.substring(j, e); out.trim();",
             "      return true;",
             "    }",
             "    i = m.indexOf(k, i + 1);",
             "  }",
             "  return false;",
             "}",
             "float nlclNum(const String& v) { if (v == \"true\") return 1; if (v == \"false\" || v == \"null\") return 0; return v.toFloat(); }",
             "bool nlclBool(const String& v) { return v == \"true\" || (v != \"false\" && v.toFloat() != 0); }",
             "int16_t nlclI16(float v) { if (v > 32767) v = 32767; if (v < -32768) v = -32768; return (int16_t)(v >= 0 ? v + 0.5f : v - 0.5f); }",
             ]
        if b.by_role("neopixel"):
            L += ["",
                  "// colour name or #rrggbb -> 0xRRGGBB",
                  "uint32_t nlclColor(const String& c) {",
                  "  if (c.startsWith(\"#\") && c.length() == 7) return (uint32_t)strtol(c.c_str() + 1, NULL, 16);"]
            for nm, (r, g, bb) in COLORS.items():
                L.append(f"  if (c.equalsIgnoreCase(\"{nm}\")) return 0x{r:02X}{g:02X}{bb:02X}UL;")
            L += ["  return 0xFFFFFFUL;", "}"]
        if wifi:
            L += ["",
                  "// WiFi: never blocks for long; reconnects in the background.",
                  "bool nlclWifiUp() {",
                  "  static unsigned long last = 0;",
                  "  if (WiFi.status() == WL_CONNECTED) return true;",
                  "  if (last != 0 && millis() - last < 10000UL) return false;",
                  "  last = millis();",
                  "  WiFi.begin(WIFI_SSID, WIFI_PASS);",
                  "  return false;",
                  "}"]
        if http:
            L += ["",
                  "// NLCL hub: POST <hub>/channel/<id> stores the latest message, GET returns it (with \"_t\").",
                  "String nlclUrl(const char* path) { return String(\"http://\") + HUB_HOST + \":\" + String(HUB_PORT) + path; }",
                  "void nlclHttpPost(const char* path, const String& body) {",
                  "  if (WiFi.status() != WL_CONNECTED) return;",
                  "  WiFiClient client;",
                  "  HTTPClient http;",
                  "  http.setTimeout(2000);",
                  "#if defined(ESP32)",
                  "  http.setConnectTimeout(1500);",
                  "#endif",
                  "  if (!http.begin(client, nlclUrl(path))) return;",
                  "  http.addHeader(\"Content-Type\", \"application/json\");",
                  "  http.POST(body);",
                  "  http.end();",
                  "}",
                  "String nlclHttpGet(const char* path) {",
                  "  if (WiFi.status() != WL_CONNECTED) return String();",
                  "  WiFiClient client;",
                  "  HTTPClient http;",
                  "  http.setTimeout(2000);",
                  "#if defined(ESP32)",
                  "  http.setConnectTimeout(1500);",
                  "#endif",
                  "  if (!http.begin(client, nlclUrl(path))) return String();",
                  "  int code = http.GET();",
                  "  String r = code == 200 ? http.getString() : String();",
                  "  http.end();",
                  "  return r;",
                  "}"]
        return L

    def _chan_cpp(self, b: Board) -> list[str]:
        L = ["", "// ======================================================================",
             "// channels", "// ======================================================================"]
        if not b.chans:
            L.append("// (no channels)")
        mqtt_in = [x for x in b.chans if x.inn and not x.local and x.transport == "mqtt"]
        for x in b.chans:
            ch = x.ch
            L.append(f"// {ch.id}: {ch.src.name} ({ch.src.domain}) -> {ch.dst.name} ({ch.dst.domain}) via "
                     f"{x.transport}{' (local)' if x.local else ''}; fields: {', '.join(ch.fields)}")
            if x.note:
                L.append(f"// NOTE: {x.note}")
            if x.out and x.local:
                L.append(f"void nlclPublish_{x.cid}() {{  // both ends on this board: copy directly")
                for f in ch.fields:
                    ce, _, t = self._field_ref(b, x, f)
                    if x.dst is not None:
                        dm = self._member(x.dst, f, t)
                        if ce == f"{x.dst.var}.{dm.name}":
                            L.append(f"  // {f}: '{ch.src.name}' has no such property (nothing to copy)")
                        else:
                            L.append(f"  {x.dst.var}.{dm.name} = {ce};")
                L += [f"  {s}" for s in self._msg_calls_cpp(b, x)]
                L.append("}")
                continue
            if x.out:
                if x.transport in ("serial", "http", "mqtt", "udp"):
                    L.append(f"void nlclTx_{x.cid}(const String& line) {{")
                    if x.transport == "serial":
                        L.append("  Serial.println(line);")
                    elif x.transport == "http":
                        L.append(f"  nlclHttpPost({self._cstr(ch.config.get('path', '/channel/' + ch.id))}, line);")
                    elif x.transport == "mqtt":
                        L += ["#if NLCL_USE_MQTT",
                              f"  if (nlclMqtt.connected()) nlclMqtt.publish({self._cstr(ch.topic)}, line.c_str());",
                              "#else", "  Serial.println(line);  // MQTT library disabled: fall back to serial", "#endif"]
                    elif x.transport == "udp":
                        L += ["  if (WiFi.status() != WL_CONNECTED) return;",
                              f"  nlclUdp.beginPacket(HUB_HOST, {_int(ch.config.get('port'), 9876)});",
                              "  nlclUdp.print(line);", "  nlclUdp.endPacket();"]
                    L.append("}")
                    L.append(f"void nlclPublish_{x.cid}() {{")
                    L += ["  " + s for s in self._json_build_cpp(b, x, ch.fields)]
                    L += [f"  nlclTx_{x.cid}(j.str());", "}"]
                elif x.transport in ("i2c", "spi"):
                    n = len(ch.fields)
                    L.append(f"// byte frame: 0x{FRAME_SYNC:02X}, {n}, then {n} x int16 big-endian "
                             f"({', '.join(ch.fields)}; rounded, true=1)")
                    L.append(f"void nlclPublish_{x.cid}() {{")
                    vals = ", ".join(f"nlclI16({self._num(self._field_ref(b, x, f))})" for f in ch.fields)
                    L.append(f"  int16_t v[{n}] = {{{vals}}};")
                    if x.transport == "i2c":
                        L += [f"  Wire.beginTransmission({self._i2c_addr(ch)});",
                              f"  Wire.write((uint8_t)0x{FRAME_SYNC:02X});", f"  Wire.write((uint8_t){n});",
                              f"  for (int i = 0; i < {n}; i++) {{ Wire.write((uint8_t)((uint16_t)v[i] >> 8)); "
                              "Wire.write((uint8_t)(v[i] & 0xFF)); }",
                              "  Wire.endTransmission();"]
                    else:
                        L += ["  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));",
                              f"  digitalWrite(PIN_CS_{x.cid.upper()}, LOW);",
                              f"  SPI.transfer((uint8_t)0x{FRAME_SYNC:02X});", f"  SPI.transfer((uint8_t){n});",
                              f"  for (int i = 0; i < {n}; i++) {{ SPI.transfer((uint8_t)((uint16_t)v[i] >> 8)); "
                              "SPI.transfer((uint8_t)(v[i] & 0xFF)); }",
                              f"  digitalWrite(PIN_CS_{x.cid.upper()}, HIGH);",
                              "  SPI.endTransaction();"]
                    L.append("}")
                elif x.transport == "gpio":
                    L.append(f"void nlclPublish_{x.cid}() {{  // one pin per field: HIGH when value != 0")
                    for f in ch.fields:
                        L.append(f"  digitalWrite(PIN_{x.cid.upper()}_{ident(f).upper()}, "
                                 f"({self._num(self._field_ref(b, x, f))}) != 0 ? HIGH : LOW);")
                    L.append("}")
            if x.inn:
                L += self._recv_cpp(b, x)
        if mqtt_in:
            L += ["#if NLCL_USE_MQTT",
                  "void nlclMqttCallback(char* topic, byte* payload, unsigned int len) {",
                  "  String msg; msg.reserve(len);",
                  "  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];",
                  "  String t(topic);"]
            for x in mqtt_in:
                L.append(f"  if (t == {self._cstr(x.ch.topic)}) nlclRecv_{x.cid}(msg);")
            L += ["}", "#endif"]
        # serial line dispatcher
        ser_in = [x for x in b.chans if x.inn and not x.local and x.transport == "serial"]
        L += ["", "// serial: newline-terminated JSON for channels, anything else is a command",
              "String nlclSerialBuf;",
              "void nlclHandleLine(String line) {",
              "  line.trim();",
              "  if (line.length() == 0) return;",
              "  String ch;",
              "  if (line[0] == '{' && nlclJsonGet(line, \"channel\", ch)) {"]
        for x in ser_in:
            L.append(f"    if (ch == {self._cstr(x.ch.id)}) nlclRecv_{x.cid}(line);")
        L += ["    return;", "  }", "  nlclCommand(line);", "}",
              "void nlclPollSerial() {",
              "  while (Serial.available() > 0) {",
              "    char c = (char)Serial.read();",
              "    if (c == '\\n' || c == '\\r') { if (nlclSerialBuf.length()) nlclHandleLine(nlclSerialBuf); nlclSerialBuf = \"\"; }",
              "    else if (nlclSerialBuf.length() < 300) nlclSerialBuf += c;",
              "  }",
              "}"]
        # comms (polling + periodic publish)
        L += ["", "void nlclComms(unsigned long now) {"]
        body = []
        if any(not x.local and x.transport in ("http", "mqtt", "udp") for x in b.chans) and b.wifi:
            body.append("bool up = nlclWifiUp();")
            if any(x.transport == "mqtt" and not x.local for x in b.chans):
                body += ["#if NLCL_USE_MQTT",
                         "static unsigned long lastTry = 0;",
                         "if (up && !nlclMqtt.connected() && (lastTry == 0 || now - lastTry > 5000UL)) {",
                         "  lastTry = now;",
                         "  String id = String(\"nlcl-\") + String((unsigned long)random(0xFFFFFF), HEX);",
                         "  if (nlclMqtt.connect(id.c_str())) {"]
                for x in mqtt_in:
                    body.append(f"    nlclMqtt.subscribe({self._cstr(x.ch.topic)});")
                body += ["  }", "}", "if (up) nlclMqtt.loop();", "#endif"]
            if any(x.transport == "udp" and x.inn and not x.local for x in b.chans):
                body += ["if (up) {",
                         "  int n = nlclUdp.parsePacket();",
                         "  if (n > 0) {",
                         "    char buf[300]; int r = nlclUdp.read(buf, sizeof(buf) - 1); if (r < 0) r = 0; buf[r] = 0;",
                         "    String m(buf); String ch;",
                         "    if (nlclJsonGet(m, \"channel\", ch)) {"]
                for x in b.chans:
                    if x.transport == "udp" and x.inn and not x.local:
                        body.append(f"      if (ch == {self._cstr(x.ch.id)}) nlclRecv_{x.cid}(m);")
                body += ["    }", "  }", "}"]
            body.append("(void)up;")
        for x in b.chans:
            iv = max(1, _int(x.ch.config.get("interval_ms"), 1000))
            if x.inn and not x.local and x is self._i2c_slave(b):
                body += [f"nlclPoll_{x.cid}();  // I2C slave: handle data received in the interrupt"]
            elif x.inn and not x.local and x.transport in ("http", "i2c", "spi", "gpio"):
                body += [f"{{ static unsigned long t = 0; if (now - t >= {iv}UL) {{ t = now; nlclPoll_{x.cid}(); }} }}"]
            if x.out:
                body += [f"{{ static unsigned long t = 0; if (now - t >= {iv}UL) {{ t = now; nlclPublish_{x.cid}(); }} }}"
                         f"  // {x.ch.id} every {iv} ms"]
        L += ["  " + s if not s.startswith("#") else s for s in body] or ["  (void)now;"]
        if not body:
            pass
        L.append("  (void)now;")
        L.append("}")
        return L

    @staticmethod
    def _num(ref: tuple[str, str, str]) -> str:
        code, _, t = ref
        return "0" if t == "String" else f"(float)({code})"

    @staticmethod
    def _i2c_slave(b: Board):
        """Inbound I2C channel from another board: this board must be the I2C *slave* (both masters can't talk)."""
        return next((x for x in b.chans if x.inn and not x.out and not x.local and x.transport == "i2c"
                     and x.ch.src.domain == "embedded"), None)

    @staticmethod
    def _i2c_addr(ch: Channel) -> str:
        a = ch.config.get("address", "0x42")
        try:
            v = int(str(a), 0)
        except ValueError:
            v = 0x42
        return f"0x{max(8, min(0x77, v)):02X}"

    def _msg_calls_cpp(self, b: Board, x: Chan) -> list[str]:
        out = []
        for bh in b.behs:
            if bh.trig != "message":
                continue
            arg = ident(bh.arg)
            if arg in (x.ch.src.ident, x.cid, ident(x.ch.src.kind)) or (arg == "any" and x.dst is bh.comp):
                out.append(f"nlcl_b{bh.n}();  // message:{bh.arg}")
        return out

    def _recv_cpp(self, b: Board, x: Chan) -> list[str]:
        ch = x.ch
        L = []
        if x.transport in ("serial", "http", "mqtt", "udp"):
            L += [f"void nlclRecv_{x.cid}(const String& msg) {{", "  String v;"]
            for f in ch.fields:
                key = f"{ch.src.ident}.{ident(f)}"
                var = b.remotes.setdefault(key, f"remote_{ch.src.ident}_{ident(f)}")
                line = f"  if (nlclJsonGet(msg, {self._cstr(f)}, v)) {{ {var} = nlclNum(v);"
                if x.dst is not None and ident(f) in x.dst.members:
                    m = x.dst.members[ident(f)]
                    conv = {"bool": "nlclBool(v)", "long": "(long)nlclNum(v)", "float": "nlclNum(v)",
                            "String": "v"}[m.ctype]
                    line += f" {x.dst.var}.{m.name} = {conv};"
                L.append(line + " }")
            L += [f"  {s}" for s in self._msg_calls_cpp(b, x)]
            L.append("}")
            if x.transport == "http":
                L += [f"String nlclLastT_{x.cid};",
                      f"void nlclPoll_{x.cid}() {{  // GET latest message from the hub; act only on new ones",
                      f"  String r = nlclHttpGet({self._cstr(ch.config.get('path', '/channel/' + ch.id))});",
                      "  String t;",
                      f"  if (r.length() > 2 && nlclJsonGet(r, \"_t\", t) && t != nlclLastT_{x.cid}) "
                      f"{{ nlclLastT_{x.cid} = t; nlclRecv_{x.cid}(r); }}",
                      "}"]
            return L
        # bus receivers: values arrive as int16 frames / pin levels
        n = len(ch.fields)
        L.append(f"void nlclStore_{x.cid}(const int16_t* v) {{")
        for i, f in enumerate(ch.fields):
            key = f"{ch.src.ident}.{ident(f)}"
            var = b.remotes.setdefault(key, f"remote_{ch.src.ident}_{ident(f)}")
            line = f"  {var} = v[{i}];"
            if x.dst is not None and ident(f) in x.dst.members:
                m = x.dst.members[ident(f)]
                if m.ctype == "String":
                    line += f" {x.dst.var}.{m.name} = String(v[{i}]);"
                elif m.ctype == "bool":
                    line += f" {x.dst.var}.{m.name} = v[{i}] != 0;"
                else:
                    line += f" {x.dst.var}.{m.name} = v[{i}];"
            L.append(line)
        L += [f"  {s}" for s in self._msg_calls_cpp(b, x)]
        L.append("}")
        if x is self._i2c_slave(b):
            k = max(n, 1)
            return L + [
                f"// I2C slave at {self._i2c_addr(ch)}: the other board writes 0x{FRAME_SYNC:02X}, {n}, "
                f"{n} x int16 big-endian (field order)",
                f"volatile int16_t nlclI2cIn_{x.cid}[{k}];",
                f"volatile bool nlclI2cNew_{x.cid} = false;",
                f"void nlclI2cRx_{x.cid}(int count) {{  // runs in the I2C interrupt: copy bytes only",
                "  (void)count;",
                "  int i = 0;",
                f"  if (Wire.available() < 2 || Wire.read() != 0x{FRAME_SYNC:02X}) {{ while (Wire.available()) "
                "Wire.read(); return; }",
                "  Wire.read();  // count",
                f"  while (Wire.available() >= 2 && i < {k}) {{ uint16_t hi = Wire.read(); uint16_t lo = Wire.read(); "
                f"nlclI2cIn_{x.cid}[i++] = (int16_t)((hi << 8) | lo); }}",
                "  while (Wire.available()) Wire.read();",
                f"  nlclI2cNew_{x.cid} = true;",
                "}",
                f"void nlclPoll_{x.cid}() {{",
                f"  if (!nlclI2cNew_{x.cid}) return;",
                f"  int16_t v[{k}];",
                f"  noInterrupts(); for (int i = 0; i < {k}; i++) v[i] = nlclI2cIn_{x.cid}[i]; "
                f"nlclI2cNew_{x.cid} = false; interrupts();",
                f"  nlclStore_{x.cid}(v);",
                "}"]
        L.append(f"void nlclPoll_{x.cid}() {{")
        L.append(f"  int16_t v[{max(n, 1)}] = {{0}};")
        if x.transport == "i2c":
            L += [f"  // read request: the slave answers 0x{FRAME_SYNC:02X}, {n}, {n} x int16 big-endian",
                  f"  uint8_t got = Wire.requestFrom((uint8_t){self._i2c_addr(ch)}, (uint8_t){2 + 2 * n});",
                  f"  if (got < {2 + 2 * n} || Wire.read() != 0x{FRAME_SYNC:02X}) "
                  "{ while (Wire.available()) Wire.read(); return; }",
                  "  Wire.read();  // count",
                  f"  for (int i = 0; i < {n}; i++) {{ uint16_t hi = Wire.read(); uint16_t lo = Wire.read(); v[i] = (int16_t)((hi << 8) | lo); }}"]
        elif x.transport == "spi":
            L += [f"  // read request 0x{FRAME_READ:02X}, then clock {1 + 2 * n} bytes: 0x{FRAME_SYNC:02X}, "
                  f"{n} x int16 big-endian",
                  "  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));",
                  f"  digitalWrite(PIN_CS_{x.cid.upper()}, LOW);",
                  f"  SPI.transfer((uint8_t)0x{FRAME_READ:02X});",
                  "  uint8_t sync = SPI.transfer(0);",
                  f"  for (int i = 0; i < {n}; i++) {{ uint16_t hi = SPI.transfer(0); uint16_t lo = SPI.transfer(0); v[i] = (int16_t)((hi << 8) | lo); }}",
                  f"  digitalWrite(PIN_CS_{x.cid.upper()}, HIGH);",
                  "  SPI.endTransaction();",
                  f"  if (sync != 0x{FRAME_SYNC:02X}) return;"]
        else:  # gpio
            L += [f"  static int16_t last[{max(n, 1)}] = {{0}}; bool changed = false;"]
            for i, f in enumerate(ch.fields):
                L.append(f"  v[{i}] = digitalRead(PIN_{x.cid.upper()}_{ident(f).upper()}) == HIGH ? 1 : 0;")
            L += [f"  for (int i = 0; i < {n}; i++) {{ if (v[i] != last[i]) changed = true; last[i] = v[i]; }}",
                  "  if (!changed) return;"]
        L += [f"  nlclStore_{x.cid}(v);", "}"]
        return L

    # ---- drivers -----------------------------------------------------------
    def _io_cpp(self, b: Board) -> list[str]:
        R = ["", "// ======================================================================",
             "// drivers", "// ======================================================================",
             "void nlclReadInputs(unsigned long now) {"]
        rd = []
        for c in b.comps:
            v, P = c.var, self._pconst(c)
            if c.role == "button":
                rd.append(f"{v}.pressed = digitalRead({P}) == LOW;   // INPUT_PULLUP: pressed connects to GND")
            elif c.role == "switch":
                rd.append(f"{v}.on = digitalRead({P}) == LOW;")
            elif c.role == "pir":
                rd.append(f"{v}.motion = digitalRead({P}) == HIGH;")
            elif c.role == "temp":
                rd += [f"{{ // {c.e.name}: NTC thermistor divider (beta 3950), as wired in Wokwi",
                       f"  float raw = analogRead({P});",
                       f"  if (raw > 0 && raw < NLCL_ADC_MAX) {v}.temperature = 1.0f / (log(1.0f / (NLCL_ADC_MAX / raw - 1.0f)) / 3950.0f + 1.0f / 298.15f) - 273.15f;",
                       f"  // LM35 instead: {v}.temperature = raw * (VREF / NLCL_ADC_MAX) * 100.0f;",
                       "}"]
            elif c.role == "dht":
                rd += [f"{{ static unsigned long t = 0; if (t == 0 || now - t >= 2000UL) {{ t = now;  // DHT: max 0.5 Hz",
                       "#if NLCL_USE_DHT",
                       f"  float tc = nlclDht_{v}.readTemperature(), h = nlclDht_{v}.readHumidity();",
                       f"  if (!isnan(tc)) {v}.temperature = tc;",
                       f"  if (!isnan(h)) {v}.humidity = h;",
                       "#else",
                       f"  {v}.temperature = 22.0f + 3.0f * sin(now / 20000.0f);  // simulated (library disabled)",
                       f"  {v}.humidity = 50.0f + 10.0f * sin(now / 30000.0f);",
                       "#endif",
                       "} }"]
            elif c.role == "distance":
                T, E = self._pconst(c, "trig"), self._pconst(c, "echo")
                rd += [f"{{ static unsigned long t = 0; if (now - t >= 60UL) {{ t = now;  // HC-SR04",
                       f"  digitalWrite({T}, LOW); delayMicroseconds(2);",
                       f"  digitalWrite({T}, HIGH); delayMicroseconds(10); digitalWrite({T}, LOW);",
                       f"  unsigned long us = pulseIn({E}, HIGH, 30000UL);",
                       f"  if (us > 0) {v}.distance = us / 58.0f;   // cm",
                       "} }"]
            elif c.role in ("light", "pot", "analog"):
                f = next((k for k in c.members if k != "alive" and k not in ("on",)), "value")
                m = c.members[f]
                if m.ctype == "String":
                    rd.append(f"{v}.{m.name} = String(analogRead({P}));")
                elif m.ctype == "bool":
                    rd.append(f"{v}.{m.name} = analogRead({P}) > NLCL_ADC_MAX / 2;")
                elif f in PERCENT_FIELDS:
                    rd.append(f"{v}.{m.name} = analogRead({P}) * 100.0f / NLCL_ADC_MAX;   // 0..100 % "
                              f"(capacitive soil probes read high when dry: use 100 - ... for those)")
                else:
                    rd.append(f"{v}.{m.name} = analogRead({P});   // 0..{b.spec['adc_max']}")
        R += ["  " + s if not s.startswith("#") else s for s in rd] or []
        R += ["  (void)now;", "}", "", "void nlclWriteOutputs(unsigned long now) {"]
        wr = []
        for c in b.comps:
            v, P = c.var, self._pconst(c)
            if c.role in ("led", "relay"):
                if c.role == "led" and "brightness" in c.members:
                    br = c.members["brightness"]
                    val = f"(int){v}.{br.name}" if br.ctype != "String" else "255"
                    wr.append(f"analogWrite({P}, ({v}.alive && {v}.on) ? constrain({val}, 0, 255) : 0);")
                else:
                    wr.append(f"digitalWrite({P}, ({v}.alive && {v}.on) ? HIGH : LOW);")
            elif c.role in ("motor", "fan"):
                sp_ = c.members.get("speed")
                val = f"(int){v}.{sp_.name}" if sp_ and sp_.ctype != "String" else "255"
                wr.append(f"analogWrite({P}, ({v}.alive && {v}.on) ? constrain({val}, 0, 255) : 0);  // PWM to a driver/transistor")
            elif c.role == "servo":
                am = c.members["angle"]
                wr += [f"{{ static int last = -1; int a = {v}.alive ? constrain((int){v}.{am.name}, 0, 180) : last;",
                       "  if (a != last) { last = a;",
                       "#if NLCL_USE_SERVO",
                       f"  nlclServo_{v}.write(a);",
                       "#endif",
                       "  } }"]
            elif c.role == "buzzer":
                fm = c.members["frequency"]
                fq = f"(unsigned int){v}.{fm.name}" if fm.ctype != "String" else "1000U"
                wr += [f"{{ static bool last = false; bool s = {v}.alive && {v}.on;",
                       f"  if (s != last) {{ last = s; if (s) tone({P}, {fq}); else noTone({P}); }} }}"]
            elif c.role == "neopixel":
                col, br = c.members["color"], c.members["brightness"]
                colv = f"{v}.{col.name}" if col.ctype == "String" else "String(\"white\")"
                brv = f"(int){v}.{br.name}" if br.ctype != "String" else "50"
                wr += ["#if NLCL_USE_NEOPIXEL",
                       f"{{ static uint32_t last = 0xFFFFFFFFUL; uint32_t col = ({v}.alive && {v}.on) ? nlclColor({colv}) : 0;",
                       f"  uint32_t key = col ^ ((uint32_t)constrain({brv}, 0, 255) << 24);",
                       f"  if (key != last) {{ last = key; nlclPix_{v}.setBrightness(constrain({brv}, 0, 255));",
                       f"    nlclPix_{v}.fill(col); nlclPix_{v}.show(); }} }}",
                       "#endif"]
            elif c.role in ("oled", "lcd"):
                tm = c.members["text"]
                txt = f"{v}.{tm.name}" if tm.ctype == "String" else f"String({v}.{tm.name})"
                lines = [f"String l1 = {txt}.length() ? {txt} : nlclLastLog;"]
                shown = [s for s in b.comps if s.role in ("temp", "dht", "distance", "light", "pot", "analog", "pir")][:3]
                rows = []
                for s in shown:
                    for f, m in s.members.items():
                        if f == "alive" or m.ctype == "String":
                            continue
                        rows.append((f"{s.e.name[:8]} {f[:4]}", f"{s.var}.{m.name}"))
                rows = rows[:3]
                if c.role == "oled":
                    wr += ["#if NLCL_USE_OLED",
                           f"{{ static unsigned long t = 0; if (now - t >= 500UL) {{ t = now;",
                           f"  {lines[0]}",
                           f"  nlclOled_{v}.clearDisplay(); nlclOled_{v}.setTextSize(1); nlclOled_{v}.setTextColor(SSD1306_WHITE);",
                           f"  nlclOled_{v}.setCursor(0, 0); nlclOled_{v}.println(l1);"]
                    for lab, code in rows:
                        wr.append(f"  nlclOled_{v}.print(\"{lab}: \"); nlclOled_{v}.println({code});")
                    wr += [f"  nlclOled_{v}.display();", "} }", "#endif"]
                else:
                    wr += ["#if NLCL_USE_LCD",
                           f"{{ static unsigned long t = 0; if (now - t >= 500UL) {{ t = now;",
                           f"  {lines[0]}",
                           f"  nlclLcd_{v}.clear(); nlclLcd_{v}.setCursor(0, 0); nlclLcd_{v}.print(l1.substring(0, 16));"]
                    if rows:
                        lab, code = rows[0]
                        wr.append(f"  nlclLcd_{v}.setCursor(0, 1); nlclLcd_{v}.print(String(\"{lab[:9]}:\") + String({code}));")
                    wr += ["} }", "#endif"]
        R += ["  " + s if not s.startswith("#") else s for s in wr]
        R += ["  (void)now;", "}"]
        return R

    def _setup_cpp(self, b: Board, wire: bool, spi: bool, wifi: bool, mqtt: bool, udp: bool) -> list[str]:
        L = []
        for c in b.comps:
            v, P = c.var, self._pconst(c)
            r = c.role
            if r in ("led", "relay", "motor", "fan", "buzzer", "neopixel"):
                L.append(f"pinMode({P}, OUTPUT);")
            elif r in ("button", "switch"):
                L.append(f"pinMode({P}, INPUT_PULLUP);")
            elif r == "pir":
                L.append(f"pinMode({P}, INPUT);")
            elif r == "distance":
                L += [f"pinMode({self._pconst(c, 'trig')}, OUTPUT);", f"pinMode({self._pconst(c, 'echo')}, INPUT);"]
            elif r == "dht":
                L += ["#if NLCL_USE_DHT", f"nlclDht_{v}.begin();", "#endif"]
            elif r == "servo":
                L += ["#if NLCL_USE_SERVO", f"nlclServo_{v}.attach({P});", "#endif"]
        for c in b.comps:
            if c.role == "neopixel":
                L += ["#if NLCL_USE_NEOPIXEL", f"nlclPix_{c.var}.begin();", "#endif"]
        slave = self._i2c_slave(b)
        if slave is not None:
            addr = self._i2c_addr(slave.ch)
            if b.spec["arch"] == "esp32":
                L.append(f"Wire.begin((uint8_t){addr}, {b.spec['i2c'][0]}, {b.spec['i2c'][1]}, 100000);  // I2C slave")
            else:
                L.append(f"Wire.begin((uint8_t){addr});  // I2C slave (another board is the master)")
            L.append(f"Wire.onReceive(nlclI2cRx_{slave.cid});")
        elif wire:
            if b.spec["arch"] == "esp32":
                L.append(f"Wire.begin({b.spec['i2c'][0]}, {b.spec['i2c'][1]});  // SDA, SCL")
            else:
                L.append("Wire.begin();")
        for c in b.comps:
            if c.role == "oled":
                L += ["#if NLCL_USE_OLED",
                      f"if (!nlclOled_{c.var}.begin(SSD1306_SWITCHCAPVCC, 0x3C)) Serial.println(\"# {c.e.name}: SSD1306 not found\");",
                      "#endif"]
            if c.role == "lcd":
                L += ["#if NLCL_USE_LCD", f"nlclLcd_{c.var}.init(); nlclLcd_{c.var}.backlight();", "#endif"]
        if spi:
            L.append("SPI.begin();")
            for x in b.chans:
                if x.transport == "spi" and "cs" in x.pins:
                    L += [f"pinMode(PIN_CS_{x.cid.upper()}, OUTPUT);", f"digitalWrite(PIN_CS_{x.cid.upper()}, HIGH);"]
        for x in b.chans:
            if x.transport == "gpio" and not x.local:
                for f in x.pins:
                    L.append(f"pinMode(PIN_{x.cid.upper()}_{ident(f).upper()}, {'OUTPUT' if x.out else 'INPUT'});")
        if wifi:
            L += ["WiFi.mode(WIFI_STA);" if b.wifi != "picow" else "WiFi.mode(WIFI_STA);",
                  "WiFi.begin(WIFI_SSID, WIFI_PASS);",
                  "Serial.print(\"# connecting to WiFi \" WIFI_SSID);",
                  "{ unsigned long t0 = millis(); while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000UL) { delay(250); Serial.print('.'); } }",
                  "Serial.println(WiFi.status() == WL_CONNECTED ? \" ok\" : \" offline (will retry)\");"]
            if mqtt:
                broker = next((x.ch.config.get("broker", "broker.hivemq.com") for x in b.chans if x.transport == "mqtt"),
                              "broker.hivemq.com")
                port = next((_int(x.ch.config.get("broker_port"), 1883) for x in b.chans if x.transport == "mqtt"), 1883)
                L += ["#if NLCL_USE_MQTT", f"nlclMqtt.setServer({self._cstr(broker)}, {port});",
                      "nlclMqtt.setBufferSize(512);"]
                if any(x.transport == "mqtt" and x.inn for x in b.chans):
                    L.append("nlclMqtt.setCallback(nlclMqttCallback);")
                L.append("#endif")
            if udp:
                port = next((_int(x.ch.config.get("port"), 9876) for x in b.chans if x.transport == "udp" and x.inn), 0)
                if port:
                    L.append(f"nlclUdp.begin({port});")
        if b.spec["arch"] == "avr":
            L.append("randomSeed(analogRead(A0) ^ micros());")
        else:
            L.append("randomSeed(micros());")
        return L

    # ======================================================================
    # MicroPython
    # ======================================================================
    def _upy(self, ctx: BuildContext, b: Board, S: dict) -> str:
        sp = b.spec
        port = "esp32" if sp["arch"] == "esp32" else "rp2"
        net = [x for x in b.chans if not x.local and x.transport in ("http", "mqtt", "udp")]
        wifi = bool(net) and bool(b.wifi)
        roles = {c.role for c in b.comps}
        L: list[str] = []
        A = L.append
        A(f'"""{ctx.system.name} - MicroPython firmware for {sp["title"]} (\'{b.ident}\').')
        A("")
        A("Generated by NLCL; same behaviour as the Arduino sketch next to it.")
        A("Flash MicroPython (micropython.org/download), then:  mpremote cp main.py :main.py  and reset.")
        A("Serial (USB REPL): JSON lines are channel messages, '#' lines are logs, other lines are commands.")
        A('"""')
        A("import sys")
        A("import time")
        A("import json")
        A("from machine import Pin, ADC, PWM")
        if roles & {"oled", "lcd"} or any(x.transport == "i2c" and not x.local for x in b.chans):
            A("from machine import I2C")
        if any(x.transport == "spi" and not x.local for x in b.chans):
            A("from machine import SPI")
        if "distance" in roles:
            A("from machine import time_pulse_us")
        if "dht" in roles:
            A("try:\n    import dht\nexcept ImportError:\n    dht = None")
        if "neopixel" in roles:
            A("try:\n    import neopixel\nexcept ImportError:\n    neopixel = None")
        if "oled" in roles:
            A("try:\n    import ssd1306  # mpremote mip install ssd1306\nexcept ImportError:\n    ssd1306 = None")
        A("try:\n    import select\n    _POLL = select.poll()\n    _POLL.register(sys.stdin, select.POLLIN)\nexcept Exception:\n    _POLL = None")
        if wifi:
            A("try:\n    import network\nexcept ImportError:\n    network = None")
            if any(x.transport == "http" for x in net):
                A("try:\n    import urequests as requests\nexcept ImportError:\n    try:\n        import requests\n    except ImportError:\n        requests = None")
            if any(x.transport == "mqtt" for x in net):
                A("try:\n    from umqtt.simple import MQTTClient  # mpremote mip install umqtt.simple\nexcept ImportError:\n    MQTTClient = None")
            if any(x.transport == "udp" for x in net):
                A("import socket")
            ssid = b.entity.props.get("wifi_ssid") if b.entity else None
            pw = b.entity.props.get("wifi_password") if b.entity else None
            host = b.entity.props.get("hub_host") if b.entity else None
            hub_port = 8765
            for x in net:
                if x.transport == "http":
                    try:
                        hub_port = int(str(x.ch.hub_url).rsplit(":", 1)[1].split("/")[0])
                    except (ValueError, IndexError):
                        pass
            A("")
            A(f"WIFI_SSID = {str(ssid or 'Wokwi-GUEST')!r}   # Wokwi simulator: 'Wokwi-GUEST', no password")
            A(f"WIFI_PASS = {str(pw or '')!r}")
            A(f"HUB_HOST = {str(host or 'host.wokwi.internal')!r}   # PC running the NLCL hub (NOT localhost)")
            A(f"HUB_PORT = {hub_port}")
        A("")
        A("ADC_MAX = 65535  # read_u16() full scale")
        A("")
        A("")
        A("class Obj:")
        A("    def __init__(self, **kw):")
        A("        self.__dict__.update(kw)")
        A("")
        A("")
        A("# ---- entity state")
        for c in b.comps:
            kw = ", ".join(f"{m.name}={self._plit(m)}" for m in c.members.values())
            A(f"{c.var} = Obj({kw})  # {c.e.kind} '{c.e.name}'")
        A("REMOTE = {}  # latest values received from other modules ('entity.field')")
        A("_ST = {}     # trigger / driver state")
        # ---- hardware
        A("")
        A("# ---- hardware")

        def pn(c, logical="pin"):
            return self._pyint(b, c.pins.get(logical, "0"))
        for c in b.comps:
            v, r = c.var, c.role
            if r in ("led", "relay"):
                if r == "led" and "brightness" in c.members:
                    A(f"_hw_{v} = PWM(Pin({pn(c)}))  # {c.e.name}")
                    A(f"_hw_{v}.freq(1000)")
                else:
                    A(f"_hw_{v} = Pin({pn(c)}, Pin.OUT)  # {c.e.name}")
            elif r in ("motor", "fan", "buzzer"):
                A(f"_hw_{v} = PWM(Pin({pn(c)}))  # {c.e.name}")
                A(f"_hw_{v}.freq({1000 if r != 'buzzer' else 1000})")
                A(f"_hw_{v}.duty_u16(0)")
            elif r == "servo":
                A(f"_hw_{v} = PWM(Pin({pn(c)}))  # {c.e.name} (50 Hz servo pulses)")
                A(f"_hw_{v}.freq(50)")
            elif r in ("button", "switch"):
                A(f"_hw_{v} = Pin({pn(c)}, Pin.IN, Pin.PULL_UP)  # {c.e.name}")
            elif r == "pir":
                A(f"_hw_{v} = Pin({pn(c)}, Pin.IN)  # {c.e.name}")
            elif r in ("temp", "light", "pot", "analog"):
                A(f"_hw_{v} = ADC(Pin({pn(c)}))  # {c.e.name}")
                if port == "esp32":
                    A(f"_hw_{v}.atten(ADC.ATTN_11DB)  # full 0..3.3 V range")
            elif r == "dht":
                model = "DHT11" if "dht11" in self._words(c.e) else "DHT22"
                A(f"_hw_{v} = dht.{model}(Pin({pn(c)})) if dht else None  # {c.e.name}")
            elif r == "distance":
                A(f"_hw_{v}_trig = Pin({pn(c, 'trig')}, Pin.OUT)  # {c.e.name}")
                A(f"_hw_{v}_echo = Pin({pn(c, 'echo')}, Pin.IN)")
            elif r == "neopixel":
                n = c.e.props.get("pixels", 1)
                n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
                A(f"_hw_{v} = neopixel.NeoPixel(Pin({pn(c)}), {n}) if neopixel else None  # {c.e.name}")
        if roles & {"oled", "lcd"} or any(x.transport == "i2c" and not x.local for x in b.chans):
            sda, scl = sp["i2c"]
            A(f"_I2C = I2C(0, scl=Pin({scl}), sda=Pin({sda}), freq=400000)")
        for c in b.comps:
            if c.role == "oled":
                A("try:")
                A(f"    _hw_{c.var} = ssd1306.SSD1306_I2C(128, 64, _I2C) if ssd1306 else None  # {c.e.name}")
                A("except Exception:")
                A(f"    _hw_{c.var} = None")
            elif c.role == "lcd":
                A(f"_hw_{c.var} = None  # {c.e.name}: no built-in LCD driver in MicroPython; text goes to the REPL")
        if any(x.transport == "spi" and not x.local for x in b.chans):
            sck, miso, mosi = sp["spi"]
            sid = 2 if port == "esp32" else 0
            A(f"_SPI = SPI({sid}, baudrate=1000000, sck=Pin({sck}), mosi=Pin({mosi}), miso=Pin({miso}))")
            for x in b.chans:
                if x.transport == "spi" and "cs" in x.pins:
                    A(f"_CS_{x.cid} = Pin({self._pyint(b, x.pins['cs'])}, Pin.OUT, value=1)")
        for x in b.chans:
            if x.transport == "gpio" and not x.local:
                for f, p in x.pins.items():
                    A(f"_GP_{x.cid}_{ident(f)} = Pin({self._pyint(b, p)}, {'Pin.OUT' if x.out else 'Pin.IN'})  # field {f}")
        # ---- helpers
        A("")
        A("")
        A("def nlcl_log(who, msg):")
        A("    _ST['last_log'] = str(msg)")
        A("    print('# %s: %s' % (who, msg))")
        A("")
        A("")
        A("def i16(v):")
        A("    try:")
        A("        v = int(round(float(v)))")
        A("    except (TypeError, ValueError):")
        A("        v = 0")
        A("    return max(-32768, min(32767, v))")
        A("")
        A("")
        A("def to_num(v):")
        A("    if v is True or v is False:")
        A("        return 1 if v else 0")
        A("    try:")
        A("        return float(v)")
        A("    except (TypeError, ValueError):")
        A("        return 0")
        if "buzzer" in roles:
            A("")
            A("")
            A("def beep(pwm, freq, ms):")
            A("    pwm.freq(int(freq) or 1000)")
            A("    pwm.duty_u16(32768)")
            A("    time.sleep_ms(ms)")
            A("    pwm.duty_u16(0)")
            A("    _ST.pop('bz', None)")
        if "neopixel" in roles:
            A("")
            A("")
            A(f"COLORS = {dict(COLORS)!r}")
            A("")
            A("")
            A("def color(c):")
            A("    c = str(c)")
            A("    if c.startswith('#') and len(c) == 7:")
            A("        return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))")
            A("    return COLORS.get(c.lower(), (255, 255, 255))")
        if wifi:
            A("")
            A("")
            A("def wifi_up():")
            A("    if network is None:")
            A("        return False")
            A("    w = _ST.get('wlan')")
            A("    if w is None:")
            A("        w = network.WLAN(network.STA_IF)")
            A("        w.active(True)")
            A("        _ST['wlan'] = w")
            A("    if w.isconnected():")
            A("        return True")
            A("    now = time.ticks_ms()")
            A("    if 'wifi_try' in _ST and time.ticks_diff(now, _ST['wifi_try']) < 10000:")
            A("        return False")
            A("    _ST['wifi_try'] = now")
            A("    try:")
            A("        w.connect(WIFI_SSID, WIFI_PASS)")
            A("    except OSError:")
            A("        pass")
            A("    return False")
            if any(x.transport == "http" for x in net):
                A("")
                A("")
                A("def _http(method, path, body=None):")
                A("    if requests is None or not wifi_up():")
                A("        return None")
                A("    url = 'http://%s:%d%s' % (HUB_HOST, HUB_PORT, path)")
                A("    kw = {'headers': {'Content-Type': 'application/json'}} if body is not None else {}")
                A("    try:")
                A("        try:")
                A("            r = requests.request(method, url, data=body, timeout=2, **kw)")
                A("        except TypeError:  # old urequests without timeout")
                A("            r = requests.request(method, url, data=body, **kw)")
                A("        txt = r.text if r.status_code == 200 else None")
                A("        r.close()")
                A("        return txt")
                A("    except Exception:")
                A("        return None")
            if any(x.transport == "mqtt" for x in net):
                broker = next((x.ch.config.get("broker", "broker.hivemq.com") for x in net if x.transport == "mqtt"),
                              "broker.hivemq.com")
                bport = next((_int(x.ch.config.get("broker_port"), 1883) for x in net if x.transport == "mqtt"), 1883)
                subs = [x for x in net if x.transport == "mqtt" and x.inn]
                A("")
                A("")
                A("def _mqtt_msg(topic, msg):")
                A("    t = topic.decode() if isinstance(topic, bytes) else topic")
                A("    try:")
                A("        m = json.loads(msg)")
                A("    except ValueError:")
                A("        return")
                for x in subs:
                    A(f"    if t == {x.ch.topic!r}:")
                    A(f"        recv_{x.cid}(m)")
                A("")
                A("")
                A("def mqtt_client():")
                A("    c = _ST.get('mqtt')")
                A("    if c is not None or MQTTClient is None or not wifi_up():")
                A("        return c")
                A("    now = time.ticks_ms()")
                A("    if 'mqtt_try' in _ST and time.ticks_diff(now, _ST['mqtt_try']) < 5000:")
                A("        return None")
                A("    _ST['mqtt_try'] = now")
                A("    try:")
                A(f"        c = MQTTClient('nlcl-%d' % (now & 0xFFFFFF), {broker!r}, port={bport})")
                A("        c.set_callback(_mqtt_msg)")
                A("        c.connect()")
                for x in subs:
                    A(f"        c.subscribe({x.ch.topic!r})")
                A("        _ST['mqtt'] = c")
                A("    except Exception:")
                A("        c = None")
                A("    return c")
            if any(x.transport == "udp" for x in net):
                A("")
                A("")
                A("def udp_sock():")
                A("    s = _ST.get('udp')")
                A("    if s is None:")
                A("        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)")
                inport = next((_int(x.ch.config.get("port"), 9876) for x in net if x.transport == "udp" and x.inn), 0)
                if inport:
                    A(f"        s.bind(('0.0.0.0', {inport}))")
                A("        s.setblocking(False)")
                A("        _ST['udp'] = s")
                A("    return s")
        # ---- channels
        A("")
        A("")
        A("# ---- channels")
        recv_map = []
        for x in b.chans:
            ch = x.ch
            A(f"# {ch.id}: {ch.src.name} ({ch.src.domain}) -> {ch.dst.name} ({ch.dst.domain}) via {x.transport}"
              f"{' (local)' if x.local else ''}; fields: {', '.join(ch.fields)}")
            if x.out and x.local:
                A(f"def publish_{x.cid}():")
                for f in ch.fields:
                    _, pe, t = self._field_ref(b, x, f)
                    if x.dst is not None:
                        dm = self._member(x.dst, f, t)
                        if pe == f"{x.dst.var}.{dm.name}":
                            A(f"    # {f}: '{ch.src.name}' has no such property (nothing to copy)")
                        else:
                            A(f"    {x.dst.var}.{dm.name} = {pe}")
                for s in self._msg_calls_py(b, x):
                    A(f"    {s}")
                A("    pass")
                A("")
                A("")
                continue
            if x.out:
                if x.transport in ("serial", "http", "mqtt", "udp"):
                    A(f"def tx_{x.cid}(line):")
                    if x.transport == "serial":
                        A("    print(line)")
                    elif x.transport == "http":
                        A(f"    _http('POST', {ch.config.get('path', '/channel/' + ch.id)!r}, line)")
                    elif x.transport == "mqtt":
                        A("    c = mqtt_client()")
                        A("    if c is not None:")
                        A("        try:")
                        A(f"            c.publish({ch.topic!r}, line)")
                        A("        except Exception:")
                        A("            _ST.pop('mqtt', None)")
                    elif x.transport == "udp":
                        A("    if wifi_up():")
                        A("        try:")
                        A(f"            udp_sock().sendto(line.encode(), (HUB_HOST, {_int(ch.config.get('port'), 9876)}))")
                        A("        except OSError:")
                        A("            pass")
                    A("")
                    A("")
                    A(f"def publish_{x.cid}():")
                    A(f"    tx_{x.cid}({self._json_build_py(b, x, ch.fields)})")
                elif x.transport in ("i2c", "spi"):
                    n = len(ch.fields)
                    vals = ", ".join(f"i16({self._field_ref(b, x, f)[1]})" for f in ch.fields)
                    A(f"def publish_{x.cid}():  # frame 0x{FRAME_SYNC:02X}, {n}, {n} x int16 big-endian")
                    A(f"    vals = [{vals}]")
                    A(f"    buf = bytearray([0x{FRAME_SYNC:02X}, {n}])")
                    A("    for v in vals:")
                    A("        buf += bytes([(v >> 8) & 0xFF, v & 0xFF])")
                    if x.transport == "i2c":
                        A("    try:")
                        A(f"        _I2C.writeto({self._i2c_addr(ch)}, buf)")
                        A("    except OSError:")
                        A("        pass")
                    else:
                        A(f"    _CS_{x.cid}.value(0)")
                        A("    _SPI.write(buf)")
                        A(f"    _CS_{x.cid}.value(1)")
                else:
                    A(f"def publish_{x.cid}():  # one pin per field")
                    for f in ch.fields:
                        A(f"    _GP_{x.cid}_{ident(f)}.value(1 if to_num({self._field_ref(b, x, f)[1]}) else 0)")
                    if not ch.fields:
                        A("    pass")
                A("")
                A("")
            if x.inn:
                A(f"def recv_{x.cid}(msg):")
                A("    if not isinstance(msg, dict):")
                A("        return")
                for f in ch.fields:
                    key = f"{ch.src.ident}.{ident(f)}"
                    A(f"    if {f!r} in msg:")
                    A(f"        REMOTE[{key!r}] = msg[{f!r}]")
                    if x.dst is not None and ident(f) in x.dst.members:
                        m = x.dst.members[ident(f)]
                        conv = {"bool": f"bool(to_num(msg[{f!r}]))", "long": f"int(to_num(msg[{f!r}]))",
                                "float": f"to_num(msg[{f!r}])", "String": f"str(msg[{f!r}])"}[m.ctype]
                        A(f"        {x.dst.var}.{m.name} = {conv}")
                for s in self._msg_calls_py(b, x):
                    A(f"    {s}")
                A("")
                A("")
                if x.transport in ("serial", "udp", "mqtt"):
                    recv_map.append(x)
                if x.transport == "http":
                    A(f"def poll_{x.cid}():  # GET the latest message from the hub, act only on new ones")
                    A(f"    r = _http('GET', {ch.config.get('path', '/channel/' + ch.id)!r})")
                    A("    if not r:")
                    A("        return")
                    A("    try:")
                    A("        m = json.loads(r)")
                    A("    except ValueError:")
                    A("        return")
                    A(f"    if m.get('_t') is not None and m.get('_t') != _ST.get('last_t_{x.cid}'):")
                    A(f"        _ST['last_t_{x.cid}'] = m.get('_t')")
                    A(f"        recv_{x.cid}(m)")
                    A("")
                    A("")
                elif x.transport in ("i2c", "spi", "gpio"):
                    n = len(ch.fields)
                    A(f"def poll_{x.cid}():")
                    if x.transport == "i2c":
                        A("    try:")
                        A(f"        buf = _I2C.readfrom({self._i2c_addr(ch)}, {2 + 2 * n})")
                        A("    except OSError:")
                        A("        return")
                        A(f"    if len(buf) < {2 + 2 * n} or buf[0] != 0x{FRAME_SYNC:02X}:")
                        A("        return")
                        A("    data = buf[2:]")
                    elif x.transport == "spi":
                        A(f"    buf = bytearray({1 + 2 * n})")
                        A(f"    _CS_{x.cid}.value(0)")
                        A(f"    _SPI.write(bytes([0x{FRAME_READ:02X}]))")
                        A("    _SPI.readinto(buf, 0)")
                        A(f"    _CS_{x.cid}.value(1)")
                        A(f"    if buf[0] != 0x{FRAME_SYNC:02X}:")
                        A("        return")
                        A("    data = buf[1:]")
                    if x.transport in ("i2c", "spi"):
                        A("    vals = []")
                        A(f"    for i in range({n}):")
                        A("        v = (data[2 * i] << 8) | data[2 * i + 1]")
                        A("        vals.append(v - 65536 if v & 0x8000 else v)")
                    else:
                        A(f"    vals = [{', '.join(f'_GP_{x.cid}_{ident(f)}.value()' for f in ch.fields) or ''}]")
                        A(f"    if vals == _ST.get('gp_{x.cid}'):")
                        A("        return")
                        A(f"    _ST['gp_{x.cid}'] = vals")
                    A(f"    recv_{x.cid}(dict(zip({list(ch.fields)!r}, vals)))")
                    A("")
                    A("")
        A("def handle_line(line):")
        A("    line = line.strip()")
        A("    if not line:")
        A("        return")
        A("    if line.startswith('{'):")
        A("        try:")
        A("            m = json.loads(line)")
        A("        except ValueError:")
        A("            return")
        A("        ch = m.get('channel')")
        for x in recv_map:
            if x.transport == "serial":
                A(f"        if ch == {x.ch.id!r}:")
                A(f"            recv_{x.cid}(m)")
        A("        return")
        A("    command(line)")
        A("")
        A("")
        A("def poll_serial():")
        A("    if _POLL is None:")
        A("        return")
        A("    while _POLL.poll(0):")
        A("        c = sys.stdin.read(1)")
        A("        if not c:")
        A("            return")
        A("        if c in '\\r\\n':")
        A("            buf = _ST.get('buf', '')")
        A("            _ST['buf'] = ''")
        A("            handle_line(buf)")
        A("        else:")
        A("            _ST['buf'] = (_ST.get('buf', '') + c)[-300:]")
        # ---- behaviours
        A("")
        A("")
        A("# ---- behaviours")
        L += S["beh_p"]
        for name, n in sorted(b.calls.items()):
            A("")
            A(f"def user_{name}(*args):  # '{name}' was requested by a behaviour: put your own code here")
            A(f"    nlcl_log('call', '{name}%r' % (args,))")
            A("")
        A("")
        A("def command(cmd):  # typed serial commands -> input:<command> behaviours")
        L += S["cmd_p"]
        A("    pass")
        # ---- drivers
        L += self._io_py(b, port)
        A("")
        A("")
        A("def comms(now):")
        body = []
        if wifi:
            if any(x.transport == "mqtt" for x in net):
                body += ["c = mqtt_client()", "if c is not None:", "    try:", "        c.check_msg()",
                         "    except Exception:", "        _ST.pop('mqtt', None)"]
            if any(x.transport == "udp" and x.inn for x in net):
                body += ["if wifi_up():", "    try:", "        data = udp_sock().recv(512)", "    except OSError:",
                         "        data = None", "    if data:", "        try:", "            m = json.loads(data)",
                         "        except ValueError:", "            m = {}"]
                for x in net:
                    if x.transport == "udp" and x.inn:
                        body += [f"        if m.get('channel') == {x.ch.id!r}:", f"            recv_{x.cid}(m)"]
        for x in b.chans:
            iv = max(1, _int(x.ch.config.get("interval_ms"), 1000))
            if x.inn and not x.local and x.transport in ("http", "i2c", "spi", "gpio"):
                body += [f"if time.ticks_diff(now, _ST.get('poll_{x.cid}', -{iv})) >= {iv}:",
                         f"    _ST['poll_{x.cid}'] = now", f"    poll_{x.cid}()"]
            if x.out:
                body += [f"if time.ticks_diff(now, _ST.get('pub_{x.cid}', -{iv})) >= {iv}:  # {x.ch.id} every {iv} ms",
                         f"    _ST['pub_{x.cid}'] = now", f"    publish_{x.cid}()"]
        L += ["    " + s for s in body] or ["    pass"]
        A("")
        A("")
        A("def triggers(now):")
        L += S["trig_p"] or ["    pass"]
        A("")
        A("")
        A("def setup():")
        A(f"    print('# {b.ident}: NLCL MicroPython firmware starting')")
        if wifi:
            A("    t0 = time.ticks_ms()")
            A("    while not wifi_up() and time.ticks_diff(time.ticks_ms(), t0) < 15000:")
            A("        time.sleep_ms(250)")
        L += ["    " + s for s in S["start_p"]]
        L += ["    " + s for s in S["chg_init_p"]]
        A("    pass")
        A("")
        A("")
        A("def loop():")
        A("    now = time.ticks_ms()")
        A("    read_inputs(now)")
        A("    poll_serial()")
        A("    comms(now)")
        A("    triggers(now)")
        A("    write_outputs(now)")
        A("")
        A("")
        A("if __name__ == '__main__':")
        A("    setup()")
        A("    while True:")
        A("        loop()")
        A("        time.sleep_ms(2)")
        return "\n".join(L) + "\n"

    def _msg_calls_py(self, b: Board, x: Chan) -> list[str]:
        return [s.split(";")[0].replace("();", "()").rstrip(";") + "  # " + s.split("// ")[1]
                for s in self._msg_calls_cpp(b, x)]

    def _io_py(self, b: Board, port: str) -> list[str]:
        L = ["", "", "# ---- drivers", "def read_inputs(now):"]
        rd = []
        for c in b.comps:
            v, r = c.var, c.role
            if r == "button":
                rd.append(f"{v}.pressed = _hw_{v}.value() == 0  # pull-up: pressed = GND")
            elif r == "switch":
                rd.append(f"{v}.on = _hw_{v}.value() == 0")
            elif r == "pir":
                rd.append(f"{v}.motion = _hw_{v}.value() == 1")
            elif r == "temp":
                rd += [f"raw = _hw_{v}.read_u16()  # NTC thermistor divider, beta 3950",
                       "if 0 < raw < ADC_MAX:",
                       "    import math",
                       f"    {v}.temperature = 1 / (math.log(1 / (ADC_MAX / raw - 1)) / 3950 + 1 / 298.15) - 273.15"]
            elif r == "dht":
                rd += [f"if _hw_{v} is not None and time.ticks_diff(now, _ST.get('dht_{v}', -2000)) >= 2000:",
                       f"    _ST['dht_{v}'] = now",
                       "    try:",
                       f"        _hw_{v}.measure()",
                       f"        {v}.temperature = _hw_{v}.temperature()",
                       f"        {v}.humidity = _hw_{v}.humidity()",
                       "    except OSError:",
                       "        pass"]
            elif r == "distance":
                rd += [f"if time.ticks_diff(now, _ST.get('us_{v}', -60)) >= 60:",
                       f"    _ST['us_{v}'] = now",
                       f"    _hw_{v}_trig.value(0)",
                       "    time.sleep_us(2)",
                       f"    _hw_{v}_trig.value(1)",
                       "    time.sleep_us(10)",
                       f"    _hw_{v}_trig.value(0)",
                       f"    us = time_pulse_us(_hw_{v}_echo, 1, 30000)",
                       "    if us > 0:",
                       f"        {v}.distance = us / 58.0  # cm"]
            elif r in ("light", "pot", "analog"):
                f = next((k for k in c.members if k != "alive" and k != "on"), "value")
                m = c.members[f]
                conv = {"String": "str({})", "bool": "{} > 32767", "long": "{} >> 4", "float": "{} / 16"}[m.ctype]
                if f in PERCENT_FIELDS and m.ctype in ("long", "float"):
                    conv = "{} * 100 // 65535" if m.ctype == "long" else "{} * 100 / 65535"
                    rd.append(f"{v}.{m.name} = {conv.format(f'_hw_{v}.read_u16()')}  # 0..100 %")
                else:
                    rd.append(f"{v}.{m.name} = {conv.format(f'_hw_{v}.read_u16()')}  # 0..4095")
        L += ["    " + s for s in rd] or ["    pass"]
        L += ["", "", "def write_outputs(now):"]
        wr = []
        for c in b.comps:
            v, r = c.var, c.role
            if r in ("led", "relay"):
                if r == "led" and "brightness" in c.members and c.members["brightness"].ctype != "String":
                    wr.append(f"_hw_{v}.duty_u16(int(max(0, min(255, {v}.brightness))) * 257 if ({v}.alive and {v}.on) else 0)")
                else:
                    wr.append(f"_hw_{v}.value(1 if ({v}.alive and {v}.on) else 0)")
            elif r in ("motor", "fan"):
                spm = c.members.get("speed")
                sv = f"{v}.{spm.name}" if spm and spm.ctype != "String" else "255"
                wr.append(f"_hw_{v}.duty_u16(int(max(0, min(255, {sv}))) * 257 if ({v}.alive and {v}.on) else 0)")
            elif r == "servo":
                am = c.members["angle"]
                wr += [f"a = int(max(0, min(180, to_num({v}.{am.name}))))",
                       f"if {v}.alive and a != _ST.get('servo_{v}'):",
                       f"    _ST['servo_{v}'] = a",
                       f"    _hw_{v}.duty_u16(int((0.5 + a / 180 * 2.0) / 20 * 65535))  # 0.5..2.5 ms pulse"]
            elif r == "buzzer":
                fm = c.members["frequency"]
                fv = f"{v}.{fm.name}" if fm.ctype != "String" else "1000"
                wr += [f"s = bool({v}.alive and {v}.on)",
                       f"if s != _ST.get('bz_{v}', False):",
                       f"    _ST['bz_{v}'] = s",
                       "    if s:",
                       f"        _hw_{v}.freq(int(to_num({fv})) or 1000)",
                       f"    _hw_{v}.duty_u16(32768 if s else 0)"]
            elif r == "neopixel":
                cm, bm = c.members["color"], c.members["brightness"]
                wr += [f"if _hw_{v} is not None:",
                       f"    k = ({v}.alive and {v}.on, {v}.{cm.name}, {v}.{bm.name})",
                       f"    if k != _ST.get('px_{v}'):",
                       f"        _ST['px_{v}'] = k",
                       f"        s = max(0, min(255, int(to_num({v}.{bm.name})))) / 255 if ({v}.alive and {v}.on) else 0",
                       f"        _hw_{v}.fill(tuple(int(ch * s) for ch in color({v}.{cm.name})))",
                       f"        _hw_{v}.write()"]
            elif r in ("oled", "lcd"):
                tm = c.members["text"]
                wr += [f"if time.ticks_diff(now, _ST.get('disp_{v}', -500)) >= 500:",
                       f"    _ST['disp_{v}'] = now",
                       f"    l1 = str({v}.{tm.name}) or _ST.get('last_log', '')"]
                rows = []
                for s in b.comps:
                    if s.role in ("temp", "dht", "distance", "light", "pot", "analog", "pir"):
                        for f, m in s.members.items():
                            if f != "alive" and m.ctype != "String":
                                rows.append((f"{s.e.name[:8]} {f[:4]}", f"{s.var}.{m.name}"))
                rows = rows[:3]
                if r == "oled":
                    wr += [f"    if _hw_{v} is not None:",
                           f"        _hw_{v}.fill(0)",
                           f"        _hw_{v}.text(l1[:16], 0, 0)"]
                    for i, (lab, code) in enumerate(rows):
                        wr.append(f"        _hw_{v}.text('{lab}: ' + str({code}), 0, {12 * (i + 1)})")
                    wr.append(f"        _hw_{v}.show()")
                else:
                    wr += [f"    if l1 != _ST.get('lcd_{v}'):", f"        _ST['lcd_{v}'] = l1",
                           f"        print('# {c.e.name} (lcd): ' + l1)"]
        L += ["    " + s for s in wr] or ["    pass"]
        return L

    # ======================================================================
    # PlatformIO / Wokwi
    # ======================================================================
    def _pio_libs(self, b: Board) -> list[str]:
        m = {"DHT sensor library": "adafruit/DHT sensor library@^1.4.6",
             "ESP32Servo": "madhephaestus/ESP32Servo@^3.0.5",
             "Servo": "arduino-libraries/Servo@^1.2.1",
             "Adafruit NeoPixel": "adafruit/Adafruit NeoPixel@^1.12.0",
             "Adafruit SSD1306": "adafruit/Adafruit SSD1306@^2.5.9",
             "Adafruit GFX Library": "adafruit/Adafruit GFX Library@^1.11.9",
             "LiquidCrystal I2C": "marcoschwartz/LiquidCrystal_I2C@^1.1.4",
             "PubSubClient": "knolleary/PubSubClient@^2.8"}
        return [m[x] for x in sorted(b.libs) if x in m]

    def _pio(self, b: Board) -> str:
        platform, board, env = b.spec["pio"]
        L = ["; PlatformIO project (free): pio run  |  pio run -t upload  |  pio device monitor",
             "[platformio]", f"default_envs = {env}", "src_dir = .", "",
             f"[env:{env}]"]
        if platform == "raspberrypi":
            L += ["platform = https://github.com/maxgerhardt/platform-raspberrypi.git",
                  f"board = {board}", "framework = arduino", "board_build.core = earlephilhower"]
        else:
            L += [f"platform = {platform}", f"board = {board}", "framework = arduino"]
        L += [f"monitor_speed = {self._baud(b)}", "build_src_filter = -<*> +<*.ino>"]
        libs = self._pio_libs(b)
        if libs:
            L.append("lib_deps =")
            L += [f"    {x}" for x in libs]
        return "\n".join(L) + "\n"

    def _wokwi_libs(self, b: Board) -> set[str]:
        libs = set(b.libs)
        if "DHT sensor library" in libs:
            libs.add("Adafruit Unified Sensor")
        return libs

    def _wokwi_toml(self, b: Board) -> str:
        env = b.spec["pio"][2]
        fw = b.spec["fw"]
        return ("# Wokwi for VS Code (free): run `pio run` first, then \"Wokwi: Start Simulator\".\n"
                "# arduino-cli users: point these at build/<fqbn>/<sketch>.ino.hex|.bin|.elf instead.\n"
                "[wokwi]\nversion = 1\n"
                f"firmware = '.pio/build/{env}/firmware.{fw}'\n"
                f"elf = '.pio/build/{env}/firmware.elf'\n")

    def _wpin(self, b: Board, p: str) -> str:
        wid = b.spec["wid"]
        if b.key in ("pico", "pico_w"):
            return f"{wid}:GP{p}"
        if b.key == "esp32":
            p = {"36": "VP", "39": "VN"}.get(p, p)
        return f"{wid}:{p}"

    def _diagram(self, b: Board) -> str:
        sp = b.spec
        wid = sp["wid"]
        gnd = f"{wid}:{sp['gnd']}"
        vcc = f"{wid}:{sp['vcc']}"
        vcc5 = f"{wid}:{sp['vcc5']}"
        parts = [{"type": sp["wokwi"], "id": wid, "top": 0, "left": 0, "attrs": {}}]
        conns: list[list] = []
        if b.key == "esp32":
            conns += [[f"{wid}:TX", "$serialMonitor:RX", "", []], [f"{wid}:RX", "$serialMonitor:TX", "", []]]
        elif b.key in ("pico", "pico_w"):
            conns += [[f"{wid}:GP0", "$serialMonitor:RX", "", []], [f"{wid}:GP1", "$serialMonitor:TX", "", []]]
        i2c = [self._wpin(b, sp["i2c"][0]), self._wpin(b, sp["i2c"][1])]
        slot = 0
        used_ids = {wid}

        def place(ptype: str, c: Comp, attrs: dict | None = None) -> str:
            nonlocal slot
            pid = c.var[:20]
            while pid in used_ids:
                pid += "_"
            used_ids.add(pid)
            col, row = slot % 6, slot // 6
            slot += 1
            parts.append({"type": ptype, "id": pid, "top": -260 - row * 230, "left": col * 140 - 40,
                          "attrs": attrs or {}})
            return pid

        def w(a: str, z: str, color: str = "green"):
            conns.append([a, z, color, []])

        for c in b.comps:
            r = c.role
            P = self._wpin(b, c.pins["pin"]) if "pin" in c.pins else None
            color = str(c.e.props.get("color", "")).lower()
            if r == "led":
                pid = place("wokwi-led", c, {"color": color if color in COLORS else "red", "label": c.e.name})
                w(f"{pid}:A", P)
                w(f"{pid}:C", gnd, "black")
            elif r in ("motor", "fan"):
                # Wokwi has no DC motor part: a blue LED shows the PWM output instead
                pid = place("wokwi-led", c, {"color": "blue", "label": f"{c.e.name} ({r})"})
                w(f"{pid}:A", P)
                w(f"{pid}:C", gnd, "black")
            elif r == "button":
                pid = place("wokwi-pushbutton", c, {"color": color if color in COLORS else "green",
                                                     "label": c.e.name})
                w(f"{pid}:1.l", P)
                w(f"{pid}:2.l", gnd, "black")
            elif r == "switch":
                pid = place("wokwi-slide-switch", c)
                w(f"{pid}:2", P)
                w(f"{pid}:1", gnd, "black")
            elif r == "pir":
                pid = place("wokwi-pir-motion-sensor", c)
                w(f"{pid}:VCC", vcc5, "red")
                w(f"{pid}:OUT", P)
                w(f"{pid}:GND", gnd, "black")
            elif r == "temp":
                pid = place("wokwi-ntc-temperature-sensor", c)
                w(f"{pid}:VCC", vcc, "red")
                w(f"{pid}:OUT", P)
                w(f"{pid}:GND", gnd, "black")
            elif r == "dht":
                pid = place("wokwi-dht22", c)
                w(f"{pid}:VCC", vcc, "red")
                w(f"{pid}:SDA", P)
                w(f"{pid}:GND", gnd, "black")
            elif r == "distance":
                pid = place("wokwi-hc-sr04", c)
                w(f"{pid}:VCC", vcc5, "red")
                w(f"{pid}:TRIG", self._wpin(b, c.pins["trig"]))
                w(f"{pid}:ECHO", self._wpin(b, c.pins["echo"]), "orange")
                w(f"{pid}:GND", gnd, "black")
            elif r == "light":
                pid = place("wokwi-photoresistor-sensor", c)
                w(f"{pid}:VCC", vcc, "red")
                w(f"{pid}:AO", P)
                w(f"{pid}:GND", gnd, "black")
            elif r in ("pot", "analog"):
                pid = place("wokwi-potentiometer", c)   # analog stand-in: turn the knob to change the value
                w(f"{pid}:VCC", vcc, "red")
                w(f"{pid}:SIG", P)
                w(f"{pid}:GND", gnd, "black")
            elif r == "servo":
                pid = place("wokwi-servo", c)
                w(f"{pid}:V+", vcc5, "red")
                w(f"{pid}:PWM", P, "orange")
                w(f"{pid}:GND", gnd, "black")
            elif r == "relay":
                pid = place("wokwi-relay-module", c)
                w(f"{pid}:VCC", vcc5, "red")
                w(f"{pid}:IN", P)
                w(f"{pid}:GND", gnd, "black")
            elif r == "buzzer":
                pid = place("wokwi-buzzer", c, {"volume": "0.1"})
                w(f"{pid}:2", P)
                w(f"{pid}:1", gnd, "black")
            elif r == "neopixel":
                n = c.e.props.get("pixels", 1)
                n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1
                if n > 1:
                    pid = place("wokwi-led-ring", c, {"pixels": str(n)})
                    w(f"{pid}:VCC", vcc5, "red")
                    w(f"{pid}:DIN", P)
                    w(f"{pid}:GND", gnd, "black")
                else:
                    pid = place("wokwi-neopixel", c)
                    w(f"{pid}:VDD", vcc5, "red")
                    w(f"{pid}:DIN", P)
                    w(f"{pid}:VSS", gnd, "black")
            elif r == "oled":
                pid = place("board-ssd1306", c, {"i2cAddress": "0x3c"})
                w(f"{pid}:VCC", vcc, "red")
                w(f"{pid}:GND", gnd, "black")
                w(f"{pid}:SDA", i2c[0], "blue")
                w(f"{pid}:SCL", i2c[1], "purple")
            elif r == "lcd":
                pid = place("wokwi-lcd1602", c, {"pins": "i2c"})
                w(f"{pid}:VCC", vcc5, "red")
                w(f"{pid}:GND", gnd, "black")
                w(f"{pid}:SDA", i2c[0], "blue")
                w(f"{pid}:SCL", i2c[1], "purple")
        doc = {"version": 1, "author": "NLCL", "editor": "wokwi", "parts": parts, "connections": conns,
               "dependencies": {}}
        return json.dumps(doc, indent=2) + "\n"

    # ======================================================================
    # README
    # ======================================================================
    def _readme(self, ctx: BuildContext, boards: list[Board]) -> str:
        L = [f"# {ctx.system.name} - embedded firmware", "",
             "Generated by NLCL. Each folder is one board; every file is plain source you can edit.", ""]
        for b in boards:
            sp = b.spec
            d = b.ident
            core = sp["fqbn"].split(":")[0] + ":" + sp["fqbn"].split(":")[1]
            L += [f"## `{d}/` - {sp['title']}" + (" (implicit default board)" if b.entity is None else ""), ""]
            L += ["| entity | kind | role | pins | state |", "|---|---|---|---|---|"]
            for c in b.comps:
                pins = ", ".join(f"{k}={v}" for k, v in c.pins.items()) or "-"
                st = ", ".join(f"{m.field}" for m in c.members.values() if m.field != "alive")
                L.append(f"| {c.e.name} | {c.e.kind} | {c.role} | {pins} | {st} |")
            L.append("")
            if b.warnings:
                L.append("**Notes:**")
                L += [f"- {w}" for w in dict.fromkeys(b.warnings)]
                L.append("")
            L += ["### Simulate for free (no hardware)", ""]
            if sp["wokwi"]:
                L += [f"1. Open https://wokwi.com and start a new **{sp['title']}** Arduino project.",
                      f"2. Paste `{d}.ino` into `sketch.ino`, replace `diagram.json` with ours"
                      + (", and add `libraries.txt`." if b.libs else "."),
                      "3. Press the green Play button. The Serial Monitor shows logs and channel JSON; "
                      "type a command (e.g. a button name) to fire `input:` behaviours.",
                      "",
                      "VS Code: install the free *Wokwi Simulator* extension, run `pio run` in this folder, then "
                      "*Wokwi: Start Simulator* (uses `wokwi.toml`).", ""]
                if any(c.role in ("motor", "fan") for c in b.comps):
                    L += ["Wokwi has no DC motor part: motors/fans are shown as blue LEDs whose brightness is the PWM "
                          "output.", ""]
                if any(c.role == "analog" for c in b.comps):
                    L += ["Generic analog sensors are simulated with a potentiometer.", ""]
                if b.key in ("pico", "pico_w"):
                    L += ["Wokwi also runs `main.py` on a MicroPython Pico project.", ""]
                elif b.key == "esp32":
                    L += ["Wokwi also runs `main.py` on a MicroPython ESP32 project.", ""]
            else:
                L += [f"Wokwi has no {sp['title']} part; use real hardware or switch the board kind to esp32/uno.", ""]
            L += ["### Build and upload", "", "```sh",
                  "# arduino-cli (free)"]
            if b.key in ("esp32",):
                L.append("arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json")
            elif b.key == "esp8266":
                L.append("arduino-cli config add board_manager.additional_urls https://arduino.esp8266.com/stable/package_esp8266com_index.json")
            elif b.key in ("pico", "pico_w"):
                L.append("arduino-cli config add board_manager.additional_urls https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json")
            elif b.key == "stm32":
                L.append("arduino-cli config add board_manager.additional_urls https://github.com/stm32duino/BoardManagerFiles/raw/main/package_stmicroelectronics_index.json")
            L.append(f"arduino-cli core install {core}")
            if b.libs:
                L.append("arduino-cli lib install " + " ".join(f'"{x}"' for x in sorted(self._wokwi_libs(b))))
            L += [f"arduino-cli compile --fqbn {sp['fqbn']} {d}",
                  f"arduino-cli upload -p <PORT> --fqbn {sp['fqbn']} {d}",
                  f"arduino-cli monitor -p <PORT> -c baudrate={self._baud(b)}", "",
                  "# or PlatformIO (free)", f"pio run -d {d} -t upload && pio device monitor -b {self._baud(b)}", "```", ""]
            if b.libs:
                L += ["Optional libraries are switched on by `#define NLCL_USE_* 1` at the top of the sketch; set a flag "
                      "to 0 to build with only the core (that part is then simulated or disabled).", ""]
            if sp["upy"]:
                L += ["### MicroPython", "",
                      "```sh",
                      "pip install mpremote esptool        # free tools",
                      f"mpremote cp {d}/main.py :main.py   # then press reset",
                      "mpremote repl                        # logs + channel JSON",
                      "```", ""]
                if b.by_role("oled"):
                    L += ["OLED: `mpremote mip install ssd1306` first.", ""]
                if any(x.transport == "mqtt" for x in b.chans):
                    L += ["MQTT: `mpremote mip install umqtt.simple` if your firmware lacks it.", ""]
            if b.chans:
                L += ["### Channels", "", "| channel | direction | transport | fields | details |", "|---|---|---|---|---|"]
                for x in b.chans:
                    ch = x.ch
                    dirn = "local" if x.local else ("out" if x.out else "in")
                    det = ""
                    if x.transport == "serial":
                        det = f"JSON line @ {ch.config.get('baud', 115200)} baud (USB serial)"
                    elif x.transport == "http":
                        det = f"hub `http://HUB_HOST:{str(ch.hub_url).rsplit(':', 1)[-1]}{ch.config.get('path', '')}`"
                    elif x.transport == "mqtt":
                        det = f"`{ch.config.get('broker')}:{ch.config.get('broker_port')}` topic `{ch.topic}`"
                    elif x.transport == "udp":
                        det = f"UDP port {ch.config.get('port', 9876)} on HUB_HOST"
                    elif x.transport == "i2c":
                        det = f"I2C master, slave address {self._i2c_addr(ch)}"
                    elif x.transport == "spi":
                        det = f"SPI master, CS pin {x.pins.get('cs', '?')}, mode 0, 1 MHz"
                    elif x.transport == "gpio":
                        det = "pins " + ", ".join(f"{f}={p}" for f, p in x.pins.items())
                    L.append(f"| {ch.id} ({ch.src.name} -> {ch.dst.name}) | {dirn} | {x.transport} | "
                             f"{', '.join(ch.fields)} | {det} |")
                L.append("")
                if any(x.transport in ("i2c", "spi") for x in b.chans):
                    L += ["**Bus byte protocol** (board is the master; same frame as the hardware module): a write is "
                          f"`0x{FRAME_SYNC:02X}, N, then N x int16 big-endian` (fields in channel order, values rounded, "
                          "true = 1). To read, I2C requests `2 + 2N` bytes and expects the same frame back; SPI sends "
                          f"`0x{FRAME_READ:02X}` then clocks `1 + 2N` bytes (`0x{FRAME_SYNC:02X}` + values).", ""]
                if any(x.transport == "gpio" for x in b.chans):
                    L += ["**GPIO channels**: one wire per field, HIGH when the value is non-zero.", ""]
                if any(x.transport in ("http", "udp", "mqtt") and not x.local for x in b.chans) and b.wifi:
                    L += ["Set `WIFI_SSID`, `WIFI_PASS` and `HUB_HOST` at the top of the sketch (and in `main.py`). "
                          "`HUB_HOST` must be the LAN IP of the PC running the python hub (`python main.py` in the "
                          "python folder) - `localhost` would be the board itself.", ""]
        L += ["## Behaviour semantics", "",
              "- `start` runs at the end of `setup()`; `update` runs 20 times per second; `timer:<ms>` uses `millis()`.",
              "- `update` rules with a condition and a one-shot action (log, send, toggle, add, call, destroy) fire once "
              "each time the condition becomes true; `set`/`move` rules apply continuously while it holds.",
              "- `input:<name>` fires on the debounced press of that button/switch, or when that word is typed on the "
              "serial monitor.",
              "- `change:<prop>` fires when the value changes; `message:<sender>` when a channel delivers data.",
              "- `destroy` disables the entity: its outputs switch off and its behaviours stop.", ""]
        return "\n".join(L)

    # ======================================================================
    # run
    # ======================================================================
    def run(self, ctx: BuildContext) -> RunResult | None:
        cli = self.which("arduino-cli", "arduino-cli.exe")
        if not cli:
            return None
        try:
            boards = self._plan(ctx)
        except Exception as e:  # never crash the runtime
            return RunResult(False, f"planning failed: {e}")
        outs, ok, cmds = [], True, []
        for b in boards:
            d = ctx.outdir / b.ident
            if not (d / f"{b.ident}.ino").exists():
                return RunResult(False, "build first")
            r = self.sh([cli, "compile", "--fqbn", b.spec["fqbn"], str(d)], ctx.outdir, timeout=300)
            ok = ok and r.ok
            cmds.append(r.command)
            outs.append(f"== {b.ident} ({b.spec['fqbn']})\n{r.output}")
        return RunResult(ok, "\n".join(outs), " && ".join(cmds))


MODULE = EmbeddedModule()
