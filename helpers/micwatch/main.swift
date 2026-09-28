// micwatch: prints a JSON array of processes that are currently capturing from an audio input.
//
//   $ micwatch          -> [{"pid":1234,"bundle":"com.microsoft.teams2","input":true}]
//   $ micwatch --all    -> every process Core Audio knows about, with its input flag
//
// Reads Core Audio process objects (macOS 14+). It never opens an audio device and never touches
// audio data. Property listeners on IsRunningInput do not fire on macOS 26, so callers poll this.
//
// Build: swiftc -O -o micwatch main.swift

import CoreAudio
import Foundation

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
}

func processObjects() -> [AudioObjectID] {
    var addr = address(kAudioHardwarePropertyProcessObjectList)
    let system = AudioObjectID(kAudioObjectSystemObject)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr, size > 0 else {
        return []
    }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else {
        return []
    }
    return ids
}

func isRunningInput(_ id: AudioObjectID) -> Bool {
    var addr = address(kAudioProcessPropertyIsRunningInput)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr && value != 0
}

func processPID(_ id: AudioObjectID) -> Int {
    var addr = address(kAudioProcessPropertyPID)
    var value: pid_t = -1
    var size = UInt32(MemoryLayout<pid_t>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr else { return -1 }
    return Int(value)
}

func bundleID(_ id: AudioObjectID) -> String {
    var addr = address(kAudioProcessPropertyBundleID)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value)
    guard status == noErr, let string = value?.takeRetainedValue() else { return "" }
    return string as String
}

let includeAll = CommandLine.arguments.contains("--all")
var rows: [[String: Any]] = []
for id in processObjects() {
    let input = isRunningInput(id)
    if !input && !includeAll { continue }
    rows.append(["pid": processPID(id), "bundle": bundleID(id), "input": input])
}

let json = (try? JSONSerialization.data(withJSONObject: rows, options: [])) ?? Data("[]".utf8)
FileHandle.standardOutput.write(json)
FileHandle.standardOutput.write(Data("\n".utf8))
