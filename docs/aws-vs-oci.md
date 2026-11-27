# AWS vs OCI: Comprehensive Comparison, Pricing & Egress Analysis

## 1. Executive Comparison

| Dimension | Oracle Cloud Infrastructure (OCI) | Amazon Web Services (EC2) |
|---|---|---|
| **Selected Region** | Hyderabad, India (`ap-hyderabad-1`) | Sydney, Australia (`ap-southeast-2`)* |
| **Ping / Latency to Campus** | **~93 ms** (Low latency, geographic proximity) | **~180–220 ms** (Undersea cable distance) |
| **Free Tier Egress Allowance** | **10,000 GB (10 TB) / month FREE** | **100 GB / month FREE** |
| **Egress Cost Above Free Tier** | ~$0.0085 / GB | **~$0.09 / GB** |
| **Default Micro Shape** | `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM) | `t4g.small` (2 vCPU, 2 GB RAM) |
| **Network Bandwidth** | 50 Mbps (~6.25 MB/s on Micro shape) | **Up to 5 Gbps burst** (`t4g.small`) |
| **Ampere ARM Free Tier** | Up to 4 OCPUs, 24 GB RAM, 1 Gbps (Always Free) | Billed against credits (~$15/mo for `t4g.small`) |
| **Monthly Cost for 500 GB Egress** | **$0.00 / month** | **~$51 / month** (~$15 compute + ~$36 egress) |

*\*Note: In the New AWS Experience (social login at settings.aws.com), accounts are assigned a single selected Region by AWS and restricted via root Service Control Policies (SCPs).*

---

## 2. The 150 Mbps Wi-Fi Bottleneck Reality

Your physical Wi-Fi connection on campus peaks at **150 Mbps (~18.75 MB/s)**:

1. **On OCI Micro VM (`E2.1.Micro`)**:
   - Oracle caps the Micro instance network interface at **50 Mbps (~6.25 MB/s)**.
   - Because 50 Mbps < 150 Mbps, the OCI Micro VM is the bottleneck. A 10 GB file takes ~25 minutes to download.
2. **On OCI Ampere A1 (`VM.Standard.A1.Flex`)**:
   - Bandwidth is **1,000 Mbps (1 Gbps)** per OCPU.
   - It will completely saturate your 150 Mbps Wi-Fi link and cost **$0.00 forever** with 10 TB free egress.
3. **On AWS EC2 (`t4g.small`)**:
   - Bandwidth is **5,000 Mbps (5 Gbps)**.
   - Swarm downloads complete on the cloud disk in minutes.
   - When syncing to your laptop over SSH, it runs at **100% of your Wi-Fi capability (~18.75 MB/s)**.
   - A 10 GB file transfers to `/mnt/DATA` in **~9 minutes**.

---

## 3. How Your $180 AWS Credits Are Consumed

For an average month of downloading **500 GB**:

```
Compute (t4g.small running 24/7):  $0.021/hr x 730 hrs  =  $15.33 / month
Storage (80 GB gp3 SSD):           80 GB x $0.08/GB-mo  =   $6.40 / month
Data Transfer Out (Egress):
  First 100 GB                     FREE                 =   $0.00
  Next 400 GB                      400 GB x $0.09/GB    =  $36.00 / month
-------------------------------------------------------------------------
TOTAL MONTHLY SPEND:                                        $57.73 / month
```

- With **$180 in AWS credits**, this setup runs continuously for **~3.2 to 3.5 months** at zero out-of-pocket cost.
- **With 30-Minute Auto-Shutdown Active**:
  - If you torrent 20 hours a month: Compute drops to `20 x $0.021` = **$0.42**.
  - Monthly spend drops to **$42.82/month** (mostly egress).
  - Your $180 in credits will last for **~4.2 months**.

---

## 4. The Long-Term Roadmap

1. **Months 1–3**: Use the **AWS `t4g.small`** VPS to burn your $180 credits at 5 Gbps speed.
2. **Month 4 onwards**: Switch back to **Oracle Cloud (OCI)** using the **Ampere A1 Flex** shape (1 Gbps network, 10 TB free egress, $0/month forever in Hyderabad).
