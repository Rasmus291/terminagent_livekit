import sounddevice as sd
import struct
import time

tests = [
    (1, 1, 16000, "Jabra 1ch 16k"),
    (1, 1, 44100, "Jabra 44100Hz"),
    (2, 1, 16000, "AirPods 1ch"),
    (3, 1, 16000, "Realtek Array 1ch"),
    (3, 2, 16000, "Realtek Array 2ch"),
    (31, 2, 16000, "Realtek HD Mic Array"),
    (33, 2, 16000, "Realtek HD Mic"),
]

for dev_id, channels, rate, label in tests:
    try:
        chunks = []
        def make_cb(store):
            def callback(indata, frames, time_info, status):
                store.append(bytes(indata))
            return callback

        stream = sd.RawInputStream(
            samplerate=rate, channels=channels, dtype="int16",
            blocksize=4096, callback=make_cb(chunks), device=dev_id
        )
        stream.start()
        time.sleep(1.5)
        stream.stop()
        stream.close()

        total = b"".join(chunks)
        if total:
            n = len(total) // 2
            samples = struct.unpack("<%dh" % n, total)
            peak = max(abs(s) for s in samples)
            if peak > 500:
                result = "OK"
            elif peak > 100:
                result = "LEISE"
            else:
                result = "KEIN SIGNAL"
            print(f"[{dev_id}] {label}: Peak={peak} -> {result}")
        else:
            print(f"[{dev_id}] {label}: Keine Daten")
    except Exception as e:
        print(f"[{dev_id}] {label}: Fehler - {e}")
