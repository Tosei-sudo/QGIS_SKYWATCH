import os
import sys

# The plugin's folder's parent, so `satellite_observation` imports as a package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
