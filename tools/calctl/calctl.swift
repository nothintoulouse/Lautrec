// calctl — Apple Calendar CLI over EventKit.
//
// Reads every calendar the Mac account holds (personal + work), with
// recurring events expanded correctly by EventKit. Writes create events on
// a named, writable calendar only — never a read-only/subscription/work cal
// it can't modify. JSON out via `--format json` (default), plain summaries
// otherwise. Same output shape as calctl.py, so the `calendar` skill can
// wrap either one identically.
//
// Build:  swiftc -O -o ~/.local/bin/calctl calctl.swift
// Auth:   first interactive run triggers the macOS Calendar-access prompt;
//         grant once and the binary keeps access (TCC keyed to its path).

import Foundation
import EventKit

// MARK: - Output

var jsonMode = true

func emit(_ obj: Any) {
    if jsonMode {
        if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]),
           let s = String(data: data, encoding: .utf8) {
            print(s)
        }
    }
}

func emitPlain(_ s: String) { if !jsonMode { print(s) } }

func fail(_ message: String) -> Never {
    if jsonMode { emit(["status": "error", "message": message]) }
    else { FileHandle.standardError.write(("error: " + message + "\n").data(using: .utf8)!) }
    exit(1)
}

// MARK: - Dates

// NSDataDetector gives natural-language parsing for free ("fri 7pm",
// "tomorrow 2pm", "next monday"), plus ISO-ish strings.
func parseDate(_ raw: String, endOfDayIfBare: Bool = false) -> Date? {
    let s = raw.trimmingCharacters(in: .whitespaces)
    let lower = s.lowercased()
    let cal = Calendar.current
    let now = Date()

    switch lower {
    case "now": return now
    case "today":
        return endOfDayIfBare ? cal.date(bySettingHour: 23, minute: 59, second: 59, of: now)
                              : cal.startOfDay(for: now)
    case "tomorrow":
        let d = cal.date(byAdding: .day, value: 1, to: now)!
        return endOfDayIfBare ? cal.date(bySettingHour: 23, minute: 59, second: 59, of: d)
                              : cal.startOfDay(for: d)
    case "yesterday":
        let d = cal.date(byAdding: .day, value: -1, to: now)!
        return endOfDayIfBare ? cal.date(bySettingHour: 23, minute: 59, second: 59, of: d)
                              : cal.startOfDay(for: d)
    default: break
    }

    // Bare ISO date → start (or end) of that day.
    let isoDay = DateFormatter()
    isoDay.dateFormat = "yyyy-MM-dd"
    isoDay.calendar = cal
    if let d = isoDay.date(from: s) {
        return endOfDayIfBare ? cal.date(bySettingHour: 23, minute: 59, second: 59, of: d) : d
    }

    // Natural language / date-time.
    if let det = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.date.rawValue) {
        let m = det.firstMatch(in: s, range: NSRange(s.startIndex..., in: s))
        if let date = m?.date { return date }
    }
    return nil
}

let outFmt: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm"
    return f
}()

func iso(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f.string(from: d)
}

// MARK: - Store / access

let store = EKEventStore()

// NEVER block on a permission prompt: under launchd/`claude -p` there is no
// interactive session to answer it, so requestAccess would hang the worker
// until its timeout backstop kills it. Normal commands fail-fast; only the
// explicit `calctl onboard` (interactive) may request access.
func ensureAccess(write: Bool) {
    let status = EKEventStore.authorizationStatus(for: .event)
    switch status {
    case .fullAccess: return
    case .writeOnly where write: return
    case .writeOnly:
        fail("Calendar access is write-only; reads need full access. Run `calctl onboard` from an interactive Terminal on this Mac and grant full access.")
    case .notDetermined:
        fail("Calendar access not yet granted. Run `calctl onboard` from an interactive Terminal on this Mac once, then retry. (Never requested here — a headless prompt would hang.)")
    case .denied, .restricted:
        fail("Calendar access denied. Grant it in System Settings › Privacy & Security › Calendars, then retry.")
    @unknown default:
        fail("Unknown Calendar authorization status.")
    }
}

// Explicit, interactive-only: the one place a prompt may be raised.
func onboardInteractive() {
    let sem = DispatchSemaphore(value: 0)
    var ok = false
    store.requestFullAccessToEvents { granted, _ in ok = granted; sem.signal() }
    // Bounded wait so even this can't hang forever if run in the wrong context.
    if sem.wait(timeout: .now() + 30) == .timedOut {
        fail("timed out waiting for the Calendar prompt — run this from an interactive Terminal, not headless.")
    }
    let s = EKEventStore.authorizationStatus(for: .event)
    emit(["status": ok ? "ok" : "error", "authorization": String(describing: s), "granted": ok])
    emitPlain("calendar authorization: \(s)")
}

func calendarDict(_ c: EKCalendar) -> [String: Any] {
    return [
        "title": c.title,
        "source": c.source?.title ?? "",
        "type": String(describing: c.type),
        "writable": c.allowsContentModifications,
        "color": c.cgColor.flatMap { hexColor($0) } ?? "",
    ]
}

func hexColor(_ cg: CGColor) -> String? {
    guard let comps = cg.components, comps.count >= 3 else { return nil }
    let r = Int((comps[0] * 255).rounded()), g = Int((comps[1] * 255).rounded()), b = Int((comps[2] * 255).rounded())
    return String(format: "#%02X%02X%02X", r, g, b)
}

func eventDict(_ e: EKEvent) -> [String: Any] {
    return [
        "title": e.title ?? "(untitled)",
        "start": iso(e.startDate),
        "end": iso(e.endDate),
        "allDay": e.isAllDay,
        "calendar": e.calendar?.title ?? "",
        "location": e.location ?? "",
        "status": String(describing: e.status),
        "id": e.eventIdentifier ?? "",
    ]
}

func eventsIn(_ start: Date, _ end: Date) -> [EKEvent] {
    let pred = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    return store.events(matching: pred).sorted { $0.startDate < $1.startDate }
}

func printEvents(_ events: [EKEvent], emptyMsg: String) {
    if jsonMode {
        emit(["status": "ok", "count": events.count, "events": events.map(eventDict)])
    } else {
        if events.isEmpty { print(emptyMsg); return }
        for e in events {
            let when = e.isAllDay ? "all-day" : outFmt.string(from: e.startDate)
            print("\(when)  \(e.title ?? "(untitled)")  [\(e.calendar?.title ?? "")]")
        }
    }
}

// MARK: - Arg parsing

var args = Array(CommandLine.arguments.dropFirst())
func takeFlag(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    let v = args[i + 1]
    args.removeSubrange(i...(i + 1))
    return v
}
// --format json|plain (json default)
if let f = takeFlag("--format") { jsonMode = (f.lowercased() == "json") }
if args.contains("--plain") { jsonMode = false; args.removeAll { $0 == "--plain" } }

let cmd = args.first ?? "today"
let rest = Array(args.dropFirst())
let cal = Calendar.current
let now = Date()

switch cmd {

case "auth":
    // Report only — never prompt (safe headless).
    let s = EKEventStore.authorizationStatus(for: .event)
    emit(["status": "ok", "authorization": String(describing: s),
          "granted": (s == .fullAccess || s == .writeOnly)])
    emitPlain("calendar authorization: \(s)")

case "onboard":
    // The one interactive-only command that may raise the macOS prompt.
    onboardInteractive()

case "calendars", "cals":
    ensureAccess(write: false)
    let cals = store.calendars(for: .event).sorted { $0.title < $1.title }
    if jsonMode { emit(["status": "ok", "count": cals.count, "calendars": cals.map(calendarDict)]) }
    else { for c in cals { print("\(c.allowsContentModifications ? "rw" : "ro")  \(c.title)  [\(c.source?.title ?? "")]") } }

case "today":
    ensureAccess(write: false)
    let start = cal.startOfDay(for: now)
    let end = cal.date(byAdding: .day, value: 1, to: start)!
    printEvents(eventsIn(start, end), emptyMsg: "Nothing on your calendar today.")

case "day":
    ensureAccess(write: false)
    guard let ref = parseDate(rest.first ?? "today") else { fail("could not parse date: \(rest.first ?? "")") }
    let start = cal.startOfDay(for: ref)
    let end = cal.date(byAdding: .day, value: 1, to: start)!
    printEvents(eventsIn(start, end), emptyMsg: "Nothing scheduled that day.")

case "week":
    ensureAccess(write: false)
    let start = cal.startOfDay(for: now)
    let end = cal.date(byAdding: .day, value: 7, to: start)!
    printEvents(eventsIn(start, end), emptyMsg: "Nothing on your calendar this week.")

case "next":
    ensureAccess(write: false)
    let end = cal.date(byAdding: .day, value: 60, to: now)!
    let upcoming = eventsIn(now, end).filter { $0.endDate > now }
    if let e = upcoming.first {
        if jsonMode { emit(["status": "ok", "event": eventDict(e)]) }
        else { print("\(outFmt.string(from: e.startDate))  \(e.title ?? "(untitled)")  [\(e.calendar?.title ?? "")]") }
    } else {
        if jsonMode { emit(["status": "ok", "event": NSNull()]) }
        else { print("Nothing coming up in the next 60 days.") }
    }

case "range":
    ensureAccess(write: false)
    guard rest.count >= 2, let s = parseDate(rest[0]), let e = parseDate(rest[1], endOfDayIfBare: true) else {
        fail("usage: calctl range <from> <to>")
    }
    printEvents(eventsIn(s, e), emptyMsg: "Nothing in that range.")

case "free":
    // Report busy blocks in a window; caller infers free gaps.
    ensureAccess(write: false)
    guard rest.count >= 2, let s = parseDate(rest[0]), let e = parseDate(rest[1], endOfDayIfBare: true) else {
        fail("usage: calctl free <from> <to>")
    }
    let busy = eventsIn(s, e).filter { !$0.isAllDay }
    if jsonMode {
        emit(["status": "ok", "window": ["start": iso(s), "end": iso(e)],
              "busy": busy.map { ["start": iso($0.startDate), "end": iso($0.endDate), "title": $0.title ?? ""] }])
    } else {
        if busy.isEmpty { print("Free the whole window (\(outFmt.string(from: s)) – \(outFmt.string(from: e))).") }
        else { print("Busy:"); for b in busy { print("  \(outFmt.string(from: b.startDate)) – \(outFmt.string(from: b.endDate))  \(b.title ?? "")") } }
    }

case "add":
    // calctl add "<title>" --cal <name> --start <when> [--end <when>] [--all-day] [--location <s>] [--notes <s>]
    var restA = rest
    func flag(_ n: String) -> String? {
        guard let i = restA.firstIndex(of: n), i + 1 < restA.count else { return nil }
        let v = restA[i + 1]; restA.removeSubrange(i...(i + 1)); return v
    }
    let calN = flag("--cal")
    let startS = flag("--start")
    let endS = flag("--end")
    let loc = flag("--location")
    let notes = flag("--notes")
    let allDay = restA.contains("--all-day"); restA.removeAll { $0 == "--all-day" }
    let title = restA.first(where: { !$0.hasPrefix("--") })

    ensureAccess(write: true)
    guard let title = title, !title.isEmpty else { fail("usage: calctl add \"<title>\" --cal <name> --start <when> [--end <when>]") }
    guard let calN = calN else { fail("a target calendar is required: --cal <name> (writes go to a named, writable calendar only)") }
    guard let startS = startS, let start = parseDate(startS) else { fail("a valid --start is required") }
    guard let target = store.calendars(for: .event).first(where: { $0.title == calN }) else {
        fail("no calendar named \"\(calN)\". Run `calctl calendars` to list.")
    }
    guard target.allowsContentModifications else { fail("calendar \"\(calN)\" is read-only; pick a writable one.") }

    let ev = EKEvent(eventStore: store)
    ev.title = title
    ev.calendar = target
    ev.isAllDay = allDay
    ev.startDate = start
    ev.endDate = endS.flatMap { parseDate($0, endOfDayIfBare: true) } ?? cal.date(byAdding: .hour, value: 1, to: start)!
    if let loc = loc { ev.location = loc }
    if let notes = notes { ev.notes = notes }
    do {
        try store.save(ev, span: .thisEvent, commit: true)
        if jsonMode { emit(["status": "ok", "created": eventDict(ev)]) }
        else { print("Created: \(title) on \(calN) at \(outFmt.string(from: ev.startDate))") }
    } catch {
        fail("save failed: \(error.localizedDescription)")
    }

default:
    fail("unknown command: \(cmd). Commands: today, day, week, next, range, free, calendars, add, auth")
}
