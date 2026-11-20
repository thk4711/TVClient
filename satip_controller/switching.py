"""Channel-switch defaults and private runtime-only diagnostic overrides."""
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SwitchExperiments:
    saved_audio: bool = False
    bounded_probe: bool = False
    adaptive_coalescing: bool = False
    redundant_ipc: bool = False
    # Accepted stage-two optimization; explicit False retains the legacy path.
    persistent_relay: bool = True

    @classmethod
    def comparison_flags(cls, phase):
        # The 128 KiB probe warm-up failed on the test Pi. Explicit debugging
        # remains possible, but the combined comparison excludes this candidate.
        return {name: phase == name or (phase == 'combined' and name not in ('bounded_probe', 'persistent_relay'))
                for name in cls.__dataclass_fields__}

    @classmethod
    def load(cls, runtime):
        # The player runtime is private. Never accept arbitrary mpv options.
        try:
            path = Path(runtime) / "switch-experiments.json"
            if path.stat().st_size > 1024:
                return cls()
            data = json.loads(path.read_text())
            if (not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__)
                    or any(type(value) is not bool for value in data.values())):
                return cls()
            return cls(**data)
        except (OSError, ValueError, TypeError):
            return cls()

    def probe_options(self):
        return ({"demuxer-lavf-analyzeduration": "0.5",
                 "demuxer-lavf-probesize": "131072"} if self.bounded_probe else {})

    def coalescing_delay(self, age, burst):
        if not self.adaptive_coalescing:
            return .08
        # Isolated taps get a short grace period; bursts retain the old bound.
        # Time already spent finishing an obsolete tune counts towards it.
        return max(0., (.08 if burst else .03) - max(0., age))
