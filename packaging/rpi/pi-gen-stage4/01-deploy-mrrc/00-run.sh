#!/bin/bash -e
# Stage: deploy MRRC Modern runtime (chroot). Code tree already in place
# under files/opt/mrrc_modern/ (copied there by build-image.sh before the run).

# pi-gen's chroot env has an empty PATH — set it explicitly.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── venv (build-time; first boot needs no network) ──
python3 -m venv /opt/mrrc_modern/venv
/opt/mrrc_modern/venv/bin/pip install --upgrade pip
PIP_BREAK_SYSTEM_PACKAGES=1 /opt/mrrc_modern/venv/bin/pip install -r /opt/mrrc_modern/requirements.txt

# ── groups & ownership ──
usermod -aG dialout,audio mrrc
chown -R mrrc:mrrc /opt/mrrc_modern
mkdir -p /var/lib/mrrc/certs
chown -R mrrc:mrrc /var/lib/mrrc

# ── service & helper wiring ──
chmod 755 /opt/mrrc_modern/linux/firstboot_wrapper.py /usr/local/bin/mrrc-show-password /etc/update-motd.d/10-mrrc
systemctl enable mrrc-firstboot.service
systemctl enable mrrc-modern.service
systemctl enable ssh

# ── version stamp (written by build-image.sh as files/opt/mrrc_modern/VERSION) ──
echo "MRRC Modern $(cat /opt/mrrc_modern/VERSION) — rpi64 image"

# ── BUILD GATE: runtime imports + syntax inside the image ──
/opt/mrrc_modern/venv/bin/python -m py_compile /opt/mrrc_modern/server.py \
    /opt/mrrc_modern/linux/first_run.py /opt/mrrc_modern/linux/firstboot_wrapper.py
/opt/mrrc_modern/venv/bin/python -c "import fastapi, uvicorn, serial, pyaudio, numpy, cryptography; print('deps OK')"
/opt/mrrc_modern/venv/bin/python -c "import sys; sys.path.insert(0,'/opt/mrrc_modern'); import scope_libraries; print('scope libs OK')"
