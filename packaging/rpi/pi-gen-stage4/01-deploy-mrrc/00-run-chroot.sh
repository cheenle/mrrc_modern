#!/bin/bash -e
# Runs INSIDE the arm64 rootfs (pi-gen on_chroot). Host-side stage has no
# commands — everything here must execute against the target rootfs.
# pi-gen's on_chroot provides a sane PATH (login shell).

# ── venv (build-time; first boot needs no network) ──
python3 -m venv /opt/mrrc_modern/venv
/opt/mrrc_modern/venv/bin/pip install --upgrade pip
# PyPI direct first; CN mirror fallback (ham build host has flaky egress).
/opt/mrrc_modern/venv/bin/pip install -r /opt/mrrc_modern/requirements.txt ||
	/opt/mrrc_modern/venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r /opt/mrrc_modern/requirements.txt

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
python3 -m py_compile /opt/mrrc_modern/server.py \
	/opt/mrrc_modern/linux/first_run.py /opt/mrrc_modern/linux/firstboot_wrapper.py
/opt/mrrc_modern/venv/bin/python -c "import fastapi, uvicorn, serial, pyaudio, numpy, cryptography; print('deps OK')"
/opt/mrrc_modern/venv/bin/python -c "import sys; sys.path.insert(0,'/opt/mrrc_modern'); import scope_libraries; print('scope libs OK')"
