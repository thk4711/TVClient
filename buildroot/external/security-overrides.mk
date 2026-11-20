# Security-only updates on the clean Buildroot 2026.05.2 checkout.
# BR2_PACKAGE_OVERRIDE_FILE is read BEFORE upstream recipes are evaluated.
# override prevents ordinary upstream assignments restoring vulnerable pins.
# Retain upstream build backends/dependencies. Use only the reviewed new hashes:
# idna's license text changed, so its old license hash cannot also apply.
# A fresh output is required. Do not include this after package evaluation.

override PYTHON_ZEROCONF_VERSION = 0.149.16
override PYTHON_ZEROCONF_SITE = https://files.pythonhosted.org/packages/83/34/c981e760690f7b7dc91532d4d4ad21e3922887aaa425a0e7bff8067152da
override PYTHON_ZEROCONF_HASH_FILES = $(BR2_EXTERNAL_SATIP_PATH)/patches/python-zeroconf/0.149.16/python-zeroconf.hash

override PYTHON_STARLETTE_VERSION = 1.3.1
override PYTHON_STARLETTE_SITE = https://files.pythonhosted.org/packages/eb/e3/7c1dc7381d9f8ab7d854328ebfa884e62cb3f3d8549ddfd37c7814f42afa
override PYTHON_STARLETTE_HASH_FILES = $(BR2_EXTERNAL_SATIP_PATH)/patches/python-starlette/1.3.1/python-starlette.hash

override PYTHON_ANYIO_VERSION = 4.14.2
override PYTHON_ANYIO_SITE = https://files.pythonhosted.org/packages/61/cc/a381afa6efea9f496eff839d4a6a1aed3bfafc7b3ab4b0d1b243a12573dd
override PYTHON_ANYIO_HASH_FILES = $(BR2_EXTERNAL_SATIP_PATH)/patches/python-anyio/4.14.2/python-anyio.hash

override PYTHON_IDNA_VERSION = 3.15
override PYTHON_IDNA_SITE = https://files.pythonhosted.org/packages/82/77/7b3966d0b9d1d31a36ddf1746926a11dface89a83409bf1483f0237aa758
override PYTHON_IDNA_HASH_FILES = $(BR2_EXTERNAL_SATIP_PATH)/patches/python-idna/3.15/python-idna.hash
override HOST_PYTHON_IDNA_HASH_FILES = $(PYTHON_IDNA_HASH_FILES)

override PYTHON_CERTIFI_VERSION = 2026.10.19
override PYTHON_CERTIFI_SITE = https://files.pythonhosted.org/packages/a6/bd/1436c50cc46699de0230fbaacc1d1de7bfec413176099949e9d5112e88f2
override PYTHON_CERTIFI_HASH_FILES = $(BR2_EXTERNAL_SATIP_PATH)/patches/python-certifi/2026.10.19/python-certifi.hash
override HOST_PYTHON_CERTIFI_HASH_FILES = $(PYTHON_CERTIFI_HASH_FILES)
