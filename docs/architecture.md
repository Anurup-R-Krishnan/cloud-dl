# Deep Technical Architecture & Packet Walkthrough Guide

## 1. Executive Summary

On restricted campus and corporate networks, outbound internet access is tightly regulated using **Stateful Packet Inspection (SPI)** and **Layer 4 Port Whitelisting**:
- Outbound web ports (`80`, `443`, `8080`, `8888`) and developer administration ports (`22` - SSH) are permitted.
- All non-whitelisted TCP ports (especially peer-to-peer ranges `1024–65535`, `6881`, `51413`) are blocked at the perimeter firewall with immediate `TCP RST` packets (`[Errno 111] Connection refused`).
- All outbound UDP traffic is dropped entirely (killing BitTorrent DHT on UDP port 6881 and UDP trackers).

By leveraging a remote Cloud VPS (Oracle Cloud or AWS EC2) listening on TCP Port 22, we construct an encrypted tunnel that encapsulates all blocked high-port peer connections inside benign, whitelisted SSH streams.

---

## 2. Packet Journey: Step-by-Step Layer Walkthrough

### Scenario A: Direct Connection Attempt (Blocked by Campus)

```
[BitTorrent App] -> [Linux Kernel] -> [Campus Gateway (192.0.2.1)] -> X (DROPPED / RST)
```

1. **Application Socket Call**: The torrent client invokes:
   ```c
   socket(AF_INET, SOCK_STREAM, 0);
   connect(sockfd, {sin_family=AF_INET, sin_port=htons(51413), sin_addr="198.51.100.10"}, sizeof(...));
   ```
2. **Packet Formation**: The local kernel constructs an IP packet:
   - Layer 3 (IP): `Src: 192.0.2.142`, `Dst: 198.51.100.10`
   - Layer 4 (TCP): `Src Port: 49152`, `Dst Port: 51413`, `Flags: [SYN]`
3. **Firewall Interception**:
   - The packet hits the campus gateway router (`192.0.2.1`).
   - The firewall checks its outbound Access Control List (ACL).
   - Port `51413` is not on the whitelist.
   - The firewall's state engine immediately injects a spoofed TCP packet with the `[RST, ACK]` flag back to the laptop:
     ```
     [Campus Firewall] -> [Laptop]: TCP RST [Src: 198.51.100.10:51413, Dst: 192.0.2.142:49152]
     ```
   - In ~32 milliseconds, the kernel aborts the socket and reports `Connection refused`.

---

### Scenario B: Dynamic SOCKS5 Tunneling (`ssh -D 1080`)

```
[Torrent Client] -> (lo:1080) -> [OpenSSH] ==[Encrypted TCP:22]==> [Campus Gateway] -> [Internet] -> [VPS sshd] -> [Peer Swarm]
```

#### Step 1: SOCKS5 Protocol Handshake (RFC 1928) on Loopback
The application connects to `127.0.0.1:1080` on the loopback (`lo`) interface:
1. **Method Selection**:
   - Client sends: `0x05 0x01 0x00` (`VER=5`, `NMETHODS=1`, `METHOD=0x00` No Authentication).
   - Local SSH daemon responds: `0x05 0x00` (`VER=5`, `METHOD=0x00` Accepted).
2. **Connection Request**:
   - Client sends: `0x05 0x01 0x00 0x01 [185.120.45.10] [0xC8 0xD5]`
     - `0x05`: SOCKS version 5
     - `0x01`: Command `CONNECT`
     - `0x00`: Reserved
     - `0x01`: Address Type (IPv4)
     - `[185.120.45.10]`: Destination IPv4 (4 bytes)
     - `[0xC8 0xD5]`: Destination Port `51413` (big-endian)

#### Step 2: SSH Channel Multiplexing & Cryptographic Encapsulation
1. The local `ssh` client intercepts the SOCKS5 request on `127.0.0.1:1080`.
2. It packages the request into an SSH binary message:
   ```text
   SSH_MSG_CHANNEL_OPEN
     string "direct-tcpip"
     uint32 sender_channel (e.g., 0)
     uint32 initial_window_size (e.g., 2097152)
     uint32 maximum_packet_size (e.g., 32768)
     string host_to_connect ("185.120.45.10")
     uint32 port_to_connect (51413)
     string originator_IP_address ("127.0.0.1")
     uint32 originator_port (48110)
   ```
3. OpenSSH encrypts this frame using authenticated symmetric encryption:
   - Cipher: **ChaCha20-Poly1305** or **AES-256-GCM** (negotiated during initial ECDH key exchange).
   - Message Authentication Code (MAC) / Poly1305 tag ensures cryptographic integrity.
4. The encrypted block is wrapped into a standard TCP segment:
   - Layer 3 (IP): `Src: 192.0.2.142`, `Dst: 203.0.113.50` (AWS VPS IP)
   - Layer 4 (TCP): `Src Port: 52410`, `Dst Port: 22` (Standard SSH)

#### Step 3: Firewall Evasion
- The campus firewall evaluates the outer headers:
  - Protocol: **TCP**
  - Destination Port: **22**
- Because port 22 is permitted outbound, the packet passes the firewall with zero resistance.
- Deep Packet Inspection (DPI) sees valid SSH transport framing (`SSH-2.0-...`) and random, high-entropy ciphertext payload. It cannot decrypt or discover what internal IP or port is being targeted.

#### Step 4: Remote Decapsulation & Real-World Egress
1. The VPS `sshd` process receives the TCP frame on port 22, decrypts the payload, and reads the `SSH_MSG_CHANNEL_OPEN (direct-tcpip)` instruction.
2. The VPS Linux kernel initiates a native socket connection:
   ```c
   socket(AF_INET, SOCK_STREAM, 0);
   connect(sockfd, "185.120.45.10:51413");
   ```
3. Outbound traffic from the cloud instance is unrestricted:
   - AWS / OCI security groups allow all outbound traffic (`0.0.0.0/0`).
   - The remote peer receives a standard TCP `SYN` coming from the VPS's public IP address.
4. When the peer replies with data chunks, the VPS `sshd` reads them, encrypts them into the reverse SSH channel, and transmits them back over port 22 to your laptop's loopback interface.

---

## 3. Remote Downloader Mode vs SOCKS5 Tunneling

| Feature | SOCKS5 Proxy Mode (`ssh -D 1080`) | Remote VPS Execution Mode (`aria2c` on VPS) |
|---|---|---|
| **Where Torrent Engine Runs** | Locally on laptop | Remotely on cloud VPS |
| **Peer Wire Sockets** | Encapsulated 1-by-1 through SSH tunnel | Native Linux kernel sockets directly to peers |
| **UDP & DHT Support** | Blocked (SOCKS5 cannot proxy raw UDP DHT) | **Full Native Support** (DHT active, UDP trackers work) |
| **Swarm Concurrency** | Bottlenecked by laptop Wi-Fi latency | **Datacenter Gigabit Pipe** (hundreds of peers connected) |
| **Disk Write Destination** | Direct to local disk | Downloaded to cloud EBS/SSD first, then synced via `rsync` |
| **Failure Tolerance** | Wi-Fi drop interrupts peer connections | Server downloads continuously even if laptop sleeps |

---

## 4. EBS Block Storage vs S3 vs EC2 Compute

### Why Can't You Sync from EBS Without EC2 Running?
- **EBS is a raw virtual hard drive**: It has no network controller, IP address, or operating system.
- An EBS volume is like an internal SSD taken out of a computer: it cannot transfer files over the network on its own.
- The **EC2 instance is the computer** that powers that drive, reads the ext4 filesystem, and runs the `rsync` / `sshd` server so your laptop can pull the files over port 22.
- Running the EC2 instance for ~50 minutes to sync 56 GB at 150 Mbps costs **~$0.017 (less than 2 cents)**.
- Once synced, the instance stops and compute drops back to **$0.00/hour**.
