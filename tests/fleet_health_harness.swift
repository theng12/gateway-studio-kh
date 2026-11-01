// FleetHealth offscreen: the Machines tab's Health, Last 7 days and Likely cause columns, and the
// Show filter — driven from the app's own `FleetMachine` / `FleetLocation` decoders.
//
// What this pins, from the owner's own words (2026-10-29: *"i want to be able to sort for the offline
// site. and also 'problematic' machine … and also a way for me to see which ones could be the likely
// cause"*):
//
//   * a row's history and causes decode from the Gateway's JSON with the field names it really uses;
//   * "Problematic" is the Gateway's verdict, drawn as sent — the app never re-decides it;
//   * a Mac nobody has measured draws "—", never "Stable";
//   * the cause is plain words and its evidence is in the tooltip;
//   * the Show filter really filters, and leaves the unknown out of "problematic" as well as "offline";
//   * the seven-day strip has a bar per day and a real width to layout against.

import AppKit

let arguments = CommandLine.arguments
let launchWindow: String? = nil

func fail(_ message: String) -> Never {
    print("FAIL: \(message)")
    exit(1)
}

func check(_ condition: Bool, _ message: String) {
    if !condition { fail(message) }
}

func descendants(_ view: NSView) -> [NSView] {
    view.subviews.flatMap { [$0] + descendants($0) }
}

let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase

func snapshot(_ json: String) -> FleetSnapshot {
    guard let data = json.data(using: .utf8),
          let value = try? decoder.decode(FleetSnapshot.self, from: data) else {
        fail("could not decode the snapshot")
    }
    return value
}

// MARK: The Gateway's own JSON decodes

let fixture = snapshot("""
{"generated_at": 1760000000,
 "summary": {"machines": 3},
 "locations": [
   {"id": "L1", "label": "Blue Room", "reachable": false, "error": "No answer within 6s.",
    "history": {"observed": true, "online": false, "observed_since": 1759000000, "observed_at": 1759999990,
                "since": 1759990000, "online_since": null, "offline_since": 1759990000,
                "drops_day": 6, "drops_week": 8, "flips_day": 11, "flips_week": 15,
                "offline_seconds_day": 3600, "offline_seconds_week": 7200,
                "days": [{"date": "2026-10-22", "offline_fraction": null},
                         {"date": "2026-10-23", "offline_fraction": 0.5}],
                "pattern": {"code": "nightly_drops", "text": "Drops at about the same time every day",
                            "evidence": "4 of 5 drops fell between 22:00-23:00."},
                "problematic": true,
                "last_drop": {"at": 1759990000, "reason": "timeout", "text": "the connection timed out"}},
    "causes": [{"code": "controller_silent",
                "text": "The site's address answered nothing in time.",
                "evidence": "No answer within 6s."}]},
   {"id": "L2", "label": "Studio 2", "reachable": true,
    "history": {"observed": false, "problematic": null, "drops_day": null},
    "causes": [{"code": "fine", "text": "Nothing wrong reported"}]}
 ],
 "machines": [
   {"machine": "1108", "label": "1108", "location": "Blue Room", "location_id": "L1",
    "state": "offline", "reachable": false,
    "history": {"observed": true, "online": false, "drops_day": 0, "drops_week": 0,
                "offline_since": 1759990000, "offline_seconds_week": 0, "problematic": false,
                "days": [{"date": "2026-10-23", "offline_fraction": 0.0}]},
    "causes": [{"code": "site_down",
                "text": "Its whole site is down — Blue Room is not answering.",
                "evidence": "Blue Room's controller was not answering: No answer within 6s."}]},
   {"machine": "1301", "label": "1301", "location": "Blue Room", "location_id": "L1",
    "state": "ready", "reachable": true,
    "history": {"observed": true, "online": true, "drops_day": 5, "drops_week": 9,
                "observed_at": 1759999990, "offline_seconds_week": 5400, "problematic": true,
                "drop_median_seconds": 120, "drop_longest_seconds": 600, "drop_gap_seconds": 480,
                "drops_completed": 5,
                "problematic_basis": "5 drops in 24 h (threshold 4) · longest 10 min · median 2 min",
                "last_drop": {"at": 1759990000, "reason": "timeout", "text": "the connection timed out",
                              "duration_seconds": 120, "ongoing": false, "recovered_by": "by_itself"},
                "days": [{"date": "2026-10-22", "offline_fraction": 0.2}]},
    "causes": [{"code": "flapping", "text": "It keeps dropping out — 5 drops in the last 24 h.",
                "evidence": "The threshold is 4 drops in 24 h; this Mac has 9 in the last 7 days."},
               {"code": "slow_link", "text": "Its Ethernet link runs at 100 Mb/s.",
                "evidence": "Its own Hub measured Ethernet 100 Mb/s full."}]},
   {"machine": "9999", "label": "9999", "location": "Studio 2", "location_id": "L2",
    "state": "ready", "reachable": true,
    "history": {"observed": false, "drops_day": null, "problematic": null, "days": []},
    "causes": [{"code": "fine", "text": "Nothing wrong reported"}]}
 ]}
""")

check(fixture.locations.count == 2, "expected two locations")
check(fixture.machines.count == 3, "expected three machines")

let site = fixture.locations[0]
check(site.history?.dropsDay == 6, "the site's drops_day did not decode: \(String(describing: site.history?.dropsDay))")
check(site.history?.problematic == true, "the site is not marked problematic")
check(site.history?.days?.count == 2, "the site's strip did not decode")
check(site.history?.days?[0].offlineFraction == nil, "an unobserved day decoded as a fraction")
check(site.history?.pattern?.code == "nightly_drops", "the pattern did not decode")
check(site.causes?.first?.code == "controller_silent", "the site's cause did not decode")

let flapping = fixture.machines.first { $0.machine == "1301" }!
check(flapping.history?.dropsDay == 5, "drops_day did not decode")
check(flapping.history?.offlineSecondsWeek == 5400, "offline_seconds_week did not decode")
check(flapping.history?.problematic == true, "problematic did not decode")
check(flapping.history?.problematicBasis?.contains("threshold 4") == true,
      "problematic_basis did not decode")
check(flapping.history?.dropLongestSeconds == 600, "drop_longest_seconds did not decode")
check(flapping.history?.dropMedianSeconds == 120, "drop_median_seconds did not decode")
check(flapping.history?.dropGapSeconds == 480, "drop_gap_seconds did not decode")
check(flapping.history?.lastDrop?.durationSeconds == 120, "the last drop's length did not decode")
check(flapping.history?.lastDrop?.recoveredBy == "by_itself", "the recovery word did not decode")
check(flapping.causes?.count == 2, "both causes did not decode")
check(flapping.causes?[0].evidence?.contains("threshold is 4 drops") == true,
      "the evidence did not decode")

let unmeasured = fixture.machines.first { $0.machine == "9999" }!
check(unmeasured.history?.measured == false, "an unobserved machine read as measured")

// MARK: The Health column draws the Gateway's verdict, never its own

let flappingVerdict = FleetHealth.verdict(flapping.history)
check(flappingVerdict.text == "Problematic", "a problematic Mac drew \(flappingVerdict.text)")
check(flappingVerdict.health == .warn, "a problematic Mac is not amber")
check(flappingVerdict.tooltip.contains("9 drops in 7 days"),
      "the tooltip does not carry the figures with their window: \(flappingVerdict.tooltip)")
// The verdict shows its basis, not just the word.
check(flappingVerdict.tooltip.contains("threshold 4") && flappingVerdict.tooltip.contains("longest 10 min"),
      "the tooltip does not carry the problematic basis: \(flappingVerdict.tooltip)")

let quiet = fixture.machines.first { $0.machine == "1108" }!
var calm = flapping.history
calm?.problematic = false
calm?.dropsDay = 0
check(FleetHealth.verdict(calm, offline: false).text == "Stable", "a Mac with no drops was not Stable")
check(FleetHealth.verdict(quiet.history, offline: true).text.hasPrefix("Offline "),
      "a Mac that is down right now read as \(FleetHealth.verdict(quiet.history, offline: true).text)")
check(FleetHealth.verdict(quiet.history, offline: true).health == .bad, "an offline Mac is not red")

let unknownVerdict = FleetHealth.verdict(unmeasured.history)
check(unknownVerdict.text == "—", "an unmeasured Mac drew \(unknownVerdict.text), not an em dash")
check(unknownVerdict.text != "Stable", "an unmeasured Mac read as healthy")
check(unknownVerdict.tooltip.contains("no history"), "an unmeasured Mac does not say why")

// The rank sorts worst first, and never sorts the unknown as healthy.
check(FleetHealth.rank(flapping.history) == 0, "problematic is not ranked first")
check(FleetHealth.rank(calm) == 2, "a stable Mac is not ranked last-but-unknown")
check(FleetHealth.rank(unmeasured.history) == 3, "an unmeasured Mac is not ranked last")

// MARK: The figures, and the strip

check(FleetHealth.shortSummary(flapping.history).contains("5 drops in 24 h"),
      "the column caption lost the 24 h figure: \(FleetHealth.shortSummary(flapping.history))")
// **Every number keeps its own window.** The dropped words "in 7 days" once left "5 in 24 h · 42 h
// off" reading as one span (Production, 2026-10-30).
check(FleetHealth.shortSummary(flapping.history).contains("off in 7 days"),
      "the column caption lost the offline window: \(FleetHealth.shortSummary(flapping.history))")
check(!FleetHealth.shortSummary(flapping.history).contains("42 h off ·"),
      "the column caption mixed two windows")
check(FleetHealth.summary(flapping.history).contains("5 drops in 24 h"),
      "the summary lost the 24 h figure: \(FleetHealth.summary(flapping.history))")
check(FleetHealth.summary(flapping.history).contains("2 h offline in 7 days"),
      "the summary lost the offline time: \(FleetHealth.summary(flapping.history))")
check(FleetHealth.summary(unmeasured.history) == "Not measured yet — this Gateway has no history for it.",
      "an unmeasured Mac does not say so")
// How long the drops lasted, and whether they came back by themselves — the difference the owner
// asked for between "dropped once for two minutes" and "drops every eight minutes".
let evidence = FleetHealth.dropEvidence(flapping.history) ?? ""
check(evidence.contains("Drops up to 10 min, median 2 min"), "drop lengths are missing: \(evidence)")
check(evidence.contains("about every 8 min"), "the gap between drops is missing: \(evidence)")
check(evidence.contains("by itself"), "the recovery cause is missing: \(evidence)")
// A Mac with drops but no measured length says so; it is never treated as short.
var unmeasuredLength = flapping.history
unmeasuredLength?.dropLongestSeconds = nil
unmeasuredLength?.dropMedianSeconds = nil
check((FleetHealth.dropEvidence(unmeasuredLength) ?? "").contains("Drop length not measured"),
      "an unmeasured drop length is not named")
check(FleetHealth.stripTooltip(site.history).contains("2026-10-22: not recorded"),
      "an unobserved day is not named in the strip tooltip")
check(FleetHealth.stripTooltip(flapping.history).contains("about every 8 min"),
      "the strip tooltip does not carry how often it drops")
check(FleetHealth.stripTooltip(flapping.history).contains("threshold 4"),
      "the strip tooltip does not carry the problematic basis")
check(FleetHealth.stripTooltip(unmeasured.history).contains("no history"),
      "an unmeasured strip does not say so")

let strip = FleetHistoryStripView()
strip.history = site.history
check(strip.intrinsicContentSize.width > 0, "the strip has no width to lay out against")
let emptyStrip = FleetHistoryStripView()
check(emptyStrip.intrinsicContentSize.width > 0, "an empty strip has no width")

// MARK: The likely cause, in plain words, evidence in the tooltip

check(FleetHealth.causeLine(flapping.causes) == "It keeps dropping out — 5 drops in the last 24 h.",
      "the cause line is \(String(describing: FleetHealth.causeLine(flapping.causes)))")
let tooltip = FleetHealth.causeTooltip(flapping.causes) ?? ""
check(tooltip.contains("Evidence: The threshold is 4 drops in 24 h"),
      "the first hint's evidence is missing from the tooltip")
check(tooltip.contains("100 Mb/s"), "the second hint is missing from the tooltip")
check(FleetHealth.causeHealth(flapping.causes) == .warn, "a real fault is not amber")
check(FleetHealth.causeHealth([FleetCause(code: "no_evidence", text: "No evidence yet", evidence: nil)]) == .off,
      "no evidence drew as a fault")
// **A reason this Gateway named itself draws amber, like the severity it carries.** The Gateway
// classifies every code beside it (`CAUSE_SEVERITIES`) and this app has no list of codes of its
// own: a code it has never heard of is one the Gateway judged, never a healthy one.
let namedByGateway = FleetCause(code: "studio_down",
                                text: "One of its studios is not running, so this Mac cannot take that kind of work.",
                                evidence: "Voice studio not running")
check(FleetHealth.causeHealth([namedByGateway]) == .warn,
      "a studio this Gateway watched go down did not draw amber")
check(FleetHealth.causeLine([namedByGateway])?.contains("not running") == true,
      "the cause line lost the Gateway's sentence")
check(FleetHealth.causeHealth([FleetCause(code: "hub_flagged",
                                          text: "Its Hub reports a problem this Gateway cannot see from outside.",
                                          evidence: "Its Hub says: Flagged by its Hub")]) == .warn,
      "a Hub-only verdict did not draw amber")
check(FleetHealth.causeLine(nil) == nil, "a machine with no causes invented one")

// MARK: Last seen — what the Seen column sorts on

check(FleetHealth.seenText(nil) == "—", "an unremembered observation drew a time")
let now = Date().timeIntervalSince1970
check(FleetHealth.seenText(now - 3 * 3600) == "3 h ago",
      "three hours read as \(FleetHealth.seenText(now - 3 * 3600))")
check(FleetHealth.seenText(now - 3 * 86_400) == "3 days ago",
      "three days read as \(FleetHealth.seenText(now - 3 * 86_400))")
// An offline Mac is last seen when it dropped; an online one when it was last observed.
check(FleetHealth.lastSeen(quiet) == quiet.history?.offlineSince, "an offline Mac's Seen is not its drop")
check(FleetHealth.lastSeen(flapping) == flapping.history?.observedAt, "an online Mac's Seen is not its observation")
check(FleetHealth.lastSeen(unmeasured) == nil, "a Mac nobody measured has a Seen time")

// MARK: The Show filter

check(FleetHealth.matches(.all, machine: unmeasured), "All excludes a machine")
check(FleetHealth.matches(.offline, machine: flapping) == false, "Offline includes an online Mac")
check(FleetHealth.matches(.offline, machine: fixture.machines[0]), "Offline excludes an offline Mac")
check(FleetHealth.matches(.problematic, machine: flapping), "Problematic excludes a problematic Mac")
check(FleetHealth.matches(.problematic, machine: quiet) == false, "Problematic includes a stable Mac")
check(FleetHealth.matches(.problematic, machine: unmeasured) == false,
      "Problematic includes a Mac nobody measured")
check(FleetHealth.matches(.offline, location: site), "Offline excludes an unreachable site")
check(FleetHealth.matches(.problematic, location: site), "Problematic excludes a flapping site")

// MARK: The Machines tab really filters

let tab = FleetMachinesTab(frame: NSRect(x: 0, y: 0, width: 1200, height: 500))
var filter = FleetFilter()
filter.health = .offline
tab.update(machines: fixture.machines, studios: [:], filter: filter)
let offlineRows = descendants(tab).compactMap { $0 as? NSTableView }.map(\.numberOfRows).reduce(0, +)
check(offlineRows == 1, "the Offline filter drew \(offlineRows) rows, expected 1")

filter.health = .problematic
tab.update(machines: fixture.machines, studios: [:], filter: filter)
let problematicRows = descendants(tab).compactMap { $0 as? NSTableView }.map(\.numberOfRows).reduce(0, +)
check(problematicRows == 1, "the Problematic filter drew \(problematicRows) rows, expected 1")

filter.health = .all
tab.update(machines: fixture.machines, studios: [:], filter: filter)
let allRows = descendants(tab).compactMap { $0 as? NSTableView }.map(\.numberOfRows).reduce(0, +)
check(allRows == 3, "the All filter drew \(allRows) rows, expected 3")

// The filter words are the page's, so the popup cannot drift from them.
check(FleetHealth.Filter.allCases.map(\.title) == ["All machines", "Offline now", "Problematic"],
      "the filter words changed")

print("PASS: history decodes, the verdict is the Gateway's, and the filter and cause draw")
