// Gentle unprivileged ICMP keepalive to the default gateway.
// Uses a SOCK_DGRAM ICMP socket, which macOS allows without root.
// Purpose: keep a little traffic on the link so the driver refreshes its
// per-chain RSSI from data frames, and report the gateway RTT as a side signal.

import Darwin
import Foundation

final class Pinger {
    private var fd: Int32 = -1
    private let target: in_addr
    private let ident = UInt16(truncatingIfNeeded: getpid())
    private var seq: UInt16 = 0
    private let lock = NSLock()
    private var sent: [UInt16: UInt64] = [:]   // seq -> send time (ns, monotonic)
    private var rtts: [Double] = []            // replies since last drain (ms)
    private var nSent = 0

    init?(gateway: String) {
        var a = in_addr()
        guard inet_pton(AF_INET, gateway, &a) == 1 else { return nil }
        target = a
        fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)
        if fd < 0 { return nil }
        var tv = timeval(tv_sec: 1, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        let t = Thread { [weak self] in self?.recvLoop() }
        t.qualityOfService = .utility
        t.start()
    }

    private static func checksum(_ b: [UInt8]) -> UInt16 {
        var sum: UInt32 = 0
        var i = 0
        while i + 1 < b.count { sum += UInt32(b[i]) << 8 | UInt32(b[i + 1]); i += 2 }
        if i < b.count { sum += UInt32(b[i]) << 8 }
        while sum >> 16 != 0 { sum = (sum & 0xffff) + (sum >> 16) }
        return ~UInt16(sum)
    }

    func send() {
        lock.lock()
        seq &+= 1
        let s = seq
        let now = DispatchTime.now().uptimeNanoseconds
        sent[s] = now
        if sent.count > 64 { sent = sent.filter { now - $0.value < 5_000_000_000 } }
        nSent += 1
        lock.unlock()
        var pkt: [UInt8] = [8, 0, 0, 0, UInt8(ident >> 8), UInt8(ident & 0xff), UInt8(s >> 8), UInt8(s & 0xff)]
        pkt += [UInt8](repeating: 0x5a, count: 8)   // 16-byte ICMP message total
        let c = Pinger.checksum(pkt)
        pkt[2] = UInt8(c >> 8); pkt[3] = UInt8(c & 0xff)
        var sa = sockaddr_in()
        sa.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        sa.sin_family = sa_family_t(AF_INET)
        sa.sin_addr = target
        _ = withUnsafePointer(to: &sa) { p in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                sendto(fd, pkt, pkt.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
    }

    private func recvLoop() {
        var buf = [UInt8](repeating: 0, count: 1500)
        while true {
            let n = recv(fd, &buf, buf.count, 0)
            if n <= 0 { continue }
            var off = 0
            if buf[0] >> 4 == 4 { off = Int(buf[0] & 0x0f) * 4 }   // macOS includes the IP header
            guard n >= off + 8, buf[off] == 0 else { continue }      // echo reply only
            let s = UInt16(buf[off + 6]) << 8 | UInt16(buf[off + 7])
            let now = DispatchTime.now().uptimeNanoseconds
            lock.lock()
            if let t0 = sent.removeValue(forKey: s) { rtts.append(Double(now - t0) / 1e6) }
            lock.unlock()
        }
    }

    /// Replies received since the last call (ms) and the running sent count.
    func drain() -> [Double] {
        lock.lock(); defer { lock.unlock() }
        let r = rtts
        rtts.removeAll(keepingCapacity: true)
        return r
    }
}
