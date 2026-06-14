> 🇯🇵 [日本語版はこちら](README.ja.md)

# YKK Smart Control Key — Home Assistant integration

Home Assistant custom integration for the YKK AP **Smart Control Key**
(SCK) BLE door lock (the `com.alpha.lockapp` family).

Reverse-engineered protocol; not affiliated with YKK AP.

## Status

**Experimental.** The GATT registration handshake is still being
shaken out against the SCK3025 firmware — the lock's post-pair
window is tight, and registration may need a retry or two before all
fields commit. If registration partially succeeds, the integration
stores what it got and backfills missing fields (`AdvDataKey`,
`lock_id`, `smartphone_id`) on the next authenticated GATT session
(your first `lock` / `unlock` action). Lock / unlock and passive
state decoding are stable once registration completes.

## What it does

Two BLE roles with independently selectable adapters:

- **Long-range (read-only)** — passive listener for the lock's
  encrypted state advertisements. Updates a `lock` entity with the
  current locked / unlocked state, plus diagnostics
  (low battery, inspection-due, firmware versions, RSSI). Works through
  walls — any HA scanner that hears the advert is enough.
- **Short-range (read/write)** — full GATT round-trip for lock /
  unlock. Requires a connectable scanner physically near the door
  (≤ ~3 m), typically an ESPHome
  [`bluetooth_proxy`](https://esphome.io/components/bluetooth_proxy.html).

You pick which scanner handles each role in the config flow, and can
re-pick after install via the integration's Options.

UI is translated to **English** and **Japanese** out of the box.

## Install

### HACS (custom repository)

1. HACS → … → *Custom repositories*.
2. Add this repo's URL as an **Integration**.
3. Install *YKK Smart Control Key*.
4. **Restart Home Assistant** so HA loads the new integration.
5. *Settings → Devices & Services → Add Integration → YKK Smart Control
   Key*.

### Manual

Copy `custom_components/ykk_door/` into your HA config's
`custom_components/`. Restart HA. Add the integration from the UI.

## Setup flow

1. HA auto-discovers SCK locks via the GATT service UUID. If you don't
   see a prompt, *Add Integration → YKK Smart Control Key* lists nearby
   advertisers.
2. **Pick adapters**: which scanner handles state (long-range) and
   which handles commands (short-range). Leave both on **Auto** if you
   only have one adapter today; revisit via the integration's *Options*
   once an ESP32 `bluetooth_proxy` is online near the door.
3. **Pick a method**:
   - **Live registration** (default) — runs the GATT handshake
     against the lock to capture credentials. Needs the short-range
     adapter physically near the lock and the lock in registration
     mode.
   - **Manual entry** — paste an `AdvDataKey`, `lock_id`, and PIN you
     already extracted (e.g. via reverse engineering). Skips the
     GATT round-trip entirely; useful while waiting on a near-door
     `bluetooth_proxy` install.
4. **Live registration**:
   - Make sure the short-range adapter is physically close to the lock.
   - Press the lock's physical registration button.
   - Pick a 6-digit PIN and submit immediately — the lock leaves
     registration mode after a short window.
   - The integration runs the registration handshake and stores
     whatever credentials the lock returned. If the lock didn't
     return every field in time, the missing fields are backfilled
     automatically on your next `lock` / `unlock` action.

## Entities

| Type | Purpose |
|---|---|
| `lock` | Locked / unlocked state from passive adverts; `lock` / `unlock` services bond and command via GATT. |
| `binary_sensor` (battery) | Low-battery warning bit. |
| `binary_sensor` (problem) | Inspection-due flag. |
| `sensor` (diagnostic) | Lock-unit and handle-unit firmware versions. |
| `sensor` (signal_strength) | Last RSSI seen by the long-range scanner. |

The `lock` entity also surfaces `long_range_source`,
`short_range_source`, and `last_seen_via` as state attributes so you
can see which scanner is doing what.

## Requirements

- Home Assistant 2024.8 or newer.
- The host running HA must use a Bleak-supported BT backend (Linux /
  macOS / Windows). **Android is not supported** by the underlying
  protocol library — the config flow refuses setup on Android with a
  clear error.
- One BLE adapter or `bluetooth_proxy` that can *hear* the lock for the
  long-range role.
- One connectable BLE adapter or `bluetooth_proxy` that can *reach*
  the lock (≤ ~3 m line-of-sight) for the short-range role. These can
  be the same adapter.

## Security

The `AdvDataKey` captured at registration is per-lock and the only way
to decrypt the lock's state advertisements. Treat it (and the PIN) as
secrets — both live in the HA config entry under
`.storage/core.config_entries`. Re-pairing requires putting the lock
back into registration mode.

## License

TBD.
