# Contributor instructions

- Support Python 3.11 and newer with no Python runtime dependencies.
- Use `uv sync --locked`, `make check` for locked Ruff and unittest checks, and `make build` for the package build. Keep CI dependencies pinned.
- Write fast, hardware-free tests using Arrange-Act-Assert. Do not run hardware tests without explicit approval.
- Preserve the watcher safety rules: require an awake, enabled, unambiguous fallback before disabling; restore only owned outputs; respect manual overrides and configuration reloads; keep subprocesses bounded and hold the singleton lock through cleanup.
- Do not add personal monitor serials, hostnames, or absolute personal paths to examples or tests.

## Safety check locations

- `Config.__post_init__` and `Restore.__post_init__` validate both TOML and direct construction.
- `find_monitor` requires an unambiguous Hyprland identity and explicit output state.
- `read_input` checks DDC identity uniqueness and response format for every CLI path.
- `cli._probe` validates discovered serials before requesting input; it never changes outputs.
- `Watcher.change` is the only mutation path. Keep its identity, fallback, ownership, and shutdown checks intact for both normal operation and cleanup.
