SATIP_SPLASH_VERSION = 0.1.0
SATIP_SPLASH_SITE = $(BR2_EXTERNAL_SATIP_PATH)/package/satip-splash/src
SATIP_SPLASH_SITE_METHOD = local
SATIP_SPLASH_LICENSE = MIT
SATIP_SPLASH_LICENSE_FILES = LICENSE qrcodegen.c
SATIP_SPLASH_DEPENDENCIES = libdrm freetype dejavu host-pkgconf

define SATIP_SPLASH_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror \
		$$($(PKG_CONFIG_HOST_BINARY) --cflags libdrm freetype2) \
		$(@D)/splash.c $(@D)/render.c $(@D)/qrcodegen.c $(TARGET_LDFLAGS) \
		$$($(PKG_CONFIG_HOST_BINARY) --libs libdrm freetype2) -lm -o $(@D)/satip-splash
endef

define SATIP_SPLASH_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/satip-splash $(TARGET_DIR)/usr/bin/satip-splash
endef

$(eval $(generic-package))
