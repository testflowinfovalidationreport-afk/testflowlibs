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
ni_4139_smu
===========
TestFlow action module for the **NI PXIe-4139** Source Measure Unit.

The NI-4139 does not support SCPI, so the normal ``CMD:`` / ``QRY:`` VISA path
in ``runner.py`` cannot drive it. Instead, ``runner.py`` recognises a dedicated
``SMU:`` line prefix in ``script.atoms`` and forwards the payload to
:func:`execute`, which:

  1. Parses the action call, e.g.  ``set_voltage(5.0, 0.1)``
  2. Resolves the target SMU from the node's ``INST::`` resource string
	 (e.g. ``PXI1Slot2`` or ``PXI1Slot2/0`` to address channel 0).
  3. Calls the matching method on the :mod:`ni_pxi` wrapper.
  4. Returns ``(reply, is_query)`` so the runner can decide whether to write a
	 value into the results CSV (queries/measurements) or just log the action
	 (orders / sets).

Script syntax
-------------
A node addresses the SMU with an ``INST::`` line and then issues ``SMU:`` lines::

	#NODE4 (SMU, PXIe-4139, NI, PXIe-4139)
	INST:: PXI1Slot2/0
	#ACTION: (Set_Voltage,Set_Voltage)
	SMU: set_voltage(5.0, 0.1)
	#END_ACTION
	#ACTION: (Measure_Current,Measure_Current)
	SMU: measure_current()
	#END_ACTION

The authoritative runtime action table is the :data:`ACTIONS` dict in this
module. A design-time reference of the same actions is kept in
``Script/ni_4139_smu_actions.csv`` for the test-builder tool only; that CSV is
NOT shipped with the package and is NEVER read at runtime, so its presence or
absence has no effect on behaviour.

Return contract
---------------
``execute(resource, payload)`` returns a tuple ``(reply, is_query)``:
  * ``is_query == True``  -> ``reply`` is a measured/queried value the runner
	should store in the CSV results column for the action.
  * ``is_query == False`` -> ``reply`` is a short human-readable confirmation
	string for logging only (no CSV result column expected).
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

# This module lives in the package ``libs`` folder, so ``ni_pxi`` is a sibling.
# runner.py already inserts ``libs`` onto sys.path, but we add this file's own
# directory defensively so the module also works when imported / tested in
# isolation.
_libs = Path(__file__).resolve().parent
if _libs.exists() and str(_libs) not in sys.path:
	sys.path.insert(0, str(_libs))

import ni_pxi  # noqa: E402  (import after sys.path tweak)


# -----------------------------------------------------------------------------
# Action registry
# -----------------------------------------------------------------------------
# Each entry: name -> (handler, is_query)
#   handler(smu, *args) -> reply
#   is_query == True means the reply is a measured value for the CSV.
#
# Keeping this table here (and mirrored in ni_4139_smu_actions.csv) means the
# script author and the runner agree on exactly which verbs are legal.
def _set_function(smu, function):
	return f"function={smu.set_function(function)}"


def _set_voltage(smu, level, current_limit=None):
	v = smu.set_voltage(level, current_limit)
	return f"voltage_set={v}"


def _set_current(smu, level, voltage_limit=None):
	i = smu.set_current(level, voltage_limit)
	return f"current_set={i}"


def _set_current_limit(smu, limit):
	return f"current_limit={smu.set_current_limit(limit)}"


def _set_voltage_limit(smu, limit):
	return f"voltage_limit={smu.set_voltage_limit(limit)}"


def _output_on(smu):
	smu.output_on()
	return "output=ON"


def _output_off(smu):
	smu.output_off()
	return "output=OFF"


def _reset(smu):
	smu.reset()
	return "reset=OK"


def _measure_voltage(smu):
	return smu.measure_voltage()


def _measure_current(smu):
	return smu.measure_current()


def _measure_both(smu):
	v, i = smu.measure_both()
	return f"{v},{i}"


ACTIONS = {
	# name				 (handler,			  is_query)
	"set_function":		 (_set_function,	  False),
	"set_voltage":		 (_set_voltage,		  False),
	"set_current":		 (_set_current,		  False),
	"set_current_limit": (_set_current_limit, False),
	"set_voltage_limit": (_set_voltage_limit, False),
	"output_on":		 (_output_on,		  False),
	"output_off":		 (_output_off,		  False),
	"reset":			 (_reset,			  False),
	"measure_voltage":	 (_measure_voltage,	  True),
	"measure_current":	 (_measure_current,	  True),
	"measure_both":		 (_measure_both,	  True),
}


def _log(msg: str) -> None:
	ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	print(f"[ {ts} ]: [ni_4139_smu] {msg}")
	sys.stdout.flush()


# -----------------------------------------------------------------------------
# Resource parsing
# -----------------------------------------------------------------------------
def parse_resource(resource: str):
	"""
	Split an INST:: resource string into (resource_name, channel).

	Accepts:
	  'PXI1Slot2'		 -> ('PXI1Slot2', '0')	 (default channel 0)
	  'PXI1Slot2/0'		 -> ('PXI1Slot2', '0')
	  'PXI1Slot2/1'		 -> ('PXI1Slot2', '1')
	"""
	resource = (resource or "").strip()
	if "/" in resource:
		name, ch = resource.rsplit("/", 1)
		return name.strip(), ch.strip() or "0"
	return resource, "0"


# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
def _coerce(token: str):
	"""Convert a raw argument token into int/float/str (None / quotes aware)."""
	t = token.strip()
	if t == "":
		return None
	if (t[0] == t[-1]) and t[0] in ("'", '"'):
		return t[1:-1]
	low = t.lower()
	if low in ("none", "null"):
		return None
	try:
		return int(t, 0)		# handles decimal and 0x hex
	except ValueError:
		pass
	try:
		return float(t)
	except ValueError:
		return t				# keep as plain string


def parse_call(payload: str):
	"""
	Parse 'func(arg1, arg2, ...)' into (func_name, [args]).
	A bare 'func' (no parentheses) is treated as 'func()'.
	"""
	payload = (payload or "").strip()
	m = re.match(r"^\s*(\w+)\s*(?:\((.*)\))?\s*$", payload, re.DOTALL)
	if not m:
		raise ValueError(f"Could not parse SMU action: {payload!r}")
	name = m.group(1)
	raw_args = m.group(2)
	args = []
	if raw_args is not None and raw_args.strip() != "":
		for tok in raw_args.split(","):
			args.append(_coerce(tok))
	return name, args


# -----------------------------------------------------------------------------
# Public entry point used by runner.py
# -----------------------------------------------------------------------------
def execute(resource: str, payload: str):
	"""
	Execute one SMU action.

	Args:
		resource: the INST:: resource string for the node (e.g. 'PXI1Slot2/0').
		payload : the action call after the 'SMU:' prefix (e.g. 'set_voltage(5,0.1)').

	Returns:
		(reply, is_query)
		  reply	   : measured value (is_query=True) or confirmation string.
		  is_query : whether reply should be written to the CSV results column.
	"""
	try:
		name, args = parse_call(payload)
	except ValueError as e:
		_log(f"\033[41mError: {e}\033[0m")
		return f"ERROR: {e}", False

	entry = ACTIONS.get(name)
	if entry is None:
		msg = (f"Unknown SMU action '{name}'. "
			   f"Known actions: {', '.join(sorted(ACTIONS))}")
		_log(f"\033[41m{msg}\033[0m")
		return f"ERROR: {msg}", False

	handler, is_query = entry
	res_name, channel = parse_resource(resource)

	try:
		smu = ni_pxi.get_smu(res_name, channel)
		reply = handler(smu, *args)
		_log(f"{res_name}/ch{channel}  {name}({', '.join(map(str, args))}) "
			 f"-> {reply}")
		return reply, is_query
	except TypeError as e:
		# Wrong number of arguments for the chosen action.
		msg = f"Bad arguments for '{name}': {e}"
		_log(f"\033[41m{msg}\033[0m")
		return f"ERROR: {msg}", is_query
	except Exception as e:
		msg = f"SMU '{name}' failed on {res_name}: {e}"
		_log(f"\033[41m{msg}\033[0m")
		return f"ERROR: {msg}", is_query


def close():
	"""Close all open SMU sessions (call at end of run)."""
	ni_pxi.close_all()


# -----------------------------------------------------------------------------
# Manual smoke test:	python ni_4139_smu.py
# -----------------------------------------------------------------------------
if __name__ == "__main__":
	print("nidcpower available:", ni_pxi.driver_available())
	for call in [
		"reset()",
		"set_function(DC_VOLTAGE)",
		"set_voltage(5.0, 0.1)",
		"output_on()",
		"measure_voltage()",
		"measure_current()",
		"measure_both()",
		"output_off()",
		"bogus_action(1)",
	]:
		reply, is_q = execute("PXI1Slot2/0", call)
		print(f"  {call:30s} -> reply={reply!r}  is_query={is_q}")
	close()
