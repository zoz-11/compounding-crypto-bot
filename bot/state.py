"""Atomic JSON state persistence for the bot (stdlib only).

Pattern: write to <file>.tmp, flush + fsync, then os.replace() -- which is
atomic on POSIX. A crash mid-write can never corrupt the live state file.
"""

import json
import logging
import os

log = logging.getLogger("state")

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# Everything needed to reconstruct the bot's brain after a restart.
DEFAULT_STATE = {
    "symbol": None,              # sanity check: refuse restore for wrong pair
    "dry_run": None,             # sanity check: don't mix testnet/live state
    "qty": 0.0,                  # position size in base asset
    "cost": 0.0,                 # total quote spent incl. fees
    "base_price": None,          # price of initial entry
    "safety_used": False,        # DCA safety order already fired?
    "session_start_equity": 0.0, # circuit-breaker reference point
    "paused": False,             # reconciliation failure -> requires /resume
    "halted": False,             # circuit breaker tripped -> stays down
}


def save(state, path=STATE_FILE):
    """Atomically persist state. Never raises -- a failed save must not
    crash a bot that is otherwise healthy (it logs loudly instead)."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.error("FAILED to persist state to %s: %s", path, e)


def load(path=STATE_FILE):
    """Load state. Returns (state_dict, ok). ok=False means the file exists
    but is corrupt/unreadable -- caller must treat that as a red flag."""
    if not os.path.exists(path):
        return None, True  # no file is a normal fresh start
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        state = dict(DEFAULT_STATE)
        state.update(data)
        return state, True
    except (OSError, ValueError) as e:
        log.error("State file %s is corrupt: %s", path, e)
        return None, False


def clear(path=STATE_FILE):
    """Remove a stale state file (e.g. position was closed manually)."""
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Cleared stale state file %s", path)
    except OSError as e:
        log.error("Could not remove %s: %s", path, e)
