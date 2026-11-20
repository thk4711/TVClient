include $(sort $(wildcard $(BR2_EXTERNAL_SATIP_PATH)/package/*/*.mk))

# Upstream couples the client and server under one Kconfig selection. Retain
# tuner discovery/client mode, but omit the server implementation entirely.
ifeq ($(BR2_PACKAGE_SATIP_APP),y)
TVHEADEND_CONF_OPTS += --disable-satip_server
# The board users table supplies the fixed backend account without DVB/video access.
TVHEADEND_USERS =
endif
