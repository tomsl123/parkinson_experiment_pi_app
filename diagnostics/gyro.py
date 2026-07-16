from smbus2 import SMBus

MPU6050_ADDR = 0x68
PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B
GYRO_XOUT_H = 0x43


def read_word(bus, reg):
    high = bus.read_byte_data(MPU6050_ADDR, reg)
    low = bus.read_byte_data(MPU6050_ADDR, reg + 1)
    value = (high << 8) + low
    if value >= 0x8000:
        value -= 65536
    return value


def read_sensor_data(bus):
    accel_x = read_word(bus, ACCEL_XOUT_H)
    accel_y = read_word(bus, ACCEL_XOUT_H + 2)
    accel_z = read_word(bus, ACCEL_XOUT_H + 4)

    gyro_x = read_word(bus, GYRO_XOUT_H)
    gyro_y = read_word(bus, GYRO_XOUT_H + 2)
    gyro_z = read_word(bus, GYRO_XOUT_H + 4)
    return {
        "accel_x": accel_x,
        "accel_y": accel_y,
        "accel_z": accel_z,
        "gyro_x": gyro_x,
        "gyro_y": gyro_y,
        "gyro_z": gyro_z,
    }

def main():
    try:
        bus = SMBus(1)
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)  # wake sensor from sleep
        for i in range(1000):
            print(read_sensor_data(bus))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()