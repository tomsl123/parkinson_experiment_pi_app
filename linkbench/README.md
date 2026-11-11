# linkbench – Pi ↔ computer connection benchmark

Measures whether a link between the Pi (Zero 2 W) and the researcher's computer is fast,
complete and intact enough for MOVES: latency, loss, achieved rates, jitter, throughput,
clock drift and data integrity – for **USB cable, Wi-Fi hotspot, LAN, Bluetooth Classic and
BLE**, with synthetic test data or the real sensors.

```
Pi (agent)                                    computer (host)
python -m linkbench.agent   ── link ──►   python -m linkbench.host serve  → http://127.0.0.1:8770
  streams with seq + timestamp + CRC          measures, records runs/<run_id>/, shows dashboard
  answers pings, reports CPU/temp/Wi-Fi
```

## Quick start (no Pi needed)

```
pip install -r linkbench/requirements_host.txt
python -m linkbench.agent                      # terminal 1: a local agent (ws :8765, udp :8766)
python -m linkbench.host serve                 # terminal 2: open http://127.0.0.1:8770
```

Pick *Local test*, profile *full*, press Start. Use *Fault injection* to see the measurements
react (e.g. 5 % drop, 30 ms delay). Tests: `python -m pytest linkbench/tests`.

On Windows the local agent's timer ticks in ~15.6 ms steps, so local jitter looks worse than
it is; on the Pi (Linux) this does not apply.

## On the Pi

Copy the project (the agent needs `linkbench/`; real-sensor mode also needs `pi/`,
`config.py` and `diagnostics/`), then from the project root:

```
pip install -r linkbench/requirements_agent.txt
python -m linkbench.agent                              # ws + udp
python -m linkbench.agent --transports ws,udp,rfcomm   # add Bluetooth Classic
python -m linkbench.agent --transports ble             # BLE (one Bluetooth mode at a time)
```

### Setting up each connection

| Connection | Setup (once) | Pi address | Dashboard: transport / address |
|---|---|---|---|
| USB cable (baseline) | `sudo pi_setup/usb_gadget.sh`, reboot, plug the Pi's USB **data** port into the computer | 10.55.0.1 | WebSocket / 10.55.0.1 |
| Wi-Fi AP (Pi hotspot) | `sudo HOTSPOT_PASS=… pi_setup/hotspot.sh`, join `MOVES-BENCH` from the computer | 10.42.0.1 | WebSocket / 10.42.0.1 |
| LAN (shared router) | Pi and computer on the same Wi-Fi/router | `hostname -I` on the Pi | WebSocket / that IP |
| Bluetooth Classic | pair once, see `pi_setup/bt_pair.md` | Pi's BT MAC | RFCOMM / MAC, channel 1 |
| BLE | `pip install bless` (Pi) and `bleak` (computer) | name `linkbench` | BLE GATT / linkbench |

For every IP link, also run once with **UDP**: TCP (WebSocket) hides loss by retransmitting,
which shows up as latency spikes and stalls instead; UDP shows the link's raw loss.

## Profiles

| Profile | What runs | Use it to answer |
|---|---|---|
| `ping` | pings only | base latency and clock behaviour |
| `imu` | 2 × IMU at 100 Hz | can the leg sensors stream reliably? |
| `full` | 2 × IMU 100 Hz, audio level 50 Hz, camera 15 fps × 8 kB | the real session load |
| `camera_stress` | IMUs + camera 30 fps × 15 kB | headroom for better lip tracking |
| `bulk` | 6 MB file (≈ 1 min of WAV), SHA-256 checked | how fast recordings come off the Pi |
| `ramp` | IMUs + camera 5 → 90 fps × 12 kB, 5 s per step | maximum sustainable throughput |

CLI equivalent: `python -m linkbench.host run --transport ws --addr 10.42.0.1 --link ap --profile full`.
`--source real` sends the real sensors instead (MPU6050 at 0x68 = left, 0x69 = right, the
INMP441 RMS level, the camera as 320×240 JPEG); a sensor that is missing falls back to the
synthetic pattern, and the dashboard says so.

## What is measured

| Measure | How |
|---|---|
| Clock offset and drift | 10 pings/s with four timestamps (NTP method), offset from the lowest-RTT pings, drift by linear fit |
| One-way latency | receive time − (send time on the Pi − clock offset), per frame |
| Round-trip time | from the pings |
| Loss, duplicates, reordering | sequence numbers per stream; tail loss from the Pi's last sent number |
| Integrity | CRC-32 per frame, plus a byte-by-byte check of the synthetic payload; SHA-256 for bulk |
| Received vs sender rate | received rate from arrival times; **sender rate from the Pi's own send timestamps** |
| Jitter and stalls | spread of inter-arrival gaps; a stall is a gap over 5 nominal periods |
| Sender overruns, send wait | Pi fell more than 5 periods behind its schedule; time blocked in `send()` |
| Pi health | CPU %, temperature, throttling flags, memory, Wi-Fi signal and bitrate (1 Hz) |

**Reading the results.** Sender rate low → the Pi could not keep up (CPU, sensor read). Sender
rate fine but received rate low, loss or stalls → the link. Long send wait → the link pushed
back (TCP congestion). Draft pass thresholds, per stream: loss ≤ 0.1 %, rate within ±2 % of
nominal, live p95 latency ≤ 100 ms, zero integrity errors.

## Suggested test plan

For each connection: `ping`, `full` (60 s, synthetic), `full` (60 s, real sensors), `bulk`,
`ramp`; add UDP runs for the IP links; repeat the Wi-Fi runs at 1 m, 5 m and through a wall
(use the *Note* field). Then tick the runs in *Compare runs*. Each run is saved in
`linkbench/runs/<run_id>/` (packets.csv, pings.csv, sysinfo.jsonl, summary.json) and has a
self-contained HTML report (`python -m linkbench.host report <run_id>`).

## Limitations

- RFCOMM and BLE are implemented but not yet tested on real hardware.
- UDP frames must stay below ~64 kB; bulk over UDP is unpaced, so it measures burst loss.
- Python on macOS has no RFCOMM sockets; use Windows or Linux for Bluetooth Classic.
- The host processes frames in Python; above a few thousand frames per second the host itself
  can become the bottleneck (check with a local run first).
