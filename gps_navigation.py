import os
import math
import threading
import time
import serial
import pynmea2
import requests
from geopy.geocoders import Nominatim

GPS_PORT         = "/dev/ttyAMA0"
GPS_BAUD         = 9600
GPS_FIX_TIMEOUT  = 60
ORS_BASE_URL     = "https://api.openrouteservice.org/v2/directions/foot-walking"

ANNOUNCE_DISTANCE = 20   # metres: speak next turn instruction
REROUTE_DISTANCE  = 50   # metres off-path before recalculating
ARRIVAL_DISTANCE  = 15   # metres from destination to declare arrival
LOCATION_INTERVAL = 30   # seconds between automatic street name updates

# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2) -> float:
    """Straight-line distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_to_direction(bearing: float) -> str:
    dirs = ["north", "north-east", "east", "south-east",
            "south", "south-west", "west", "north-west"]
    return dirs[round(bearing / 45) % 8]


def compute_bearing(lat1, lon1, lat2, lon2) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def metres_to_words(metres: float) -> str:
    if metres < 50:
        return f"{int(round(metres / 5) * 5)} metres"
    if metres < 1000:
        return f"{int(round(metres / 10) * 10)} metres"
    return f"{metres / 1000:.1f} kilometres"


def format_instruction(step: dict) -> str:
    raw  = step.get("instruction", "")
    dist = metres_to_words(step.get("distance", 0))
    return f"{raw}, in {dist}" if raw else f"Continue for {dist}"


# ---------------------------------------------------------------------------
# NavigationModule
# ---------------------------------------------------------------------------

class NavigationModule:
    """Owns all GPS I/O and turn-by-turn navigation logic.

    Lifecycle
    ---------
    nav = NavigationModule(ors_api_key="...", speak_fn=speak)
    nav.start()          # launches GPS daemon thread
    nav.handle_command(text)  # call from VisionX command loop
    """

    # Voice-command keywords → handler method name
    _NAV_KEYWORDS = {
        "take me to":       "_cmd_navigate",
        "navigate to":      "_cmd_navigate",
        "where am i":       "announce_current_location",
        "stop navigation":  "stop_navigation",
        "navigation help":  "navigation_help",
    }

    def __init__(self, ors_api_key: str, speak_fn):
        self.api_key  = ors_api_key
        self.speak    = speak_fn
        self.geocoder = Nominatim(user_agent="visionx_nav")

        self._gps_lock  = threading.Lock()
        self._lat: float | None = None
        self._lon: float | None = None
        self._gps_ready = threading.Event()

        self._nav_lock   = threading.Lock()
        self._active     = False
        self._steps: list[dict] = []
        self._step_index = 0
        self._destination: tuple[float | None, float | None] = (None, None)
        self._dest_name  = ""
        self._last_location_announce = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Launch the GPS reader daemon. Call once at startup."""
        t = threading.Thread(target=self._gps_loop, daemon=True, name="gps-loop")
        t.start()

    def handle_command(self, command: str) -> bool:
        """Parse a recognised navigation voice command.

        Returns True if a navigation keyword was matched (so the caller
        knows not to pass the command further down the dispatch chain).
        """
        lower = command.lower()
        for phrase, method_name in self._NAV_KEYWORDS.items():
            if phrase in lower:
                if method_name == "_cmd_navigate":
                    # Extract destination: everything after the trigger phrase
                    dest = lower.split(phrase, 1)[-1].strip()
                    threading.Thread(
                        target=self.start_navigation,
                        args=(dest,),
                        daemon=True,
                        name="nav-start"
                    ).start()
                else:
                    getattr(self, method_name)()
                return True
        return False

    def announce_current_location(self):
        lat, lon = self._get_coords()
        if lat is None:
            self.speak("No GPS signal yet. Please wait.")
            return
        threading.Thread(target=self._reverse_geocode_speak, args=(lat, lon), daemon=True).start()

    def stop_navigation(self):
        with self._nav_lock:
            if not self._active:
                self.speak("No active navigation.")
                return
            self._active = False
            self._steps  = []
        self.speak("Navigation stopped.")

    def navigation_help(self):
        lines = [
            "Navigation commands:",
            "Say: take me to, followed by a place name to start turn-by-turn walking navigation.",
            "Say: where am I, to hear your current street and area.",
            "Say: stop navigation, to cancel the active route.",
            "Say: navigation help, to repeat these commands.",
            "VisionX announces turns within 20 metres, speaks your street every 30 seconds, "
            "and recalculates if you drift more than 50 metres off path.",
        ]
        for line in lines:
            self.speak(line)

    def start_navigation(self, destination_name: str):
        """Geocode destination, fetch route, run guidance. Runs in its own thread."""
        destination_name = destination_name.strip()
        if not destination_name:
            self.speak("Please say a destination. For example, take me to the railway station.")
            return

        with self._nav_lock:
            if self._active:
                self.speak("Already navigating. Say stop navigation to cancel.")
                return

        self.speak(f"Looking up {destination_name}. Please wait.")

        if not self._gps_ready.wait(timeout=30):
            self.speak("No GPS signal. Please go outside and try again.")
            return

        start_lat, start_lon = self._get_coords()
        if start_lat is None:
            self.speak("Could not get your location. Please try again.")
            return

        dest_lat, dest_lon = self._geocode(destination_name)
        if dest_lat is None:
            self.speak(f"Could not find {destination_name}. Try being more specific.")
            return

        dist = haversine(start_lat, start_lon, dest_lat, dest_lon)
        if dist < ARRIVAL_DISTANCE:
            self.speak(f"You are already at {destination_name}.")
            return

        self.speak(f"{destination_name} is {metres_to_words(dist)} away. Fetching directions.")

        steps = self._fetch_route(start_lat, start_lon, dest_lat, dest_lon)
        if not steps:
            return  # error spoken inside _fetch_route

        with self._nav_lock:
            self._active      = True
            self._steps       = steps
            self._step_index  = 0
            self._destination = (dest_lat, dest_lon)
            self._dest_name   = destination_name

        self.speak(f"Starting navigation to {destination_name}. {format_instruction(steps[0])}")
        self._guidance_loop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_coords(self) -> tuple[float | None, float | None]:
        with self._gps_lock:
            return self._lat, self._lon

    def _geocode(self, place_name: str) -> tuple[float | None, float | None]:
        try:
            loc = self.geocoder.geocode(place_name, timeout=5)
            if loc:
                return loc.latitude, loc.longitude
        except Exception as e:
            print(f"Geocode error: {e}")
        return None, None

    def _reverse_geocode_speak(self, lat: float, lon: float, dist_remaining: float | None = None):
        """Reverse-geocode and speak position. Runs in a background thread."""
        try:
            loc = self.geocoder.reverse((lat, lon), language="en", timeout=5)
            if not loc:
                self.speak(f"You are at coordinates {lat:.4f}, {lon:.4f}")
                return
            addr   = loc.raw.get("address", {})
            road   = addr.get("road", "")
            suburb = addr.get("suburb", addr.get("neighbourhood", ""))
            city   = addr.get("city", addr.get("town", addr.get("village", "")))
            parts  = [p for p in [road, suburb, city] if p]
            label  = ", ".join(parts) if parts else f"{lat:.4f}, {lon:.4f}"
            prefix = "You are on " if road else "You are at "
            suffix = f". {metres_to_words(dist_remaining)} remaining." if dist_remaining else "."
            self.speak(f"{prefix}{label}{suffix}")
        except Exception as e:
            print(f"Reverse geocode error: {e}")

    def _fetch_route(self, start_lat, start_lon, end_lat, end_lon) -> list[dict]:
        if not self.api_key:
            self.speak("Navigation API key not set. Please set ORS_API_KEY.")
            return []

        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        # ORS uses GeoJSON [lon, lat] order
        body = {
            "coordinates": [[start_lon, start_lat], [end_lon, end_lat]],
            "instructions": True,
            "language": "en",
            "units": "m",
        }

        try:
            resp = requests.post(ORS_BASE_URL, json=body, headers=headers, timeout=10)
            resp.raise_for_status()
            data  = resp.json()
            route = data["routes"][0]
            geom  = route["geometry"]["coordinates"]  # each entry is [lon, lat]

            steps = []
            for segment in route["segments"]:
                for step in segment["steps"]:
                    idx = step.get("way_points", [0])[0]
                    lon, lat = geom[idx]          # unpack GeoJSON [lon, lat]
                    steps.append({
                        "instruction": step.get("instruction", ""),
                        "distance":    step.get("distance", 0),
                        "duration":    step.get("duration", 0),
                        "lat": lat,
                        "lon": lon,
                    })
            return steps

        except requests.exceptions.Timeout:
            self.speak("Navigation service timed out. Check your internet connection.")
        except requests.exceptions.HTTPError as e:
            print(f"ORS HTTP error: {e}")
            self.speak("Could not fetch route. Please try again.")
        except (KeyError, IndexError) as e:
            print(f"ORS response parse error: {e}")
            self.speak("Route data was unexpected. Please try again.")
        except Exception as e:
            print(f"ORS error: {e}")
            self.speak("Navigation service unavailable.")

        return []

    def _guidance_loop(self):
        """Poll position every 3 s; advance steps, detect arrival, trigger reroute."""
        reroute_cooldown   = 30  # seconds between consecutive reroute API calls
        last_reroute_check = time.time()

        while True:
            with self._nav_lock:
                if not self._active:
                    break
                steps       = self._steps
                step_index  = self._step_index
                dest_lat, dest_lon = self._destination
                dest_name   = self._dest_name

            lat, lon = self._get_coords()
            if lat is None:
                time.sleep(2)
                continue

            # 1. Arrival check
            dist_to_dest = haversine(lat, lon, dest_lat, dest_lon)
            if dist_to_dest <= ARRIVAL_DISTANCE:
                self.speak(f"You have arrived at {dest_name}.")
                with self._nav_lock:
                    self._active = False
                break

            # 2. Periodic street announcement (background thread — no loop stall)
            now = time.time()
            if now - self._last_location_announce >= LOCATION_INTERVAL:
                self._last_location_announce = now
                threading.Thread(
                    target=self._reverse_geocode_speak,
                    args=(lat, lon, dist_to_dest),
                    daemon=True
                ).start()

            # 3. Step advance
            if step_index < len(steps):
                dist_to_step = haversine(lat, lon, steps[step_index]["lat"], steps[step_index]["lon"])
                if dist_to_step <= ANNOUNCE_DISTANCE:
                    next_index = step_index + 1
                    with self._nav_lock:
                        self._step_index = next_index
                    if next_index < len(steps):
                        self.speak(format_instruction(steps[next_index]))

            # 4. Reroute — cooldown timer reset unconditionally so it fires correctly
            #    (bug in original: timer only reset inside inner if-branch)
            if now - last_reroute_check >= reroute_cooldown:
                last_reroute_check = now          # ← moved outside inner branch
                if step_index < len(steps):
                    dist_off = haversine(lat, lon, steps[step_index]["lat"], steps[step_index]["lon"])
                    if dist_off > REROUTE_DISTANCE:
                        self.speak("You seem to be off the route. Recalculating.")
                        new_steps = self._fetch_route(lat, lon, dest_lat, dest_lon)
                        if new_steps:
                            with self._nav_lock:
                                self._steps      = new_steps
                                self._step_index = 0
                            self.speak(format_instruction(new_steps[0]))

            time.sleep(3)

    def _gps_loop(self):
        """Daemon: read NMEA sentences from NEO-6M, update _lat/_lon."""
        print("GPS: opening", GPS_PORT)
        try:
            ser = serial.Serial(GPS_PORT, baudrate=GPS_BAUD, timeout=1)
        except serial.SerialException as e:
            print(f"GPS: failed to open {GPS_PORT}: {e}")
            self.speak("GPS hardware not found. Navigation unavailable.")
            return

        print("GPS: waiting for fix…")
        fix_acquired   = False
        fix_wait_start = time.time()

        while True:
            try:
                raw = ser.readline().decode("ascii", errors="replace").strip()
                if not raw.startswith("$"):
                    continue

                msg = pynmea2.parse(raw)
                if not isinstance(msg, pynmea2.GGA):
                    continue

                if msg.gps_qual and int(msg.gps_qual) > 0:
                    lat, lon = msg.latitude, msg.longitude
                    with self._gps_lock:
                        self._lat, self._lon = lat, lon
                    if not fix_acquired:
                        fix_acquired = True
                        self._gps_ready.set()
                        print(f"GPS: fix acquired — {lat:.6f}, {lon:.6f}")
                else:
                    if fix_acquired:
                        fix_acquired = False
                        self._gps_ready.clear()
                    if time.time() - fix_wait_start > GPS_FIX_TIMEOUT:
                        self.speak("GPS signal is weak. Please move to an open area.")
                        fix_wait_start = time.time()

            except pynmea2.ParseError:
                continue
            except Exception as e:
                print(f"GPS loop error: {e}")
                time.sleep(1)
