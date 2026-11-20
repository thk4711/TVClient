SATIP_APP_VERSION = 0.1.1
SATIP_APP_SITE = $(BR2_EXTERNAL_SATIP_PATH)/../..
SATIP_APP_SITE_METHOD = local
# Allowlist local builds too: never stage private configuration or deploy examples.
SATIP_APP_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = \
    --include=/LICENSE \
    --include=/satip_controller/ --include=/satip_controller/*.py \
    --include=/satip_controller/static/ --include=/satip_controller/static/*.html \
    --include=/satip_controller/static/*.js --include=/satip_controller/static/*.css --include=/satip_controller/static/*.txt \
    --include=/satip_appliance/ --include=/satip_appliance/*.py --exclude='*'
SATIP_APP_LICENSE = MIT, Apache-2.0 (hls.js)
SATIP_APP_LICENSE_FILES = LICENSE satip_controller/static/hls-LICENSE.txt satip_controller/static/qrcode-LICENSE.txt
SATIP_APP_DEPENDENCIES = host-python3 python3 python-evdev python-zeroconf python-fastapi python-uvicorn python-httpx python-satip-yt-dlp tvheadend

define SATIP_APP_INSTALL_TARGET_CMDS
	$(INSTALL) -d $(TARGET_DIR)/opt/satip
	$(INSTALL) -D -m 0644 $(BR2_EXTERNAL_SATIP_PATH)/board/satip/satip-config.txt $(TARGET_DIR)/usr/share/satip/satip-config.txt
	$(HOST_DIR)/bin/python$(PYTHON3_VERSION_MAJOR) \
		$(BR2_EXTERNAL_SATIP_PATH)/board/satip/validate-app-build.py \
		--install-sources $(@D) $(TARGET_DIR)
	# Use the matching Buildroot Python, never the orchestration host interpreter.
	$(HOST_DIR)/bin/python$(PYTHON3_VERSION_MAJOR) -m compileall -q \
		--invalidation-mode checked-hash -d /opt/satip $(TARGET_DIR)/opt/satip
	find $(TARGET_DIR)/opt/satip -type d -name __pycache__ -exec chmod 0755 {} +
	find $(TARGET_DIR)/opt/satip -type f -name '*.pyc' -exec chmod 0644 {} +
endef

$(eval $(generic-package))
