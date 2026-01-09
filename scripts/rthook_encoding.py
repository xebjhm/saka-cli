
import sys

# Force UTF-8 encoding for stdout/stderr on Windows to avoid CP950/CP932 errors
# This is critical for displaying characters like '坂' on non-UTF8 consoles.
# This hook runs before the main script.
if sys.platform == 'win32':
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except AttributeError:
             pass # In case stdout is not a TextIOWrapper (e.g. pythonw)
    if sys.stderr:
        try:
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass
