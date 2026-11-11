# Bluetooth setup for linkbench

The Pi Zero 2 W has one radio for Wi-Fi **and** Bluetooth. Run Bluetooth tests with the Pi
connected over USB (not over its own Wi-Fi) so the two do not compete, and test Classic and
BLE one at a time.

## Pair the Pi with the computer (needed for Bluetooth Classic / RFCOMM)

On the Pi:

```
sudo bluetoothctl
power on
agent NoInputNoOutput
default-agent
discoverable on
pairable on
show            # note the Controller address, e.g. B8:27:EB:12:34:56
```

On the computer, add a Bluetooth device and pick the Pi (usually named after its hostname).
Confirm the pairing on both sides if asked. Then on the Pi: `discoverable off`, `quit`.

## Bluetooth Classic (RFCOMM)

```
# Pi
python -m linkbench.agent --transports rfcomm          # listens on channel 1
# Computer (Windows or Linux – Python on macOS has no RFCOMM sockets)
python -m linkbench.host run --transport rfcomm --addr B8:27:EB:12:34:56 --link bt --profile imu
```

If the agent cannot bind the channel, another service is using it: try `--rfcomm-channel 3`
on the Pi and `--port 3` on the computer.

## Bluetooth Low Energy (GATT)

```
# Pi
pip install bless
python -m linkbench.agent --transports ble             # advertises as "linkbench"
# Computer
pip install bleak
python -m linkbench.host run --transport ble --addr linkbench --link ble --profile imu
```

No pairing is needed for BLE. Throughput is small (tens of kB/s), so start with the `imu`
profile; `full` with camera frames will saturate it, which is itself a useful result.
