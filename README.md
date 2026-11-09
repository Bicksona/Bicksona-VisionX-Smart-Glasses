
# VisionX – Smart Glasses for Visually Impaired People

VisionX is a Raspberry Pi-based assistive smart-glasses project designed to help visually impaired people understand nearby surroundings and navigate using voice interaction and audio feedback.

## Project Features

- **Obstacle alerts:** Uses a camera feed and YOLO object detection to identify selected nearby objects. The current code checks for people, cars, buses, trucks, bicycles, chairs, and benches.
- **Direction announcements:** Reports detected obstacles as being on the left, center, or right.
- **Proximity indication:** Uses the detected object's bounding-box area as a rough proximity heuristic and announces objects as near or very close. This is an estimate, not a measured distance.
- **Scene description:** Sends a camera snapshot to Google Gemini and speaks a short description of the scene.
- **Text reading (OCR):** Uses Google Gemini to read visible text from a camera snapshot and speaks the result.
- **Voice commands:** Uses a Picovoice Porcupine wake word and speech recognition to receive commands.
- **GPS walking navigation:** Reads GPS coordinates and uses OpenRouteService to request walking directions, announce turns, detect arrival, and request a reroute when the user moves off the route.
- **Location sharing:** Can pass the current GPS coordinates to an external `sms.py` script for sharing.

## Hardware and Project Image

The hardware components are shown in the infographic below. Keep the image file in the **same folder as this README** in the GitHub repository so that GitHub can display it here.

![VisionX Hardware Components](./VisionX%20Hardware%20Components%20Infographic.png)

## Project Files

```text
VisionX-Smart-Glasses/
├── README.md
├── VisionX Hardware Components Infographic.png
├── visionx.py
└── gps_navigation.py
```

- **`visionx.py`** – Main application: camera handling, obstacle detection, voice commands, Gemini-based scene description and text reading, audio feedback, and command dispatch.
- **`gps_navigation.py`** – GPS reading, geocoding, walking-route requests, turn guidance, arrival detection, and rerouting.
- **`VisionX Hardware Components Infographic.png`** – Hardware-components infographic displayed above.

## Software and Services

The current source code uses:

- Python 3.10 or later (the code uses modern Python type-hint syntax)
- Raspberry Pi OS and the `Picamera2` camera library
- OpenCV
- Ultralytics YOLO (`yolov8n.pt`)
- Picovoice Porcupine
- `sounddevice` and PortAudio
- SpeechRecognition (Google speech-recognition service)
- Google Gen AI Python SDK (Gemini 2.5 Flash)
- gTTS and the `mpg123` command-line audio player
- pySerial and pynmea2
- Requests and geopy (Nominatim geocoding)

The Python dependencies can be installed with pip where supported:

```bash
pip install ultralytics pvporcupine sounddevice SpeechRecognition google-genai gTTS pyserial pynmea2 requests geopy
```

Install and configure Raspberry Pi-specific components separately. In particular, `Picamera2`, OpenCV, PortAudio, and `mpg123` may need to be installed using Raspberry Pi OS packages or other platform-specific instructions. Make sure the camera, microphone, speaker/audio output, and GPS receiver are configured and accessible to the user running the application.

## API Keys and Configuration

**Do not commit API keys, access keys, passwords, or `.env` files to GitHub.** The application reads credentials from environment variables; it does not require secret values to be written into the Python source files.

Set the following environment variables on the Raspberry Pi before starting the application:

| Environment Variable | Used For | Required? |
|---|---|---|
| `PORCUPINE_ACCESS_KEY` | Picovoice Porcupine wake-word detection | Yes |
| `GEMINI_API_KEY` | Gemini scene description and text reading | Yes |
| `ORS_API_KEY` | OpenRouteService walking directions | Required for route navigation |
| `KEYWORD_PATH` | Path to your Porcupine `.ppn` wake-word file | Optional; defaults to `/home/pi/project/hey-vision.ppn` |

Example for a Linux shell (replace the placeholders with your own values; do not put real values in this README):

```bash
export PORCUPINE_ACCESS_KEY="YOUR_PICOVOICE_ACCESS_KEY"
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
export ORS_API_KEY="YOUR_OPENROUTESERVICE_API_KEY"
export KEYWORD_PATH="/path/to/your/hey-vision.ppn"
```

`PORCUPINE_ACCESS_KEY` and `GEMINI_API_KEY` are checked when the main application starts. The GPS navigation module reports that navigation is unavailable when `ORS_API_KEY` is not set.

## Hardware and Runtime Configuration

Check these settings in the source code and adjust them to match your Raspberry Pi setup before running:

- GPS serial port: `/dev/ttyAMA0`
- GPS baud rate: `9600`
- Microphone input device index: `1`
- Audio input sample rate: `48000 Hz`
- Camera capture size: `320 × 240`
- Porcupine keyword file: set `KEYWORD_PATH` to the location of your `.ppn` file

The location-sharing command currently runs `/home/pi/project/sms.py`. Add and configure that script at the expected location, or update the path in `visionx.py` to match your setup before using location sharing.

## Run VisionX

Place `visionx.py` and `gps_navigation.py` in the same directory. Configure the hardware, install the required system and Python dependencies, and set the environment variables above. Then run:

```bash
python3 visionx.py
```

## Important Notes

- An internet connection is needed for Gemini, online speech recognition, gTTS, geocoding, and OpenRouteService features.
- GPS navigation requires a working GPS fix and a valid OpenRouteService API key.
- The current obstacle proximity labels are inferred from image bounding-box area; they are not calibrated physical distance measurements.
- The location-sharing feature depends on the separate `sms.py` script, which is not included in the two Python files listed here.
- Before publishing the repository, check all project files for credentials and keep private keys outside source control.

## License

No license has been specified yet. Add a `LICENSE` file if you decide to publish the project under an open-source license.
