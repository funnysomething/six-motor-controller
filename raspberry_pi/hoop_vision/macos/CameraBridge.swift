import AppKit
import AVFoundation
import CoreImage
import CoreMedia
import ImageIO
import UniformTypeIdentifiers

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

func cameras() -> [AVCaptureDevice] {
    var types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera]
    if #available(macOS 14.0, *) {
        types += [.external, .continuityCamera]
    } else {
        types.append(.externalUnknown)
    }
    return AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video,
                                            position: .unspecified).devices
}

func authorize() {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized: return
    case .notDetermined:
        let semaphore = DispatchSemaphore(value: 0)
        var allowed = false
        AVCaptureDevice.requestAccess(for: .video) { granted in
            allowed = granted
            semaphore.signal()
        }
        let deadline = Date(timeIntervalSinceNow: 60)
        while semaphore.wait(timeout: .now()) != .success {
            if Date() > deadline { fail("Camera permission was not answered. Allow Hoop Camera Bridge in System Settings > Privacy & Security > Camera, then restart.") }
            RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
        }
        if !allowed { fail("Camera permission denied. Enable Hoop Camera Bridge in System Settings > Privacy & Security > Camera.") }
    default:
        fail("Camera access denied or restricted. Enable Hoop Camera Bridge in System Settings > Privacy & Security > Camera.")
    }
}

final class FrameWriter: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let device: AVCaptureDevice
    let fps: Double
    let context = CIContext(options: [.cacheIntermediates: false])
    var lastPTS = -Double.infinity
    var sequence = 0

    init(device: AVCaptureDevice, fps: Double) { self.device = device; self.fps = fps }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard device.isConnected else { fail("The selected camera disconnected. No other camera will be opened.") }
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        guard pts.isFinite, pts >= 0 else { return }
        // Source sampling only. Never fabricate evenly spaced capture times.
        if pts > lastPTS && pts - lastPTS < 1.0 / fps - 0.001 { return }
        lastPTS = pts
        guard let pixel = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let image = CIImage(cvPixelBuffer: pixel)
        guard let cg = context.createCGImage(image, from: image.extent) else { return }
        let bytes = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(bytes, UTType.jpeg.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(destination, cg, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return }
        sequence += 1
        let header: [String: Any] = ["sequence": sequence, "source_time_s": pts,
            "device_id": device.uniqueID, "width": cg.width, "height": cg.height,
            "jpeg_bytes": bytes.length, "timestamp_provenance": "avfoundation_pts"]
        guard let json = try? JSONSerialization.data(withJSONObject: header) else { return }
        var length = UInt32(json.count).bigEndian
        // One serial writer. The consumer reads these length-prefixed packets
        // continuously into a single latest-frame slot.
        FileHandle.standardOutput.write(Data(bytes: &length, count: 4))
        FileHandle.standardOutput.write(json)
        FileHandle.standardOutput.write(bytes as Data)
    }
}

let args = Array(CommandLine.arguments.dropFirst())
if args.contains("--help") {
    print("HoopCameraBridge --list | --device-id ID [--fps 10]")
    exit(0)
}
func argument(_ flag: String) -> String? {
    guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
authorize()
let discovered = cameras()
if args.contains("--list") {
    let descriptions = discovered.map { ["id": $0.uniqueID, "name": $0.localizedName,
        "type": $0.deviceType.rawValue] }
    let json = try JSONSerialization.data(withJSONObject: descriptions, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(json)
    exit(0)
}
guard let id = argument("--device-id"), let device = discovered.first(where: { $0.uniqueID == id }) else {
    fail("Selected camera is not available. Run --list again after setting up Continuity Camera. No fallback camera was opened.")
}
let fps = Double(argument("--fps") ?? "10") ?? 0
guard fps.isFinite && fps > 0 && fps <= 60 else { fail("FPS must be in (0, 60].") }
let session = AVCaptureSession()
let writer = FrameWriter(device: device, fps: fps)
let delegateQueue = DispatchQueue(label: "org.hoopvision.camera-frames")
session.beginConfiguration()
if session.canSetSessionPreset(.vga640x480) { session.sessionPreset = .vga640x480 }
do {
    let input = try AVCaptureDeviceInput(device: device)
    guard session.canAddInput(input) else { fail("Cannot add the selected camera input.") }
    session.addInput(input)
} catch { fail("Cannot open selected camera: \(error.localizedDescription)") }
let output = AVCaptureVideoDataOutput()
output.alwaysDiscardsLateVideoFrames = true
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
output.setSampleBufferDelegate(writer, queue: delegateQueue)
guard session.canAddOutput(output) else { fail("Cannot add camera frame output.") }
session.addOutput(output)
if let connection = output.connection(with: .video), connection.isVideoMirroringSupported {
    connection.automaticallyAdjustsVideoMirroring = false
    connection.isVideoMirrored = false
}
session.commitConfiguration()

let disconnected = NotificationCenter.default.addObserver(forName: AVCaptureDevice.wasDisconnectedNotification,
    object: device, queue: nil) { _ in fail("Selected camera disconnected; restart explicitly after reconnecting. No fallback.") }
let runtimeError = NotificationCenter.default.addObserver(forName: AVCaptureSession.runtimeErrorNotification,
    object: session, queue: nil) { note in fail("Camera session failed: \(note.userInfo ?? [:])") }
DispatchQueue.global(qos: .userInitiated).async { session.startRunning() }
app.run()
