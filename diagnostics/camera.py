import cv2
import time


def main():
    from picamera2 import Picamera2

    frame_width, frame_height = 640, 480
    fps = 20.0

    picam2 = Picamera2()
    config = picam2.create_video_configuration(
        main={"size": (frame_width, frame_height), "format": "RGB888"}
    )
    picam2.configure(config)
    picam2.start()

    print(f"Camera resolution set to: {frame_width}x{frame_height} at {fps} FPS")

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    output_filename = 'output_recording.mp4'
    out = cv2.VideoWriter(output_filename, fourcc, fps, (frame_width, frame_height))

    print("Warming up camera sensor...")
    time.sleep(1)

    print("Recording started for 5 seconds...")
    duration = 5.0
    total_frames_to_record = int(fps * duration)
    frames_recorded = 0
    frame_delay = 1.0 / fps

    while frames_recorded < total_frames_to_record:
        loop_start = time.time()

        frame_rgb = picam2.capture_array()
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        out.write(frame_bgr)
        frames_recorded += 1

        time_elapsed = time.time() - loop_start
        if time_elapsed < frame_delay:
            time.sleep(frame_delay - time_elapsed)

    picam2.stop()
    picam2.close()
    out.release()
    print(f"Recording finished! Video saved as '{output_filename}'")


if __name__ == "__main__":
    main()
