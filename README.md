# hyprland-input-watch

Watch a monitor's DDC/CI input selection and keep Hyprland workspaces accessible when you switch away. When the selected input changes to a configured away input, the watcher disables that output so Hyprland can move workspaces to a fallback. When the input returns, it restores the configured monitor rule.

The initial public version is 0.1.0. This does not promise to restore workspaces to their original locations or force window moves. It does not write DDC input settings, update firmware, or fix kernel hotplug issues. Only one output can be watched per user.

## Requirements

| Requirement | Details |
| --- | --- |
| Platform | Linux, Python 3.11 or newer, Hyprland 0.57 with its Lua API, `hyprctl`, and `ddcutil` |
| Configuration | Hyprland Lua configuration. Legacy hyprlang is not supported. |
| DDC/CI | Enabled on the monitor, with user access to the appropriate `/dev/i2c-*` devices |
| Monitor identity | Target and fallback must have distinct, nonempty, usable EDID serials that are unique in both the Hyprland and `ddcutil` inventories |
| Hardware | The target must answer DDC while its input is inactive **and** while its Hyprland output is disabled |

The Dell U2723QE is known to have been tested. Other monitors may not meet these requirements. Run as your normal user, not root.

## Install and configure 🔧

There is no PyPI release. Install from the Git URL with either tool:

```sh
uv tool install git+https://github.com/major/hyprland-input-watch.git
# Or: pipx install git+https://github.com/major/hyprland-input-watch.git
```

Clone the repository to get the example configuration:

```sh
git clone https://github.com/major/hyprland-input-watch.git
cd hyprland-input-watch
# Alternative local installation: uv tool install .
install -Dm600 examples/config.toml ~/.config/hyprland-input-watch/config.toml
```

If `XDG_CONFIG_HOME` is set, place the file at `$XDG_CONFIG_HOME/hyprland-input-watch/config.toml` instead. Otherwise the default is `~/.config/hyprland-input-watch/config.toml`. Edit the file before running `check` or `run`.

1. Stop the watcher before using other DDC management tools or updating monitor firmware.
2. Run `hyprland-input-watch probe` to inspect the read-only JSON output. Identify the target and fallback by their unique usable EDID serials.
3. With the target enabled in Hyprland, physically select each input you intend to use and run `hyprland-input-watch probe --serial TARGET_SERIAL` on each. Record the returned input codes. Values such as `0x0f` and `0x11` in the example are **examples, not universal codes**. A USB-C vendor code is often undocumented, so never guess it.
4. Confirm the target still answers a probe while another physical input is selected. With an awake, enabled fallback available, temporarily disable the target using your Hyprland configuration and confirm it still answers the probe. Re-enable the target using your own monitor configuration and reload Hyprland. If either probe fails, do not use this watcher.
5. Set both serials and the observed input codes in the config. Set the restore rule to the monitor rule you actually want in Hyprland. Do not copy `auto-left` blindly, especially for rotated or differently positioned monitors.
6. Run `hyprland-input-watch check` to inspect the read-only JSON result, then start the watcher using one integration below.

| Config field | Requirement |
| --- | --- |
| `monitor_serial`, `fallback_serial` | Nonempty and distinct |
| `active_input` | Byte value from 0 to 255 |
| `away_inputs` | Nonempty list of known byte values, disjoint from `active_input` |
| `interval_seconds` | Finite number from 1 to 3600 |
| `confirmations` | Integer from 1 to 10, default 2 |
| `restore.mode`, `restore.position` | Your desired Hyprland monitor rule |
| `restore.scale` | Finite positive number |
| `restore.transform` | Integer from 0 to 7 |

Unknown input codes are ignored: they leave the output unchanged and reset debounce.
`interval_seconds` is the wait between completed attempts, not a fixed end-to-end detection latency.

## Commands

```text
hyprland-input-watch [--config PATH] [run|probe|check]
hyprland-input-watch --version
```

`run` is the default. `probe` and `check` return read-only JSON. `probe` needs no configuration and accepts `--serial SERIAL`; `check` requires configuration. Use `--config PATH` before the subcommand to select a different file.

Redact monitor serial numbers before sharing diagnostic output in an issue.

## Start with Hyprland

Choose **one** integration. Do not run both the UWSM-managed service and the optional static unit.

### Recommended: UWSM

Add the command to your existing Hyprland Lua startup callback:

```lua
hl.on("hyprland.start", function()
    hl.exec_cmd("uwsm app -s b -t service -u hyprland-input-watch.service -p TimeoutStopSec=60s -- hyprland-input-watch run")
end)
```

### Optional: static user unit

Install the supplied unit from the clone:

```sh
install -Dm644 contrib/hyprland-input-watch.service ~/.config/systemd/user/hyprland-input-watch.service
systemctl --user daemon-reload
```

The unit uses `%h/.local/bin/hyprland-input-watch`. Compare it with `command -v hyprland-input-watch` and edit `ExecStart` if your tool bin is elsewhere. Do not enable the unit globally: it needs the active Hyprland environment. Start it from Hyprland only after importing that environment:

```lua
hl.on("hyprland.start", function()
    hl.exec_cmd("systemctl --user import-environment HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY && systemctl --user start hyprland-input-watch.service")
end)
```

The unit has no automatic restart: a manual override must be respected, and a crash loses ownership of the output.

For either integration:

```sh
systemctl --user stop hyprland-input-watch.service
journalctl --user -u hyprland-input-watch.service -b
```

## Safety and recovery 🛡️

- Before every disable, the specifically configured fallback must be awake and enabled. Missing or ambiguous serials do not authorize a change.
- Before every input query in `probe`, `check`, or `run`, a bounded `ddcutil detect --terse` checks for an exact, unique serial match. Unknown or malformed detection results fail closed; `ddcutil --sn` alone can silently select the first match.
- Two matching polls are required by default. Failed or timed-out input and detection reads, and unknown inputs, do not trigger changes. A timed-out Hyprland IPC mutation may still apply, so ownership is retained. Subprocess calls are bounded to 8 seconds, with up to 2 seconds total for verification; allow 60 seconds for service stop.
- A singleton lock is held through cleanup. The watcher restores only an output it owns. If you or a configuration reload re-enable an output it disabled, the watcher stops rather than fighting your changes.
- Normal SIGTERM performs cleanup. SIGKILL, a crash, or power loss cannot guarantee restoration, and a new instance will not adopt an already disabled output.

If recovery is needed, stop the service, then run `hyprctl reload` to reapply your own monitor configuration. That configuration must enable the output. Restart using the startup command or static unit for the integration you chose.

### Manual hardware test

After validating DDC access above, start the watcher and switch to a configured away input. Wait for the configured number of samples and verify the target is disabled and workspaces move to the fallback. Switch back, wait for the same number of samples, and verify the configured monitor rule is restored. While the watcher owns a disabled output, also test a configuration reload and a manual re-enable: the watcher should stop rather than override either. Check the journal and recover as described above if needed.

## Development

```sh
uv sync --locked
make check  # locked Ruff and unittest checks
make build  # uv build
```

Tests should be fast, use Arrange-Act-Assert, and require no hardware.

## License

MIT. See [LICENSE](LICENSE).
