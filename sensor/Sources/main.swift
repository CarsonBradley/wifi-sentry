// wifisense: sample Wi-Fi link-layer signals on macOS and print JSON lines.
// See README.md for field meanings, measured update rates and limits.

import CoreWLAN
import Darwin
import Foundation

// MARK: - Options

struct Options {
    var hz = 10.0
    var keepaliveHz = 0.0         // built-in ICMP keepalive rate; 0 = off (measured: no benefit, see sensor/README.md)
    var gateway: String? = nil    // default: the IPv4 default route's gateway
    var duration: Double? = nil   // seconds; nil = run forever
    var slowEvery = 1.0           // seconds between IOReport (firmware stats) reads
    var probe = false             // --probe: sample briefly, print one capabilities object, exit
    var probeSeconds = 2.0
    var ifaceOnly = false         // --iface: print the Wi-Fi interface name / IPv4 / link state, exit
}

func usage(_ code: Int32 = 2) -> Never {
    let s = """
    usage: wifisense [--hz N] [--keepalive HZ|0] [--gateway IP] [--duration SECONDS] [--slow-every SECONDS]
           wifisense --probe [--probe-seconds S]
           wifisense --iface | --version
      --hz N            lines per second (default 10)
      --keepalive HZ    built-in ICMP echo to the gateway, HZ per second, 0 = off (default 0 = off, max 10)
      --gateway IP      keepalive target (default: IPv4 default-route gateway)
      --duration S      stop after S seconds (default: run forever)
      --slow-every S    how often to read driver firmware stats via IOReport (default 1)
      --probe           read the Wi-Fi link for --probe-seconds (default 2) and print one JSON object
                        describing which signals this Mac provides, then exit
      --iface           print {"iface", "ipv4", "link"} of the Wi-Fi interface and exit
      --version         print the sampler version and exit
    One JSON object per line on stdout. Diagnostics go to stderr. In streaming mode a
    {"type":"caps",...} line is printed first and again whenever the link or the
    available signals change.

    """
    FileHandle.standardError.write(s.data(using: .utf8)!)
    exit(code)
}

func parseOptions() -> Options {
    var o = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    func num(_ name: String) -> Double {
        guard !args.isEmpty, let v = Double(args.removeFirst()), v >= 0 else { usage() }
        return v
    }
    while !args.isEmpty {
        let a = args.removeFirst()
        switch a {
        case "--hz": o.hz = num(a); if o.hz <= 0 || o.hz > 50 { usage() }
        case "--keepalive": o.keepaliveHz = min(num(a), 10)
        case "--gateway": guard !args.isEmpty else { usage() }; o.gateway = args.removeFirst()
        case "--duration": o.duration = num(a)
        case "--slow-every": o.slowEvery = max(num(a), 0.2)
        case "--probe": o.probe = true
        case "--probe-seconds": o.probeSeconds = min(max(num(a), 0.2), 30)
        case "--iface": o.ifaceOnly = true
        case "--version": print(SAMPLER_VERSION); exit(0)
        case "-h", "--help": usage(0)
        default: usage()
        }
    }
    return o
}

func log(_ s: String) { FileHandle.standardError.write("wifisense: \(s)\n".data(using: .utf8)!) }

// MARK: - Helpers

/// IPv4 default gateway via the routing socket (no subprocess).
func defaultGateway() -> String? {
    var mib: [Int32] = [CTL_NET, PF_ROUTE, 0, AF_INET, NET_RT_FLAGS, RTF_GATEWAY]
    var len = 0
    guard sysctl(&mib, 6, nil, &len, nil, 0) == 0, len > 0 else { return nil }
    var buf = [UInt8](repeating: 0, count: len)
    guard sysctl(&mib, 6, &buf, &len, nil, 0) == 0 else { return nil }
    var off = 0
    while off + MemoryLayout<rt_msghdr>.size <= len {
        let result: String? = buf.withUnsafeBytes { raw -> String? in
            let hdr = raw.load(fromByteOffset: off, as: rt_msghdr.self)
            var p = off + MemoryLayout<rt_msghdr>.size
            var dst: in_addr?; var gw: in_addr?
            for bit in 0..<Int(RTAX_MAX) where hdr.rtm_addrs & (1 << bit) != 0 {
                guard p + MemoryLayout<sockaddr>.size <= len else { break }
                let sa = raw.load(fromByteOffset: p, as: sockaddr.self)
                if sa.sa_family == AF_INET, p + MemoryLayout<sockaddr_in>.size <= len {
                    let sin = raw.load(fromByteOffset: p, as: sockaddr_in.self)
                    if bit == RTAX_DST { dst = sin.sin_addr }
                    if bit == RTAX_GATEWAY { gw = sin.sin_addr }
                }
                let l = Int(sa.sa_len)
                p += l > 0 ? (l + 3) & ~3 : 4
            }
            if let d = dst, d.s_addr == 0, let g = gw {
                var g2 = g
                var s = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                inet_ntop(AF_INET, &g2, &s, socklen_t(INET_ADDRSTRLEN))
                return String(cString: s)
            }
            return nil
        }
        if let r = result { return r }
        let msglen = buf.withUnsafeBytes { $0.load(fromByteOffset: off, as: rt_msghdr.self).rtm_msglen }
        if msglen == 0 { break }
        off += Int(msglen)
    }
    return nil
}

/// Host packet counters for the interface (32-bit, wrap at 2^32).
func ifCounters(_ name: String?) -> (rx: UInt32, tx: UInt32)? {
    guard let name = name else { return nil }
    var ifap: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifap) == 0, let first = ifap else { return nil }
    defer { freeifaddrs(ifap) }
    var p: UnsafeMutablePointer<ifaddrs>? = first
    while let cur = p {
        let ifa = cur.pointee
        if let a = ifa.ifa_addr, a.pointee.sa_family == UInt8(AF_LINK),
           String(cString: ifa.ifa_name) == name, let d = ifa.ifa_data {
            let data = d.assumingMemoryBound(to: if_data.self).pointee
            return (data.ifi_ipackets, data.ifi_opackets)
        }
        p = ifa.ifa_next
    }
    return nil
}

/// First IPv4 address of the interface, if any.
func ipv4Address(_ name: String?) -> String? {
    guard let name = name else { return nil }
    var ifap: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifap) == 0, let first = ifap else { return nil }
    defer { freeifaddrs(ifap) }
    var p: UnsafeMutablePointer<ifaddrs>? = first
    while let cur = p {
        let ifa = cur.pointee
        if let a = ifa.ifa_addr, a.pointee.sa_family == UInt8(AF_INET), String(cString: ifa.ifa_name) == name {
            var sin = a.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee }
            var s = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
            inet_ntop(AF_INET, &sin.sin_addr, &s, socklen_t(INET_ADDRSTRLEN))
            return String(cString: s)
        }
        p = ifa.ifa_next
    }
    return nil
}

func sysctlString(_ name: String) -> String? {
    var len = 0
    guard sysctlbyname(name, nil, &len, nil, 0) == 0, len > 0 else { return nil }
    var buf = [CChar](repeating: 0, count: len)
    guard sysctlbyname(name, &buf, &len, nil, 0) == 0 else { return nil }
    return String(cString: buf)
}

func machineArch() -> String {
    var u = utsname()
    uname(&u)
    return withUnsafePointer(to: &u.machine) { $0.withMemoryRebound(to: CChar.self, capacity: 256) { String(cString: $0) } }
}

func osVersion() -> String {
    let v = ProcessInfo.processInfo.operatingSystemVersion
    return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
}

/// JSON string literal (escapes quotes, backslashes and control characters).
func jsonString(_ s: String) -> String {
    var out = "\""
    for u in s.unicodeScalars {
        switch u {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if u.value < 0x20 { out += String(format: "\\u%04x", u.value) } else { out.unicodeScalars.append(u) }
        }
    }
    return out + "\""
}

// MARK: - Wi-Fi state

enum Link: String { case up, disconnected, off, none }

/// Result of one perAntennaRSSI read.
struct PerAntenna {
    let agg: Int?
    let n: Int            // chains the driver reports (0...4)
    let chains: [Int?]    // n values; nil where the slot holds no valid reading
}

final class WiFi {
    private var iface: CWInterface?
    private var cwf: NSObject?                 // private CoreWiFi CWFInterface
    private let corewifiSel = NSSelectorFromString("corewifi")
    private let perAntSel = NSSelectorFromString("perAntennaRSSI")
    private let mcsSel = NSSelectorFromString("MCSIndex")
    private let nssSel = NSSelectorFromString("numberOfSpatialStreams")
    private typealias UIntGetter = @convention(c) (AnyObject, Selector) -> UInt
    private var lastAcquire = 0.0
    private(set) var name: String? = nil
    /// Whether each private selector exists with the expected return type on this macOS.
    private(set) var hasPerAntenna = false
    private(set) var hasMCS = false
    private(set) var hasNSS = false

    init() { acquire(force: true) }

    /// Objective-C type encoding of a method's return value ("@" = object, "Q" = unsigned long long ...).
    private static func returnType(_ obj: NSObject, _ sel: Selector) -> String? {
        guard obj.responds(to: sel), let m = class_getInstanceMethod(type(of: obj), sel) else { return nil }
        let enc = method_copyReturnType(m)
        defer { free(enc) }
        return String(cString: enc)
    }

    private static func isUnsigned(_ enc: String?) -> Bool {
        guard let e = enc?.first else { return false }
        return "QLISC".contains(e)
    }

    func acquire(force: Bool = false) {
        let now = Date().timeIntervalSince1970
        if !force && now - lastAcquire < 5 { return }
        lastAcquire = now
        // CoreWLAN's default Wi-Fi interface (whatever its BSD name: en0 on laptops, en1 on many desktops).
        iface = CWWiFiClient.shared().interface()
        name = iface?.interfaceName
        cwf = nil
        hasPerAntenna = false; hasMCS = false; hasNSS = false
        // Private bridge to CoreWiFi. Only call selectors whose return type is what we expect: a selector
        // that exists but changed type in a macOS update must not be performed blindly.
        if let i = iface as NSObject?, WiFi.returnType(i, corewifiSel)?.hasPrefix("@") == true,
           let f = i.perform(corewifiSel)?.takeUnretainedValue() as? NSObject {
            cwf = f
            hasPerAntenna = WiFi.returnType(f, perAntSel)?.hasPrefix("@") == true
            hasMCS = WiFi.isUnsigned(WiFi.returnType(f, mcsSel))
            hasNSS = WiFi.isUnsigned(WiFi.returnType(f, nssSel))
        }
    }

    var current: CWInterface? {
        if iface == nil { acquire() }
        return iface
    }

    /// Link state of the default Wi-Fi interface (a few XPC calls; call about once per second).
    func link() -> Link {
        guard let i = current else { return .none }
        if !i.powerOn() { return .off }
        if i.serviceActive() || i.rssiValue() != 0 { return .up }
        return .disconnected
    }

    private func uintGetter(_ sel: Selector, _ ok: Bool) -> UInt? {
        guard ok, let f = cwf, let imp = f.method(for: sel) else { return nil }
        return unsafeBitCast(imp, to: UIntGetter.self)(f, sel)
    }

    /// Per-chain RSSI from CWFInterface.perAntennaRSSI (private). Layout observed on
    /// macOS 27.0.1: Int32[13] = [version=1, nChains, ?, chain0..chain3, aggregate, 0...].
    /// The aggregate tracks CWInterface.rssiValue (7180 of 7200 paired reads equal; the
    /// other 20 were 1 dB apart at transitions, the two calls being ~10 ms apart),
    /// so one XPC round trip yields all of them. Returns nil if the call is unavailable
    /// or the layout is not the one above.
    func perAntenna() -> PerAntenna? {
        guard hasPerAntenna, let f = cwf,
              let d = f.perform(perAntSel)?.takeUnretainedValue() as? Data, d.count >= 32 else { return nil }
        let v: [Int32] = d.withUnsafeBytes { raw in
            (0..<(raw.count / 4)).map { raw.loadUnaligned(fromByteOffset: $0 * 4, as: Int32.self) }
        }
        guard v.count >= 8, v[0] == 1, v[1] >= 0 else { return nil }
        let n = min(Int(v[1]), 4)
        let chains: [Int?] = (0..<n).map { k in v[3 + k] < 0 && v[3 + k] > -128 ? Int(v[3 + k]) : nil }
        return PerAntenna(agg: v[7] < 0 && v[7] > -128 ? Int(v[7]) : nil, n: n, chains: chains)
    }

    func mcs() -> Int? { uintGetter(mcsSel, hasMCS).map { Int($0) } }
    func nss() -> Int? { uintGetter(nssSel, hasNSS).flatMap { $0 > 0 && $0 < 16 ? Int($0) : nil } }
}

// MARK: - JSON line building

struct Line {
    private var s = "{"
    private var first = true
    private mutating func key(_ k: String) { s += first ? "\"\(k)\":" : ",\"\(k)\":"; first = false }
    mutating func str(_ k: String, _ v: String?) { key(k); s += v.map { jsonString($0) } ?? "null" }
    mutating func int<T: BinaryInteger>(_ k: String, _ v: T?) { key(k); s += v.map { "\($0)" } ?? "null" }
    mutating func bool(_ k: String, _ v: Bool) { key(k); s += v ? "true" : "false" }
    mutating func dbl(_ k: String, _ v: Double?, _ fmt: String = "%.1f") {
        key(k)
        if let v = v, v.isFinite { s += String(format: fmt, v) } else { s += "null" }
    }
    func finish() -> String { s + "}\n" }
}

// MARK: - Capabilities

/// What this Mac provides right now. A change is announced with a {"type":"caps"} line.
struct Caps: Equatable {
    var iface: String?
    var link: Link
    var perAntenna: String     // "ok" | "missing" (API absent / unexpected layout) | "unknown" (link not up)
    var chains: Int            // chains reported by perAntennaRSSI when "ok", else 0
    var ioreport: Bool         // libIOReport could be loaded
    var mcs: Bool
}

func capsLine(_ c: Caps, _ t: Double, extra: (inout Line) -> Void = { _ in }) -> String {
    var l = Line()
    l.str("type", "caps")
    l.int("v", 1)
    l.dbl("t", t, "%.3f")
    l.str("src", "mac")
    l.str("sampler", SAMPLER_VERSION)
    l.str("os", osVersion())
    l.str("arch", machineArch())
    l.str("model", sysctlString("hw.model"))
    l.str("iface", c.iface)
    l.str("link", c.link.rawValue)
    l.str("per_antenna", c.perAntenna)
    l.int("chains", c.chains)
    l.bool("ioreport", c.ioreport)
    l.bool("mcs", c.mcs)
    extra(&l)
    return l.finish()
}

func writeOut(_ s: String) {
    if fputs(s, stdout) == EOF || fflush(stdout) != 0 {
        if errno == EPIPE { exit(0) }     // consumer went away
        clearerr(stdout)
    }
}

// MARK: - Main

let opt = parseOptions()
signal(SIGPIPE, SIG_IGN)
setvbuf(stdout, nil, _IOFBF, 1 << 14)

let wifi = WiFi()

if opt.ifaceOnly {
    var l = Line()
    l.str("iface", wifi.name)
    l.str("ipv4", ipv4Address(wifi.name))
    l.str("link", wifi.link().rawValue)
    writeOut(l.finish())
    exit(0)
}

let slowWants: [IOReportReader.Want] = [
    .init(key: "cca", group: "Interface $IF", sub: "Average CCA", name: "Interface Average CCA"),
    .init(key: "cca_obss", group: "Interface $IF", sub: "OBSS CCA", name: "Interface OBSS CCA"),
    .init(key: "cca_ibss", group: "Interface $IF", sub: "IBSS CCA", name: "Interface IBSS CCA"),
    .init(key: "cca_intf", group: "Interface $IF", sub: "Interference CCA", name: "Interface Interference CCA"),
    .init(key: "snr", group: "$PEER", sub: "Link quality monitor measurements", name: "SNR"),
    .init(key: "rx_beacons", group: "Chip Frames", sub: "Rx Management Frame Counters", name: "Rx Beacon"),
    .init(key: "tx_frames", group: "Chip", sub: "Tx Counters", name: "Tx Data Frames"),
    .init(key: "tx_retrans", group: "Chip", sub: "Tx Counters", name: "Tx Retransmissions"),
    .init(key: "rx_frames", group: "Chip", sub: "Rx Counters", name: "Rx Data Frames"),
    .init(key: "rx_retry", group: "Chip", sub: "Rx Counters", name: "Rx Retry bit set"),
    .init(key: "rx_bad_fcs", group: "Chip", sub: "Rx Error Counters", name: "Rx Bad FCS"),
    .init(key: "rx_bad_plcp", group: "Chip", sub: "Rx Error Counters", name: "Rx Bad PLCP"),
    .init(key: "rx_good_plcp", group: "Chip", sub: "Rx Error Counters", name: "Rx Good PLCP"),
]
let slowKeys = slowWants.map { $0.key }
let ioreportLoadable = IOReportReader.loadable()
if !ioreportLoadable { log("libIOReport unavailable; firmware stats will be null") }
// The reader is bound to an interface name; (re)created once the Wi-Fi interface is known.
var ioreport: IOReportReader? = nil
func ensureIOReport() {
    guard ioreportLoadable, let n = wifi.name else { return }
    if ioreport?.ifname != n { ioreport = IOReportReader(ifname: n, wants: slowWants) }
}
ensureIOReport()

func currentCaps(_ link: Link, _ pa: PerAntenna?) -> Caps {
    let paState = link != .up ? "unknown" : (pa == nil ? "missing" : (pa!.n > 0 ? "ok" : "missing"))
    return Caps(iface: wifi.name, link: link, perAntenna: paState, chains: paState == "ok" ? pa!.n : 0,
                ioreport: ioreportLoadable, mcs: wifi.hasMCS)
}

// MARK: Probe mode

if opt.probe {
    let n = max(2, Int(opt.probeSeconds * 10))
    var link = wifi.link()
    var okReads = 0, reads = 0, changes = 0, aggReads = 0
    var last: [Int?]? = nil
    var chainsSeen = 0
    var lastPA: PerAntenna? = nil
    var noise = false, mcs = false, txrate = false, rssiPublic = false
    if link == .up {
        for k in 0..<n {
            reads += 1
            if let pa = wifi.perAntenna() {
                lastPA = pa
                let valid = pa.chains.compactMap { $0 }.count
                if valid > 0 { okReads += 1 }
                chainsSeen = max(chainsSeen, valid)
                if let l = last, l != pa.chains { changes += 1 }
                last = pa.chains
                if pa.agg != nil { aggReads += 1 }
            }
            if let i = wifi.current {
                if i.rssiValue() != 0 { rssiPublic = true }
                if i.noiseMeasurement() != 0 { noise = true }
                if i.transmitRate() > 0 { txrate = true }
            }
            if wifi.mcs() != nil { mcs = true }
            if k < n - 1 { usleep(100_000) }
        }
        link = wifi.link()
    }
    var ioChannels = 0
    if let r = ioreport, link == .up { ioChannels = r.sample().count }
    // A layout we parse but with no valid chain value in any read counts as missing.
    var caps = currentCaps(link, okReads > 0 ? lastPA : nil)
    if caps.perAntenna == "ok" { caps.chains = max(caps.chains, chainsSeen) }
    let line = capsLine(caps, Date().timeIntervalSince1970) { l in
        l.str("ipv4", ipv4Address(wifi.name))
        l.bool("per_antenna_api", wifi.hasPerAntenna)
        l.int("reads", reads)
        l.int("chain_reads", okReads)
        l.int("chain_changes", changes)
        l.int("chains_valid", chainsSeen)
        l.bool("rssi", rssiPublic || aggReads > 0)
        l.bool("noise", noise)
        l.bool("txrate", txrate)
        l.bool("mcs_value", mcs)
        l.int("ioreport_channels", ioChannels)
        l.int("ioreport_wanted", slowWants.count)
    }
    writeOut(line)
    exit(0)
}

// MARK: Streaming mode

var pinger: Pinger? = nil
if opt.keepaliveHz > 0 {
    if let gw = opt.gateway ?? defaultGateway(), let p = Pinger(gateway: gw) {
        pinger = p
        log("keepalive: ICMP echo to \(gw) at \(opt.keepaliveHz) Hz")
    } else {
        log("keepalive disabled: no gateway or ICMP socket refused")
    }
}

// Slowly-changing values, refreshed about once per second.
var channel: Int? = nil, chanWidth: Int? = nil, nss: Int? = nil
var link: Link = .none
var lastMeta = 0.0
var slow: [String: Int64] = [:]
var lastSlow = 0.0
// Capabilities announcement: the first one after 1 s of identical readings, later changes after 3 s,
// so a single failed XPC read never flips the engine into another scoring mode.
var announced: Caps? = nil
var pendingCaps: Caps? = nil
var pendingSince = 0.0

func widthMHz(_ w: CWChannelWidth) -> Int? {
    switch w { case .width20MHz: return 20; case .width40MHz: return 40; case .width80MHz: return 80; case .width160MHz: return 160; default: return nil }
}

// Timers run on a dedicated serial queue with absolute deadlines (no drift).
let q = DispatchQueue(label: "wifisense", qos: .utility)
let start = DispatchTime.now()
let period = 1.0 / opt.hz
var tick: UInt64 = 0

func emit() {
    let now = Date().timeIntervalSince1970
    let iface = wifi.current

    if now - lastMeta >= 1 {
        lastMeta = now
        link = wifi.link()
        if link == .none || wifi.name == nil { wifi.acquire() }
        ensureIOReport()
        if link == .up, let ch = iface?.wlanChannel() {
            channel = ch.channelNumber; chanWidth = widthMHz(ch.channelWidth)
        } else {
            channel = nil; chanWidth = nil
            wifi.acquire()          // throttled to once per 5 s
        }
        nss = link == .up ? wifi.nss() : nil
    }
    let up = link == .up
    if let r = ioreport, now - lastSlow >= opt.slowEvery {
        lastSlow = now
        slow = up ? r.sample() : [:]
    }

    var rssi: Int? = nil, noise: Int? = nil, tx: Double? = nil, mcs: Int? = nil
    var chains: [Int?] = []
    var pa: PerAntenna? = nil
    if let i = iface, up {
        pa = wifi.perAntenna()
        if let p = pa { rssi = p.agg; chains = p.chains }
        if rssi == nil { let r = i.rssiValue(); rssi = r != 0 ? r : nil }   // public API fallback
        let n = i.noiseMeasurement(); noise = n != 0 ? n : nil
        let t = i.transmitRate(); tx = t > 0 ? t : nil
        if rssi != nil { mcs = wifi.mcs() }
    }

    let caps = currentCaps(link, pa)
    if caps != pendingCaps { pendingCaps = caps; pendingSince = now }
    if caps != announced && now - pendingSince >= (announced == nil ? 1.0 : 3.0) {
        announced = caps
        writeOut(capsLine(caps, now))
        log("caps: iface=\(caps.iface ?? "none") link=\(caps.link.rawValue) per_antenna=\(caps.perAntenna) chains=\(caps.chains)")
    }

    let rtts = pinger?.drain() ?? []
    let cnt = ifCounters(wifi.name)
    func chain(_ k: Int) -> Int? { k < chains.count ? chains[k] : nil }

    var l = Line()
    l.dbl("t", now, "%.3f")
    l.str("src", "mac")
    l.int("rssi", rssi)
    l.int("noise", noise)
    l.dbl("txrate", tx)
    l.int("channel", channel)
    l.int("rssi_c0", chain(0))
    l.int("rssi_c1", chain(1))
    if chains.count > 2 {               // only Macs with 3-4 receive chains print these
        for k in 2..<chains.count { l.int("rssi_c\(k)", chain(k)) }
    }
    l.int("mcs", mcs)
    l.int("nss", nss)
    l.int("chan_width", chanWidth)
    l.dbl("rtt", rtts.isEmpty ? nil : rtts.reduce(0, +) / Double(rtts.count), "%.2f")
    l.int("rx_pkts", cnt?.rx)
    l.int("tx_pkts", cnt?.tx)
    for k in slowKeys { l.int(k, slow[k]) }
    writeOut(l.finish())
}

func schedule() {
    // Next tick on the absolute grid; if a call stalled, skip missed ticks
    // instead of bursting to catch up.
    let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1e9
    tick = max(tick + 1, UInt64(elapsed / period) + 1)
    let deadline = start + period * Double(tick)
    if let d = opt.duration, period * Double(tick) > d { exit(0) }
    q.asyncAfter(deadline: deadline) {
        emit()
        schedule()
    }
}

var pingTimer: DispatchSourceTimer? = nil
if let p = pinger {
    let pt = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "wifisense.ping", qos: .utility))
    pt.schedule(deadline: .now(), repeating: 1.0 / opt.keepaliveHz, leeway: .milliseconds(5))
    pt.setEventHandler { p.send() }
    pt.resume()
    pingTimer = pt
}

q.async { emit(); schedule() }
dispatchMain()
