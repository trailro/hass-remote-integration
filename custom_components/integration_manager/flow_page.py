"""The per-integration Config page (``/config?domain=X``, ``/flow`` kept as
an alias): versions in the store and releases on GitHub, patches, the
config flow driven in-process (only while that integration runs), and its
config entries.  The generic form renderer covers the classic voluptuous
types (string/integer/float/boolean, ``options`` lists) and the modern
selectors (text/number/boolean/select/object); anything else falls back to
a JSON textarea."""

from .ui import load_template

FLOW_HTML = load_template("config")
