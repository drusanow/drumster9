# =====================================================================

#drumster8.py - touchscreen drum machine for Tulip CC
# =====================================================================
# Install: copy this "drumster8" folder to /user, then:  run('drumster8.py')
#
#drumster8 is a 32 step Drum Machine for the Tulip Creative Computer. It uses the drum kits and 
#samples that are in the Tulip CC and AMYboard. It has 4 assignable FX Buses with Reverb and Delay
#as well as a filter and drive. 
#
# =====================================================================
#created by David Rusanow 10/08/26
# =====================================================================

from tulip import UIScreen, UIElement, pal_to_lv, lv_depad, lv
import tulip
import amy
import sequencer
import random

try:
    import ujson as json
except ImportError:
    import json

try:
    import os
except ImportError:
    os = None

try:
    import gc
except ImportError:
    gc = None


# --- configuration -----------------------------------------------------

# Each of our 7 drum lanes gets ONE fixed PCM oscillator, forever - no
# shared "kit synth", no oscillator-table budget to manage. This is the
# thing the original 7-fixed-synth idea got right in spirit and wrong in
# execution: it's correct for one-shot PCM samples (~1 oscillator each,
# cheap), it was only unsafe applied to whole GM drum kits (each kit
# needs its own set of oscillators internally, so 7 kits overran the
# table - see the sound engine section below for the full story).
PCM_OSC_BASE = 20        # lanes use oscillators 20..26
TEST_OSC = PCM_OSC_BASE + 7   # scratch oscillator for kit auditioning,
#                                outside the live lanes so previewing a
#                                kit never disturbs what's playing
NO_SAMPLE = -1            # this kit has no sample for this drum role

# How long to wait after amy.reset() before (re)configuring oscillators.
# See start_engine() for why - this is a hypothesis about hardware
# settling time, not a confirmed AMY behavior. If drums are still silent
# right after startup/RESTART, try raising this.
ENGINE_SETTLE_MS = 60

# Optional performance instrumentation. Off by default: serial printing
# on a hot path is itself expensive. When True, FX/bus AMY sends are
# counted so you can confirm from the REPL that one knob = one message:
#     import drumster8; drumster8.DEBUG_PERFORMANCE = True
#     ... tweak an FX knob ...
#     drumster8.perf_report()
DEBUG_PERFORMANCE = False
_fx_msg_count = 0


def _amy_fx(**kw):
    """amy.send for FX/bus messages, with an optional counter. The
    counter only does work when DEBUG_PERFORMANCE is on."""
    if DEBUG_PERFORMANCE:
        global _fx_msg_count
        _fx_msg_count += 1
    amy.send(**kw)


def perf_report():
    """Print and reset the FX message counter (needs DEBUG_PERFORMANCE)."""
    global _fx_msg_count
    print("FX/bus AMY messages since last report:", _fx_msg_count)
    _fx_msg_count = 0

NUM_BUSES = 4
NUM_STEPS = 32           # fixed 32-step pattern (one bar of 1/32 notes)
PROJECTS_FILE = "/user/tulipdrums_projects.json"   # the whole library
BANK_LETTERS = "ABCDEFGHIJ"
NUM_BANKS = len(BANK_LETTERS)

# --- sample kits: raw PCM presets, not GM synth patches -----------------
#
# Each entry maps our 7 drum roles to a specific AMY PCM preset number.
# Presets are verified against AMY's own manifest for the Gamma9001
# sample banks (github.com/shorepine/amy/blob/main/sounds/gamma9001/
# manifest.json) and docs/synth.md, which give the preset ranges:
#   TR-808        0-18    (19 samples, all present here)
#   TR-909      256-272   (17 samples, all present here)
#   Linn 9000   273-282   (10 samples - no dedicated clap sample)
#   Univox MR-12 283-286  (4 samples only - kick/snare/CHat/OHat; the
#                          MR-12 hardware itself never had a clap,
#                          cymbal or tom, so those roles are silent by
#                          request rather than left playing a wrong
#                          sound)
#   Tokyo Synthetics 287-310 (24 samples - this is a glitch/FX bank, not
#                          a drum kit, so there's no "correct" mapping.
#                          The picks below are a deliberate creative
#                          choice, not a documented one - change any
#                          preset number to taste, the manifest lists
#                          all 24: metallic/static/wooden/BD/tokyo-*/
#                          hizz/pew/boink/blop/click, presets 287-310
#                          in that order)
#   80s Power Kit 311-330 (20 samples - this bank's own root notes ARE
#                          GM note numbers, e.g. root 36=kick, 38=snare,
#                          42=closed hat, 46=open hat, 49=crash, so
#                          these preset picks are exact GM-note matches,
#                          not guesses)
#   Percussion  331-374  (44 samples - congas/bongos/tabla/timbales/
#                          timpani/triangle/etc, no natural kick or snare)
#   Acoustic    375-377  (3 samples - tambourine + 2 shakers)
#   Extras      378-391  (14 samples - ambient/FX one-shots: laser,
#                          mach3, silver, dim future, etc; not used
#                          below, but valid presets if you want them)
#
# You supplied the complete manifest (155 samples total, ending exactly
# at preset 391 - confirms the doc's stated range), so the Percussion
# "kit" below is no longer a guess: the bank has samples literally named
# "Noice Kick" and "Old Snare", and the Acoustic bank's two shakers make
# a natural closed/open hat pair.
#
# To change any sample: replace the preset number here (and update the
# comment). PCM presets have no oscillator-budget risk the way loading
# a full GM kit did - this table can be edited freely.
SAMPLE_KITS = [
    ("TR-808", {
        "Kick": 0, "Snare": 12, "Clap": 3, "CHat": 9, "OHat": 10,
        "Cymbal": 18, "Tom": 16,
    }),
    ("TR-909", {
        "Kick": 256, "Snare": 268, "Clap": 258, "CHat": 260, "OHat": 263,
        "Cymbal": 259, "Tom": 271,
    }),
    ("Linn 9000", {
        "Kick": 273, "Snare": 280, "Clap": NO_SAMPLE, "CHat": 277,
        "OHat": 278, "Cymbal": 276, "Tom": 282,
    }),
    ("MR-12", {
        "Kick": 285, "Snare": 286, "Clap": NO_SAMPLE, "CHat": 283,
        "OHat": 284, "Cymbal": NO_SAMPLE, "Tom": NO_SAMPLE,
    }),
    ("Tokyo Syn", {
        # creative pick, not a documented mapping - see note above
        "Kick": 297,     # Tokyo Deep bd
        "Snare": 296,    # Tokyo Burst bd
        "Clap": 300,     # Hizz 06
        "CHat": 309,     # Click 08
        "OHat": 289,     # Static 07
        "Cymbal": 299,   # Tokyo Space
        "Tom": 303,      # Boink 04
    }),
    ("80s Power", {
        # exact GM-note matches (this bank's root notes ARE GM numbers)
        "Kick": 312,     # Metal Kick, root 36
        "Snare": 314,    # Power Snare, root 38
        "Clap": 315,     # Hand Claps, root 39
        "CHat": 318,     # Tight HiHat, root 42
        "OHat": 322,     # Open HiHat, root 46
        "Cymbal": 325,   # Crash Cymbal, root 49
        "Tom": 321,      # Process Tom 3, root 45
    }),
    ("Percussion", {
        # you provided the full manifest, so this is exact now, not a
        # guess - the bank actually has a literal "Kick" and "Snare"
        "Kick": 350,     # Noice Kick
        "Snare": 351,    # Old Snare
        "Clap": 375,     # Tambourine Short (acoustic bank)
        "CHat": 377,     # Shaker Tiny (acoustic bank) - shortest/tightest
        "OHat": 376,     # Shaker Short (acoustic bank) - longer decay
        "Cymbal": 372,   # Triangle 002 - longest metallic-ish decay
        "Tom": 368,      # Timbales 004
    }),
]
KITS = [(i, name) for i, (name, _roles) in enumerate(SAMPLE_KITS)]

# name, GM note (reference only - PCM oscillators play at native pitch,
# so this is no longer used to address AMY, just documents each role),
# default volume
ELEMENTS = [
    ("Kick",   36, 0.90),
    ("Snare",  38, 0.75),
    ("Clap",   39, 0.55),
    ("CHat",   42, 0.45),
    ("OHat",   46, 0.40),
    ("Cymbal", 49, 0.45),
    ("Tom",    45, 0.50),
]

RANDOM_RANGES = {
    "Kick": (3, 6), "Snare": (1, 3), "Clap": (0, 3), "CHat": (4, 10),
    "OHat": (0, 4), "Cymbal": (0, 2), "Tom": (0, 4),
}

PRESETS = [
    ("4 on Floor", {
        "Kick": [0, 8, 16, 24], "Snare": [8, 24], "Clap": [8, 24],
        "CHat": [0, 4, 8, 12, 16, 20, 24, 28], "OHat": [6, 14, 22, 30],
        "Cymbal": [0], "Tom": []}),
    ("Floor+Antic", {
        "Kick": [0, 8, 16, 24, 28], "Snare": [8, 24], "Clap": [8, 24],
        "CHat": [0, 4, 8, 12, 16, 20, 24, 28], "OHat": [2, 10, 18, 26],
        "Cymbal": [0, 16], "Tom": [30]}),
    ("Half Time", {
        "Kick": [0, 16], "Snare": [16], "Clap": [],
        "CHat": [0, 4, 8, 12, 16, 20, 24, 28], "OHat": [12, 28],
        "Cymbal": [0], "Tom": [24]}),
    ("Syncopa", {
        "Kick": [0, 12, 24], "Snare": [8, 24], "Clap": [24],
        "CHat": [0, 6, 12, 18, 24, 30], "OHat": [3, 9, 15, 21, 27],
        "Cymbal": [0], "Tom": [20]}),
    ("Breakbeat", {
        "Kick": [0, 10, 16, 22], "Snare": [8, 24, 26], "Clap": [8],
        "CHat": [2, 6, 10, 14, 18, 22, 26, 30], "OHat": [4, 20],
        "Cymbal": [0], "Tom": [14, 30]}),
    ("Offbeat", {
        "Kick": [4, 8, 20, 24, 28], "Snare": [8, 24], "Clap": [8, 24],
        "CHat": [0, 4, 8, 12, 16, 20, 24, 28], "OHat": [2, 10, 18, 26],
        "Cymbal": [], "Tom": [30]}),
    ("Double", {
        "Kick": [0, 4, 8, 12, 16, 20, 24, 28], "Snare": [8, 24],
        "Clap": [8, 24],
        "CHat": [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30],
        "OHat": [], "Cymbal": [0], "Tom": []}),
    ("Sparse", {
        "Kick": [0, 20], "Snare": [16], "Clap": [],
        "CHat": [0, 8, 16, 24], "OHat": [28], "Cymbal": [], "Tom": [10]}),
]

MIN_BPM = 40
MAX_BPM = 240
BPM_STEP = 1             # a single tap of +/- nudges by this
BPM_STEP_FAST = 10       # holding +/- repeats at this rate instead
VOL_STEP = 0.05
MUTE_VEL = 0.001         # vel=0 is a note-off, so silence is a tiny vel

FILTER_NAMES = ["OFF", "LPF", "HPF"]
BUS_FX_DEFAULTS = {
    "rev_level": 0.0, "rev_liveness": 0.75, "rev_damping": 0.15,
    "echo_level": 0.0, "echo_ms": 250, "echo_fb": 0.3, "echo_tone": 0.0,
    "eq_l": 0.0, "eq_m": 0.0, "eq_h": 0.0,
    "filter_type": 0, "filter_freq": 4000, "resonance": 1.0,
    "drive": 1.0,
}
# All buses (including bus 0) start completely dry - reverb and echo
# at 0.00. Add FX per bus from the FX page.
#
# BUS_VOLUME is the single knob for overall loudness: it multiplies
# EVERYTHING on every bus at final mixdown (AMY's `volume` field has no
# documented upper limit, so values above 1.0 are valid - they add gain
# rather than clip immediately). 1.0 was AMY's own default and sounded
# quiet at the individual per-drum velocities this app uses (0.4-0.9,
# tuned to avoid seven-voice-kit overload on the old GM synth engine,
# which no longer applies now that each drum is its own PCM sample).
# Raised to 2.0 as a reasonable first boost. I can't verify actual
# output level or clipping without hardware - if it's still too quiet,
# raise further; if busy patterns start sounding harsh/distorted,
# that's clipping and this is the number to bring back down.
BUS_VOLUME = 3.0         # per-bus mixdown level into the final output


# --- layout (1024 x 600) ----------------------------------------------

HDR_H = 60
HDR_BTN_H = 48
ROW_H = 50
STEP_W = 24
STEP_RECT_W = 21
STEP_H = 40
BUS_ROW_H = 56
LANE_BTN_H = 38
BTN_D = 28
LED_ROW_H = 22          # row of 32 position LEDs above the grid
LED_W = 21              # each LED, aligned under its step column
LED_H = 12

X_LABEL = 0
X_MUTE = 56
X_SOLO = 88
X_RND = 120
X_VOL = 152
X_KIT = 184
X_STEPS = 224
ROW_W = X_STEPS + NUM_STEPS * STEP_W + 8

# Worked back from the screen bottom:
#   20 + 2*60 + 22(LED) + 8 + 7*50 + 12 + 56 = 578 of 600
SCREEN_TOP = 20
GAP_HEADER_TO_LED = 4
GAP_LED_TO_GRID = 8
GAP_GRID_TO_BUS = 12


def rgb332(r, g, b):
    return ((r >> 5) << 5) | ((g >> 5) << 2) | (b >> 6)


C_CELL_OFF = rgb332(45, 45, 55)
C_CELL_BEAT = rgb332(85, 85, 100)
C_CELL_ON = rgb332(0, 210, 90)
C_LED_OFF = rgb332(30, 60, 40)         # step LED, idle
C_LED_ON = rgb332(60, 255, 120)        # step LED, lit as the bar plays
C_LED_BEAT = rgb332(50, 90, 70)        # every 4th LED, idle (beat marker)
C_BTN = rgb332(60, 110, 200)
C_MUTE_ON = rgb332(220, 60, 50)
C_SOLO_ON = rgb332(240, 190, 40)
C_PANEL = rgb332(40, 40, 52)
C_PLAY = rgb332(40, 170, 90)
C_PENDING = rgb332(220, 180, 40)
C_KIT_BTN = rgb332(70, 70, 90)
C_BANK_EMPTY = rgb332(35, 35, 42)
C_KIT_ON = rgb332(150, 90, 200)
C_RESTART = rgb332(200, 40, 40)
C_FLASH = rgb332(255, 140, 0)     # brief orange press-feedback flash
C_BUS = [rgb332(60, 110, 200), rgb332(200, 120, 40),
         rgb332(40, 160, 130), rgb332(170, 70, 150)]

app = None


# Colour cache. pal_to_lv() allocates a new LVGL colour object every
# call. Cell.redraw() runs ~14 times per step on the sequencer callback,
# so calling pal_to_lv there churned the heap and made GC pauses pile up
# over time - the app "ran fine for a while" then the tempo dragged and
# every action got laggy. There are only a handful of distinct palette
# colours, so memoise them once and never allocate on the hot path
# again.
_LVCOLOR = {}


def lv_color(c):
    v = _LVCOLOR.get(c)
    if v is None:
        v = pal_to_lv(c)
        _LVCOLOR[c] = v
    return v


# --- small UI helpers --------------------------------------------------

def strut(parent, height, width=1):
    """Invisible child that forces `parent` to be `height` tall.

    The screen stacks elements by content height, so without this an
    empty element collapses and set_size() appears to do nothing."""
    o = lv.obj(parent)
    o.set_size(width, height)
    o.align_to(parent, lv.ALIGN.LEFT_MID, 0, 0)
    try:
        o.set_style_bg_opa(0, 0)
        o.set_style_border_width(0, 0)
    except Exception:
        pass
    lv_depad(o)
    try:
        o.remove_flag(lv.obj.FLAG.SCROLLABLE)
        o.remove_flag(lv.obj.FLAG.CLICKABLE)
    except Exception:
        pass
    return o


# --- GC-safe deferred callbacks ----------------------------------------
#
# tulip.defer() stores the callback in a plain C array (defer_callbacks[]
# in tulip/shared/tsequencer.c). That array lives in .bss, which the
# ESP32 port's gc_collect() does NOT scan - it scans registers and the C
# stack only. So a Python object whose ONLY reference is that array is
# invisible to the garbage collector: if a collection runs during the
# defer window, the object is freed, its memory reused, and when the
# timer expires Tulip calls mp_sched_schedule() on a dangling pointer.
# That is a C-level crash - instant reboot, no traceback, nothing in the
# REPL.
#
# It bites hardest with freshly-made closures (Button.flash()'s _revert),
# because nothing else refers to them. It is intermittent only because
# the conservative stack scan often keeps the object alive by accident,
# via a stale stack slot - until a deeper/allocation-heavy call (a big
# json.dumps, say) overwrites that slot. Hence "fine twice, dies on the
# third save".
#
# defer_safe() keeps a real Python reference in _pending_defers until the
# callback actually fires. Always use it instead of tulip.defer().

_pending_defers = {}
_defer_seq = 0


def defer_try(fn, arg, ms):
    """Schedule fn. Returns True if it was scheduled, False if there was
    no free defer slot (Tulip has 32). Never runs fn inline - callers
    that chain onto themselves must not recurse."""
    global _defer_seq
    _defer_seq += 1
    key = _defer_seq

    def _wrapper(a=None, _key=key, _fn=fn):
        _pending_defers.pop(_key, None)
        try:
            _fn(a)
        except Exception as ex:
            print("deferred callback error:", ex)

    _pending_defers[key] = _wrapper
    try:
        tulip.defer(_wrapper, arg, ms)
        return True
    except Exception as ex:
        _pending_defers.pop(key, None)
        print("defer failed (no slot?):", ex)
        return False


def defer_safe(fn, arg, ms):
    """defer_try, falling back to running fn now rather than dropping
    it. Safe for one-shot callbacks; NOT for self-chaining ones."""
    if defer_try(fn, arg, ms):
        return
    try:
        fn(arg)
    except Exception as ex:
        print("inline callback error:", ex)


class Button:
    """`repeat_cb`, if given, turns this into a hold-to-repeat button:
    a quick tap runs `cb` once, holding it down runs `repeat_cb` over and
    over until you let go. That uses LVGL's own long-press repeat, so the
    delay before it kicks in and the repeat rate match the rest of the
    UI. Falls back to a plain CLICKED button if this LVGL build doesn't
    have the two events."""
    def __init__(self, parent, text, x, w, h, cb, color=C_BTN, y=0,
                  repeat_cb=None):
        self.cb = cb
        self.repeat_cb = repeat_cb
        self.color = color
        self.obj = lv.obj(parent)
        self.obj.set_size(w, h)
        self.obj.set_style_radius(5, 0)
        self.obj.set_style_bg_color(lv_color(color), 0)
        self.obj.align_to(parent, lv.ALIGN.LEFT_MID, x, y)
        wired = False
        if repeat_cb is not None:
            try:
                # SHORT_CLICKED, not CLICKED: CLICKED also fires when you
                # release after a long press, which would add a stray
                # single step on top of everything the repeat already did
                self.obj.add_event_cb(self._cb, lv.EVENT.SHORT_CLICKED, None)
                self.obj.add_event_cb(self._on_repeat,
                                       lv.EVENT.LONG_PRESSED_REPEAT, None)
                wired = True
            except Exception:
                wired = False
        if not wired:
            self.obj.add_event_cb(self._cb, lv.EVENT.CLICKED, None)
        self.obj.remove_flag(lv.obj.FLAG.SCROLLABLE)
        lv_depad(self.obj)
        self.label = lv.label(self.obj)
        self.label.set_text(text)
        self.label.align_to(self.obj, lv.ALIGN.CENTER, 0, 0)

    def _cb(self, e):
        try:
            self.cb(e)
        except Exception as ex:
            print("button error:", ex)

    def _on_repeat(self, e):
        try:
            self.repeat_cb(e)
        except Exception as ex:
            print("button repeat error:", ex)

    def set_color(self, c):
        self.color = c
        try:
            self.obj.set_style_bg_color(lv_color(c), 0)
        except Exception:
            pass

    def set_text(self, t):
        try:
            self.label.set_text(t)
        except Exception:
            pass

    def flash(self, color=None, ms=700):
        """Briefly show `color` (default: orange) as press feedback,
        then revert to whatever this button's normal colour is *at that
        later moment* - self.color (used for state-driven recolouring
        elsewhere, e.g. the pattern bank) is read only when reverting,
        never overwritten here, so a flash never fights a state change
        that happens to land during the same window."""
        if color is None:
            color = C_FLASH
        try:
            self.obj.set_style_bg_color(lv_color(color), 0)
        except Exception:
            pass

        def _revert(arg=None):
            try:
                self.obj.set_style_bg_color(lv_color(self.color), 0)
            except Exception:
                pass
        # defer_safe, not tulip.defer: _revert is a brand-new closure and
        # the defer table is not a GC root (see defer_safe above)
        defer_safe(_revert, None, ms)


class Spacer(UIElement):
    def __init__(self, height):
        super().__init__()
        self.group.set_size(ROW_W, height)
        lv_depad(self.group)
        self.group.remove_flag(lv.obj.FLAG.SCROLLABLE)
        try:
            self.group.set_style_bg_opa(0, 0)
        except Exception:
            pass
        strut(self.group, height)


def clampf(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


# =====================================================================
# SOUND ENGINE  -  ONE FIXED PCM OSCILLATOR PER LANE
#
# Each of the 7 drum lanes owns ONE oscillator, forever (PCM_OSC_BASE +
# row index). Changing a lane's kit or bus is a couple of short osc-level
# messages to that lane's own oscillator - never a shared pool, never a
# reload of anything else. There is no oscillator-table risk here the
# way there was with GM drum kits: a GM kit synth needs its own set of
# oscillators internally (that's what overran the table when we loaded
# seven of them), but a bare one-shot PCM sample is a single oscillator.
# Seven fixed oscillators is nothing against AMY's default budget of 180.
# =====================================================================

def configure_lane(lane):
    """(Re)point this lane's fixed oscillator at the sample its current
    kit assigns to its role, and (re)apply its bus routing + filter/
    drive. Called on kit or bus change - at most a couple of short
    messages, and ONLY for the lane(s) that actually changed."""
    role = lane.name
    kit_idx = lane.kit_override if lane.kit_override is not None else app.kit_idx
    preset = SAMPLE_KITS[kit_idx][1].get(role, NO_SAMPLE)
    if preset != lane._preset:
        lane._preset = preset
        if preset != NO_SAMPLE:
            try:
                amy.send(osc=lane._osc, wave=amy.PCM, preset=preset)
            except Exception as ex:
                print("sample load failed on osc", lane._osc, ex)
        # if NO_SAMPLE: nothing to configure - out_vel() gates the lane
        # silent below, exactly like a GM kit lacking that drum used to.
    apply_lane_route(lane)


def apply_lane_route(lane):
    """This lane's bus routing plus the per-osc filter/drive taken from
    that bus's FX settings."""
    try:
        amy.send(osc=lane._osc, bus=lane.bus)
    except Exception:
        pass
    f = app.bus_fx[lane.bus]
    try:
        if f["filter_type"] == 1:
            _amy_fx(osc=lane._osc, filter_type=amy.FILTER_LPF,
                     filter_freq=f["filter_freq"], resonance=f["resonance"])
        elif f["filter_type"] == 2:
            _amy_fx(osc=lane._osc, filter_type=amy.FILTER_HPF,
                     filter_freq=f["filter_freq"], resonance=f["resonance"])
        else:
            _amy_fx(osc=lane._osc, filter_freq=0)
    except Exception:
        pass
    try:
        _amy_fx(osc=lane._osc, amp=f["drive"])
    except Exception:
        pass


def configure_all_lanes():
    """Every lane. Startup / RESTART only."""
    for r in app.rows:
        configure_lane(r)


def send_reverb(b):
    f = app.bus_fx[b]
    try:
        _amy_fx(bus=b, reverb="%s,%s,%s" % (
            f["rev_level"], f["rev_liveness"], f["rev_damping"]))
    except Exception:
        pass


def send_echo(b):
    f = app.bus_fx[b]
    try:
        _amy_fx(bus=b, echo="%s,%s,%s,%s,%s" % (
            f["echo_level"], int(f["echo_ms"]), 500,
            f["echo_fb"], f["echo_tone"]))
    except Exception:
        pass


def send_eq(b):
    f = app.bus_fx[b]
    try:
        _amy_fx(bus=b, eq="%s,%s,%s" % (f["eq_l"], f["eq_m"], f["eq_h"]))
    except Exception:
        pass


def apply_bus_fx(b):
    """FULL sync of one bus: all three bus effects plus the per-osc
    filter/drive of every slot routed to it. Use this only when the
    whole bus state genuinely needs re-establishing - bus creation, a
    lane being routed here, an engine restart. Interactive single-knob
    edits must NOT come through here; see fx_set, which sends only the
    one effect that changed."""
    send_reverb(b)
    send_echo(b)
    send_eq(b)
    for r in app.rows:
        if r.bus == b:
            apply_lane_route(r)


def apply_all_fx():
    """Every bus. Only needed after a reset wipes the engine."""
    for b in range(NUM_BUSES):
        apply_bus_fx(b)


def activate_buses():
    """Set the per-bus mixdown volume for ALL buses, and turn OFF chorus.

    Volume fix: AMY only mixes a bus to the output once its volume is
    set. `volume` is a SINGLE float per bus - amy.send(bus=N, volume=X).

    Chorus fix: AMY boots with chorus ON (features.chorus defaults on),
    and this app never exposed it - so a modulated-delay 'reverb/delay'
    smear was always present even with reverb and echo at zero. We don't
    offer chorus, so force its level to 0 on every bus. `chorus` is
    'level,delay,freq,depth'; level 0 = off."""
    for b in range(NUM_BUSES):
        try:
            _amy_fx(bus=b, volume=BUS_VOLUME)
        except Exception as ex:
            print("bus volume set failed:", ex)
        try:
            _amy_fx(bus=b, chorus="0,320,0.5,0.5")   # kill default chorus
        except Exception:
            pass


# Which effect each FX parameter belongs to, so changing one knob sends
# exactly one AMY message rather than resyncing the whole bus + slots.
_FX_GROUP = {
    "rev_level": "reverb", "rev_liveness": "reverb", "rev_damping": "reverb",
    "echo_level": "echo", "echo_ms": "echo", "echo_fb": "echo",
    "echo_tone": "echo",
    "eq_l": "eq", "eq_m": "eq", "eq_h": "eq",
    "filter_type": "slot", "filter_freq": "slot", "resonance": "slot",
    "drive": "slot",
}


def fx_set(key, delta, lo, hi, step, row=None):
    """Change ONE FX parameter and send ONLY the message it affects.

    Reverb params -> one reverb message. Echo -> one echo. EQ -> one eq.
    Filter/drive are per-oscillator, so those touch just the lanes routed
    to this bus. Nothing else is resent - an unrelated knob no longer
    triggers a full bus+lane resync (which was flooding AMY and dragging
    the sequencer)."""
    b = app.fx_bus
    f = app.bus_fx[b]
    f[key] = clampf(f[key] + delta * step, lo, hi)

    grp = _FX_GROUP.get(key, "reverb")
    if grp == "reverb":
        send_reverb(b)
    elif grp == "echo":
        send_echo(b)
    elif grp == "eq":
        send_eq(b)
    else:                       # per-osc: only lanes on THIS bus
        for r in app.rows:
            if r.bus == b:
                apply_lane_route(r)

    # refresh only the row that changed, not all thirteen labels
    if row is not None:
        row.refresh()
    elif app.fx_page is not None:
        app.fx_page.refresh()


# --- sequencer ---------------------------------------------------------

def ticks_per_step():
    """AMY_SEQUENCER_PPQ is ticks per QUARTER note, so a 1/n note is
    PPQ*4/n ticks. 32 steps of 1/32 notes at 48 PPQ = 6 ticks a step."""
    try:
        n = int(amy.AMY_SEQUENCER_PPQ * 4 / NUM_STEPS)
        return n if n > 0 else 6
    except Exception:
        return 6


# The drum hits are sequenced by AMY itself, not by a Python callback.
#
#   - app.seq = sequencer.AMYSequence(NUM_STEPS, NUM_STEPS): a 32-step
#     pattern of 1/32 notes (one bar). Each active, audible (lane, step)
#     is ONE event added to this sequence. AMY fires them from its own C
#     sequencer, sample-accurately - no Python runs per hit. This is the
#     stable, recommended way (AMYSequence for audio events).
#   - app.led_seq = sequencer.TulipSequence(NUM_STEPS, _led_tick): a
#     lightweight callback every 1/32 note, in sync with the same AMY
#     clock, used ONLY to move the LED row. No audio work.
#
# The pattern length is fixed at 32 (no adjustable seq_len), so the
# sequence never needs a length change / rebuild - the reason we ever
# left AMYSequence in the first place is gone.
#
# Event bookkeeping: each Lane keeps events[step] -> event object. A step
# toggle adds/removes one event. A change that alters what a lane plays
# (its slot synth via kit/bus, or its velocity via vol/mute/solo/play)
# rebuilds that lane's events. All of this happens off the audio path.

def build_sequence():
    """Create the AMYSequence and add every active event. Called once at
    startup / RESTART."""
    drop_sequence()
    _reset_seq_tags()     # a full rebuild is the moment to recycle tags
    app.seq = sequencer.AMYSequence(NUM_STEPS, NUM_STEPS)
    for r in app.rows:
        r.rebuild_events()


# AMY's C sequencer (src/sequencer.c) holds only max_sequencer_tags = 256
# events, indexed by tag, and any event whose tag >= 256 is silently
# dropped. Tulip's sequencer.py hands out tags from a global counter
# (AMYSequenceEvent.SEQUENCE_TAG) that only ever GROWS and never reuses a
# freed tag - so a long session of edits/kit-changes/pattern-loads
# eventually pushes new drum events past 256 and they stop sounding.
# That is the "some hits don't play" bug.
#
# app.seq is the ONLY AMYSequence in this app (the LED clock is a
# TulipSequence, a different mechanism that doesn't use these tags), so
# whenever we have just fully cleared app.seq we can safely rewind the
# counter to 0. We also compact opportunistically once the counter climbs
# near the limit during normal editing.

_TAG_COMPACT_AT = 200   # rebuild (recycling tags) once the counter passes
#                         this; max simultaneous events is 7*32 = 224 < 256
_compacting = False


def _reset_seq_tags():
    try:
        sequencer.AMYSequenceEvent.SEQUENCE_TAG = 0
    except Exception:
        pass


def _seq_tag_now():
    try:
        return sequencer.AMYSequenceEvent.SEQUENCE_TAG
    except Exception:
        return 0


def _maybe_compact_tags():
    """If the tag counter has climbed near AMY's limit, do a full
    rebuild, which drops every event, rewinds the counter to 0, and
    re-adds all active events with fresh low tags. Guarded so it never
    recurses through the rebuild it triggers."""
    global _compacting
    if _compacting or app.rebuilding:
        return
    if _seq_tag_now() < _TAG_COMPACT_AT:
        return
    _compacting = True
    try:
        build_sequence()
    except Exception as ex:
        print("tag compaction failed:", ex)
    finally:
        _compacting = False


def drop_sequence():
    old = app.seq
    app.seq = None
    if old is not None:
        try:
            old.clear()
        except Exception:
            pass
    for r in app.rows:
        r.events = {}


def refresh_all_events():
    for r in app.rows:
        r.refresh_events()


def make_led_clock():
    """The single TulipSequence that drives the LED row, in sync with the
    AMY sequencer. Created once; never rebuilt."""
    drop_led_clock()
    app.led_seq = sequencer.TulipSequence(NUM_STEPS, _led_tick)


def drop_led_clock():
    old = app.led_seq
    app.led_seq = None
    if old is not None:
        try:
            old.clear()
        except Exception:
            pass


def _led_tick(x):
    """Runs on AMY's own sequencer clock (the same clock that fires the
    drum hits in app.seq). x is the RAW tick count, not a step index -
    Tulip's docs confirm this (setting divider all the way up to 192
    makes the callback fire on every single raw tick), so it must be
    divided by ticks_per_step before wrapping to a 0..31 step. Moves the
    LED row (<=2 colour writes) and applies a queued play/stop at the
    top of the bar. No audio work - the drums are triggered by AMY
    itself, not from here."""
    try:
        if app.rebuilding:
            return
        step = int(x / app.ticks_per_step) % NUM_STEPS
        app.current_step = step
        if step == 0 and app.pending_play is not None:
            _apply_transport(app.pending_play)
        # A bank switch calls apply_kit() -> configure_lane(), which
        # would point the oscillators straight back at mmapped presets
        # while the cache is off. Hold it for the next bar; a save is
        # ~20ms, so this costs at most one bar of latency and only if
        # you switch banks during the exact bar you saved in.
        if step == 0 and app.bank_pending is not None and not app.saving:
            _apply_bank(app.bank_pending)
        # NOTE: this deliberately does NOT pause for saves any more.
        # Tulip dispatches sequencer callbacks with mp_sched_schedule()
        # (tulip/shared/tsequencer.c), so _led_tick always runs on the
        # main MicroPython thread - it can never interleave with a file
        # write, only run between writes. There was never a reentrancy
        # window to protect against; the only real hazard was how LONG a
        # single write blocked the main loop, and saves are now written
        # in small chunks (see the chunked writer) so nothing here has to
        # stand still. Transport, bank switches and the playhead all keep
        # their timing straight through a save.
        _choke_next_step(step)
        _cv_tick(step)
        if app.playing and not app.ui_paused:
            app.led_row.light(step)
    except Exception as ex:
        print("led tick error (ignored):", ex)


# --- pre-hit choke: works around an AMY PCM retrigger bug --------------
#
# In AMY's pcm_note_on(), retriggering an oscillator whose sample is still
# sounding takes a "restart at the next zero crossing" path:
#
#     msynth[osc]->loopend = pcm_find_next_zero_crossing(osc, base_index);
#     msynth[osc]->state   = PCM_LOOP_ONCE_INTERNAL;
#
# pcm_find_next_zero_crossing() returns int -1 when it can't find one -
# which is exactly what happens when the old sample has just run out
# (its search loop breaks on `base_index >= sample_length` before it
# ever sets an index). msynth->loopend is a uint32_t, so that -1 is
# stored as 4294967295, the render loop's `base_index >= loopend` test
# can then never fire, the one-shot loop never returns to the new note's
# start, and the very next check trips `base_index >= sample_length` ->
# status = SYNTH_OFF. The retriggered hit is swallowed entirely.
#
# It only bites when a hit lands within about one audio block of the
# previous hit's sample ENDING - so it needs a sample whose length nearly
# equals the gap between two hits. That is why it showed up on exactly
# one sound: Tokyo Syn's snare (preset 296, a bass-drum-length sample in
# the snare role) with hits 8 steps apart. And it explains the shape of
# it - hit 1 sounds, hit 2 is swallowed AND leaves the osc off, so hit 3
# finds a free oscillator and sounds again.
#
# The fix from our side: send an explicit note-off one step before a hit,
# so the oscillator is already SYNTH_OFF and note-on takes the clean
# fresh-start branch instead. pcm_note_off() in the default PCM_PLAY_STOP
# mode sets status = SYNTH_OFF immediately, so this is reliable.
#
# Skipped when this lane also plays on the CURRENT step, so drum rolls on
# consecutive steps are never choked - and they don't need it anyway,
# since a hit that close lands nowhere near the end of the sample.

def _choke_next_step(step):
    """Note-off any lane that is about to be retriggered on the next step."""
    nxt = (step + 1) % NUM_STEPS
    for r in app.rows:
        if nxt not in r.step_set or step in r.step_set:
            continue
        if not r.has_sample() or not r.audible():
            continue
        try:
            amy.send(osc=r._osc, vel=0)
        except Exception:
            pass


def update_solo_state():
    app.any_solo = False
    for r in app.rows:
        if r.solo:
            app.any_solo = True


def clear_leds():
    if app.led_row is not None:
        try:
            app.led_row.clear()
        except Exception:
            pass


# --- full engine start (the only place amy.reset() lives) -------------

def start_engine():
    """The one heavy operation. Startup and RESTART only.

    amy.reset() clears every oscillator. On real hardware a reset can
    take a moment to actually settle before it's safe to configure
    oscillators again - this matches a reported symptom where the
    startup kit (TR-808) played nothing until a kit was changed, which
    re-sends the exact same oscillator config well after reset had time
    to finish. So the reconfiguration is deferred a beat rather than
    firing in the same breath as the reset. I can't verify the exact
    timing needed without hardware - if drums are still silent at
    startup, try raising ENGINE_SETTLE_MS below."""
    app.rebuilding = True
    try:
        drop_sequence()
        drop_led_clock()
        clear_leds()

        try:
            amy.reset()
        except Exception as ex:
            print("amy.reset failed:", ex)

        defer_safe(_finish_engine_start, None, ENGINE_SETTLE_MS)
    except Exception as ex:
        app.rebuilding = False
        print("start_engine failed:", ex)


def _finish_engine_start(arg=None):
    """The rest of engine bringup, run ENGINE_SETTLE_MS after reset."""
    try:
        activate_buses()      # make ALL buses audible (not just bus 0)
        for r in app.rows:
            r._preset = None  # invalidate: reset() wiped every oscillator,
            #                   so the cache can't skip the resend below
        configure_all_lanes() # (re)point every lane's fixed oscillator
        apply_all_fx()

        try:
            sequencer.tempo(app.bpm)
        except Exception:
            pass
        build_sequence()      # AMYSequence with all active events
        make_led_clock()      # TulipSequence that moves the LED row
    except Exception as ex:
        print("engine bringup failed:", ex)
    finally:
        app.rebuilding = False
        if app.header is not None:
            app.header.restart_btn.set_text("RESTART")
        _update_play_btn()
        if app.header2 is not None and app._restart_msg is not None:
            app.header2.set_info(app._restart_msg)
        app._restart_msg = None


def restart_audio(e=None):
    """Panic button: rebuild everything from scratch. Finishes
    asynchronously (see start_engine) - the RESTART label and info line
    are restored by _finish_engine_start once bringup completes."""
    if app.header is not None:
        app.header.restart_btn.set_text("...")
    app.playing = True
    app.pending_play = None
    app.rebuilding = False
    app._restart_msg = "restarted"
    try:
        start_engine()
    except Exception as ex:
        print("restart failed:", ex)
        app._restart_msg = "restart failed"
        if app.header is not None:
            app.header.restart_btn.set_text("RESTART")
        _update_play_btn()


# --- Euclidean randomisation -------------------------------------------

def euclid(n, k):
    if k <= 0:
        return []
    if k >= n:
        return list(range(n))
    out = []
    bucket = 0
    for i in range(n):
        bucket += k
        if bucket >= n:
            bucket -= n
            out.append(i)
    return out


def _snap(step, grid):
    return (int(round(step / grid)) * grid) % NUM_STEPS


def random_steps(name):
    lo, hi = RANDOM_RANGES.get(name, (2, 6))
    hits = random.randint(lo, hi)
    if hits <= 0:
        return []
    base = euclid(NUM_STEPS, hits)
    rot = random.randint(0, NUM_STEPS - 1)
    steps = [(s + rot) % NUM_STEPS for s in base]
    if name == "Kick":
        if steps and 0 not in steps:
            shift = (-steps[0]) % NUM_STEPS
            steps = [(s + shift) % NUM_STEPS for s in steps]
        steps = sorted(set(_snap(s, 4) for s in steps))
    elif name in ("Snare", "Clap"):
        steps = sorted(set(_snap(s, 4) for s in steps))
        if 8 not in steps and 24 not in steps and random.random() < 0.6:
            steps.append(8 if random.random() < 0.5 else 24)
    elif name in ("CHat", "OHat"):
        steps = sorted(set(_snap(s, 2) for s in steps))
    elif name == "Cymbal":
        steps = sorted(set(_snap(s, 8) for s in steps))
    elif name == "Tom":
        steps = sorted(set(_snap(s, 2) for s in steps))
        back = [s for s in steps if s >= 16]
        if back:
            steps = back
    return sorted(set(s % NUM_STEPS for s in steps))


# --- one step cell -----------------------------------------------------

class Cell:
    def __init__(self, parent, row_i, step_i):
        self.row_i = row_i
        self.step_i = step_i
        self.on = False
        self.obj = lv.obj(parent)
        self.obj.set_size(STEP_RECT_W, STEP_H)
        self.obj.set_style_radius(3, 0)
        self.obj.align_to(parent, lv.ALIGN.LEFT_MID,
                           X_STEPS + step_i * STEP_W, 0)
        self.obj.add_event_cb(self._cb, lv.EVENT.CLICKED, None)
        self.obj.remove_flag(lv.obj.FLAG.SCROLLABLE)
        lv_depad(self.obj)
        self.redraw()

    def _cb(self, e):
        try:
            app.rows[self.row_i].toggle_step(self.step_i)
        except Exception as ex:
            print("cell error:", ex)

    def redraw(self):
        # cells only show pattern on/off now; the moving position is the
        # LED row above the grid, not a cell highlight
        if self.on:
            c = C_CELL_ON
        elif self.step_i % 4 == 0:
            c = C_CELL_BEAT
        else:
            c = C_CELL_OFF
        try:
            self.obj.set_style_bg_color(lv_color(c), 0)
        except Exception:
            pass      # widget deleted (stale instance) - skip, don't crash

    def set_on(self, v):
        self.on = v
        self.redraw()


# --- 32-LED position row (replaces the moving cell playhead) -----------

class LEDRow(UIElement):
    """A row of 32 small LEDs above the grid, aligned with the step
    columns. One LED lights as the bar plays, driven by a TulipSequence
    in sync with the AMY sequencer. Far lighter than the old playhead:
    at most two LED colour writes per step, and never inside the audio
    path."""
    def __init__(self):
        super().__init__()
        self.group.set_size(ROW_W, LED_ROW_H)
        lv_depad(self.group)
        self.group.remove_flag(lv.obj.FLAG.SCROLLABLE)
        strut(self.group, LED_ROW_H)
        self.leds = []
        y = (LED_ROW_H - LED_H) // 2
        for i in range(NUM_STEPS):
            o = lv.obj(self.group)
            o.set_size(LED_W, LED_H)
            o.set_style_radius(3, 0)
            o.align_to(self.group, lv.ALIGN.LEFT_MID, X_STEPS + i * STEP_W, 0)
            o.remove_flag(lv.obj.FLAG.SCROLLABLE)
            o.remove_flag(lv.obj.FLAG.CLICKABLE)
            lv_depad(o)
            self.leds.append(o)
        self.lit = None
        self.redraw_all()

    def _idle_color(self, i):
        return C_LED_BEAT if i % 4 == 0 else C_LED_OFF

    def redraw_all(self):
        for i in range(NUM_STEPS):
            try:
                self.leds[i].set_style_bg_color(lv_color(self._idle_color(i)), 0)
            except Exception:
                pass
        self.lit = None

    def light(self, step):
        """Light `step`, dim the previously lit one. <=2 LVGL writes."""
        if step == self.lit:
            return
        if self.lit is not None:
            try:
                self.leds[self.lit].set_style_bg_color(
                    lv_color(self._idle_color(self.lit)), 0)
            except Exception:
                pass
        try:
            self.leds[step].set_style_bg_color(lv_color(C_LED_ON), 0)
        except Exception:
            pass
        self.lit = step

    def clear(self):
        if self.lit is not None:
            try:
                self.leds[self.lit].set_style_bg_color(
                    lv_color(self._idle_color(self.lit)), 0)
            except Exception:
                pass
        self.lit = None

class Lane(UIElement):
    def __init__(self, name, note, vol, row_i):
        super().__init__()
        self.name = name
        self.note = note
        self.vol = vol
        self.row_i = row_i
        self.muted = False
        self.solo = False
        self.kit_override = None      # None = follow the global kit
        self.bus = 0
        self._osc = PCM_OSC_BASE + row_i   # this lane's fixed oscillator,
        #                                    forever - never reassigned
        self._preset = None           # currently-configured PCM preset
        #                              (None = not yet configured; -1/
        #                              NO_SAMPLE = this kit has no sample
        #                              for this role)
        self.steps = []
        self.step_set = set()
        self.events = {}              # step -> AMYSequence event object

        self.group.set_size(ROW_W, ROW_H)
        lv_depad(self.group)
        self.group.remove_flag(lv.obj.FLAG.SCROLLABLE)
        strut(self.group, ROW_H)

        self.label = lv.label(self.group)
        self.label.set_text(name)
        self.label.align_to(self.group, lv.ALIGN.LEFT_MID, X_LABEL, 0)

        self.mute_btn = Button(self.group, "M", X_MUTE, BTN_D, LANE_BTN_H,
                                self.mute_cb)
        self.solo_btn = Button(self.group, "S", X_SOLO, BTN_D, LANE_BTN_H,
                                self.solo_cb)
        self.rnd_btn = Button(self.group, "R", X_RND, BTN_D, LANE_BTN_H,
                               self.rnd_cb)
        self.vol_btn = Button(self.group, "V", X_VOL, BTN_D, LANE_BTN_H,
                               self.vol_cb)
        self.kit_btn = Button(self.group, "-", X_KIT, 34, LANE_BTN_H,
                               self.kit_cb, C_KIT_BTN)

        self.cells = []
        for s in range(NUM_STEPS):
            self.cells.append(Cell(self.group, row_i, s))

    # -- which sample this lane plays --

    def kit_index(self):
        if self.kit_override is None:
            return app.kit_idx
        return self.kit_override

    def osc(self):
        # fixed oscillator, assigned once at __init__ and never changed
        return self._osc

    def has_sample(self):
        return self._preset != NO_SAMPLE

    def refresh_kit_btn(self):
        if self.kit_override is None:
            self.kit_btn.set_text("-")
            self.kit_btn.set_color(C_KIT_BTN)
        else:
            self.kit_btn.set_text(KITS[self.kit_override][1][:4])
            self.kit_btn.set_color(C_KIT_ON)

    def set_kit_override(self, kit):
        if kit == self.kit_override:
            return
        self.kit_override = kit
        self.refresh_kit_btn()
        configure_lane(self)  # re-point THIS lane's oscillator only
        self.refresh_events() # vel may change if the new kit lacks this drum

    def set_bus(self, bus):
        bus = bus % NUM_BUSES
        if bus == self.bus:
            return
        self.bus = bus
        configure_lane(self)  # re-route THIS lane's oscillator only

    # -- sound --

    def audible(self):
        if self.muted:
            return False
        if app is not None and app.any_solo and not self.solo:
            return False
        return True

    def out_vel(self):
        """The velocity this lane's events should fire at right now.
        Muted / soloed-out / zero-volume / transport-stopped / no sample
        for this role on the current kit => MUTE_VEL (effectively
        silent, same as a GM kit simply lacking that drum). Otherwise the
        lane volume."""
        if (self.has_sample() and app is not None and app.playing
                and self.audible() and self.vol > 0.0):
            return self.vol
        return MUTE_VEL

    # -- AMYSequence event bookkeeping (off the audio path) --

    def _add_event(self, step):
        if app.seq is None:
            return
        try:
            e = app.seq.add(step, amy.send, [],
                            osc=self._osc, vel=self.out_vel())
            self.events[step] = e
        except Exception as ex:
            print("seq add failed:", ex)
        # recycle AMY's finite tag space before it overflows and starts
        # dropping events (see _maybe_compact_tags)
        _maybe_compact_tags()

    def _remove_event(self, step):
        e = self.events.pop(step, None)
        if e is not None:
            try:
                e.remove()
            except Exception:
                pass

    def rebuild_events(self):
        """Drop and re-add all of this lane's events from the grid."""
        for step in list(self.events.keys()):
            self._remove_event(step)
        if app.seq is None:
            return
        for step in self.step_set:
            self._add_event(step)

    def refresh_events(self):
        """Update existing events in place (velocity changed - the
        oscillator number itself never changes). Rebuilds if an event is
        missing."""
        if app.seq is None:
            return
        kw = dict(osc=self._osc, vel=self.out_vel())
        for step in self.step_set:
            e = self.events.get(step)
            if e is None:
                self._add_event(step)
            else:
                try:
                    e.update(step, amy.send, [], **kw)
                except Exception:
                    self._remove_event(step)
                    self._add_event(step)

    def refresh_kit_btn_and_events(self):
        self.refresh_kit_btn()

    def should_fire(self, step):
        """Kept for tests/diagnostics: would this lane sound on `step`?"""
        if not self.has_sample():
            return False
        if self.vol <= 0.0:
            return False
        if not self.audible():
            return False
        return step in self.step_set

    # -- steps (self.steps / self.step_set are a source of truth) --

    def set_step(self, step, on):
        if on and step not in self.step_set:
            self.step_set.add(step)
            self.steps = sorted(self.step_set)
            self.cells[step].set_on(True)
            self._add_event(step)
        elif (not on) and step in self.step_set:
            self.step_set.discard(step)
            self.steps = sorted(self.step_set)
            self.cells[step].set_on(False)
            self._remove_event(step)

    def toggle_step(self, step):
        self.set_step(step, step not in self.step_set)
        _auto_save_slot()

    def set_steps(self, steps):
        want = set(steps)
        for s in list(self.step_set):
            if s not in want:
                self.set_step(s, False)
        for s in want:
            if s not in self.step_set:
                self.set_step(s, True)
        # note: callers that use set_steps for bulk loads (apply_preset,
        # apply_pattern_snapshot, _apply_bank) call _auto_save_slot()
        # themselves AFTER loading all lanes, not per-lane here, to
        # avoid one snapshot per lane during a full 7-lane preset load.

    # -- buttons --

    def mute_cb(self, e=None):
        self.muted = not self.muted
        self.mute_btn.set_color(C_MUTE_ON if self.muted else C_BTN)
        refresh_all_events()      # audibility affects every lane via solo

    def solo_cb(self, e=None):
        self.solo = not self.solo
        self.solo_btn.set_color(C_SOLO_ON if self.solo else C_BTN)
        update_solo_state()
        refresh_all_events()

    def rnd_cb(self, e=None):
        self.set_steps(random_steps(self.name))
        _auto_save_slot()

    def vol_cb(self, e=None):
        app.vol_popup.show(self.row_i)

    def kit_cb(self, e=None):
        app.kit_popup.show(self.row_i)

    def set_vol(self, v):
        self.vol = clampf(v, 0.0, 1.0)
        self.refresh_events()     # events fire at the new velocity



# --- volume popup ------------------------------------------------------

class VolPopup:
    def __init__(self, parent):
        self.lane_i = None
        self.panel = lv.obj(parent)
        self.panel.set_size(430, 120)
        self.panel.set_style_bg_color(lv_color(C_PANEL), 0)
        self.panel.set_style_radius(8, 0)
        self.panel.align_to(parent, lv.ALIGN.CENTER, 0, 0)
        self.panel.remove_flag(lv.obj.FLAG.SCROLLABLE)

        self.title = lv.label(self.panel)
        self.title.set_text("Volume")
        self.title.align_to(self.panel, lv.ALIGN.LEFT_MID, 16, -36)
        Button(self.panel, "Close", 330, 84, 32, self.close, C_BTN, -36)

        self.down = Button(self.panel, "-", 16, 44, 40, self.dec, C_BTN, 14)
        self.slider = lv.slider(self.panel)
        self.slider.set_size(200, 20)
        self.slider.align_to(self.panel, lv.ALIGN.LEFT_MID, 72, 14)
        self.slider.add_event_cb(self.slider_cb, lv.EVENT.VALUE_CHANGED, None)
        self.up = Button(self.panel, "+", 288, 44, 40, self.inc, C_BTN, 14)
        self.value = lv.label(self.panel)
        self.value.set_text("0%")
        self.value.align_to(self.panel, lv.ALIGN.LEFT_MID, 348, 14)
        self.hide()

    def _lane(self):
        if self.lane_i is None:
            return None
        return app.rows[self.lane_i]

    def _refresh(self, move_slider=True):
        lane = self._lane()
        if lane is None:
            return
        pct = int(lane.vol * 100 + 0.5)
        self.title.set_text(lane.name + " volume")
        self.value.set_text(str(pct) + "%")
        if move_slider:
            try:
                self.slider.set_value(pct, 0)
            except Exception:
                pass

    def slider_cb(self, e=None):
        lane = self._lane()
        if lane is None:
            return
        try:
            v = e.get_target_obj().get_value()
        except Exception:
            v = self.slider.get_value()
        lane.set_vol(v / 100.0)
        self._refresh(move_slider=False)   # don't fight the finger

    def dec(self, e=None):
        lane = self._lane()
        if lane:
            lane.set_vol(lane.vol - VOL_STEP)
            self._refresh()

    def inc(self, e=None):
        lane = self._lane()
        if lane:
            lane.set_vol(lane.vol + VOL_STEP)
            self._refresh()

    def close(self, e=None):
        self.hide()

    def show(self, lane_i):
        self.lane_i = lane_i
        self._refresh()
        self.panel.remove_flag(lv.obj.FLAG.HIDDEN)
        self.panel.move_foreground()

    def hide(self):
        self.lane_i = None
        self.panel.add_flag(lv.obj.FLAG.HIDDEN)


# --- save-as popup (name entry via Tulip's touch keyboard) -------------

class SaveProjectPopup:
    """SAVE opens this instead of saving immediately: type a name, then
    Save or the keyboard's enter/checkmark key. Cancel discards.

    Uses a real lv.keyboard() explicitly bound to our textarea via
    set_textarea() - LVGL's keyboard widget does not know where to type
    unless you tell it to (this was the actual bug: tulip.keyboard()'s
    global toggle was never bound to this textarea at all, so nothing
    typed and Enter had nothing to confirm)."""
    def __init__(self, parent):
        self.panel = lv.obj(parent)
        self.panel.set_size(560, 150)
        self.panel.set_style_bg_color(lv_color(C_PANEL), 0)
        self.panel.set_style_radius(8, 0)
        self.panel.align_to(parent, lv.ALIGN.CENTER, 0, -180)
        self.panel.remove_flag(lv.obj.FLAG.SCROLLABLE)

        self.title = lv.label(self.panel)
        self.title.set_text("Save project as:")
        self.title.align_to(self.panel, lv.ALIGN.TOP_LEFT, 16, 16)

        self.ta = lv.textarea(self.panel)
        self.ta.set_size(528, 48)
        self.ta.align_to(self.panel, lv.ALIGN.TOP_LEFT, 16, 46)
        self.ta.set_one_line(True)
        self.ta.set_max_length(MAX_PROJECT_NAME_LEN)
        self.ta.set_placeholder_text("Project name")
        self.ta.add_event_cb(self._on_ready, lv.EVENT.READY, None)

        Button(self.panel, "Save", 316, 110, 40, self._on_save, C_PLAY, 102)
        Button(self.panel, "Cancel", 434, 110, 40, self.hide, C_BTN, 102)
        self.panel.add_flag(lv.obj.FLAG.HIDDEN)   # start hidden

        # the on-screen keyboard - a real widget, bound to self.ta, sized
        # to sit below the popup at the bottom of the screen
        self.kb = lv.keyboard(parent)
        self.kb.set_size(1024, 260)
        self.kb.align_to(parent, lv.ALIGN.BOTTOM_MID, 0, 0)
        try:
            self.kb.set_textarea(self.ta)
        except Exception:
            pass
        self.kb.add_flag(lv.obj.FLAG.HIDDEN)

    def show(self):
        default = "Project %d" % (len(app.user_projects) + 1)
        try:
            self.ta.set_text(default)
        except Exception:
            pass
        self.panel.remove_flag(lv.obj.FLAG.HIDDEN)
        self.panel.move_foreground()
        self.kb.remove_flag(lv.obj.FLAG.HIDDEN)
        self.kb.move_foreground()
        try:
            lv.group_focus_obj(self.ta)
        except Exception:
            pass

    def hide(self, e=None):
        self.panel.add_flag(lv.obj.FLAG.HIDDEN)
        self.kb.add_flag(lv.obj.FLAG.HIDDEN)

    def _on_ready(self, e=None):
        # Enter / checkmark on the keyboard - same as tapping Save
        self._on_save()

    def _on_save(self, e=None):
        name = ""
        try:
            name = self.ta.get_text()
        except Exception:
            pass
        if do_save_project(name):
            self.hide()
        # else: blocked by the save cooldown - stay open, the "wait a
        # sec" message is already showing in the info line, and the
        # typed name is preserved so the user can just try again shortly


# --- per-lane kit picker -----------------------------------------------

class KitPopup:
    def __init__(self, parent):
        self.lane_i = None
        self.panel = lv.obj(parent)
        self.panel.set_size(760, 130)
        self.panel.set_style_bg_color(lv_color(C_PANEL), 0)
        self.panel.set_style_radius(8, 0)
        self.panel.align_to(parent, lv.ALIGN.CENTER, 0, 0)
        self.panel.remove_flag(lv.obj.FLAG.SCROLLABLE)

        self.title = lv.label(self.panel)
        self.title.set_text("Kit")
        self.title.align_to(self.panel, lv.ALIGN.LEFT_MID, 16, -44)
        Button(self.panel, "Close", 650, 90, 36, self.close, C_BTN, -44)

        self.buttons = [Button(self.panel, "Global", 16, 92, 46,
                                self.make_cb(None), C_KIT_BTN, 20)]
        for i in range(len(KITS)):
            self.buttons.append(
                Button(self.panel, KITS[i][1][:5], 116 + i * 92, 88, 46,
                        self.make_cb(i), C_KIT_BTN, 20))
        self.hide()

    def make_cb(self, kit):
        def _cb(e=None):
            lane_i = self.lane_i
            self.hide()
            if lane_i is not None:
                app.rows[lane_i].set_kit_override(kit)
        return _cb

    def show(self, lane_i):
        self.lane_i = lane_i
        lane = app.rows[lane_i]
        cur = lane.kit_override
        if cur is None:
            now = "Global - " + KITS[app.kit_idx][1]
        else:
            now = KITS[cur][1]
        self.title.set_text("%s kit  (now: %s)" % (lane.name, now))
        for i in range(len(self.buttons)):
            sel = (i == 0 and cur is None) or (i > 0 and cur == i - 1)
            self.buttons[i].set_color(C_KIT_ON if sel else C_KIT_BTN)
        self.panel.remove_flag(lv.obj.FLAG.HIDDEN)
        self.panel.move_foreground()

    def close(self, e=None):
        self.hide()

    def hide(self):
        self.lane_i = None
        self.panel.add_flag(lv.obj.FLAG.HIDDEN)


# --- per-lane bus picker -----------------------------------------------

# --- FX page -----------------------------------------------------------

class FXRow:
    """One FX parameter: label, - / +, value. `right` puts it in the
    second column."""
    def __init__(self, parent, y, name, key, lo, hi, step, fmt="%.2f",
                  right=False):
        self.key = key
        self.lo = lo
        self.hi = hi
        self.step = step
        self.fmt = fmt
        x = 480 if right else 20
        bx = 610 if right else 150
        vx = 670 if right else 210
        px = 750 if right else 290
        self.name = lv.label(parent)
        self.name.set_text(name)
        self.name.align_to(parent, lv.ALIGN.LEFT_MID, x, y)
        Button(parent, "-", bx, 46, 38, self.dec, C_BTN, y)
        self.value = lv.label(parent)
        self.value.set_text("")
        self.value.align_to(parent, lv.ALIGN.LEFT_MID, vx, y)
        Button(parent, "+", px, 46, 38, self.inc, C_BTN, y)

    def dec(self, e=None):
        fx_set(self.key, -1, self.lo, self.hi, self.step, row=self)

    def inc(self, e=None):
        fx_set(self.key, 1, self.lo, self.hi, self.step, row=self)

    def refresh(self):
        try:
            self.value.set_text(self.fmt % app.bus_fx[app.fx_bus][self.key])
        except Exception:
            pass


class FXPage:
    Y_TITLE = -205
    Y_BUS = -155
    Y_FIRST = -95
    Y_STEP = 45

    def __init__(self, parent):
        self.panel = lv.obj(parent)
        self.panel.set_size(1000, 470)
        self.panel.set_style_bg_color(lv_color(C_PANEL), 0)
        self.panel.set_style_radius(8, 0)
        self.panel.align_to(parent, lv.ALIGN.CENTER, 0, 0)
        self.panel.remove_flag(lv.obj.FLAG.SCROLLABLE)

        title = lv.label(self.panel)
        title.set_text("FX  -  each bus has its own reverb, echo and EQ. "
                        "Tap a lane below to move it onto this bus.")
        title.align_to(self.panel, lv.ALIGN.LEFT_MID, 20, FXPage.Y_TITLE)
        Button(self.panel, "Close", 860, 110, 44, self.close, C_BTN,
                FXPage.Y_TITLE)
        Button(self.panel, "RESET BUS", 700, 140, 44, self.reset_fx, C_BTN,
                FXPage.Y_TITLE)

        Button(self.panel, "< BUS", 20, 90, 40, self.bus_prev, C_KIT_ON,
                FXPage.Y_BUS)
        self.bus_value = lv.label(self.panel)
        self.bus_value.set_text("BUS 0")
        self.bus_value.align_to(self.panel, lv.ALIGN.LEFT_MID, 124,
                                 FXPage.Y_BUS)
        Button(self.panel, "BUS >", 200, 90, 40, self.bus_next, C_KIT_ON,
                FXPage.Y_BUS)

        # One button per lane: tap to move that lane onto the bus you're
        # currently viewing. Each button is coloured by whichever bus the
        # lane is CURRENTLY on (same palette as everywhere else), so you
        # can see at a glance where every lane is routed, and matching
        # the "BUS N" label's own colour tells you which lanes are on
        # the bus you're looking at right now. A lane only ever belongs
        # to one bus - tapping moves it, it doesn't add a second route.
        self.lane_btns = []
        n = len(ELEMENTS)
        lb_start = 310
        lb_pitch = (960 - lb_start) // n
        lb_w = lb_pitch - 6
        for i in range(n):
            btn = Button(self.panel, ELEMENTS[i][0],
                          lb_start + i * lb_pitch, lb_w, 40,
                          self.make_assign(i), C_BUS[0], FXPage.Y_BUS)
            self.lane_btns.append(btn)

        y0 = FXPage.Y_FIRST
        ys = FXPage.Y_STEP
        self.rows = [
            FXRow(self.panel, y0 + 0 * ys, "Reverb level",
                   "rev_level", 0.0, 1.0, 0.05),
            FXRow(self.panel, y0 + 1 * ys, "Reverb liveness",
                   "rev_liveness", 0.0, 1.0, 0.05),
            FXRow(self.panel, y0 + 2 * ys, "Reverb damping",
                   "rev_damping", 0.0, 1.0, 0.05),
            FXRow(self.panel, y0 + 3 * ys, "Echo level",
                   "echo_level", 0.0, 1.0, 0.05),
            FXRow(self.panel, y0 + 4 * ys, "Echo time ms",
                   "echo_ms", 20, 500, 10, "%d"),
            FXRow(self.panel, y0 + 5 * ys, "Echo feedback",
                   "echo_fb", 0.0, 0.95, 0.05),
            FXRow(self.panel, y0 + 6 * ys, "Echo tone",
                   "echo_tone", -1.0, 1.0, 0.1),
        ]

        self.ftype_label = lv.label(self.panel)
        self.ftype_label.set_text("Filter")
        self.ftype_label.align_to(self.panel, lv.ALIGN.LEFT_MID, 480, y0)
        Button(self.panel, "TYPE", 610, 90, 38, self.cycle_filter, C_BTN, y0)
        self.ftype_value = lv.label(self.panel)
        self.ftype_value.set_text("OFF")
        self.ftype_value.align_to(self.panel, lv.ALIGN.LEFT_MID, 720, y0)

        self.rows.append(FXRow(self.panel, y0 + 1 * ys, "Cutoff Hz",
                                "filter_freq", 100, 12000, 200, "%d", True))
        self.rows.append(FXRow(self.panel, y0 + 2 * ys, "Resonance",
                                "resonance", 0.5, 8.0, 0.5, "%.2f", True))
        self.rows.append(FXRow(self.panel, y0 + 3 * ys, "Drive",
                                "drive", 0.2, 4.0, 0.2, "%.2f", True))
        self.rows.append(FXRow(self.panel, y0 + 4 * ys, "EQ low dB",
                                "eq_l", -15.0, 15.0, 1.0, "%.0f", True))
        self.rows.append(FXRow(self.panel, y0 + 5 * ys, "EQ mid dB",
                                "eq_m", -15.0, 15.0, 1.0, "%.0f", True))
        self.rows.append(FXRow(self.panel, y0 + 6 * ys, "EQ high dB",
                                "eq_h", -15.0, 15.0, 1.0, "%.0f", True))
        self.panel.add_flag(lv.obj.FLAG.HIDDEN)   # start hidden

    def bus_prev(self, e=None):
        app.fx_bus = (app.fx_bus - 1) % NUM_BUSES
        self.refresh()

    def bus_next(self, e=None):
        app.fx_bus = (app.fx_bus + 1) % NUM_BUSES
        self.refresh()

    def make_assign(self, lane_i):
        def _cb(e=None):
            app.rows[lane_i].set_bus(app.fx_bus)
            self.refresh()
        return _cb

    def cycle_filter(self, e=None):
        f = app.bus_fx[app.fx_bus]
        f["filter_type"] = (f["filter_type"] + 1) % 3
        apply_bus_fx(app.fx_bus)
        self.refresh()

    def reset_fx(self, e=None):
        f = app.bus_fx[app.fx_bus]
        for k in BUS_FX_DEFAULTS:
            f[k] = BUS_FX_DEFAULTS[k]
        apply_bus_fx(app.fx_bus)
        self.refresh()

    def refresh(self):
        for r in self.rows:
            r.refresh()
        try:
            self.ftype_value.set_text(
                FILTER_NAMES[app.bus_fx[app.fx_bus]["filter_type"]])
            self.bus_value.set_text("BUS %d" % app.fx_bus)
            self.bus_value.set_style_text_color(
                lv_color(C_BUS[app.fx_bus % len(C_BUS)]), 0)
            for i, btn in enumerate(self.lane_btns):
                lane = app.rows[i]
                btn.set_color(C_BUS[lane.bus % len(C_BUS)])
        except Exception:
            pass

    def show(self):
        # switch to the FX page: hide the sequencer view and pause the
        # visual playhead so nothing is drawing the grid behind us. Audio
        # keeps running - only the visuals pause.
        app.ui_paused = True
        set_sequencer_view(False)
        self.refresh()
        self.panel.remove_flag(lv.obj.FLAG.HIDDEN)
        self.panel.move_foreground()

    def close(self, e=None):
        self.hide()

    def hide(self):
        self.panel.add_flag(lv.obj.FLAG.HIDDEN)
        # back to the sequencer page: clear the LED row, show the grid
        # again, and let it resume lighting from the current step.
        set_sequencer_view(True)
        clear_leds()
        app.ui_paused = False


def set_sequencer_view(visible):
    """Show or hide the entire drum-sequencer view as one page. Used so
    the FX page is a real separate page rather than an overlay composited
    on top of the grid - only one of the two is ever on screen."""
    widgets = [app.header, app.header2, app.top_spacer, app.led_row,
               app.led_spacer, app.mid_spacer, app.bank_row] + list(app.rows)
    for w in widgets:
        if w is None:
            continue
        try:
            if visible:
                w.group.remove_flag(lv.obj.FLAG.HIDDEN)
            else:
                w.group.add_flag(lv.obj.FLAG.HIDDEN)
        except Exception:
            pass


def open_fx(e=None):
    app.fx_page.show()


# --- pattern bank strip --------------------------------------------------

class PatternBankRow(UIElement):
    """A-J pattern slots plus COPY / PASTE (no SAVE - the active slot
    auto-updates in RAM whenever you edit the grid; the bank is saved to
    disk only when you save a project). Slot colour: green = active,
    amber = queued (will swap at the top of the next bar), grey = has
    content, dark = empty. COPY/PASTE flash orange as press feedback."""
    def __init__(self):
        super().__init__()
        self.group.set_size(ROW_W, BUS_ROW_H)
        lv_depad(self.group)
        self.group.remove_flag(lv.obj.FLAG.SCROLLABLE)
        strut(self.group, BUS_ROW_H)

        label = lv.label(self.group)
        label.set_text("BANK")
        label.align_to(self.group, lv.ALIGN.LEFT_MID, X_LABEL, 0)

        # Widened from 42/36 to 54/48 - these are the buttons you tap
        # mid-performance, so they get the space freed up by shrinking
        # the CV indicator and sliding COPY/PASTE right. Slots run
        # 78..612, COPY starts at 630.
        self.slot_btns = []
        start = 78
        pitch = 54
        for i in range(NUM_BANKS):
            btn = Button(self.group, BANK_LETTERS[i], start + i * pitch,
                          pitch - 6, HDR_BTN_H, self.make_select(i),
                          C_BANK_EMPTY)
            self.slot_btns.append(btn)

        # COPY and PASTE only - no SAVE, slots auto-update on every edit
        self.copy_btn = Button(self.group, "COPY", 630, 136,
                                 HDR_BTN_H, bank_copy, C_BTN)
        self.paste_btn = Button(self.group, "PASTE", 778, 136,
                                  HDR_BTN_H, bank_paste, C_BTN)
        # CV clock out to eurorack. Half width - it is really just an
        # indicator (green = clocking) that happens to be tappable.
        self.cv_btn = Button(self.group, "CV", 926, 70, HDR_BTN_H,
                              toggle_cv, C_BANK_EMPTY)

    def refresh_cv(self):
        """Green = clocking, dark = off or no DAC on the I2C port. The
        label stays "CV" - the button is too narrow for more, and the
        colour is what you read at a glance anyway."""
        try:
            on = CV_ENABLED and cv_present()
            self.cv_btn.set_color(C_PLAY if on else C_BANK_EMPTY)
        except Exception:
            pass

    def make_select(self, i):
        def _cb(e=None):
            bank_select(i)
        return _cb

    def refresh(self):
        for i, btn in enumerate(self.slot_btns):
            if i == app.bank_pending:
                btn.set_color(C_PENDING)
            elif i == app.bank_active:
                btn.set_color(C_PLAY)
            elif app.bank_patterns[i] is not None:
                btn.set_color(C_KIT_BTN)
            else:
                btn.set_color(C_BANK_EMPTY)


# --- headers -----------------------------------------------------------

class HeaderTop(UIElement):
    def __init__(self):
        super().__init__()
        self.group.set_size(ROW_W, HDR_H)
        lv_depad(self.group)
        self.group.remove_flag(lv.obj.FLAG.SCROLLABLE)
        strut(self.group, HDR_H)

        Button(self.group, "<", 0, 44, HDR_BTN_H, pattern_prev)
        self.pat_label = lv.label(self.group)
        self.pat_label.set_text(PRESETS[0][0])
        self.pat_label.align_to(self.group, lv.ALIGN.LEFT_MID, 48, 0)
        Button(self.group, ">", 176, 44, HDR_BTN_H, pattern_next)

        Button(self.group, "<", 240, 44, HDR_BTN_H, kit_prev)
        self.kit_label = lv.label(self.group)
        self.kit_label.set_text(KITS[0][1])
        self.kit_label.align_to(self.group, lv.ALIGN.LEFT_MID, 288, 0)
        Button(self.group, ">", 400, 44, HDR_BTN_H, kit_next)

        self.play_btn = Button(self.group, "STOP", 466, 100, HDR_BTN_H,
                                toggle_play, C_PLAY)

        Button(self.group, "-", 586, 44, HDR_BTN_H, bpm_down,
                repeat_cb=bpm_down_fast)
        self.bpm_label = lv.label(self.group)
        self.bpm_label.set_text("120 bpm")
        self.bpm_label.align_to(self.group, lv.ALIGN.LEFT_MID, 638, 0)
        Button(self.group, "+", 724, 44, HDR_BTN_H, bpm_up,
                repeat_cb=bpm_up_fast)

        Button(self.group, "FX", 790, 90, HDR_BTN_H, open_fx,
                rgb332(150, 90, 200))
        self.restart_btn = Button(self.group, "RESTART", 890, 110,
                                   HDR_BTN_H, restart_audio, C_RESTART)

    def set_pattern(self, t):
        self.pat_label.set_text(t)

    def set_kit(self, t):
        self.kit_label.set_text(t)

    def set_bpm(self, v):
        if app is not None and app.midi_sync:
            self.bpm_label.set_text("MIDI clk")
        else:
            self.bpm_label.set_text(str(int(v)) + " bpm")


class HeaderBottom(UIElement):
    def __init__(self):
        super().__init__()
        self.group.set_size(ROW_W, HDR_H)
        lv_depad(self.group)
        self.group.remove_flag(lv.obj.FLAG.SCROLLABLE)
        strut(self.group, HDR_H)

        Button(self.group, "SAVE", 0, 96, HDR_BTN_H, open_save_popup)
        self.update_btn = Button(self.group, "UPDATE", 106, 96, HDR_BTN_H,
                                  self._on_update)
        Button(self.group, "<", 216, 44, HDR_BTN_H, project_prev)
        self.project_label = lv.label(self.group)
        self.project_label.set_text("-- no project --")
        self.project_label.align_to(self.group, lv.ALIGN.LEFT_MID, 264, 0)
        Button(self.group, ">", 386, 44, HDR_BTN_H, project_next)
        Button(self.group, "LOAD", 436, 96, HDR_BTN_H, project_load)
        Button(self.group, "CLEAR", 546, 100, HDR_BTN_H, clear_all)
        self.sync_btn = Button(self.group, "SYNC INT", 666, 130,
                                HDR_BTN_H, toggle_midi_sync)

        self.info = lv.label(self.group)
        self.info.set_text("")
        self.info.align_to(self.group, lv.ALIGN.LEFT_MID, 812, 0)

    def set_project(self, t):
        self.project_label.set_text(t)

    def set_info(self, t):
        try:
            self.info.set_text(t)
        except Exception:
            pass

    def _on_update(self, e=None):
        self.update_btn.flash()
        update_project()


# --- kit / pattern controls -------------------------------------------

def apply_kit(idx):
    """Change the global kit. Re-points only the lanes that follow it
    (no per-lane override) - each is one short osc message to that
    lane's own fixed oscillator, plus an event-velocity refresh in case
    the new kit doesn't have a sample for that role. No reset, no
    sequence rebuild."""
    app.kit_idx = idx % len(KITS)
    for r in app.rows:
        if r.kit_override is None:
            configure_lane(r)
            r.refresh_events()
    if app.header is not None:
        app.header.set_kit(KITS[app.kit_idx][1])


def kit_prev(e=None):
    apply_kit(app.kit_idx - 1)


def kit_next(e=None):
    apply_kit(app.kit_idx + 1)


def apply_preset(idx):
    idx = idx % len(PRESETS)
    app.pat_idx = idx
    name = PRESETS[idx][0]
    steps_map = PRESETS[idx][1]
    for r in app.rows:
        r.set_steps(steps_map.get(r.name, []))
    app.header.set_pattern(name)
    _auto_save_slot()


def pattern_prev(e=None):
    apply_preset(app.pat_idx - 1)


def pattern_next(e=None):
    apply_preset(app.pat_idx + 1)


def clear_all(e=None):
    for r in app.rows:
        r.set_steps([])
    _auto_save_slot()
    app.header.set_pattern("(empty)")


# --- user projects ------------------------------------------------------
#
# A "project" is everything needed to fully restore a session: BPM, the
# global kit, every lane's own settings (steps, volume, mute/solo,
# per-lane kit/bus override), every bus's FX settings, and the entire
# pattern bank (all slots, plus which one's active). This is the "save
# my whole setup" feature - the pattern bank above is deliberately
# lighter (just steps/kit/bpm) since it's meant for quick note-pattern
# swaps during a performance without also yanking the mix around.

# Tulip's own hardware spec gives MicroPython 2MB of RAM total - shared
# with LVGL, AMY, and everything else running. Each project embeds the
# whole pattern bank (10 slots), so at the old cap of 30 a fully-used
# session's worth of projects measured out to ~350KB of JSON - and
# every single save rewrites that WHOLE file, not just the new entry,
# so later saves in a session do more work than earlier ones. Capped
# much lower to keep both the live in-memory list and each write small.
MAX_USER_PROJECTS = 8


def _json_safe(obj):
    """Recursively coerce a structure to plain JSON-safe types before
    writing, dropping or stringifying anything that isn't a clean
    None/bool/int/float/str/list/dict - and catching NaN/Infinity floats,
    which json.dump will happily emit but which aren't valid JSON and
    can make a saved file unreadable later. Belt-and-suspenders: nothing
    in this app should ever produce a bad value, but a save must never
    be the thing that corrupts a file."""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # NaN/Inf
            return 0.0
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                k = str(k)
            out[k] = _json_safe(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return None   # anything else (shouldn't happen) - drop it, don't guess


SAVE_COOLDOWN_MS = 1500


def _save_allowed():
    now = None
    try:
        now = amy.ticks_ms()
    except Exception:
        return True   # can't time it - don't block saves entirely
    if app.last_save_ms is not None and now - app.last_save_ms < SAVE_COOLDOWN_MS:
        return False
    app.last_save_ms = now
    return True

MAX_PROJECT_NAME_LEN = 20


def _deep_copy_pattern(pat):
    """An independent copy of a snapshot_pattern() dict - specifically,
    its nested "steps" dict and the per-lane lists inside it, which a
    plain dict(pat) would leave shared by reference. Not a correctness
    bug on its own (nothing mutates a stored snapshot in place), but a
    bank slot, the clipboard, and every project that embeds that slot
    could otherwise end up silently pointing at the exact same nested
    objects - worth avoiding on principle even without direct evidence
    it caused a problem."""
    if not isinstance(pat, dict):
        return pat
    out = dict(pat)
    steps = out.get("steps")
    if isinstance(steps, dict):
        out["steps"] = {k: list(v) if isinstance(v, list) else v
                         for k, v in steps.items()}
    return out


def snapshot_pattern():
    """Lightweight snapshot: live grid + kit + bpm only. Used by the
    pattern bank (see below) - deliberately does NOT touch bus/FX/lane
    mix settings, so cueing a bank slot only ever changes the notes."""
    snap = {}
    for r in app.rows:
        snap[r.name] = list(r.steps)
    return {"steps": snap, "kit": app.kit_idx, "bpm": app.bpm}


def apply_pattern_snapshot(pat):
    """Load a lightweight snapshot onto the live grid. Defensive on
    purpose: a hand-edited or partially-written JSON file must never be
    able to crash the app - anything malformed is just ignored field by
    field rather than raising."""
    if not isinstance(pat, dict):
        return
    kit = pat.get("kit", None)
    if isinstance(kit, int) and 0 <= kit < len(KITS):
        apply_kit(kit)
    bpm = pat.get("bpm", None)
    if isinstance(bpm, (int, float)) and bpm > 0:
        set_bpm(bpm)
    steps = pat.get("steps", {})
    if not isinstance(steps, dict):
        steps = {}
    for r in app.rows:
        s = steps.get(r.name, [])
        if not isinstance(s, list):
            s = []
        r.set_steps(s)


def snapshot_project():
    """The full-session snapshot: bpm/kit, every lane's own settings,
    every bus's FX, and the whole pattern bank."""
    lanes = []
    for r in app.rows:
        lanes.append({
            "name": r.name, "steps": list(r.steps), "vol": r.vol,
            "muted": r.muted, "solo": r.solo,
            "kit_override": r.kit_override, "bus": r.bus,
        })
    return {
        "bpm": app.bpm,
        "kit": app.kit_idx,
        "lanes": lanes,
        "bus_fx": [dict(f) for f in app.bus_fx],
        "bank": [_deep_copy_pattern(p) for p in app.bank_patterns],
        "bank_active": app.bank_active,
    }


def apply_project_snapshot(proj):
    """Load a full-session snapshot. Defensive throughout, field by
    field: a corrupted file, a hand-edited one, or an entry saved by an
    older version of this app (which only ever stored flat "steps" +
    kit + bpm) must never be able to crash the app."""
    if not isinstance(proj, dict):
        return

    kit = proj.get("kit", None)
    if isinstance(kit, int) and 0 <= kit < len(KITS):
        apply_kit(kit)
    bpm = proj.get("bpm", None)
    if isinstance(bpm, (int, float)) and bpm > 0:
        set_bpm(bpm)

    bus_fx = proj.get("bus_fx", None)
    if isinstance(bus_fx, list):
        for b in range(min(len(bus_fx), NUM_BUSES)):
            f = bus_fx[b]
            if not isinstance(f, dict):
                continue
            for k in BUS_FX_DEFAULTS:
                v = f.get(k, None)
                if isinstance(v, (int, float)):
                    app.bus_fx[b][k] = v
            apply_bus_fx(b)

    lanes = proj.get("lanes", None)
    if isinstance(lanes, list):
        by_name = {}
        for entry in lanes:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                by_name[entry["name"]] = entry
        for r in app.rows:
            entry = by_name.get(r.name)
            if not isinstance(entry, dict):
                continue
            ko = entry.get("kit_override", None)
            if ko is None or (isinstance(ko, int) and 0 <= ko < len(KITS)):
                r.kit_override = ko
            bus = entry.get("bus", None)
            if isinstance(bus, int) and 0 <= bus < NUM_BUSES:
                r.bus = bus
            configure_lane(r)
            r.refresh_kit_btn()
            vol = entry.get("vol", None)
            if isinstance(vol, (int, float)):
                r.vol = clampf(vol, 0.0, 1.0)
            r.muted = bool(entry.get("muted", False))
            r.mute_btn.set_color(C_MUTE_ON if r.muted else C_BTN)
            r.solo = bool(entry.get("solo", False))
            r.solo_btn.set_color(C_SOLO_ON if r.solo else C_BTN)
            s = entry.get("steps", [])
            r.set_steps(s if isinstance(s, list) else [])
        update_solo_state()
        refresh_all_events()
    else:
        # older save format: a flat {"steps": {lane_name: [..]}} dict
        steps = proj.get("steps", {})
        if not isinstance(steps, dict):
            steps = {}
        for r in app.rows:
            s = steps.get(r.name, [])
            r.set_steps(s if isinstance(s, list) else [])

    bank = proj.get("bank", None)
    if isinstance(bank, list):
        new_bank = bank[:NUM_BANKS]
        while len(new_bank) < NUM_BANKS:
            new_bank.append(None)
        app.bank_patterns = [b if isinstance(b, dict) else None
                              for b in new_bank]
    bank_active = proj.get("bank_active", None)
    if isinstance(bank_active, int) and 0 <= bank_active < NUM_BANKS:
        app.bank_active = bank_active
    app.bank_pending = None
    # load the active slot's pattern onto the live grid so what's playing
    # matches what's selected - the lanes section above may have already
    # set the grid, but keeping the two in sync is simpler and cheaper
    # than trying to detect whether they differ
    active_slot = app.bank_patterns[app.bank_active]
    if isinstance(active_slot, dict):
        apply_pattern_snapshot(active_slot)
    if app.bank_row is not None:
        app.bank_row.refresh()


def _cleanup_stale_tmp(path):
    """One-time cleanup: a previous version wrote to `path + '.tmp'`
    before switching to direct writes. If a stuck ".tmp" is still on
    disk from back then, remove it so it isn't wasting flash. Harmless
    if there's nothing there."""
    if os is None:
        return
    try:
        os.remove(path + ".tmp")
    except Exception:
        pass


GAMMA9001_BASE = 256    # presets >= this are served from mmapped flash
BAKED_KIT = 0           # SAMPLE_KITS[0] = TR-808, presets 0-18, in-image

# --- the fix that stopped the save crashes ----------------------------
# AMY's Gamma9001 drum banks (every preset >= 256) are not in RAM: Tulip
# memory-maps the `drums` flash partition and AMY reads sample data
# straight out of it, live, every audio buffer. Writing to flash
# disables the flash cache, and a read from a memory-mapped flash address
# with the cache off is an illegal access that resets the chip with no
# traceback. TR-808 (presets 0-18) is baked into the firmware image, so
# it never touches the mapping - which is exactly why saves crashed on
# every kit EXCEPT 808.
#
# So before any flash access we hard-reset every lane's oscillator onto
# its baked 808 equivalent (reset=1 tears the voice down immediately,
# read pointer and all) and restore the real kit afterwards. _fs_begin /
# _fs_end bracket the whole save; the depth counter keeps the region open
# across the settle delay and the write, and closes it exactly once.

SAVE_QUIESCE = True      # re-point mmapped presets to baked ones around a
#                          write. This is THE fix - leave it on.
QUIESCE_SETTLE_MS = 60   # wait this long after the re-point before touching
#                          flash, so AMY has consumed the reset (it eats
#                          wire messages at ~5.8ms block boundaries).
SAVE_STOP_TRANSPORT = False  # stronger fallback: hard-reset EVERY PCM osc
#                          for the save, not just the ones we think are on
#                          an mmapped preset. Costs a brief audio gap.
#                          Turn on only if a save ever still crashes.

_quiesce_depth = 0
_pcm_quiesced = False
_transport_held = False


def _fs_begin():
    """Open a region in which flash may be touched. On the outermost
    entry it quiesces the mmapped drum voices; returns True so that
    caller knows to wait QUIESCE_SETTLE_MS before its first access."""
    global _quiesce_depth
    _quiesce_depth += 1
    if _quiesce_depth != 1:
        return False
    app.saving = True
    if SAVE_QUIESCE:
        _quiesce_pcm()
    if SAVE_STOP_TRANSPORT:
        _save_transport_hold(True)
    return True


def _fs_end():
    """Close a region. Only the outermost exit restores the kit."""
    global _quiesce_depth
    if _quiesce_depth <= 0:
        return
    _quiesce_depth -= 1
    if _quiesce_depth != 0:
        return
    if SAVE_STOP_TRANSPORT:
        _save_transport_hold(False)
    if SAVE_QUIESCE:
        _restore_pcm()
    app.saving = False


def _quiesce_pcm():
    """Re-point every lane on an mmapped preset (>=256) onto its baked
    TR-808 equivalent, hard-stopping any voice still reading the mapping."""
    global _pcm_quiesced
    if _pcm_quiesced:
        return
    _pcm_quiesced = True
    baked = SAMPLE_KITS[BAKED_KIT][1]
    for r in app.rows:
        if not isinstance(r._preset, int) or r._preset < GAMMA9001_BASE:
            continue          # already on a baked sample - nothing to do
        try:
            amy.send(osc=r._osc, reset=1)   # hard stop: kills the read pointer
        except Exception:
            pass
        sub = baked.get(r.name, NO_SAMPLE)
        try:
            if sub != NO_SAMPLE:
                amy.send(osc=r._osc, wave=amy.PCM, preset=sub)
            r._preset = sub
        except Exception as ex:
            print("[save] quiesce failed on osc", r._osc, ex)
    try:
        amy.send(osc=TEST_OSC, reset=1)     # the kit auditioner's scratch osc
    except Exception:
        pass


def _restore_pcm():
    """Put the real kit back - configure_all_lanes re-sends because
    _quiesce_pcm left each _preset holding the baked value."""
    global _pcm_quiesced
    if not _pcm_quiesced:
        return
    _pcm_quiesced = False
    try:
        configure_all_lanes()
    except Exception as ex:
        print("[save] restore failed:", ex)


def _save_transport_hold(on):
    """Hard-reset EVERY oscillator in the PCM range and rebuild the audio
    config afterwards - the strongest quiesce available from Python."""
    global _transport_held
    if on == _transport_held:
        return
    _transport_held = on
    if on:
        try:
            for o in range(PCM_OSC_BASE, PCM_OSC_BASE + 8):
                amy.send(osc=o, reset=1)
        except Exception as ex:
            print("[save] full stop failed:", ex)
    else:
        try:
            activate_buses()
            configure_all_lanes()
            apply_all_fx()
            refresh_all_events()
        except Exception as ex:
            print("[save] full restart failed:", ex)


# --- saving: the whole library in one file ----------------------------
# All projects live in one JSON list at PROJECTS_FILE. A save serialises
# the list in RAM (no flash yet), then writes it in a single f.write()
# inside a quiesced region, on a defer tick a beat after the button press
# so it never runs inside the LVGL event callback. Playback continues
# through the write - for the ~20ms of it the drums play 808 samples.

_pending_write = [None]   # (text, label) waiting for the settle delay


def load_user_projects():
    _cleanup_stale_tmp(PROJECTS_FILE)
    try:
        with open(PROJECTS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)][:MAX_USER_PROJECTS]
    except Exception:
        pass
    return []


def persist_projects(label=None):
    """Write the whole library to PROJECTS_FILE. Returns immediately; the
    write lands a few ticks later and `label` shows in the info line then."""
    if gc is not None:
        try:
            gc.collect()
        except Exception:
            pass
    try:
        text = json.dumps(_json_safe(app.user_projects))
    except Exception as ex:
        print("[save] SERIALIZE FAILED  repr:", repr(ex), " args:", ex.args)
        if app.header2 is not None:
            app.header2.set_info("save FAILED")
        return False
    _pending_write[0] = (text, label)
    _fs_begin()                       # quiesce now; write after the settle
    defer_safe(_do_write, None, QUIESCE_SETTLE_MS)
    return True


def _do_write(arg=None):
    """Runs QUIESCE_SETTLE_MS after the quiesce, so nothing is reading the
    mmapped drum bank. One open/write/flush/close, then close the region."""
    pend = _pending_write[0]
    _pending_write[0] = None
    if pend is None:
        _fs_end()
        return
    text, label = pend
    ok = True
    try:
        same = False
        try:
            with open(PROJECTS_FILE, "r") as f:
                same = (f.read() == text)   # unchanged? skip the write entirely
        except Exception:
            same = False
        if not same:
            f = open(PROJECTS_FILE, "w")
            try:
                f.write(text)
                f.flush()
            finally:
                f.close()
    except Exception as ex:
        ok = False
        print("[save] WRITE FAILED  repr:", repr(ex), " args:", ex.args)
    if label and app.header2 is not None:
        try:
            app.header2.set_info(label if ok else (label + " - save FAILED"))
        except Exception:
            pass
    _fs_end()


def open_save_popup(e=None):
    """SAVE button: open the name-entry popup rather than saving
    immediately - the actual save happens once a name is confirmed,
    see do_save_project()."""
    if app.save_popup is not None:
        app.save_popup.show()


def do_save_project(name):
    """The actual save, called once a name has been confirmed. Never
    allowed to take down the app - any failure here is reported in the
    info line instead of raising. Returns True if the save proceeded
    (whether it succeeded or failed for some other reason), False if it
    was blocked by the cooldown - callers use that to decide whether to
    close the save popup."""
    if not _save_allowed():
        if app.header2 is not None:
            app.header2.set_info("saving too fast - wait a sec")
        return False
    try:
        name = (name or "").strip()[:MAX_PROJECT_NAME_LEN]
        if not name:
            name = "Project %d" % (len(app.user_projects) + 1)
        proj = snapshot_project()
        proj["name"] = name
        # Overwrite in place if a project with this name already exists,
        # rather than appending a duplicate.
        existing = -1
        for i in range(len(app.user_projects)):
            e = app.user_projects[i]
            if isinstance(e, dict) and e.get("name") == name:
                existing = i
                break
        if existing >= 0:
            app.user_projects[existing] = proj
            app.project_idx = existing
        else:
            while len(app.user_projects) >= MAX_USER_PROJECTS:
                app.user_projects.pop(0)   # bound growth - oldest goes first
            app.user_projects.append(proj)
            app.project_idx = len(app.user_projects) - 1
        if app.header2 is not None:
            app.header2.set_project(name)
            app.header2.set_info("saving...")
        # returns straight away - the write lands a few ticks later and
        # the info line updates when it does
        persist_projects(("updated " if existing >= 0 else "saved ") + name)
    except Exception as ex:
        print("save failed  repr:", repr(ex), " args:", ex.args)
        if app.header2 is not None:
            app.header2.set_info("save FAILED")
    return True


def _show_project():
    if 0 <= app.project_idx < len(app.user_projects):
        app.header2.set_project(app.user_projects[app.project_idx].get("name", "?"))
    else:
        app.header2.set_project("-- no project --")
def project_prev(e=None):
    if app.user_projects:
        app.project_idx = (app.project_idx - 1) % len(app.user_projects)
        _show_project()


def update_project(e=None):
    """Overwrite the currently-selected project with the live state -
    same slot, new snapshot, no name prompt. Silently a no-op if there
    is nothing selected (user hasn't loaded or saved anything yet, in
    which case the SAVE button is the right action)."""
    if not app.user_projects:
        if app.header2 is not None:
            app.header2.set_info("nothing to update - use SAVE")
        return
    if app.project_idx < 0 or app.project_idx >= len(app.user_projects):
        if app.header2 is not None:
            app.header2.set_info("select a project first")
        return
    if not _save_allowed():
        if app.header2 is not None:
            app.header2.set_info("saving too fast - wait a sec")
        return
    try:
        name = app.user_projects[app.project_idx].get("name", "project")
        proj = snapshot_project()
        proj["name"] = name
        app.user_projects[app.project_idx] = proj
        if app.header2 is not None:
            app.header2.set_info("saving...")
        persist_projects("updated " + name)
    except Exception as ex:
        print("update failed  repr:", repr(ex), " args:", ex.args)
        if app.header2 is not None:
            app.header2.set_info("update FAILED")


def project_next(e=None):
    if app.user_projects:
        app.project_idx = (app.project_idx + 1) % len(app.user_projects)
        _show_project()


def project_load(e=None):
    if not app.user_projects:
        app.header2.set_info("nothing saved")
        return
    if app.project_idx < 0 or app.project_idx >= len(app.user_projects):
        return
    proj = app.user_projects[app.project_idx]
    apply_project_snapshot(proj)
    app.header.set_pattern(proj.get("name", "?"))
    app.header2.set_info("loaded")



# --- pattern bank: A-H slots, cued to the start of the bar -------------
#
# A separate, small, fixed-size (8 slot) bank meant for live use: select
# a different letter WHILE PLAYING and the switch is queued rather than
# instant, landing cleanly at the top of the next bar (see _led_tick,
# which applies app.bank_pending at step 0 - the same mechanism already
# used to queue play/stop). While stopped, selecting a letter applies
# immediately, since there's no audio to glitch and it's more convenient
# for browsing/editing. This is separate from the named user-pattern
# library above: that's a long-term save-by-name collection, this is a
# small quick-recall set for performing.


def _apply_bank(i):
    """Switch to bank slot i. Snapshots the current slot first so any
    edits made since the last switch aren't lost, then loads the new
    slot and snapshots that too so it shows as populated immediately."""
    # commit any edits to the slot we're leaving
    if app.bank_patterns[app.bank_active] is not None or any(r.steps for r in app.rows):
        app.bank_patterns[app.bank_active] = snapshot_pattern()
    slot = app.bank_patterns[i]
    if slot is None:
        for r in app.rows:
            r.set_steps([])
    else:
        apply_pattern_snapshot(slot)
    app.bank_active = i
    app.bank_pending = None
    # snapshot the newly-loaded slot so it's marked populated in the bank
    app.bank_patterns[i] = snapshot_pattern()
    if app.header is not None:
        app.header.set_pattern("Bank " + BANK_LETTERS[i])
    if app.bank_row is not None:
        app.bank_row.refresh()


def bank_select(i):
    # re-tapping the active slot or a queued slot cancels any pending queue
    if i == app.bank_active or i == app.bank_pending:
        if app.bank_pending is not None:
            app.bank_pending = None
            if app.bank_row is not None:
                app.bank_row.refresh()
        return
    if app.playing:
        app.bank_pending = i              # lands at the next bar (see _led_tick)
        if app.bank_row is not None:
            app.bank_row.refresh()
    else:
        _apply_bank(i)                    # stopped - nothing to glitch, do it now


def _auto_save_slot():
    """Snapshot the current grid into the active bank slot, in RAM only.
    No flash write. Called after every grid edit (step toggle, random
    fill, preset load, clear) so the slot always reflects live state.
    The data only reaches disk when the user saves a project."""
    try:
        app.bank_patterns[app.bank_active] = snapshot_pattern()
        if app.bank_row is not None:
            app.bank_row.refresh()
    except Exception:
        pass   # silently ignore if called before app is fully built


def bank_copy(e=None):
    """Copy the active slot's current state to the clipboard. RAM only,
    no flash write."""
    app.bank_clipboard = snapshot_pattern()
    if app.bank_row is not None:
        app.bank_row.copy_btn.flash()
    if app.header2 is not None:
        app.header2.set_info("copied " + BANK_LETTERS[app.bank_active])


def bank_paste(e=None):
    """Paste the clipboard into the active slot and load it onto the
    grid. RAM only - the change is preserved when the user next saves
    a project, not written to flash here."""
    if app.bank_clipboard is None:
        if app.header2 is not None:
            app.header2.set_info("nothing copied")
        return
    apply_pattern_snapshot(app.bank_clipboard)
    app.bank_patterns[app.bank_active] = _deep_copy_pattern(app.bank_clipboard)
    _auto_save_slot()
    if app.bank_row is not None:
        app.bank_row.paste_btn.flash()
        app.bank_row.refresh()
    if app.header2 is not None:
        app.header2.set_info("pasted to " + BANK_LETTERS[app.bank_active])


# --- tempo / transport -------------------------------------------------

def set_bpm(v):
    if app.midi_sync:
        app.header.set_bpm(app.bpm)      # tempo comes from MIDI clock
        return
    app.bpm = clampf(v, MIN_BPM, MAX_BPM)
    try:
        sequencer.tempo(app.bpm)
    except Exception:
        pass
    app.header.set_bpm(app.bpm)


def bpm_up(e=None):
    set_bpm(app.bpm + BPM_STEP)


def bpm_down(e=None):
    set_bpm(app.bpm - BPM_STEP)


def bpm_up_fast(e=None):
    set_bpm(app.bpm + BPM_STEP_FAST)


def bpm_down_fast(e=None):
    set_bpm(app.bpm - BPM_STEP_FAST)


def set_midi_sync(on):
    """external_midi_sync=1 makes AMY's sequencer follow incoming MIDI
    clock and start/stop, so an external device drives tempo and
    transport."""
    app.midi_sync = bool(on)
    try:
        amy.send(external_midi_sync=1 if app.midi_sync else 0)
    except Exception:
        pass
    if not app.midi_sync:
        try:
            sequencer.tempo(app.bpm)
        except Exception:
            pass
    if app.header2 is not None:
        app.header2.sync_btn.set_text("SYNC EXT" if app.midi_sync
                                       else "SYNC INT")
        app.header2.sync_btn.set_color(C_KIT_ON if app.midi_sync else C_BTN)
    if app.header is not None:
        app.header.set_bpm(app.bpm)


def toggle_midi_sync(e=None):
    set_midi_sync(not app.midi_sync)


def toggle_play(e=None):
    """Queue a transport change for the top of the next bar, so playback
    always starts and stops in time. Tapping again cancels."""
    want = not app.playing
    if app.pending_play == want:
        app.pending_play = None
    else:
        app.pending_play = want
    _update_play_btn()


def _apply_transport(playing):
    app.playing = playing
    app.pending_play = None
    refresh_all_events()      # events fire at vol when playing, else MUTE_VEL
    if not playing:
        clear_leds()
    cv_transport(playing)     # the rack starts/stops with the app
    _update_play_btn()


def _update_play_btn():
    if app.header is None:
        return
    b = app.header.play_btn
    if app.pending_play is not None:
        b.set_text("PLAY..." if app.pending_play else "STOP...")
        b.set_color(C_PENDING)
    else:
        b.set_text("STOP" if app.playing else "PLAY")
        b.set_color(C_PLAY if app.playing else C_BTN)


# The visual is a single lightweight TulipSequence callback (_led_tick,
# defined above) that moves the LED row and applies queued transport
# changes at the top of the bar. There is no separate Python beat
# callback and no deferred ~30fps redraw loop: AMY's own C sequencer
# fires every drum hit sample-accurately from app.seq (see "AMYSequence
# for audio events" above), and _led_tick does at most two LVGL colour
# writes per 1/32 step. That is strictly less Python work per step than
# the old design (a per-step amy.send from a Python beat callback, PLUS
# a free-running ~30fps defer loop redrawing playhead cells).


# =====================================================================
# CV CLOCK OUT  ->  EURORACK   (Makerfabs Mabee DAC, GP8413)
#
# Plug the Mabee DAC into Tulip's I2C port on the side (GND/VCC/SDA/SCL -
# the connector is keyed, so it only goes in one way). Tulip already
# ships the driver for this exact board: tulip/shared/py/mabeedac.py,
# which is frozen into the firmware, so `import mabeedac` just works.
# Its whole API is:
#
#     mabeedac.set(volts, channel=0)     # 0.0 - 10.0 V, channel 0 or 1
#
# Internally that is one I2C write to address 89 (0x59), register 0x02
# for VOUT0 and 0x04 for VOUT1, at 400kHz - well under a millisecond, so
# it is cheap enough to do from the step callback.
#
#     VOUT0 -> clock   : a pulse every CV_DIV steps while the transport runs
#     VOUT1 -> run/reset (see CV_VOUT1 below)
#
# Levels are 0V low / CV_HIGH_V high. 5V is the eurorack standard for
# clocks and gates; the DAC can go to 10V but most modules only need 5V
# and some don't like 10V on a gate input, so 5V is the default.
#
# Sync: the pulses come from _led_tick, which Tulip drives off AMY's own
# sequencer clock, so the CV clock is locked to the same clock the drums
# play on and follows tempo and MIDI-sync changes automatically. It is
# still a software clock dispatched through mp_sched, so expect a
# millisecond or two of jitter - fine for driving a eurorack sequencer,
# not a substitute for an analogue master clock if you need sample-level
# accuracy.
#
# Everything here fails soft: if the DAC isn't plugged in, or is
# unplugged mid-session, CV output disables itself and the drum machine
# carries on.
# =====================================================================

# NOTE ON POWER. The Mabee DAC makes its 0-10V rail with an onboard boost
# converter (the 3R3 inductor next to the switcher). That converter draws
# current the moment the board has power - whether or not anything talks
# to it over I2C - and its worst moment is inrush at power-on, when
# Tulip's display backlight, PSRAM and radios are all coming up too. If
# Tulip browns out at boot with the DAC plugged in, that is the cause,
# and no change in here can fix it: Tulip's own boot never opens I2C
# (see tulip/shared/py/_boot.py), so a plugged-in board can only affect
# startup electrically. Power the DAC from its own 5V supply, sharing
# ground with Tulip, and leave VCC on the Tulip connector disconnected.
#
# CV_AUTOSTART exists so you can boot with the DAC attached but idle
# while you sort the supply out - set it False and use the CV button.

CV_AUTOSTART = True      # bring the DAC up automatically a moment after
#                          the UI is ready (False = start with CV off,
#                          enable it by hand with the CV button)
CV_INIT_DELAY_MS = 2500  # keep DAC bringup off the critical boot path
CV_ENABLED = True        # master switch (the CV button toggles this too)
CV_HIGH_V = 5.0          # gate/clock high level in volts (eurorack: 5V)
CV_LOW_V = 0.0
CV_CLOCK_CH = 0          # VOUT0
CV_RUN_CH = 1            # VOUT1
CV_DIV = 2               # one clock pulse every N of the 32 steps:
#                          1 = 1/32, 2 = 1/16, 4 = 1/8, 8 = 1/4 note.
#                          2 (a 16th-note clock) suits most eurorack gear.
CV_PULSE_MS = 10         # clock/reset pulse width. Tulip's defer fires on
#                          AMY sequencer ticks (~10ms at 120bpm), so the
#                          real pulse is 10-20ms - plenty for eurorack,
#                          and short enough not to swallow a 1/32 step.
CV_VOUT1 = "off"         # "off"   - VOUT1 parked at 0V and never touched
#                                    again (clock on VOUT0 only)
#                          "run"   - high the whole time the transport runs
#                          "reset" - short pulse at the top of every bar
CV_MAX_FAILS = 5         # consecutive I2C errors before giving up

# SOFT START. The first clock pulse after power-up is a 0V -> 5V step,
# which asks the DAC's boost converter for its hardest transient right
# when the rest of the board is still settling. Instead of slamming it,
# ease the output up to CV_HIGH_V and back down once at init. That
# pre-charges the output stage gradually, and no clock pulses are sent
# until the ramp has finished.
CV_RAMP_STEPS = 16       # increments each way (0 disables the ramp)
CV_RAMP_MS = 40          # gap between increments -> ~1.3s for the pair

# The DAC does NOT have Tulip's I2C bus to itself. A scan shows two
# devices: 0x59 (the Mabee) and 0x5D - which is Tulip's own GT911
# TOUCHSCREEN. gt911_touchscreen.c installs the ESP-IDF driver on the
# same peripheral we use here (I2C_NUM_0, IO17/IO18):
#
#     i2c_param_config(I2C_NUM, &i2c_conf);
#     i2c_driver_install(I2C_NUM, i2c_conf.mode, 0, 0, 0);
#
# and pins.h sets I2C_CLK_FREQ to 100000 for this board. Tulip's shipped
# mabeedac.py opens the bus at 400000 instead, which reconfigures the
# peripheral out from under a driver that is actively polling the
# touchscreen. The GT911 can do 400kHz, so it may well be fine - but
# there is no reason to take the chance for a DAC we write to a handful
# of times a second. We talk to it at the bus's existing 100kHz instead,
# which makes our setup a no-op as far as the touchscreen is concerned.
#
# The protocol below is exactly what mabeedac.py does: 16-bit value,
# low byte first, to register 0x02 (VOUT0) or 0x04 (VOUT1).

CV_I2C_FREQ = 100000     # match the touchscreen; do not raise this
CV_ADDR = 89             # 0x59 default; 88 (0x58) if the A0 link is cut

# OUTPUT RANGE. The GP8413 has a range register, and what comes out of
# the jack depends on it: the value we send is a FRACTION of full scale,
# not a voltage. Tulip's mabeedac.py never writes that register - it just
# assumes the chip is in its 0-10V range, which is how the Makerfabs
# board is sold and almost certainly how it ships.
#
# So asking for 5.0V sends half scale, which is 5V on a 0-10V range but
# would be 2.5V on a 0-5V one. MEASURE IT with cv_test() before trusting
# it (see that function below). If you measure 2.5V instead of 5V, the
# chip is in the 5V range: set CV_SET_RANGE_10V = True and it will be
# configured explicitly at startup instead of assumed.
#
# Register/values are from DFRobot's own GP8XXX library (setDACOutRange):
# register 0x01, 0x11 = 0-10V, 0x00 = 0-5V.
CV_SET_RANGE_10V = False  # True = write the 0-10V range at init
CV_RANGE_REG = 0x01
CV_RANGE_10V = 0x11

_cv = None               # the DAC handle once we've proven it responds
_cv_fails = 0
_cv_ready = False        # False until the soft-start ramp has finished
_cv_tokens = {}          # channel -> id of the pulse that owns it


class _MabeeDac:
    """Minimal GP8413 writer - same registers as Tulip's mabeedac.py, but
    at the bus's existing clock so we don't reconfigure it under the
    touchscreen driver."""
    def __init__(self):
        from machine import I2C
        self.bus = I2C(0, freq=CV_I2C_FREQ)
        if CV_SET_RANGE_10V:
            # only when asked - an unnecessary config write to a chip
            # that is already right is a risk with no upside
            self.bus.writeto_mem(CV_ADDR, CV_RANGE_REG,
                                  bytes([CV_RANGE_10V]))

    def set(self, volts, channel=0):
        v = int((volts / 10.0) * 65535.0)
        if v > 65535:
            v = 65535
        if v < 0:
            v = 0
        addr = CV_ADDR if channel < 2 else 88
        reg = 0x02 if (channel % 2) == 0 else 0x04
        self.bus.writeto_mem(addr, reg, bytes([v & 0xff, (v >> 8) & 0xff]))


def cv_init():
    """Bring up the DAC if one is attached. Never raises - a missing or
    unplugged board just means no CV output."""
    global _cv, _cv_fails
    _cv = None
    _cv_fails = 0
    if not CV_ENABLED:
        return False
    try:
        dac = _MabeeDac()
    except Exception as ex:
        print("[cv] could not open I2C:", repr(ex))
        return False
    try:
        # a real write is the probe - it raises if nothing ACKs on I2C.
        # VOUT1 is parked at 0V here even when CV_VOUT1 is "off", so it
        # is left at a known level rather than whatever it held before.
        dac.set(CV_LOW_V, channel=CV_CLOCK_CH)
        dac.set(CV_LOW_V, channel=CV_RUN_CH)
    except Exception as ex:
        print("[cv] no DAC responding on the I2C port:", repr(ex))
        return False
    _cv = dac
    print("[cv] Mabee DAC ready - clock on VOUT0, VOUT1 %s" % CV_VOUT1)
    _cv_ramp_start()
    return True


def _cv_ramp_start():
    """Ease VOUT0 up to CV_HIGH_V and back to 0 before any clock runs."""
    global _cv_ready
    _cv_ready = False
    if CV_RAMP_STEPS <= 0:
        _cv_write(CV_LOW_V, CV_CLOCK_CH)
        _cv_ready = True
        return
    _cv_ramp_step(0)


def _cv_ramp_step(i=0):
    """One increment of the soft start, chained on the defer clock."""
    global _cv_ready
    if _cv is None:
        _cv_ready = False
        return
    n = CV_RAMP_STEPS
    if not isinstance(i, int):
        i = 0
    if i > 2 * n:
        _cv_write(CV_LOW_V, CV_CLOCK_CH)
        _cv_ready = True
        print("[cv] soft start done - clock running")
        return
    # up for the first half, back down for the second
    frac = (i if i <= n else (2 * n - i)) / float(n)
    _cv_write(CV_HIGH_V * frac, CV_CLOCK_CH)
    defer_safe(_cv_ramp_step, i + 1, CV_RAMP_MS)


def cv_present():
    return _cv is not None


def _cv_write(volts, ch):
    """One DAC write, with a failure budget: if the board stops ACKing
    (unplugged mid-session) we disable CV rather than raise on every
    step for the rest of the session."""
    global _cv, _cv_fails
    if _cv is None:
        return
    try:
        _cv.set(volts, channel=ch)
        _cv_fails = 0
    except Exception as ex:
        _cv_fails += 1
        if _cv_fails >= CV_MAX_FAILS:
            _cv = None
            print("[cv] DAC stopped responding - CV output disabled:", repr(ex))
            if app is not None and app.header2 is not None:
                app.header2.set_info("CV lost - check the DAC")
            if app is not None and app.bank_row is not None:
                app.bank_row.refresh_cv()


def _cv_pulse(ch):
    """Take `ch` high now and schedule it low CV_PULSE_MS later. The
    token guards the case where the next pulse starts before this one's
    low lands (very fast tempo + CV_DIV=1) - the stale low is dropped
    instead of cutting the new pulse short."""
    tok = _cv_tokens.get(ch, 0) + 1
    _cv_tokens[ch] = tok
    _cv_write(CV_HIGH_V, ch)

    def _low(arg=None, _ch=ch, _tok=tok):
        if _cv_tokens.get(_ch) == _tok:
            _cv_write(CV_LOW_V, _ch)

    defer_safe(_low, None, CV_PULSE_MS)


def _cv_tick(step):
    """Called from _led_tick, once per 1/32 step, on the AMY clock."""
    if _cv is None or not _cv_ready or not app.playing:
        return
    if step % CV_DIV == 0:
        _cv_pulse(CV_CLOCK_CH)
    if CV_VOUT1 == "reset" and step == 0:
        _cv_pulse(CV_RUN_CH)


def cv_transport(playing):
    """Follow the app's transport: the eurorack side starts and stops
    with the drum machine."""
    if _cv is None:
        return
    if playing:
        if CV_VOUT1 == "run":
            _cv_write(CV_HIGH_V, CV_RUN_CH)
    else:
        # drop everything low so nothing downstream is left gated on
        _cv_tokens[CV_CLOCK_CH] = _cv_tokens.get(CV_CLOCK_CH, 0) + 1
        _cv_tokens[CV_RUN_CH] = _cv_tokens.get(CV_RUN_CH, 0) + 1
        _cv_write(CV_LOW_V, CV_CLOCK_CH)
        if CV_VOUT1 != "off":
            _cv_write(CV_LOW_V, CV_RUN_CH)


def cv_all_low():
    """Leave the rack quiet on quit."""
    if _cv is None:
        return
    try:
        _cv_write(CV_LOW_V, CV_CLOCK_CH)
        _cv_write(CV_LOW_V, CV_RUN_CH)
    except Exception:
        pass


def toggle_cv(e=None):
    """CV button: turn the clock output on or off."""
    global CV_ENABLED
    CV_ENABLED = not CV_ENABLED
    if CV_ENABLED:
        if cv_init():
            cv_transport(app.playing)
            app.header2.set_info("CV clock on (VOUT0)")
        else:
            CV_ENABLED = False
            app.header2.set_info("no DAC found on I2C")
    else:
        cv_all_low()
        globals()["_cv"] = None
        globals()["_cv_ready"] = False
        app.header2.set_info("CV clock off")
    if app.bank_row is not None:
        app.bank_row.refresh_cv()


def _cv_autostart(arg=None):
    """Deferred DAC bringup, a moment after the UI is live."""
    if not CV_ENABLED:
        return
    if cv_init():
        cv_transport(app.playing)
        if app.header2 is not None:
            app.header2.set_info("CV clock on (VOUT0)")
    if app.bank_row is not None:
        app.bank_row.refresh_cv()


def cv_scan():
    """List everything answering on Tulip's I2C port:

        import drumster8; drumster8.cv_scan()

    There is only one I2C bus exposed on Tulip and it is bus 0. Tulip's
    mpconfigport.h points MicroPython's I2C0 defaults at the Grove
    connector (MICROPY_HW_I2C0_SDA = 17, SCL = 18, matching IO17/IO18 on
    the schematic), so a bare I2C(0) is already the right bus - the DAC
    cannot be on some other one."""
    try:
        from machine import I2C
        found = I2C(0, freq=400000).scan()
    except Exception as ex:
        print("[cv] I2C scan failed:", repr(ex))
        return []
    if not found:
        print("[cv] nothing answering on I2C(0).")
        print("[cv] the bus is fine - check VCC, GND and the pull-ups.")
        print("[cv] (Tulip fits none of its own: R27 is NC on the r11 board,")
        print("[cv]  so the accessory has to provide them.)")
        return found
    for a in found:
        who = ""
        if a == 89:
            who = "  <- Mabee DAC, default address: use channel 0 / 1"
        elif a == 88:
            who = "  <- Mabee DAC with A0 removed: use channel 2 / 3"
        print("[cv] 0x%02x (%d)%s" % (a, a, who))
    return found


def cv_test(volts=5.0, channel=0):
    """Hold a steady voltage on an output so you can put a meter on it:

        import drumster8
        drumster8.cv_test(5.0)     # measure VOUT0 tip vs sleeve
        drumster8.cv_test(0.0)     # back to 0V

    Press STOP first, or the running clock will overwrite it on the very
    next step. Returns the exact bytes sent, so you can check the maths
    against what the meter says."""
    if _cv is None:
        print("[cv] no DAC - run cv_scan() first")
        return None
    if app is not None and app.playing:
        print("[cv] NOTE: transport is running - press STOP or the clock")
        print("[cv]       will overwrite VOUT0 on the next step")
    val = int((volts / 10.0) * 65535.0)
    if val > 65535:
        val = 65535
    if val < 0:
        val = 0
    _cv_write(volts, channel)
    print("[cv] VOUT%d <- %.3fV  (code %d = 0x%04x of 65535 full scale,"
           % (channel, volts, val, val))
    print("[cv]  bytes %s to reg 0x%02x at addr %d)"
           % ([val & 0xff, (val >> 8) & 0xff],
              0x02 if (channel % 2) == 0 else 0x04,
              CV_ADDR if channel < 2 else 88))
    print("[cv] expect %.2fV on a 0-10V range, %.2fV on a 0-5V range"
           % (volts, volts / 2.0))
    return val


def cv_status():
    """From the REPL: import drumster8; drumster8.cv_status()"""
    print("CV enabled :", CV_ENABLED)
    print("DAC present:", cv_present())
    print("clock      : VOUT%d, 1 pulse / %d steps (%s note), %.1fV, %dms"
           % (CV_CLOCK_CH, CV_DIV,
              {1: "1/32", 2: "1/16", 4: "1/8", 8: "1/4"}.get(CV_DIV, "?"),
              CV_HIGH_V, CV_PULSE_MS))
    print("VOUT%d      : %s" % (CV_RUN_CH, CV_VOUT1))


# --- diagnostics -------------------------------------------------------

# Kit checker. Each sample kit maps our 7 drum roles to a specific PCM
# preset (see SAMPLE_KITS above). A kit only sounds a role if it has a
# preset assigned - some banks genuinely don't have all 7 (MR-12 has no
# clap/cymbal/tom; that's the kit's real sample set, not a load failure).
# These preview every role on a kit through a scratch oscillator so you
# can hear (and adjust, in SAMPLE_KITS) exactly what's assigned.

_kit_test_q = []


def _kit_test_step(arg=None):
    if not _kit_test_q:
        print("kit test done.")
        return
    fn = _kit_test_q.pop(0)
    try:
        fn()
    except Exception as ex:
        print("  kit test step error:", ex)
    defer_safe(_kit_test_step, 0, 450)


def _kit_hit(label, role, preset):
    def go():
        if preset == NO_SAMPLE:
            print("   %s -- (no sample assigned)" % label)
            return
        print("   %s (preset %d)" % (label, preset))
        try:
            amy.send(osc=TEST_OSC, wave=amy.PCM, preset=preset)
            amy.send(osc=TEST_OSC, vel=1)
        except Exception as ex:
            print("    (send failed: %s)" % ex)
    return go


def _kit_load(idx):
    name, roles = SAMPLE_KITS[idx]

    def go():
        print("--- kit %d: %s ---" % (idx, name))
    return go


def test_kit(idx=None):
    """Play every drum role on ONE kit, ~0.45s apart, so you can hear
    exactly which samples that kit has assigned. Defaults to the current
    global kit. From the REPL:
           import tulipdrums
           tulipdrums.test_kit(3)     # 3 = MR-12
    If a role prints "no sample assigned", that kit's SAMPLE_KITS entry
    has NO_SAMPLE there (not a load failure) - edit SAMPLE_KITS to add
    one. Uses a scratch oscillator, so it won't disturb what's playing."""
    global _kit_test_q
    if idx is None:
        idx = app.kit_idx
    idx = idx % len(KITS)
    name, roles = SAMPLE_KITS[idx]
    _kit_test_q = [_kit_load(idx)]
    for role_name, _note, _vol in ELEMENTS:
        preset = roles.get(role_name, NO_SAMPLE)
        _kit_test_q.append(_kit_hit("%-7s" % role_name, role_name, preset))
    _kit_test_step()


def test_all_kits():
    """Sweep every kit x every drum role so you can hear exactly which
    samples each kit has assigned. From the REPL:
           import tulipdrums; tulipdrums.test_all_kits()
    Takes about half a minute. Uses a scratch oscillator."""
    global _kit_test_q
    _kit_test_q = []
    for k in range(len(KITS)):
        name, roles = SAMPLE_KITS[k]
        _kit_test_q.append(_kit_load(k))
        for role_name, _note, _vol in ELEMENTS:
            preset = roles.get(role_name, NO_SAMPLE)
            _kit_test_q.append(_kit_hit("%-7s" % role_name, role_name, preset))
    _kit_test_step()


_KNOWN_DEBRIS = [
    # bank was previously its own file; it's now embedded in the project
    # file, so the old standalone file is debris
    "/user/tulipdrums_bank.json",
    # left behind by the old atomic-write version (.tmp files that
    # never got renamed into place because open() on them was failing)
    "/user/tulipdrums_projects.json.tmp",
    "/user/tulipdrums_bank.json.tmp",
    # the old filename from before "user patterns" was renamed to
    # "user projects" - orphaned, nothing reads it anymore
    "/user/tulipdrums_patterns.json",
    # left behind by the old on-device save diagnostics
    "/user/_drumster_difftest.json",
    "/user/_drumster_sizetest.json",
    # left behind by the manual REPL diagnostic snippets from earlier
    # troubleshooting
    "/user/_test0.json", "/user/_test1.json", "/user/_test2.json",
    "/user/_test3.json", "/user/_test4.json",
    "/user/_sizetest.json",
    # the per-project files from the brief one-file-per-slot experiment
    "/user/tulipdrums_p0.json", "/user/tulipdrums_p1.json",
    "/user/tulipdrums_p2.json", "/user/tulipdrums_p3.json",
    "/user/tulipdrums_p4.json", "/user/tulipdrums_p5.json",
    "/user/tulipdrums_p6.json", "/user/tulipdrums_p7.json",
    # the crash-debug log
    "/user/drumster_log.txt",
]


def cleanup_old_files(wipe_saved_data=False):
    """Remove debris left on /user by earlier versions of this app (old
    .tmp files, the pre-rename patterns file, leftover diagnostic test
    files). Safe to run any time - only ever removes files, only ever
    the specific known names below, and reports what it finds either
    way. From the REPL:
           import tulipdrums; tulipdrums.cleanup_old_files()

    wipe_saved_data=True ALSO deletes your current saved projects and
    pattern bank (tulipdrums_projects.json / tulipdrums_bank.json) for
    a completely fresh start. Off by default - that's real saved work."""
    if os is None:
        print("no os module available - can't list/remove files")
        return
    print("=== CLEANUP ===")
    try:
        print("current /user contents:", os.listdir("/user"))
    except Exception as ex:
        print("couldn't list /user  repr:", repr(ex))

    removed = []
    for path in _KNOWN_DEBRIS:
        try:
            os.stat(path)   # raises if it doesn't exist - nothing to do
        except Exception:
            continue
        try:
            os.remove(path)
            removed.append(path)
            print("removed:", path)
        except Exception as ex:
            print("FAILED to remove", path, " repr:", repr(ex))

    if wipe_saved_data:
        for path in (PROJECTS_FILE,):
            try:
                os.stat(path)
            except Exception:
                continue
            try:
                os.remove(path)
                removed.append(path)
                print("removed (fresh start):", path)
            except Exception as ex:
                print("FAILED to remove", path, " repr:", repr(ex))

    if not removed:
        print("nothing to clean up - /user is already tidy")
    print("================")


def diag():
    """Print app state. Run from the REPL if anything looks wrong:
           import tulipdrums; tulipdrums.diag()"""
    print("--- tulipdrums diag ---")
    try:
        print("playing/pending :", app.playing, "/", app.pending_play)
        print("rebuilding      :", app.rebuilding)
        print("current step    :", app.current_step)
        print("bpm / midi_sync :", app.bpm, "/", app.midi_sync)
        print("global kit      :", app.kit_idx, KITS[app.kit_idx][1])
        print("clock (led_seq) :", "ok" if app.led_seq is not None else "None")
        print("bank active/pend:", BANK_LETTERS[app.bank_active], "/",
               ("-" if app.bank_pending is None else BANK_LETTERS[app.bank_pending]))
        print("bank filled     :", "".join(
            BANK_LETTERS[i] if app.bank_patterns[i] is not None else "."
            for i in range(NUM_BANKS)))
        for r in app.rows:
            print("  %-7s kit=%-4s bus=%d osc=%-3s preset=%-4s steps=%-2d "
                   "mute=%s solo=%s vol=%.2f"
                   % (r.name,
                      "-" if r.kit_override is None else r.kit_override,
                      r.bus, r.osc(), r._preset, len(r.steps),
                      r.muted, r.solo, r.vol))
    except Exception as ex:
        print("diag error:", ex)
    print("-----------------------")


# --- lifecycle ---------------------------------------------------------

def quit(screen):
    try:
        cv_all_low()          # don't leave a gate stuck high in the rack
    except Exception:
        pass
    try:
        if screen.seq is not None:
            screen.seq.clear()
    except Exception:
        pass
    try:
        screen.seq = None
    except Exception:
        pass
    try:
        if screen.led_seq is not None:
            screen.led_seq.clear()
    except Exception:
        pass
    try:
        screen.led_seq = None
    except Exception:
        pass


def run(screen):
    global app

    # tear down a previous instance, or its sequence callback keeps
    # firing against widgets Tulip has already deleted
    if app is not None:
        try:
            quit(app)
        except Exception:
            pass

    # run('tulipdrums') passes the package NAME (a str) on this build
    if not hasattr(screen, "add"):
        screen = tulip.UIScreen()

    app = screen
    app.offset_y = SCREEN_TOP
    app.set_bg_color(0)
    app.quit_callback = quit

    app.kit_idx = 0
    app.pat_idx = 0
    app.project_idx = -1
    app.bpm = 120
    app.playing = False       # start stopped - press PLAY when ready
    app.pending_play = None
    app.midi_sync = False
    app.ticks_per_step = ticks_per_step()
    app.current_step = 0        # last step _led_tick lit (diagnostics only)
    app.ui_paused = False       # True while a full-screen page is open
    app.any_solo = False
    app.rebuilding = False
    app.seq = None              # AMYSequence: fires every drum hit
    app.led_seq = None          # TulipSequence: moves the LED row
    app._restart_msg = None     # info text to show once engine bringup finishes
    app.save_popup = None
    app.bank_row = None
    app.bank_patterns = [None] * NUM_BANKS   # loaded from project on project_load
    app.bank_active = 0
    app.bank_pending = None
    app.bank_clipboard = None
    app.last_save_ms = None     # save-cooldown timer, see _save_allowed()
    app.saving = False          # True while a save write is in flight
    app.header = None
    app.header2 = None
    app.led_row = None
    app.fx_page = None
    app.fx_bus = 0
    app.bus_fx = []
    for b in range(NUM_BUSES):
        f = {}
        for k in BUS_FX_DEFAULTS:
            f[k] = BUS_FX_DEFAULTS[k]
        app.bus_fx.append(f)
    app.user_projects = load_user_projects()

    app.rows = []
    for i in range(len(ELEMENTS)):
        nm = ELEMENTS[i][0]
        note = ELEMENTS[i][1]
        vol = ELEMENTS[i][2]
        app.rows.append(Lane(nm, note, vol, i))

    app.header = HeaderTop()
    app.header2 = HeaderBottom()

    app.add(app.header, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.add(app.header2, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.top_spacer = Spacer(GAP_HEADER_TO_LED)
    app.add(app.top_spacer, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.led_row = LEDRow()
    app.add(app.led_row, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.led_spacer = Spacer(GAP_LED_TO_GRID)
    app.add(app.led_spacer, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.add(app.rows, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.mid_spacer = Spacer(GAP_GRID_TO_BUS)
    app.add(app.mid_spacer, direction=lv.ALIGN.OUT_BOTTOM_LEFT)
    app.bank_row = PatternBankRow()
    app.add(app.bank_row, direction=lv.ALIGN.OUT_BOTTOM_LEFT)

    app.vol_popup = VolPopup(app.group)
    app.save_popup = SaveProjectPopup(app.group)
    app.kit_popup = KitPopup(app.group)
    app.fx_page = FXPage(app.group)

    app.bank_row.refresh_cv()
    if CV_AUTOSTART:
        # deferred, not inline: bringing the DAC up is not worth doing
        # while the engine, display and audio are all still starting
        defer_safe(_cv_autostart, None, CV_INIT_DELAY_MS)

    start_engine()        # the only amy.reset() in normal operation
    apply_preset(0)
    _show_project()
    _update_play_btn()
    app.bank_row.refresh()
    app.present()