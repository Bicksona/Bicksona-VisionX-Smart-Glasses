"""VisionX — main entry point (with GPS navigation integration).

Changes from visionx.py
-----------------------
* NavigationModule integrated: GPS daemon starts at boot, handle_command()
  is checked before the generic COMMANDS dict.
* Navigation tasks run in their own thread inside NavigationModule, so
  resume_hazard() fires only after the task is fully done.
* ORS_API_KEY read from environment and passed to NavigationModule.
* Hazard pause/resume wired correctly around navigation commands too.
* Direct function references used in COMMANDS (no redundant lambda wrappers).
"""

import os
import time
import threading
import cv2
import pvporcupine
import sounddevice as sd
import wave
import subprocess
import speech_recognition as sr
from ultralytics import YOLO
from picamera2 import Picamera2
from google.genai import Client, types
from gtts import gTTS
from gps_navigation import NavigationModule

# ================= CONFIG =================

ACCESS_KEY   = os.environ.get("PORCUPINE_ACCESS_KEY", "")
API_KEY      = os.environ.get("GEMINI_API_KEY", "")
ORS_API_KEY  = os.environ.get("ORS_API_KEY", "")
KEYWORD_PATH = os.environ.get("KEYWORD_PATH", "/home/pi/project/hey-vision.ppn")

if not ACCESS_KEY:
    raise ValueError("PORCUPINE_ACCESS_KEY environment variable is not set")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is not set")

MIC_SAMPLE_RATE      = 48000
PORCUPINE_SAMPLE_RATE = 16000
CHANNELS             = 2
DEVICE_INDEX         = 1
RECORD_SECONDS       = 5
DOWNSAMPLE_FACTOR    = MIC_SAMPLE_RATE // PORCUPINE_SAMPLE_RATE

HAZARD_CONFIDENCE    = 0.6
ALERT_COOLDOWN       = 3
HAZARD_FPS_CAP       = 5
HAZARD_PAUSE_SECONDS = 45

client = Client(api_key=API_KEY)
model  = YOLO("yolov8n.pt")

# ================= GLOBAL STATE =================

latest_frame  = None
frame_lock    = threading.Lock()
camera_ready  = threading.Event()
shutdown_event = threading.Event()

speech_process = None
speech_lock    = threading.Lock()

hazard_pause_until = 0.0
hazard_pause_lock  = threading.Lock()

nav: NavigationModule | None = None

# ================= HAZARD PAUSE =================

def pause_hazard(seconds: float = HAZARD_PAUSE_SECONDS):
    global hazard_pause_until
    with hazard_pause_lock:
        hazard_pause_until = time.time() + seconds
    print(f"[Hazard] Paused for {seconds}s")

def resume_hazard():
    global hazard_pause_until
    with hazard_pause_lock:
        hazard_pause_until = 0.0
    print("[Hazard] Resumed")

def is_hazard_paused() -> bool:
    with hazard_pause_lock:
        return time.time() < hazard_pause_until

# ================= SPEAK =================

def speak(text: str, wait: bool = False):
    global speech_process
    print("VisionX:", text)
    try:
        gTTS(text=text, lang="en").save("response.mp3")
    except Exception as e:
        print("TTS save error:", e)
        return

    with speech_lock:
        if speech_process is not None:
            speech_process.wait()
        speech_process = subprocess.Popen(
            ["mpg123", "response.mp3"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if wait:
        with speech_lock:
            if speech_process is not None:
                speech_process.wait()

def stop_speaking():
    global speech_process
    with speech_lock:
        if speech_process is not None:
            speech_process.terminate()
            speech_process.wait()
            speech_process = None

# ================= CAMERA =================

def camera_loop():
    global latest_frame
    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(
        main={"size": (320, 240), "format": "RGB888"}
    ))
    picam2.start()
    for _ in range(20):
        picam2.capture_array()
    camera_ready.set()
    print("Camera ready.")

    while not shutdown_event.is_set():
        frame = picam2.capture_array()
        with frame_lock:
            latest_frame = frame

    picam2.stop()

# ================= HAZARD DETECTION =================

DANGEROUS_OBJECTS = {"person", "car", "bus", "truck", "bicycle", "chair", "bench"}
last_alert      = ""
last_alert_time = 0.0

def hazard_loop():
    global last_alert, last_alert_time
    frame_interval = 1.0 / HAZARD_FPS_CAP

    while not shutdown_event.is_set():
        t0 = time.time()

        if is_hazard_paused():
            time.sleep(0.1)
            continue

        with frame_lock:
            if latest_frame is None:
                time.sleep(0.05)
                continue
            frame = latest_frame.copy()

        for r in model.predict(frame, imgsz=320, verbose=False):
            for box in r.boxes:
                conf  = float(box.conf[0])
                label = model.names[int(box.cls[0])]
                if conf < HAZARD_CONFIDENCE or label not in DANGEROUS_OBJECTS:
                    continue

                x1, y1, x2, y2 = map(int, box.xyxy[0])
                area = (x2 - x1) * (y2 - y1)
                if area > 45000:
                    distance = "very close"
                elif area > 20000:
                    distance = "near"
                else:
                    continue

                cx    = (x1 + x2) // 2
                width = frame.shape[1]
                direction = ("left" if cx < width / 3
                             else "center" if cx < 2 * width / 3
                             else "right")

                alert = f"{label} {distance} on {direction}"
                now   = time.time()
                if alert != last_alert and now - last_alert_time > ALERT_COOLDOWN:
                    speak(alert)
                    last_alert      = alert
                    last_alert_time = now

        sleep_t = frame_interval - (time.time() - t0)
        if sleep_t > 0:
            time.sleep(sleep_t)

# ================= AUDIO =================

def record_command() -> str:
    audio = sd.rec(
        int(RECORD_SECONDS * MIC_SAMPLE_RATE),
        samplerate=MIC_SAMPLE_RATE, channels=CHANNELS,
        device=DEVICE_INDEX, dtype="int16",
    )
    sd.wait()
    filename = "command.wav"
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(MIC_SAMPLE_RATE)
        wf.writeframes(audio[:, 0].tobytes())
    return filename

def speech_to_text(filename: str, retries: int = 2) -> str | None:
    recognizer = sr.Recognizer()
    recognizer.operation_timeout = 5
    with sr.AudioFile(filename) as src:
        audio = recognizer.record(src)

    for attempt in range(retries):
        try:
            text = recognizer.recognize_google(audio).lower()
            print("You:", text)
            return text
        except sr.UnknownValueError:
            if attempt < retries - 1:
                time.sleep(0.5)
            else:
                return None
        except sr.RequestError as e:
            print(f"STT error (attempt {attempt + 1}): {e}")
            if attempt < retries - 1:
                time.sleep(1)

    speak("Network unavailable or too slow. Please try again.")
    return None

# ================= GEMINI VISION =================

def clean_for_speech(text: str) -> str:
    """Strip markdown and symbols that TTS reads aloud awkwardly."""
    import re
    # Remove bullet points / list markers at line start
    text = re.sub(r"^\s*[\*\-•]\s+", "", text, flags=re.MULTILINE)
    # Remove bold/italic markers
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"_+", "", text)
    # Remove markdown headers
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove backticks
    text = text.replace("`", "")
    # Collapse multiple blank lines into one
    text = re.sub(r"\n{2,}", "\n", text)
    # Join lines into natural sentences for TTS
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return ". ".join(lines)


def run_gemini_vision(prompt: str, speaking_msg: str, error_prefix: str):
    speak(speaking_msg)
    try:
        if not camera_ready.wait(timeout=10):
            speak("Camera not ready")
            return

        snapshot = None
        deadline = time.time() + 2
        while time.time() < deadline:
            with frame_lock:
                if latest_frame is not None:
                    snapshot = latest_frame.copy()
                    break
            time.sleep(0.05)

        if snapshot is None:
            speak("Camera not ready")
            return

        _, buf = cv2.imencode(".jpg", snapshot, [cv2.IMWRITE_JPEG_QUALITY, 70])
        response = client.models.generate_content(
            model="models/gemini-2.5-flash",
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=buf.tobytes(), mime_type="image/jpeg"),
            ],
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(timeout=15000)
            ),
        )
        raw = response.text.strip() if response.text else "Nothing detected"
        speak(clean_for_speech(raw), wait=True)

    except Exception as e:
        err = str(e).lower()
        if any(w in err for w in ("network", "connection", "unreachable", "timeout", "socket", "deadline")):
            print(f"Network error — {error_prefix}: {e}")
            speak("Network is too slow or unavailable. Please try again later.", wait=True)
        else:
            print(f"{error_prefix} error: {e}")
            speak("Cloud service unavailable. Please try later.", wait=True)

def run_ocr():
    run_gemini_vision(
        "Read all visible text in the image. "
        "Reply in plain sentences only. No bullet points, no asterisks, no markdown, no lists. "
        "Just read the text naturally as you would speak it aloud.",
        "Reading text", "OCR"
    )

def run_description():
    run_gemini_vision(
        "Describe this scene in two short plain sentences. "
        "Focus on nearby important objects. "
        "No bullet points, no asterisks, no markdown formatting.",
        "Analyzing scene", "Description"
    )

# ================= WAKE WORD =================

porcupine = pvporcupine.create(
    access_key=ACCESS_KEY,
    keyword_paths=[KEYWORD_PATH],
    sensitivities=[0.7],
)

def wake_listener():
    with sd.InputStream(
        device=DEVICE_INDEX, samplerate=MIC_SAMPLE_RATE,
        channels=CHANNELS, dtype="int16"
    ) as stream:
        while not shutdown_event.is_set():
            pcm, _ = stream.read(porcupine.frame_length * DOWNSAMPLE_FACTOR)
            pcm = pcm[::DOWNSAMPLE_FACTOR, 0]     # mono + downsample in one step
            if porcupine.process(pcm) >= 0:
                return

# ================= COMMAND DISPATCH =================

COMMANDS = {
    "stop":     stop_speaking,
    "read":     run_ocr,
    "describe": run_description,
    "time":     lambda: speak(time.strftime("The time is %I:%M %p")),
}

def send_location():
    """Get coords from the already-running NavigationModule and pass to sms.py.
    This avoids opening the serial port twice (which causes the conflict error).
    """
    speak("Sending your location. Please wait.")

    # Get coords from the shared GPS reader — no second serial connection needed
    lat, lon = nav._get_coords() if nav else (None, None)

    if lat is None or lon is None:
        speak("No GPS fix yet. Please move to an open area and try again.")
        return

    try:
        result = subprocess.run(
            ["python3", "/home/pi/project/sms.py", str(lat), str(lon)],
            timeout=30,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            speak("Your location has been sent successfully.")
        else:
            print("sms.py error:", result.stderr)
            speak("Failed to send location. Please try again.")
    except subprocess.TimeoutExpired:
        speak("Location send timed out. Please try again.")
    except Exception as e:
        print(f"send_location error: {e}")
        speak("Could not send location. Please try again.")


def handle_command(command: str) -> bool:
    """Return True if VisionX should shut down."""
    if "shutdown" in command:
        speak("Shutting down Vision X", wait=True)
        return True

    # SOS / location sharing
    if any(p in command for p in ("send my location", "i am lost", "send location")):
        threading.Thread(target=send_location, daemon=True).start()
        return False

    # Navigation module gets first look at every command
    if nav and nav.handle_command(command):
        return False

    for keyword, action in COMMANDS.items():
        if keyword in command:
            action()
            return False

    speak("I did not understand")
    return False

# ================= MAIN =================

def main():
    global nav
    print("VisionX starting…")

    nav = NavigationModule(ors_api_key=ORS_API_KEY, speak_fn=speak)
    nav.start()

    speak("Vision X is ready")
    threading.Thread(target=camera_loop, daemon=True).start()
    threading.Thread(target=hazard_loop, daemon=True).start()

    try:
        while True:
            wake_listener()
            pause_hazard(HAZARD_PAUSE_SECONDS)
            speak("Yes?")
            time.sleep(0.8)

            audio_file = record_command()
            command    = speech_to_text(audio_file)

            if not command:
                resume_hazard()
                continue

            if handle_command(command):
                break

            resume_hazard()

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        shutdown_event.set()
        stop_speaking()
        porcupine.delete()
        print("VisionX stopped.")

if __name__ == "__main__":
    main()