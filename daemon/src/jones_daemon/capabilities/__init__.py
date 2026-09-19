"""CapabilityRegistry (Issue #17/#19 daemon 侧, docs/design/03-w4-interfaces.md §2).

See `registry.py` for the pure computation and `methods.py` for the `capability.list`
RPC handler that wires it to real Session/Agent/config state. `browser.py` (#15/#16,
docs/design/00-foundation.md §9) also lives in this package.
"""
