# mrrc-decode — the hub side of the FT8 cloud deep path

Design: `docs/superpowers/specs/2026-10-14-ft8-cloud-escalation-design.md`
(scope **A1**: one box, one hub).  Implementation plan:
`docs/superpowers/plans/2026-10-14-ft8-cloud-escalation.md` (task 6).

The box's cascade escalates to the deep path when its policy says so.  With
`MRRC_FT8_CLOUD_DECODE_URL` set, that escalation is executed **here** instead of
on the box: same `ft8/core` worker, same library, same `decode_ok` — a decode
that happens to run on a faster core, where `cycles=3` costs 1.2 s instead of
the ~6.5 s the A53 cannot afford inside the 4.0 s decision cutoff.

Measured before it was built (hub, EPYC-Milan 1 vCPU, 2026-10-14):
`cycles=1` **0.476 s**, `cycles=2` 0.904 s, `cycles=3` **1.208 s** — all 5/5
known decodes; the box's own single core does `cycles=1` in 3.131 s.

## Install (on the hub)

```bash
# 1. the tree: the same ft8/ subtree the box runs
sudo mkdir -p /opt/mrrc-decode && sudo chown "$USER": /opt/mrrc-decode
rsync -a --delete ./ft8/ /opt/mrrc-decode/ft8/            # from the repo
rsync -a ./tools/hub_decode/ /opt/mrrc-decode/tools/hub_decode/
# the native library (built on the hub; see packaging/common/packages.txt for
# the toolchain — gfortran, g++, cmake, make, pkg-config, libfftw3-dev)
(cd /opt/mrrc-decode && FT8_BUILD_JOBS=1 bash ft8/dsp/build.sh)

# 2. the token.  The service user must own the *directory*: a 0700
#    root-owned /etc/mrrc-decode means the unit cannot even open the file it
#    is configured to read (measured on the first deploy: PermissionError, then
#    a systemd restart loop).
sudo useradd --system --no-create-home --shell /usr/sbin/nologin mrrcdecode || true
sudo install -d -m 700 -o mrrcdecode -g mrrcdecode /etc/mrrc-decode
head -c 32 /dev/urandom | base64 | sudo tee /etc/mrrc-decode/token >/dev/null
sudo chmod 600 /etc/mrrc-decode/token
sudo chown mrrcdecode:mrrcdecode /etc/mrrc-decode/token

# 3. the service
sudo install -m 644 /opt/mrrc-decode/tools/hub_decode/mrrc-decode.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mrrc-decode

# 4. nginx: insert the location from tools/hub_decode/nginx-mrrc-decode.conf
#    into the **live** file — on this hub that is /etc/nginx/sites-enabled/vlsc.net,
#    not sites-available (they are different inodes; only enabled/* is included).
#    Keep the backup OUTSIDE sites-enabled/: nginx loads every file there, so a
#    .bak sibling is parsed as a second config and the next `nginx -t` dies with
#    "duplicate listen options for 0.0.0.0:443" (measured on the first deploy).
sudo cp /etc/nginx/sites-enabled/vlsc.net /etc/nginx/backups/vlsc.net.bak-$(date +%Y%m%d-%H%M%S)
sudo nginx -t && sudo systemctl reload nginx
```

## Verify

```bash
TOKEN=$(sudo cat /etc/mrrc-decode/token)
# no token -> 401 (and the body is never read)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://www.vlsc.net/mrrc-decode
# a wrong length -> 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $TOKEN" -H "X-MRRC-Config: e30=" \
  --data-binary @/dev/null https://www.vlsc.net/mrrc-decode
```

The real end-to-end check is the box's own decode — see the plan's task 9
(enable `MRRC_FT8_CLOUD_DECODE_URL` on the box, watch `cloud_ok` in
`GET /api/ft8/engine/stats`).

## The failure drills that matter

| drill | expected |
| --- | --- |
| `systemctl stop mrrc-decode` | the box logs `cloud_fallback`, escalations run locally, decode counts unchanged |
| hub unreachable (network) | identical to the above — the channel is additive by construction |
| two boxes at once | the second gets `503` immediately and falls back; nothing queues |
| decode while nginx serves | entry latency stays flat (the unit is `Nice=10 CPUQuota=70%`) |

## Notes

- **Never log or persist the PCM.**  It is written into a shared segment and
  dropped; there is no file path for it anywhere in the service.
- **The request deadline is ignored on purpose**: `deadline_monotonic` comes
  from the box's clock.  The hub bounds each request with `--timeout`.
- **One decode at a time** by design (one core, and the entry path shares it).
  `max_threads` caps whatever the caller asks for; the box already sends 1.

## Optional: the ML front-end (`--enhance`)

Decodes each slot **twice** — the untouched PCM and a mask-enhanced copy — in
parallel, and unions the answers, so a message the mask damages costs nothing.
Measured on real slots at `alpha=0.7`: **+0.5-0.9%** net, with 2.4% lost inside
the enhanced copy alone.  (See `docs/superpowers/specs/2026-10-14-ft8-ml-enhancement-design.md`.)

```sh
python3 -m venv /opt/mrrc-decode/.venv
/opt/mrrc-decode/.venv/bin/pip install numpy torch \
    --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
# a model, trained from the slot corpus (see tools/ft8/ml_enhance.py train):
install -m 644 mask-v2.pt /opt/mrrc-decode/models/mask-v2.pt
```

Add to the unit (the CLI raises the worker pool to `2 x portfolio` on its own,
with a log line, because the two decodes must not serialise):

```ini
ExecStart=/opt/mrrc-decode/.venv/bin/python /opt/mrrc-decode/tools/hub_decode/service.py \
    --portfolio p1c3lt-s3 --portfolio-workers 2 \
    --enhance /opt/mrrc-decode/models/mask-v2.pt:0.7 \
```

Without torch, without the model, or with a model that raises, the service
decodes once and warns once — the exact behaviour it had before this flag
existed.  Drop `--enhance` to go back.

