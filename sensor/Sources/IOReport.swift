// Minimal, read-only access to Apple's private libIOReport (no sudo needed).
// Used for the Wi-Fi driver's firmware statistics (CCA, retries, FCS errors,
// beacons, SNR). Everything is resolved with dlsym so a missing symbol on a
// future macOS degrades to "fields are null" instead of a launch failure.

import CoreFoundation
import Foundation

final class IOReportReader {
    private typealias CopyChannelsInGroup = @convention(c) (CFString?, CFString?, UInt64, UInt64, UInt64) -> Unmanaged<CFMutableDictionary>?
    private typealias CopyAllChannels = @convention(c) (UInt64, UInt64) -> Unmanaged<CFMutableDictionary>?
    private typealias CreateSubscription = @convention(c) (UnsafeMutableRawPointer?, CFMutableDictionary, UnsafeMutablePointer<Unmanaged<CFMutableDictionary>?>, UInt64, CFTypeRef?) -> OpaquePointer?
    private typealias CreateSamples = @convention(c) (OpaquePointer, CFMutableDictionary, CFTypeRef?) -> Unmanaged<CFDictionary>?
    private typealias GetString = @convention(c) (CFDictionary) -> Unmanaged<CFString>?
    private typealias GetFormat = @convention(c) (CFDictionary) -> Int32
    private typealias SimpleGet = @convention(c) (CFDictionary, Int32) -> Int64

    private let copyInGroup: CopyChannelsInGroup
    private let copyAll: CopyAllChannels
    private let createSub: CreateSubscription
    private let createSamples: CreateSamples
    private let getGroup: GetString
    private let getSubGroup: GetString
    private let getName: GetString
    private let getFormat: GetFormat
    private let simpleGet: SimpleGet

    /// A wanted channel: output key -> (group, subgroup, channel name).
    /// Group "$IF" / "$PEER" are substituted with the interface / peer group name.
    struct Want { let key: String; let group: String; let sub: String; let name: String }

    private let wants: [Want]
    let ifname: String
    private var sub: OpaquePointer?
    private var subbed: CFMutableDictionary?
    private var lastSetupAttempt = 0.0

    /// True when libIOReport and every symbol this reader needs can be resolved on this macOS.
    static func loadable() -> Bool {
        guard let h = dlopen("/usr/lib/libIOReport.dylib", RTLD_NOW) else { return false }
        let names: [String] = ["IOReportCopyChannelsInGroup", "IOReportCopyAllChannels", "IOReportCreateSubscription",
                "IOReportCreateSamples", "IOReportChannelGetGroup", "IOReportChannelGetSubGroup",
                "IOReportChannelGetChannelName", "IOReportChannelGetFormat", "IOReportSimpleGetIntegerValue"]
        return names.allSatisfy { dlsym(h, $0) != nil }
    }

    init?(ifname: String, wants: [Want]) {
        guard let h = dlopen("/usr/lib/libIOReport.dylib", RTLD_NOW) else { return nil }
        func sym<T>(_ n: String, _ t: T.Type) -> T? { dlsym(h, n).map { unsafeBitCast($0, to: t) } }
        guard let a = sym("IOReportCopyChannelsInGroup", CopyChannelsInGroup.self),
              let b = sym("IOReportCopyAllChannels", CopyAllChannels.self),
              let c = sym("IOReportCreateSubscription", CreateSubscription.self),
              let d = sym("IOReportCreateSamples", CreateSamples.self),
              let e = sym("IOReportChannelGetGroup", GetString.self),
              let f = sym("IOReportChannelGetSubGroup", GetString.self),
              let g = sym("IOReportChannelGetChannelName", GetString.self),
              let i = sym("IOReportChannelGetFormat", GetFormat.self),
              let j = sym("IOReportSimpleGetIntegerValue", SimpleGet.self) else { return nil }
        copyInGroup = a; copyAll = b; createSub = c; createSamples = d
        getGroup = e; getSubGroup = f; getName = g; getFormat = i; simpleGet = j
        self.ifname = ifname
        self.wants = wants
    }

    /// Elements of an IOReport channel array that really are dictionaries. `as? [CFDictionary]` can't
    /// check a CF element type (the cast always "succeeds"), and a non-dictionary would crash later.
    private func dicts(_ v: Any?) -> [CFDictionary] {
        guard let arr = v as? [AnyObject] else { return [] }
        return arr.compactMap { o in
            CFGetTypeID(o) == CFDictionaryGetTypeID() ? unsafeBitCast(o, to: CFDictionary.self) : nil
        }
    }

    /// Release the current subscription (CreateSubscription returns it retained) before making a new one.
    private func dropSubscription() {
        if let s = sub { Unmanaged<AnyObject>.fromOpaque(UnsafeRawPointer(s)).release() }
        sub = nil
        subbed = nil
    }

    deinit { dropSubscription() }

    private func str(_ fn: GetString, _ ch: CFDictionary) -> String {
        fn(ch)?.takeUnretainedValue() as String? ?? ""
    }

    /// The per-peer group name embeds the AP MAC (zeroed when location access is
    /// not granted), so find it by prefix instead of hard-coding it.
    private func findPeerGroup() -> String? {
        guard let all = copyAll(0, 0)?.takeRetainedValue() as NSDictionary? else { return nil }
        let chans = dicts(all["IOReportChannels"])
        let prefix = "Interface \(ifname) Peer "
        for ch in chans {
            let g = str(getGroup, ch)
            if g.hasPrefix(prefix) { return g }
        }
        return nil
    }

    private func setup() {
        let now = Date().timeIntervalSince1970
        if now - lastSetupAttempt < 30 { return }   // retry at most every 30 s
        lastSetupAttempt = now
        let peer = findPeerGroup()
        var groups = Set<String>()
        for w in wants {
            if w.group == "$PEER" { if let p = peer { groups.insert(p) } }
            else { groups.insert(w.group.replacingOccurrences(of: "$IF", with: ifname)) }
        }
        let wanted = Set(wants.compactMap { w -> String? in
            let g = w.group == "$PEER" ? peer : w.group.replacingOccurrences(of: "$IF", with: ifname)
            return g.map { "\($0)|\(w.sub)|\(w.name)" }
        })
        var filtered: [CFDictionary] = []
        var template: NSMutableDictionary?
        for g in groups {
            guard let d = copyInGroup(g as CFString, nil, 0, 0, 0)?.takeRetainedValue() as NSMutableDictionary? else { continue }
            let chans = dicts(d["IOReportChannels"])
            if chans.isEmpty { continue }
            if template == nil { template = d }
            for ch in chans where wanted.contains("\(str(getGroup, ch))|\(str(getSubGroup, ch))|\(str(getName, ch))") {
                filtered.append(ch)
            }
        }
        guard let t = template, !filtered.isEmpty else { return }
        let desired = t.mutableCopy() as! NSMutableDictionary
        desired["IOReportChannels"] = filtered
        var out: Unmanaged<CFMutableDictionary>?
        dropSubscription()
        guard let s = createSub(nil, desired as CFMutableDictionary, &out, 0, nil), let o = out else { return }
        sub = s
        subbed = o.takeRetainedValue()
    }

    /// Returns key -> value for every wanted channel currently readable.
    func sample() -> [String: Int64] {
        if sub == nil { setup() }
        guard let s = sub, let sd = subbed,
              let smp = createSamples(s, sd, nil)?.takeRetainedValue() as NSDictionary? else {
            dropSubscription()
            return [:]
        }
        let chans = dicts(smp["IOReportChannels"])
        var byName: [String: Int64] = [:]
        for ch in chans where getFormat(ch) == 1 {
            var g = str(getGroup, ch)
            if g.hasPrefix("Interface \(ifname) Peer ") { g = "$PEER" }
            else { g = g.replacingOccurrences(of: ifname, with: "$IF") }
            byName["\(g)|\(str(getSubGroup, ch))|\(str(getName, ch))"] = simpleGet(ch, 0)
        }
        var out: [String: Int64] = [:]
        for w in wants {
            if let v = byName["\(w.group)|\(w.sub)|\(w.name)"] { out[w.key] = v }
        }
        if out.isEmpty { dropSubscription() }   // interface went away; resubscribe later
        return out
    }
}
