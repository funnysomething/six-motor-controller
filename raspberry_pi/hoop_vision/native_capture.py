"""Explicit macOS camera selection and a bounded binary frame transport."""

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[2]


def build_bridge(cache=None):
    if sys.platform != "darwin":
        raise ValueError("Continuity Camera capture requires macOS")
    if not shutil.which("xcrun"):
        raise ValueError("Install Apple's command line tools first: xcode-select --install")
    native = Path(__file__).with_name("macos")
    compiler = subprocess.run(["xcrun", "--find", "swiftc"], check=True, capture_output=True, text=True).stdout.strip()
    sdk = subprocess.run(["xcrun", "--show-sdk-path"], check=True, capture_output=True, text=True).stdout.strip()
    key = hashlib.sha256((native/"CameraBridge.swift").read_bytes() + (native/"Info.plist").read_bytes() +
                         (platform.mac_ver()[0]+sdk+compiler+str(Path(compiler).stat().st_mtime_ns)).encode()).hexdigest()[:16]
    cache = Path(cache) if cache else ROOT/"camera_results/.continuity-camera"
    destination = cache/key/"HoopCameraBridge.app"
    binary = destination/"Contents/MacOS/HoopCameraBridge"
    if binary.is_file():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        app = Path(temporary)/"HoopCameraBridge.app"
        executable = app/"Contents/MacOS/HoopCameraBridge"
        executable.parent.mkdir(parents=True)
        shutil.copyfile(native/"Info.plist", app/"Contents/Info.plist")
        arch = "arm64" if platform.machine()=="arm64" else "x86_64"
        subprocess.run([compiler, "-O", "-sdk", sdk, "-swift-version", "5", "-target", f"{arch}-apple-macosx13.0",
                        str(native/"CameraBridge.swift"), "-o", str(executable)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(app)], check=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            app.rename(destination)
        except OSError:
            # Another launcher may have finished the same build first.
            if not binary.is_file():
                raise
    return binary


def list_cameras(binary):
    result = subprocess.run([str(binary), "--list"], capture_output=True, text=True, timeout=75)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Camera discovery failed")
    devices = json.loads(result.stdout)
    if not isinstance(devices,list) or any(not isinstance(d,dict) or not d.get("id") or not d.get("name") for d in devices):
        raise ValueError("Malformed camera discovery response")
    return devices


def select_camera(devices, device_id=None, name=None):
    if bool(device_id)==bool(name):
        raise ValueError("Choose exactly one --device-id or --camera-name; no automatic camera fallback")
    matches = [d for d in devices if d["id"]==device_id] if device_id else [d for d in devices if name.casefold() in d["name"].casefold()]
    if len(matches)!=1:
        raise ValueError("Camera selection must match exactly one available device. Run --list-cameras and use its exact --device-id.")
    return matches[0]


def read_exact(stream, count, allow_eof=False):
    chunks = bytearray()
    while len(chunks)<count:
        part = stream.read(count-len(chunks))
        if not part:
            if not chunks and allow_eof:
                return None
            raise EOFError("Camera frame transport ended mid-packet")
        chunks.extend(part)
    return bytes(chunks)


@dataclass(frozen=True)
class CameraPacket:
    metadata: dict
    jpeg: bytes
    received_at: float


def read_packet(stream, clock=time.monotonic):
    prefix = read_exact(stream,4,allow_eof=True)
    if prefix is None:
        return None
    size = struct.unpack("!I",prefix)[0]
    if not 1<=size<=16384:
        raise ValueError("Invalid camera frame header length")
    metadata = json.loads(read_exact(stream,size))
    if not isinstance(metadata, dict):
        raise ValueError("Invalid camera frame header")
    if type(metadata.get("sequence")) is not int or metadata["sequence"] < 1:
        raise ValueError("Invalid camera frame sequence")
    for key,maximum in (("width",8192),("height",8192),("jpeg_bytes",16*1024*1024)):
        value=metadata.get(key)
        if type(value) is not int or not 1<=value<=maximum:
            raise ValueError(f"Invalid camera {key}")
    pts=metadata.get("source_time_s")
    if (type(pts) not in (int,float) or not math.isfinite(pts) or pts<0 or
            metadata.get("timestamp_provenance")!="avfoundation_pts" or
            not isinstance(metadata.get("device_id"),str) or not metadata["device_id"]):
        raise ValueError("Invalid camera identity or presentation timestamp")
    jpeg=read_exact(stream,metadata["jpeg_bytes"])
    return CameraPacket(metadata,jpeg,clock())


class LatestSlot:
    """One frame only. Publishing replaces any unconsumed frame."""
    def __init__(self):
        self.condition=threading.Condition()
        self.version=0
        self.value=None
        self.closed=False
        self.error=None

    def publish(self,value):
        with self.condition:
            if self.closed:
                return
            self.value=value
            self.version+=1
            self.condition.notify_all()

    def take(self,after=0,timeout=.1):
        with self.condition:
            self.condition.wait_for(lambda:self.version>after or self.closed,timeout)
            return (self.version,self.value) if self.version>after else (after,None)

    def close(self,error=None):
        with self.condition:
            self.closed=True
            self.error=error
            self.value=None
            self.condition.notify_all()


class BridgeCapture:
    def __init__(self,binary,device,fps,on_frame=None,max_dimension=960):
        self.device=device
        self.slot=LatestSlot()
        self.logs=deque(maxlen=12)
        self.on_frame=on_frame
        self.process=subprocess.Popen([str(binary),"--device-id",device["id"],"--fps",str(fps),
                                       "--max-dimension",str(max_dimension)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL)
        self.error_reader=threading.Thread(target=self._errors,daemon=True)
        self.reader=threading.Thread(target=self._read,daemon=True)
        self.error_reader.start()
        self.reader.start()

    def _errors(self):
        for line in iter(self.process.stderr.readline,b""):
            self.logs.append(line.decode("utf-8",errors="replace").strip())

    def _read(self):
        error="Camera stream ended. Restart explicitly after reconnecting; no fallback camera was opened."
        try:
            while True:
                packet=read_packet(self.process.stdout)
                if packet is None:
                    break
                if packet.metadata["device_id"]!=self.device["id"]:
                    raise ValueError("Camera identity changed; stream rejected")
                if self.on_frame:
                    self.on_frame(packet)
                self.slot.publish(packet)
        except (ValueError,EOFError,OSError) as exc:
            error=str(exc)
        finally:
            self.slot.close(error)
            if self.process.poll() is None:
                self.process.terminate()

    def close(self):
        self.slot.close("Camera stopped")
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.error_reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()
