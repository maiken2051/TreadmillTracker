# TRUE treadmill console: Bluetooth protocol

What a **TRUE Performance 8000** treadmill console sends over Bluetooth Low Energy, decoded
for this project from captures of real workouts and checked against the console's own
display. The console does **not** implement the standard Fitness Machine Service (FTMS);
it uses a vendor service, `a026ee07-0a7d-4ab3-97fa-f1500f9feb8b`, which also appears on
other treadmills sold as "Zwift compatible". Nothing here comes from TRUE documentation:
treat it as observed behaviour of one console, firmware as of October 2026.

The captures behind this are in [`tests/fixtures/`](../tests/fixtures/): one JSON line per
notification (`{"t": ms, "dev": address, "char": uuid, "data": hex}`), plus each device's
GATT table as captured (`*.gatt.json`). The parsers are in
[`src/gymtracker/ble/treadmill.py`](../src/gymtracker/ble/treadmill.py).

**This project only listens.** Several characteristics are writable, presumably for remote
control; they were never written to, and nothing below describes them.

## Finding the console

- The console advertises only after its **Bluetooth button** is pressed, for about a
  minute, then stops until the button is pressed again.
- It advertises as `Treadmill NNNN`, with service `a026ee07-…` in the advertisement.
- **Both the 4-digit number and the device address change on every press** (the address is
  random). Match on the name prefix and the service UUID; an address cannot be pinned.
- No pairing or bonding: a plain connection works.
- The console drops the connection itself about a second after a workout ends.

## GATT table

All vendor UUIDs share the base `a026XXXX-0a7d-4ab3-97fa-f1500f9feb8b`.

| Service | Characteristic | Properties | Use |
|---|---|---|---|
| `a026ee07` | `a026e01d` | notify | **workout data** (below) |
| | `a026e01e` | read, notify | **workout state** (below) |
| | `a026e01b` | read, notify | ASCII `PROGRAM # 0` |
| | `a026e01f` | read, notify | one byte, `00` seen |
| | `a026e020` | read, notify | not seen to change |
| | `a026e024` | write-without-response, notify | not used |
| | `a026e016` | write-without-response | not used |
| `a026ee03` | `a026e00a` | write-without-response, notify | not used |
| `a026ee01` | `a026e002` | write-without-response, notify | not used |
| | `a026e004` | notify | not used |
| `180a` Device Information | `2a26` firmware, `2a29` manufacturer | read | `0.0`, `unknown` |
| `1800` Generic Access | `2a00` name | read, write | `Treadmill NNNN` |

## Workout data: `a026e01d` (notify)

About once a second the console sends two frames, told apart by their first byte.

### `0x53` frame, 19 bytes, little-endian

| Offset | Type | Field | Unit |
|---|---|---|---|
| 0 | u8 | frame type, `0x53` | |
| 1–2 | | `cb 1a`, constant (meaning unknown) | |
| 3–6 | u32 | elapsed workout time; frozen while paused, 0 before start | s |
| 7–8 | u16 | belt speed | 0.01 km/h |
| 9–10 | u16 | total distance | m |
| 11–13 | | always 0 | |
| 14–15 | u16 | total calories | kcal |
| 16 | u8 | METs × 10 | |
| 17–18 | | always 0 | |

Example, from a walk at 4.0 mph:

```
53 cb1a 9b000000 8302 b100 000000 0b00 29 0000
   │    │        │    │           │    └ 41 → 4.1 METs
   │    │        │    │           └ 11 kcal
   │    │        │    └ 177 m
   │    │        └ 643 → 6.43 km/h (4.0 mph)
   │    └ 155 s
   └ constant
```

Checked against the console: a calibration walk ended at 5:08, 0.26 mi, 29 kcal on the
console, and the last frame said 308 s, 418 m, 29 kcal.

Notes:

- **Speed** is the console's mph setting × 1.609, truncated: 2.0 mph is sent as 321.
- **Distance** is counted by the console in 0.01 mi and sent rounded to whole metres. It
  runs about 2 % ahead of what the speed field implies, so an average computed as
  distance ÷ time can come out slightly above the speed field's maximum.
- **METs** match the ACSM walking equation: 2.0 mph = 2.5, 3.0 = 3.3, 4.0 = 4.1,
  3.0 mph at 5 % = 5.4.
- Time, distance and calories are the console's own totals for the current workout, not
  deltas. They keep their values through a pause.

### `0x22` frame, 4 bytes

`22 80 II II`: bytes 2–3 are the incline, u16 little-endian, in 0.1 %. Seen: `14 00` = 2 %,
`1e 00` = 3 %, `32 00` = 5 %, `05 00` = 0.5 %. Byte 3 has never been non-zero.

## Workout state: `a026e01e` (read, notify)

One byte. Read it once after connecting to learn the current state, then follow
notifications.

| Value | Meaning |
|---|---|
| `0x01` | connected, no workout started yet |
| `0x0a` | running: Quick Start, or resume after a pause |
| `0x02` | paused, or stopped (see below) |
| `0x80` | workout ended |

**Stop** sends `0x02` at once, like a pause. The belt then coasts down (speed falls over
about 10 s while elapsed time stays frozen), and `0x80` follows 6–19 s later (19 s was
seen after 2.5 mph). The console then drops the connection. So `0x02` alone cannot tell a
pause from a stop; wait for `0x80`, or for a long enough pause.

## How this project uses it

Recording starts on `0x0a`, pauses on `0x02`, resumes on `0x0a`, and ends on `0x80` (or if
the console disconnects, or after a long pause). The workout's time, distance and calories
are the console's own totals from the last `0x53` frame, so they match the display; the
1 Hz records keep speed, incline and heart rate (from a standard BLE heart-rate strap) for
charts. Every raw notification is logged, so a parsing fix can be replayed over old
workouts (`gymtracker rebuild-db --reparse`).
