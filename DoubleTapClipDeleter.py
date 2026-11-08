"""
DoubleTapClipDeleter - double-click a Session clip to delete it (no Max for Live).

How it works:
  1st click fires the slot (button blinks while waiting for launch quantization).
  2nd click while it's still blinking re-fires the same slot (Trigger launch mode)
  or cancels it (Toggle mode) -> that's a double tap -> delete.
  3rd click lands on the now-empty slot -> records again (if the track is armed).

Works for mouse clicks and controller pads alike. Double-clicking a scene
deletes every clip in that row the same way.

Limitation: needs launch quantization (global or per clip) != None, because the
2nd click has to arrive while the 1st one is still waiting to launch.
"""

import time

from _Framework.ControlSurface import ControlSurface

# ---- settings -------------------------------------------------------------
DOUBLE_TAP_MS = 500       # max time between 1st and 2nd click
RESTART_TOLERANCE = 1.0   # beats; playing clip within this of its start = it relaunched
DEBUG = True              # True -> log every event to Live's Log.txt
# ---------------------------------------------------------------------------


class DoubleTapClipDeleter(ControlSurface):

    def __init__(self, c_instance):
        super().__init__(c_instance)
        self._watchers = []
        self._busy = False  # ignore notifications caused by our own changes
        with self.component_guard():
            self.song().add_tracks_listener(self._rebuild)
            self._rebuild()
        self.log_message("DoubleTapClipDeleter loaded")

    def disconnect(self):
        if self.song().tracks_has_listener(self._rebuild):
            self.song().remove_tracks_listener(self._rebuild)
        self._clear()
        super().disconnect()

    def debug(self, *parts):
        if DEBUG:
            self.log_message("DoubleTapClipDeleter: " + " ".join(str(p) for p in parts))

    def _clear(self):
        for w in self._watchers:
            w.disconnect()
        self._watchers = []

    def _rebuild(self):
        self._clear()
        self._watchers = [_TrackWatcher(self, t) for t in self.song().tracks]
        self.debug("watching", len(self._watchers), "tracks; global quantization =",
                   self.song().clip_trigger_quantization)


class _TrackWatcher:

    def __init__(self, surface, track):
        self.surface = surface
        self.track = track
        self.fired_index = track.fired_slot_index
        self.fired_at = 0.0
        self.snapshot = None
        self.pending_delete = None
        self.clicked_again = False
        self._cb = self._on_fired  # keep one callable so removal works
        track.add_fired_slot_index_listener(self._cb)
        # Debug only: also log raw per-slot trigger changes to compare signals.
        self._slot_cbs = []
        if DEBUG:
            for i, slot in enumerate(track.clip_slots):
                cb = (lambda i=i, s=slot: surface.debug(
                    track.name, "slot", i, "is_triggered =", s.is_triggered,
                    "has_clip =", s.has_clip))
                slot.add_is_triggered_listener(cb)
                self._slot_cbs.append((slot, cb))

    def disconnect(self):
        try:
            if self.track.fired_slot_index_has_listener(self._cb):
                self.track.remove_fired_slot_index_listener(self._cb)
        except Exception:
            pass  # track already deleted
        for slot, cb in self._slot_cbs:
            try:
                if slot.is_triggered_has_listener(cb):
                    slot.remove_is_triggered_listener(cb)
            except Exception:
                pass
        self._slot_cbs = []

    # --- helpers -----------------------------------------------------------

    def _slot(self, idx):
        slots = self.track.clip_slots
        return slots[idx] if 0 <= idx < len(slots) else None

    def _state(self, idx):
        slot = self._slot(idx)
        if slot is None or not slot.has_clip:
            return {"has_clip": False}
        clip = slot.clip
        return {"has_clip": True, "playing": clip.is_playing, "recording": clip.is_recording}

    def _near_start(self, clip):
        try:
            d = clip.playing_position - clip.start_marker
            return -0.05 <= d <= RESTART_TOLERANCE
        except Exception:
            return False

    # --- listener ----------------------------------------------------------

    def _on_fired(self):
        if self.surface._busy:
            return
        idx = self.track.fired_slot_index
        prev, self.fired_index = self.fired_index, idx
        now = time.monotonic()
        in_window = (now - self.fired_at) * 1000.0 <= DOUBLE_TAP_MS
        self.surface.debug(self.track.name, "fired", prev, "->", idx)

        if idx >= 0:
            if idx == self.pending_delete:
                # Clicked again before our delete ran (triple click).
                self.clicked_again = True
                return
            if idx == prev and in_window and self.snapshot is not None:
                # Trigger launch mode: 2nd click re-fires the same blinking slot.
                self.snapshot = None
                self._schedule_delete(idx)
                return
            # A slot started blinking: remember when and what it looked like.
            self.fired_at = now
            self.snapshot = self._state(idx)
            return

        # Blinking stopped. Normally it launched; in Toggle launch mode a
        # 2nd click can instead cancel it.
        snap, self.snapshot = self.snapshot, None
        if idx != -1 or prev < 0 or snap is None or not in_window:
            return
        slot = self._slot(prev)
        early_near_start = bool(slot and slot.has_clip and self._near_start(slot.clip))
        self.surface.schedule_message(1, lambda: self._evaluate(prev, snap, early_near_start))

    def _schedule_delete(self, idx):
        # Can't change the Live Set inside a notification -> defer one tick.
        self.pending_delete = idx
        self.clicked_again = False
        self.surface.schedule_message(1, lambda: self._delete(idx))

    def _evaluate(self, idx, snap, early_near_start):
        try:
            slot = self._slot(idx)
            if slot is None or not slot.has_clip or not snap["has_clip"]:
                return  # empty slot: nothing to delete / recording just started
            clip = slot.clip

            if snap["recording"]:
                launched = not clip.is_recording        # recording ended -> normal stop
            elif snap["playing"]:
                launched = early_near_start or self._near_start(clip)  # restarted
            else:
                launched = clip.is_playing              # stopped clip started

            self.surface.debug(self.track.name, "slot", idx, "snap", snap, "launched", launched)
            if not launched:
                self.pending_delete = idx
                self.clicked_again = self.track.fired_slot_index == idx
                self._delete(idx)
        except Exception as e:
            self.surface.log_message("DoubleTapClipDeleter error: %r" % (e,))

    def _delete(self, idx):
        refire, self.clicked_again = self.clicked_again, False
        self.pending_delete = None
        try:
            slot = self._slot(idx)
            if slot is None or not slot.has_clip:
                return
            self.surface._busy = True
            try:
                slot.delete_clip()
                # Triple click landed before the delete: record again.
                if refire and self.track.can_be_armed and self.track.arm:
                    slot.fire()
            finally:
                self.surface._busy = False
            self.fired_index = self.track.fired_slot_index
            self.snapshot = None  # don't treat our own re-fire as a click
            self.surface.debug(self.track.name, "DELETED slot", idx, "refire", refire)
        except Exception as e:
            self.surface.log_message("DoubleTapClipDeleter error: %r" % (e,))
