"""The per-integration Config page (``/config?domain=X``, ``/flow`` kept as
an alias): versions in the store and releases on GitHub, patches, the
config flow driven in-process (only while that integration runs), and its
config entries.  The generic form renderer covers the classic voluptuous
types (string/integer/float/boolean, ``options`` lists) and the selectors
text/number (box and slider)/boolean/select/object/constant/duration/
time/date/datetime/color_rgb; anything else falls back to a JSON textarea.
Labels, menu entries, errors and abort reasons come from the integration's
own ``translations/en.json``, sliced per step by ``flows.py``."""

from .ui import load_template

FLOW_HTML = load_template("config")
