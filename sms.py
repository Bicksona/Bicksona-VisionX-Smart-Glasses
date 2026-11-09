"""sms.py — Send current GPS location via Twilio SMS.

Usage (standalone):   python3 sms.py
Usage (from VisionX): python3 sms.py <latitude> <longitude>

When lat/lon are passed as arguments the GPS serial port is NOT opened,
avoiding the "multiple access on port" conflict with gps_navigation.py.
When run standalone (no args) it reads from the serial port directly.
"""

import os
import sys
import time
import serial
import pynmea2
from twilio.rest import Client
from geopy.geocoders import Nominatim

# Twilio Configuration
TWILIO_ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID",    "KEY")
TWILIO_AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN",     "TOKEN")
TWILIO_PHONE_NUMBER   = os.environ.get("TWILIO_PHONE_NUMBER",   "NUMBER")
RECIPIENT_PHONE_NUMBER = os.environ.get("RECIPIENT_PHONE_NUMBER", "NUMBER")

GPS_PORT     = "/dev/serial0"
GPS_BAUDRATE = 9600


def get_gps_location():
    """Read GPS from serial port — only used when run standalone."""
    ser = None
    try:
        ser = serial.Serial(GPS_PORT, baudrate=GPS_BAUDRATE, timeout=1)
        print("GPS serial port opened. Waiting for fix...")

        for i in range(60):
            try:
                line = ser.readline().decode("ascii", errors="replace").strip()
            except Exception as e:
                print(f"Error reading serial: {e}")
                time.sleep(1)
                continue

            if not line.startswith("$GNGGA"):
                continue

            try:
                msg = pynmea2.parse(line)
                if (msg.latitude and msg.longitude
                        and msg.latitude != 0.0
                        and msg.longitude != 0.0
                        and int(msg.gps_qual) > 0):
                    print(f"GPS fix: {msg.latitude}, {msg.longitude}")
                    return msg.latitude, msg.longitude
                else:
                    print(f"Attempt {i+1}/60: waiting for fix (qual={msg.gps_qual})")
            except pynmea2.ParseError:
                continue
            except Exception as e:
                print(f"GNGGA error: {e}")

        print("No GPS fix after 60 attempts.")
        return None, None

    except serial.SerialException as e:
        print(f"Serial error: {e}")
        return None, None
    finally:
        if ser and ser.is_open:
            ser.close()


def get_location_name(latitude, longitude) -> str:
    try:
        loc = Nominatim(user_agent="visionx_sms").reverse(
            f"{latitude}, {longitude}", language="en"
        )
        return loc.address if loc and loc.address else "Location name not found"
    except Exception as e:
        print(f"Geocoding error: {e}")
        return "Unable to get location name"


def send_sms(latitude, longitude) -> bool:
    try:
        location_name   = get_location_name(latitude, longitude)
        maps_link       = f"https://www.google.com/maps?q={latitude},{longitude}"
        message_body    = (
            f"VisionX SOS — Current Location:\n{location_name}\n\n"
            f"Coordinates: {latitude:.6f}, {longitude:.6f}\n\n"
            f"Map: {maps_link}"
        )

        print("Sending SMS…")
        msg = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=RECIPIENT_PHONE_NUMBER,
        )
        print(f"SMS sent! SID: {msg.sid}")
        return True

    except Exception as e:
        print(f"SMS send failed: {e}")
        return False


def main():
    # If lat/lon passed as args (called from VisionX), use them directly
    if len(sys.argv) == 3:
        try:
            lat = float(sys.argv[1])
            lon = float(sys.argv[2])
            print(f"Using provided coordinates: {lat}, {lon}")
        except ValueError:
            print("Invalid coordinates passed as arguments.")
            sys.exit(1)
    else:
        # Standalone mode — read from GPS serial port
        print("Starting GPS tracker (standalone mode)...")
        lat, lon = get_gps_location()
        if lat is None:
            print("Could not get GPS fix. SMS not sent.")
            sys.exit(1)

    success = send_sms(lat, lon)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
