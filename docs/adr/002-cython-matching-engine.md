# ADR-002: Cython matching engine with `with nogil:`

Hot-path matching runs in pure C structs, releasing GIL.
