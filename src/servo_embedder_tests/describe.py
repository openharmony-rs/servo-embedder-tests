"""Describes the differences of a failed page scenario comparison: runs the `page_scenarios.Description` pickled on
stdin, at a lower priority, for a `page_scenarios.Describer`."""

import os
import pickle
import sys

if __name__ == "__main__":
    if hasattr(os, "nice"):
        os.nice(10)
    pickle.load(sys.stdin.buffer).run()
