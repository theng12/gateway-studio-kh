// FleetConnection offscreen: the Machines tab's Network cell and the fleet-wide link line.
//
// Driven from the real `FleetMachine` / `FleetSummary` decoders, so what is asserted is what the
// console builds from the Gateway's JSON — not a re-implementation of it.
//
// The defects this pins, in the order they cost a morning: a thin Ethernet link drawn as healthy,
// Wi-Fi drawn as a slow cable, an unreadable Mac drawn as fine, and a summary figure that disagrees
// with the rows beside it.

import AppKit

// The app's entry point (CLI flags, NSApplication.run) is stripped by `menubar/render/patch_sources.py`;
// these two are the globals the rest of the app still refers to. Same two lines the render harness uses.
let arguments = CommandLine.arguments
let launchWindow: String? = nil

func fail(_ message: String) -> Never {
    print("FAIL: \(message)")
    exit(1)
}

func check(_ condition: Bool, _ message: String) {
    if !condition { fail(message) }
}

let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase

func machine(_ json: String) -> FleetMachine {
    do {
        return try decoder.decode(FleetMachine.self, from: Data(json.utf8))
    } catch {
        fail("could not decode the machine row: \(error)")
    }
}

// MARK: A slow Ethernet link is called out

let slow = machine("""
{"machine":"1301","label":"1301","reachable":true,
 "connection":{"status":"measured","interface":"en0","kind":"ethernet","speed_mbps":100,
               "duplex":"full","media":"autoselect (100baseTX <full-duplex>)","link_up":true,
               "wifi_up":false,"wifi_rate_mbps":null,"slow":true,"text":"Ethernet 100 Mb/s full"}}
""")
check(slow.connection != nil, "the connection block did not decode")
check(slow.connection?.measured == true, "a measured link read as unmeasured")
check(slow.connection?.display == "Ethernet 100 Mb/s full", "the cell text is \(slow.connection?.display ?? "nil")")
check(slow.connection?.health == .warn, "a 100 Mb/s link was not amber")
check(slow.connection?.tooltip.contains("check the cable") == true, "the tooltip does not name the fix")

// A gigabit link is green and is not called slow.
let fast = machine("""
{"machine":"1207","label":"1207","reachable":true,
 "connection":{"status":"measured","interface":"en0","kind":"ethernet","speed_mbps":1000,
               "duplex":"full","media":"autoselect (1000baseT <full-duplex>)","link_up":true,
               "wifi_up":false,"wifi_rate_mbps":null,"slow":false,"text":"Ethernet 1000 Mb/s full"}}
""")
check(fast.connection?.health == .ok, "a gigabit link was not green")
check(fast.connection?.tooltip.contains("check the cable") == false, "a gigabit link warned about a cable")

// MARK: Wi-Fi is named as Wi-Fi and never as a slow cable

let wifi = machine("""
{"machine":"1304","label":"1304","reachable":true,
 "connection":{"status":"measured","interface":"en1","kind":"wifi","speed_mbps":null,
               "duplex":null,"media":"autoselect","link_up":true,
               "wifi_up":true,"wifi_rate_mbps":432,"slow":false,"text":"Wi-Fi 432 Mb/s"}}
""")
check(wifi.connection?.display == "Wi-Fi 432 Mb/s", "the Wi-Fi cell text is \(wifi.connection?.display ?? "nil")")
check(wifi.connection?.health == .off, "Wi-Fi drew as a fault")
check(wifi.connection?.tooltip.contains("check the cable") == false, "Wi-Fi was told to check a cable")

// MARK: Unknown is not fine

let olderHub = machine("""
{"machine":"1108","label":"1108","reachable":true}
""")
check(olderHub.connection == nil, "a Hub with no block produced a connection")
// What the column draws for it, exactly the way the cell does.
check((olderHub.connection?.display ?? "—") == "—", "an older Hub drew something other than an em dash")

let unreadable = machine("""
{"machine":"1301","label":"1301","reachable":true,
 "connection":{"status":"unmeasured","reason":"This Mac's routing table could not be read.",
               "interface":null,"kind":null,"speed_mbps":null,"duplex":null,"media":null,
               "link_up":null,"wifi_up":null,"wifi_rate_mbps":null,"slow":false,"text":"—"}}
""")
check(unreadable.connection?.measured == false, "an unmeasured link read as measured")
check(unreadable.connection?.display == "—", "an unreadable link drew \(unreadable.connection?.display ?? "nil")")
check(unreadable.connection?.health == .off, "an unreadable link drew as a colour")
check(unreadable.connection?.tooltip.contains("routing table") == true, "the reason was lost")

// MARK: The fleet-wide line, and the figure agreeing with it

let summary = try! decoder.decode(FleetSummary.self, from: Data("""
{"machines":3,"connections":{"slow_ethernet":2,"slow_machines":["1301","1304"],"wifi":0,
                             "ethernet":3,"unmeasured":0,"total":3}}
""".utf8))
check(summary.connections?.slowLine == "2 Macs on slow Ethernet — check the cable: 1301, 1304.",
      "the summary line is \(summary.connections?.slowLine ?? "nil")")

let quiet = try! decoder.decode(FleetSummary.self, from: Data("""
{"machines":1,"connections":{"slow_ethernet":0,"slow_machines":[],"wifi":1,"ethernet":0,
                             "unmeasured":0,"total":1}}
""".utf8))
check(quiet.connections?.slowLine == nil, "a fleet with no slow link drew a line anyway")

let partial = try! decoder.decode(FleetSummary.self, from: Data("""
{"machines":2}
""".utf8))
check(partial.connections == nil, "a Gateway with no summary built a link line")

print("PASS: fleet connection cells, the slow-link note and the fleet-wide line")
