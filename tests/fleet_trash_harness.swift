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
var checks = 0
func check(_ condition: Bool, _ what: String) {
    checks += 1
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

// 7. The Mac's macOS Trash (Hub 0.6.243), read from the fleet row into its own column.
let withMacos = decodeMachine("""
{"location_id":"ctl1","machine":"node-3","label":"terranash-1300",
 "macos_trash":{"count":4,"bytes":1500000000}}
""")
let macosRow = makeRow(withMacos, ["supported": true, "status": "fresh", "measured_at": fresh,
                                   "count": 0, "bytes": 0])
check(macosRow.macosTrashBytes == 1_500_000_000, "the macOS Trash size is read from the fleet row")
check(macosRow.canEmpty, "a Mac whose Hub folder is empty can still be emptied for its macOS Trash")
check(macosRow.macosTrashText.contains("4 items"), "and the macOS column states it")

// 8. A Hub older than 0.6.243 states none: "not reported", never 0 — and Empty stays live, the Hub decides.
let olderHub = makeRow(bare, ["supported": true, "status": "fresh", "measured_at": fresh,
                              "count": 0, "bytes": 0])
check(olderHub.macosTrashText == "not reported", "an unreported macOS Trash reads not reported")
check(olderHub.macosTrashBytes == nil && olderHub.canEmpty, "an unreported part does not hide Empty")

// 9. A stated 0 is a reading: "empty"; both parts stated empty leave nothing to empty.
let macosZero = decodeMachine("""
{"location_id":"ctl1","machine":"node-4","macos_trash":{"count":0,"bytes":0}}
""")
let zeroRow = makeRow(macosZero, ["supported": true, "status": "fresh", "measured_at": fresh,
                                  "count": 0, "bytes": 0])
check(zeroRow.macosTrashText == "empty", "a stated 0 in macOS Trash reads empty")
check(!zeroRow.canEmpty, "both parts stated empty: no Empty offered")
check(!failed.canEmpty && !away.canEmpty, "a Mac that could not be read cannot be emptied")

// 10. A boolean is not a number here either.
let macosBool = decodeMachine("""
{"location_id":"ctl1","machine":"node-5","macos_trash":{"count":true,"bytes":true}}
""")
check(makeRow(macosBool, nil).macosTrashText == "not reported", "a boolean macOS Trash size is not a reading")

// 10b. A Hub that states a reason instead of a size says so, never 0 and never "not reported".
let needsAccess = decodeMachine("""
{"location_id":"ctl1","machine":"node-6",
 "macos_trash":{"count":null,"bytes":null,"code":"FULL_DISK_ACCESS_NEEDED"}}
""")
check(makeRow(needsAccess, nil).macosTrashText == "needs Full Disk Access",
      "a macOS Trash the Hub cannot read names the permission it needs")
let unreadable = decodeMachine("""
{"location_id":"ctl1","machine":"node-7","macos_trash":{"count":null,"bytes":null,"code":"TRASH_UNREADABLE"}}
""")
check(makeRow(unreadable, nil).macosTrashText == "cannot be read",
      "an unreadable macOS Trash says so rather than claiming it is empty")

// 11. The confirmation: each Mac's line, and a total of stated figures only.
let lines = [full.emptyLine, macosRow.emptyLine, olderHub.emptyLine, makeRow(bare, nil).emptyLine]
check(lines[0].text.contains("Hub folder 3 items") && lines[0].text.contains("macOS Trash not reported"),
      "a Mac's line names each part, and an unstated part as not reported")
let total = FleetTrash.total(lines)
check(total.contains("7 items") && total.contains(FleetTrash.size(6_500_000_000)),
      "the total adds only stated figures (3 + 4 items, 5 GB + 1.5 GB)")
check(total.contains("1 Mac not reported"), "a Mac that stated nothing is named, not counted as 0")

// 12. The outcome names every Mac not emptied, its reason, and what is left.
let said = FleetTrash.outcome([
    "totals": ["emptied": 1, "skipped": 1, "failed": 1, "freed_bytes": 2_000_000_000, "left_bytes": 300_000_000],
    "machines": [
        ["machine": "node-1", "label": "terranash-1500", "state": "emptied", "freed_bytes": 2_000_000_000],
        ["machine": "node-2", "label": "terranash-1200", "state": "skipped", "reason": "its switch is off"],
        ["machine": "node-3", "state": "failed", "reason": "permission denied", "left_bytes": 300_000_000],
        ["location_id": "ctl9", "state": "skipped", "reason": "offline"],
    ]])
check(said.hasPrefix("emptied 1 · skipped 1 · failed 1"), "the outcome leads with the counts")
check(said.contains("terranash-1200 skipped: its switch is off"), "a skipped Mac says why")
check(said.contains("node-3 failed: permission denied (\(FleetTrash.size(300_000_000)) left)"),
      "a failed Mac says why and what is left")
check(said.contains("site ctl9 skipped: offline"), "an offline site is named as skipped")
check(!said.contains("terranash-1500"), "an emptied Mac is in the totals, not the exceptions")

if failures > 0 {
    print("FAIL: \(failures) check(s) failed")
    exit(1)
}
print("PASS: \(checks) checks - four columns, not reported, never 0, empty confirmation and outcome")
