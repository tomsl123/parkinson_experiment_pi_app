import sounddevice as sd
from scipy.io.wavfile import write

sample_rate = 48000
duration = 5

print("Recording...")
audio = sd.rec(
    int(duration * sample_rate),
    samplerate=sample_rate,
    channels=1,
    dtype="int32",
)
sd.wait()

write("recording.wav", sample_rate, audio)
print("Saved recording.wav")