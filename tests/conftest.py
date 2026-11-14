import os
import sys

# Make the package importable when the tests run from a source checkout
# without installing it (``pip install -e .`` is still the documented way).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
