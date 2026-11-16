// Native half of the Fleet Trash view (Gateway 0.2.264): the three facts each row states, and the rule
// that a field a Mac does not report reads "not reported" — never 0, which would claim a measurement.
//
// The view is `menubar/FleetTrashTab.swift`; this compiles it with every other menubar source, so a
// renamed field in the decoders fails here rather than on the owner's screen.

import Foundation

// The two globals the app's entry point would have defined below `// MARK: - Entry point` (that section
// is not compiled in a harness); same values `tests/fleet_health_harness.swift` supplies.
let arguments = CommandLine.arguments
let launchWindow: String? = nil

var failures = 0
func check(_ condition: Bool, _ what: String) {
    if condition { print("ok: \(what)") } else { failures += 1; print("FAIL: \(what)") }
}

func decodeMachine(_ json: String) -> FleetMachine {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    do {
        return try decoder.decode(FleetMachine.self, from: Data(json.utf8))
    } catch {
        print("FAIL: could not decode the fixture machine: \(error)")
        exit(2)
    }
}

func makeRow(_ machine: FleetMachine, _ payload: [String: Any]?, error: String? = nil) -> FleetTrashRow {
    FleetTrashRow(machine: machine,
                  report: FleetTrash.Report(machine: machine, payload: payload, error: error))
}

let mac = decodeMachine("""
{"location_id":"ctl1","location":"Terranash","machine":"node-1","label":"terranash-1500","chip":"M4 Pro",
 "disk_free_gb":40.0,"available_gb":7.5,
 "disk":{"state":"green","free_gb":40.0,"total_gb":500.0}}
""")
// A Mac whose Hub states none of the three fleet figures: only the trash answer can supply them.
let bare = decodeMachine("""
{"location_id":"ctl1","location":"Terranash","machine":"node-2","label":"terranash-1200"}
""")

let fresh = Date().timeIntervalSince1970

// 1. All three stated by the Mac's own trash answer (Hub 0.6.243), each read into its own column.
let full = makeRow(mac, ["supported": true, "status": "fresh", "measured_at": fresh,
                         "count": 3, "bytes": 5_000_000_000,
                         "disk": ["free_bytes": 41_000_000_000],
                         "memory": ["free_bytes": 9_000_000_000]])
check(full.trashBytes == 5_000_000_000, "the folder's size is the Mac's stated bytes")
check(full.freeDiskBytes == 41_000_000_000, "free disk is the answer's disk figure")
check(full.freeMemoryBytes == 9_000_000_000, "free memory is the answer's memory figure")
check(full.trashText.contains("3 items"), "the Trash column names the item count")
check(full.deletable, "a fresh reading with items can be emptied")

// 2. An answer that states no memory falls back to the fleet row's free memory — never to the disk.
let noMemory = makeRow(mac, ["supported": true, "status": "fresh", "measured_at": fresh,
                             "count": 1, "bytes": 1_000_000])
check(noMemory.freeMemoryBytes == 7_500_000_000, "free memory falls back to the fleet row")
check(noMemory.freeDiskBytes == 40_000_000_000, "free disk falls back to the fleet row's disk")

// 3. A Mac that reports nothing reads "not reported" in both columns — never 0.
let none = makeRow(bare, ["supported": true, "status": "fresh", "measured_at": fresh, "count": 0, "bytes": 0])
check(none.freeDiskBytes == nil && none.diskText == "not reported", "an unstated free disk reads not reported")
check(none.freeMemoryBytes == nil && none.memoryText == "not reported", "an unstated free memory reads not reported")
check(none.trashText == "empty", "a stated 0 bytes is a real reading, shown as empty, not as not reported")

// 4. A boolean is not a number: `true` must not become one byte.
let flag = makeRow(bare, ["supported": true, "status": "fresh", "measured_at": fresh,
                          "count": 1, "bytes": true])
check(flag.trashBytes == nil && flag.trashText == "not reported", "a boolean size is not read as a number")

// 5. A Mac that cannot be read says why, in the Trash column, and cannot be emptied.
let away = makeRow(mac, ["supported": false, "status": "unreachable", "detail": "asleep"])
check(away.trashText == "unreachable" && !away.deletable, "an unreachable Mac says so and cannot be emptied")
let old = makeRow(mac, ["supported": false, "status": "unknown", "needs_hub": "0.6.182"])
check(old.trashText == "needs Hub 0.6.182", "an older Hub is named, not zeroed")
let failed = makeRow(mac, nil, error: "timed out")
check(failed.trashText == "could not read" && !failed.deletable, "a failed read says so and cannot be emptied")
check(failed.diskText != "not reported", "a failed trash read still draws the fleet row's disk")

// 6. Memory never lands in the disk column: an answer with memory only, on a Mac with no disk figures.
let memoryOnly = makeRow(bare, ["supported": true, "status": "fresh", "measured_at": fresh,
                                "count": 2, "bytes": 2_000_000_000,
                                "memory": ["free_bytes": 2_000_000_000]])
check(memoryOnly.diskText == "not reported", "a memory figure is never drawn as free disk")
check(memoryOnly.memoryText != "not reported", "and it is drawn in its own column")

if failures > 0 {
    print("FAIL: \(failures) check(s) failed")
    exit(1)
}
print("PASS: 17 checks - three fields, not reported, never 0")
