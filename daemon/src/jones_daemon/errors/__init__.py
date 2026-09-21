"""Error classification + `ErrorCard` construction (Issue #22, FR14, docs/design/
04-w5-interfaces.md §4). See `classify.py` for the actual implementation — this
package only exists (rather than a bare module) because the ownership table
(04-w5-interfaces.md §1) names `daemon/src/jones_daemon/errors/` as a directory.
"""

from __future__ import annotations
