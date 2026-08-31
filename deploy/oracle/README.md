# Running AutoTube AI on Oracle Cloud Always Free

Goal: stop needing your laptop switched on. The backend runs on a free Oracle
instance, your phone talks to it over HTTPS, and you shut the laptop.

**Read the reality check first.** Oracle changed the free tier in 2026 and it
matters for this workload.

---

## Reality check (verified 2026-08)

| | |
|---|---|
| Ampere A1 allowance | **2 OCPU / 12 GB RAM** — halved from 4/24 on **15 June 2026**, with no announcement. Instances above the new limit were terminated from 18 August 2026. |
| Storage | 200 GB block volume total, across at most 2 volumes |
| Egress | 10 TB/month — irrelevant here, uploads go to YouTube |
| The other free shape | 2x AMD E2.1.Micro, 1/8 OCPU, 1 GB RAM. **Useless for this** — do not bother. |
| Availability | "Out of host capacity" is common. US regions can refuse for days; Mumbai/Hyderabad/Singapore usually work. Pick a region with 3 availability domains if you have the choice. |
| **Idle reclamation** | Oracle evaluates Always Free compute over a rolling **7-day** window and may reclaim instances that stay idle. A box that renders one video a day and sleeps is close to that profile. |

### What 2 Arm cores means for render time

Measured on the dev laptop (4 cores, x86): **~15x realtime** — a 4-minute
long-form video took 64 minutes. On 2 Ampere cores, expect meaningfully worse:

| Video | Laptop (4 cores) | Expect on 2 OCPU |
|---|---|---|
| 45 s Short | ~7 min | ~15-25 min |
| 4 min long-form | ~64 min | **2-3 hours** |

Shorts are comfortable. Long-form is an overnight job. If long-form daily is
your plan, this free tier is not the right home for it, and I would rather tell
you that now than after you have spent an evening on the migration.

### Two things to decide before starting

**Where the credentials live.** This backend holds a YouTube refresh token that
can upload to your channel. On the laptop that token sits on a machine you
physically control. On a VM it does not. The `.env` is `chmod 600` and owned by
a system account, and the systemd unit is hardened, but the honest framing is:
you are moving a publishing credential onto rented infrastructure.

**Idle reclamation.** If you generate daily, the render itself is probably
enough activity. If you generate weekly, read the note at the bottom.

---

## Step 1 — Oracle account

<https://www.oracle.com/cloud/free/>

- A **credit card is required for identity verification**. Always Free
  resources are not charged, but the card check is unavoidable.
- Choose your home region carefully: **it cannot be changed later.** For India,
  Mumbai or Hyderabad. Singapore is a good alternative.
- After the 30-day trial ends your account drops to Always Free automatically.
  Do not upgrade to Pay As You Go unless you intend to pay.

## Step 2 — Create the instance

**Compute → Instances → Create instance**

| Field | Value |
|---|---|
| Image | **Ubuntu 24.04** (Canonical) |
| Shape | **VM.Standard.A1.Flex** — the Ampere/Arm one |
| OCPUs / Memory | **2 / 12 GB** (the current Always Free maximum) |
| Boot volume | 100 GB — videos are 30-150 MB each and the default 47 GB fills fast |
| SSH keys | Generate a key pair and **download the private key** before creating |

If you get **"Out of host capacity"**: try a different availability domain, try
again later, or try another region. It is a genuine capacity limit, not a
configuration error on your side.

Note the instance's **public IP**.

## Step 3 — Open the network, both layers

This is the step people lose an afternoon to. There are **two** firewalls and
opening one does nothing on its own — you get a silent connection timeout with
no log line anywhere.

**Layer 1, the cloud Security List.** Networking → Virtual Cloud Networks →
your VCN → Subnet → Security List → **Add Ingress Rules**:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

Port 80 is not optional — Let's Encrypt's HTTP challenge uses it.

**Layer 2, the instance's own iptables.** Oracle's Ubuntu images ship rules
that reject everything except SSH. `setup.sh` fixes this for you and persists
it, so you do not need to do it by hand.

## Step 4 — A hostname, so you get real HTTPS

You need a DNS name for a certificate. Free option: **DuckDNS**.

1. <https://www.duckdns.org> — sign in, create a subdomain, e.g. `autotube-ck`.
2. Point it at the instance's public IP.
3. Your backend URL becomes `https://autotube-ck.duckdns.org/`.

Why bother instead of using the raw IP: the API token is a bearer credential,
so plain HTTP puts it on the wire in the clear. And the Android **release**
build refuses cleartext by design — only the debug build allows it, and only
for LAN development.

*(Prefer no public exposure at all? See "Tailscale instead" below.)*

## Step 5 — Run the setup script

SSH in, then:

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq git
git clone https://github.com/ck17nov/auto_vid_app.git /tmp/autotube
sudo bash /tmp/autotube/deploy/oracle/setup.sh --domain autotube-ck.duckdns.org
```

It installs Python, ffmpeg, a `autotube` service account, the venv, a hardened
systemd unit and Caddy for automatic HTTPS. It is idempotent — re-run it to
update.

It also runs `scripts/check_ffmpeg.py` and **refuses to continue if ffmpeg is
missing anything**. That check exists because `ffmpeg -version` succeeding
proves very little: a build without libass lists the `subtitles` filter and
then renders every video with no captions, failing at the last step after the
voice and images are already done.

## Step 6 — Add your keys

The script generates a fresh `AUTOTUBE_API_TOKEN` and prints it. Add the rest:

```bash
sudo -u autotube nano /opt/autotube/.env
sudo systemctl restart autotube
sudo -u autotube /opt/autotube/.venv/bin/python -m backend.cli doctor
```

## Step 7 — Point the phone at it

In the app: **Settings → Backend URL** → `https://autotube-ck.duckdns.org/`,
and the `AUTOTUBE_API_TOKEN` from the server (**not** your laptop's — they are
different secrets). Tap **Test connection**.

Your Google Cloud **Android** OAuth client is unchanged; it is tied to the
app's package and signing certificate, not to the backend address.

## Step 8 — Tune for 2 cores

```yaml
video:
  render_parallel: 2       # 0 auto-derives; 2 is right for a 2-core box
  preset: veryfast         # 'fast' costs ~40% more CPU for little visible gain
visuals:
  parallel: 2
automation:
  daily_video_limit: 2     # be honest about what this box can finish in a day
```

---

## Operating it

```bash
journalctl -u autotube -f                     # live logs
sudo systemctl restart autotube               # after editing .env
df -h /                                       # watch the disk
```

**Prune old output.** Each finished job is 30-150 MB and the boot volume will
fill. Published videos live on YouTube, so the local copy is only for review:

```bash
sudo -u autotube /opt/autotube/.venv/bin/python -m backend.cli prune --videos-only --keep-days 7 --yes
```

Weekly, via `sudo crontab -e`:

```
0 4 * * 0 sudo -u autotube /opt/autotube/.venv/bin/python -m backend.cli prune --videos-only --keep-days 7 --yes
```

**Idle reclamation.** If you generate daily, the render is real load and you
are probably fine. If you generate rarely, Oracle may reclaim the instance.
Deliberately burning CPU to look busy is gaming the free tier — I am noting the
mechanism, not recommending it. The cleaner answer for occasional use is to
keep it on the laptop, where the same idleness costs nothing.

---

## Tailscale instead of a public hostname

More secure, and simpler in some ways: the instance is never exposed to the
internet at all.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install Tailscale on the phone, sign in with the same account, and set the
Backend URL to `http://100.x.y.z:8099/` (the tailnet IP). Then run `setup.sh`
with `--no-tls` and change the systemd unit's `--host` to `0.0.0.0`, because
only tailnet peers can reach it.

Trade-offs, stated plainly:

- **Better:** no open ports, no certificate, no DNS, and the API is
  unreachable from the public internet even if the token leaks.
- **Worse:** the traffic is plain HTTP inside the encrypted tailnet, so the
  Android **release** build will refuse it. The debug build is fine. You also
  depend on Tailscale being up on both devices.

---

## Honest summary

| | |
|---|---|
| Cost | ₹0, genuinely — Always Free has no expiry |
| Good for | Shorts, and scheduled publishing without a laptop |
| Poor for | Long-form daily — 2 Arm cores means hours per video |
| Real risks | Capacity refusals when creating, idle reclamation, a publishing credential on rented infrastructure |
| Reversible? | Yes. Both machines can run the backend; the phone just needs a different URL and token. |

Scheduled publishing is worth calling out: an uploaded video goes to YouTube as
`private` with a `publishAt` timestamp, and **YouTube** publishes it at the
appointed time. So even on the laptop you only need it running to *generate and
upload* — not for the video to go live.
