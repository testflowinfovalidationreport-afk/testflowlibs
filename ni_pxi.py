#================================================================================
#									DISCLAIMER
#================================================================================
# Copyright (c) 2025 TestFlow
# Licensed under the TestFlow Community License (TCL).
# This file may be used internally for testing and evaluation only.
# Commercial use, redistribution, or competitive use is strictly prohibited.
#--------------------------------------------------------------------------------
#								 www.testflowic.com
#--------------------------------------------------------------------------------
#									 © TestFlow
#================================================================================
"""
ni_pxi
======
Low-level support library for National Instruments PXI / PXIe Source Measure
Units (SMU), specifically the **NI PXIe-4139** which is a register/driver based
instrument and does **NOT** speak SCPI.

The NI-4139 is controlled through NI's ``nidcpower`` driver (NI-DCPower runtime).
This module is a thin, dependency-tolerant wrapper around ``nidcpower`` so the
rest of the TestFlow package never has to know the details of session
management, channel handling, or the NI driver API.

Design notes
------------
* One :class:`Ni4139Smu` object owns one ``nidcpower.Session`` bound to a
  resource name (e.g. ``"PXI1Slot2"``) and a channel string (default ``"0"``).
* Sessions are cached per (resource, channels) so repeated actions inside a
  test loop reuse the same open session instead of re-opening it every step.
* If the ``nidcpower`` package (or the NI-DCPower runtime) is not installed,
  the module automatically falls back to a **simulation backend** that logs the
  call and returns deterministic fake values. This lets the whole TestFlow
  package run end-to-end on a machine without NI hardware/drivers, and the same
  code drives real hardware once the driver is present.

This file is intentionally self-contained (only depends on ``nidcpower`` when
available) so it can live next to the other vendored libraries in ``libs/``.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

# -----------------------------------------------------------------------------
# Optional driver import. We keep working (in simulation) even if it is missing.
# -----------------------------------------------------------------------------
try:
	import nidcpower  # type: ignore
	_NIDCPOWER_AVAILABLE = True
except Exception:  # ImportError, or DLL/runtime load failure
	nidcpower = None  # type: ignore
	_NIDCPOWER_AVAILABLE = False


def driver_available() -> bool:
	"""Return True when the real nidcpower driver could be imported."""
	return _NIDCPOWER_AVAILABLE


def _log(msg: str) -> None:
	"""Tiny timestamped logger that writes to stdout (captured by the tool)."""
	ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	print(f"[ {ts} ]: [ni_pxi] {msg}")
	sys.stdout.flush()


# =============================================================================
# Function / measure mode constants (string -> driver enum mapping)
# =============================================================================
# We expose human-readable strings in the CSV / script layer and translate them
# to driver enums here, so the action list never hard-codes driver internals.

def _output_function(mode: str):
	"""Map a friendly source-mode string to a nidcpower OutputFunction enum."""
	mode = (mode or "").strip().upper()
	if not _NIDCPOWER_AVAILABLE:
		return mode  # simulation: just carry the string
	mapping = {
		"DC_VOLTAGE": nidcpower.OutputFunction.DC_VOLTAGE,
		"VOLTAGE": nidcpower.OutputFunction.DC_VOLTAGE,
		"DC_CURRENT": nidcpower.OutputFunction.DC_CURRENT,
		"CURRENT": nidcpower.OutputFunction.DC_CURRENT,
	}
	if mode not in mapping:
		raise ValueError(f"Unsupported source function '{mode}'. "
						 f"Use one of {sorted(mapping)}.")
	return mapping[mode]


# =============================================================================
# SMU wrapper
# =============================================================================
class Ni4139Smu:
	"""Object-oriented wrapper around a single NI PXIe-4139 SMU channel."""

	def __init__(self, resource_name: str, channels: str = "0",
				 settling_s: float = 0.05):
		self.resource_name = resource_name
		self.channels = str(channels)
		self.settling_s = settling_s
		self._session = None		# real nidcpower.Session or None (sim)
		self._sim_state = {			# remembered values for the sim backend
			"function": "DC_VOLTAGE",
			"voltage_level": 0.0,
			"current_limit": 0.1,
			"current_level": 0.0,
			"voltage_limit": 1.0,
			"output_enabled": False,
		}
		self._open()

	# ------------------------------------------------------------------ session
	def _open(self) -> None:
		"""Open (or note simulated) the driver session."""
		if not _NIDCPOWER_AVAILABLE:
			_log(f"SIMULATION: nidcpower not installed -> simulating "
				 f"'{self.resource_name}' ch{self.channels}.")
			return
		try:
			self._session = nidcpower.Session(
				resource_name=self.resource_name, channels=self.channels)
			_log(f"Opened NI-DCPower session on '{self.resource_name}' "
				 f"ch{self.channels}.")
		except Exception as e:
			# Fall back to simulation rather than crashing the whole run.
			self._session = None
			_log(f"WARNING: failed to open '{self.resource_name}' ({e}). "
				 f"Falling back to SIMULATION.")

	@property
	def simulated(self) -> bool:
		return self._session is None

	def close(self) -> None:
		if self._session is not None:
			try:
				self._session.close()
				_log(f"Closed session on '{self.resource_name}'.")
			except Exception as e:
				_log(f"WARNING: error closing '{self.resource_name}': {e}")
			finally:
				self._session = None

	# ----------------------------------------------------------------- helpers
	def _ch(self):
		"""Return the channel view of the session (real backend only)."""
		# nidcpower sessions are indexable by channel string.
		return self._session.channels[self.channels]

	def _settle(self):
		if self.settling_s:
			time.sleep(self.settling_s)

	# =============================================================== source ops
	def set_function(self, function: str) -> str:
		"""Select the source function: DC_VOLTAGE or DC_CURRENT."""
		if self.simulated:
			self._sim_state["function"] = (function or "").strip().upper()
			_log(f"SIM set_function({function}) on {self.resource_name}")
			return self._sim_state["function"]
		ch = self._ch()
		ch.output_function = _output_function(function)
		return function

	def set_voltage(self, level: float, current_limit: Optional[float] = None) -> float:
		"""Source a DC voltage `level` (V) with optional `current_limit` (A)."""
		level = float(level)
		if self.simulated:
			self._sim_state.update(function="DC_VOLTAGE", voltage_level=level)
			if current_limit is not None:
				self._sim_state["current_limit"] = float(current_limit)
			_log(f"SIM set_voltage({level} V, Ilim="
				 f"{self._sim_state['current_limit']} A) on {self.resource_name}")
			return level
		ch = self._ch()
		ch.output_function = _output_function("DC_VOLTAGE")
		if current_limit is not None:
			ch.current_limit = float(current_limit)
			ch.current_limit_autorange = False
		ch.voltage_level = level
		self._settle()
		return level

	def set_current(self, level: float, voltage_limit: Optional[float] = None) -> float:
		"""Source a DC current `level` (A) with optional `voltage_limit` (V)."""
		level = float(level)
		if self.simulated:
			self._sim_state.update(function="DC_CURRENT", current_level=level)
			if voltage_limit is not None:
				self._sim_state["voltage_limit"] = float(voltage_limit)
			_log(f"SIM set_current({level} A, Vlim="
				 f"{self._sim_state['voltage_limit']} V) on {self.resource_name}")
			return level
		ch = self._ch()
		ch.output_function = _output_function("DC_CURRENT")
		if voltage_limit is not None:
			ch.voltage_limit = float(voltage_limit)
			ch.voltage_limit_autorange = False
		ch.current_level = level
		self._settle()
		return level

	def set_current_limit(self, limit: float) -> float:
		"""Set the current compliance/limit (A) for voltage sourcing."""
		limit = float(limit)
		if self.simulated:
			self._sim_state["current_limit"] = limit
			_log(f"SIM set_current_limit({limit} A) on {self.resource_name}")
			return limit
		ch = self._ch()
		ch.current_limit_autorange = False
		ch.current_limit = limit
		return limit

	def set_voltage_limit(self, limit: float) -> float:
		"""Set the voltage compliance/limit (V) for current sourcing."""
		limit = float(limit)
		if self.simulated:
			self._sim_state["voltage_limit"] = limit
			_log(f"SIM set_voltage_limit({limit} V) on {self.resource_name}")
			return limit
		ch = self._ch()
		ch.voltage_limit_autorange = False
		ch.voltage_limit = limit
		return limit

	# =============================================================== output ops
	def output_on(self) -> bool:
		if self.simulated:
			self._sim_state["output_enabled"] = True
			_log(f"SIM output_on() on {self.resource_name}")
			return True
		ch = self._ch()
		ch.output_enabled = True
		ch.initiate()
		self._settle()
		return True

	def output_off(self) -> bool:
		if self.simulated:
			self._sim_state["output_enabled"] = False
			_log(f"SIM output_off() on {self.resource_name}")
			return False
		ch = self._ch()
		try:
			ch.abort()
		except Exception:
			pass
		ch.output_enabled = False
		return False

	# ============================================================== measure ops
	def measure_voltage(self) -> float:
		if self.simulated:
			# Return the programmed level so downstream math stays sensible.
			v = self._sim_state["voltage_level"]
			_log(f"SIM measure_voltage() -> {v} V on {self.resource_name}")
			return float(v)
		val = self._ch().measure(nidcpower.MeasurementTypes.VOLTAGE)
		return float(val)

	def measure_current(self) -> float:
		if self.simulated:
			i = self._sim_state["current_level"]
			_log(f"SIM measure_current() -> {i} A on {self.resource_name}")
			return float(i)
		val = self._ch().measure(nidcpower.MeasurementTypes.CURRENT)
		return float(val)

	def measure_both(self) -> Tuple[float, float]:
		"""Return (voltage, current) in one call."""
		if self.simulated:
			v = float(self._sim_state["voltage_level"])
			i = float(self._sim_state["current_level"])
			_log(f"SIM measure_both() -> ({v} V, {i} A) on {self.resource_name}")
			return v, i
		ch = self._ch()
		v = float(ch.measure(nidcpower.MeasurementTypes.VOLTAGE))
		i = float(ch.measure(nidcpower.MeasurementTypes.CURRENT))
		return v, i

	def reset(self) -> bool:
		"""Reset the channel to a safe default state (output off, 0 V)."""
		if self.simulated:
			self._sim_state.update(voltage_level=0.0, current_level=0.0,
								   output_enabled=False)
			_log(f"SIM reset() on {self.resource_name}")
			return True
		try:
			self._session.reset()
		except Exception as e:
			_log(f"WARNING: reset failed on {self.resource_name}: {e}")
			return False
		return True


# =============================================================================
# Session cache  (one SMU object per resource+channel, reused across actions)
# =============================================================================
_SMU_CACHE: Dict[Tuple[str, str], Ni4139Smu] = {}


def get_smu(resource_name: str, channels: str = "0") -> Ni4139Smu:
	"""Return a cached :class:`Ni4139Smu` for (resource, channels)."""
	key = (str(resource_name), str(channels))
	smu = _SMU_CACHE.get(key)
	if smu is None:
		smu = Ni4139Smu(resource_name, channels)
		_SMU_CACHE[key] = smu
	return smu


def close_all() -> None:
	"""Close every cached SMU session. Call at end of a run."""
	for smu in list(_SMU_CACHE.values()):
		smu.close()
	_SMU_CACHE.clear()
