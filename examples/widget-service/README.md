# widget-service (fictional example)

A tiny Python service used by the Bellhop demo and tests. It exists so the framework has something to dispatch work against; it is not a real product.

- `widget/__init__.py` — the "service": a `health()` function and a `list_widgets()` function.
- `tests/test_widget.py` — its tests (`python3 -m unittest discover tests`).
- `check.sh` — the required-checks script a lane's rules would point at.
