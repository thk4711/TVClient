PYTHON_SATIP_YT_DLP_VERSION = 2026.10.26
PYTHON_SATIP_YT_DLP_SOURCE = yt_dlp-$(PYTHON_SATIP_YT_DLP_VERSION).tar.gz
PYTHON_SATIP_YT_DLP_SITE = https://files.pythonhosted.org/packages/83/31/4be3987022a6244550ab6a3afe45ad10ac10b5e5ab8874bd8eecc7f8b937
PYTHON_SATIP_YT_DLP_LICENSE = Unlicense
PYTHON_SATIP_YT_DLP_LICENSE_FILES = LICENSE
PYTHON_SATIP_YT_DLP_DEPENDENCIES = python3

# Pure Python, no target pip or optional binary dependencies. yt-dlp's source
# contains vendored code with additional notices; retain the complete package.
define PYTHON_SATIP_YT_DLP_INSTALL_TARGET_CMDS
	$(INSTALL) -d $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages
	cp -r $(@D)/yt_dlp $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/
endef

$(eval $(generic-package))
