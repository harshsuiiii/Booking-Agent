from typing import Literal

# Sentinel for data the supplier didn't provide.
# Distinct from None (not applicable) and 0/False (explicitly none/not-allowed).
UNKNOWN: Literal["UNKNOWN"] = "UNKNOWN"
Unknown = Literal["UNKNOWN"]
