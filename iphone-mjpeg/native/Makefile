CC ?= clang
TARGET ?= iphone-camera
MIN_IOS ?= 15.0
SHELL := /var/jb/usr/bin/sh
APP_BUILD_DIR := build/iPhoneCamera.app
APP_TARGET := $(APP_BUILD_DIR)/iPhoneCamera
APP_INFO := $(APP_BUILD_DIR)/Info.plist
IPA_TARGET := build/iPhoneCamera.ipa

CFLAGS ?= -O2
CFLAGS += -Wall -Wextra -fobjc-arc -fblocks -miphoneos-version-min=$(MIN_IOS)
FRAMEWORKS = -framework Foundation -framework AVFoundation -framework CoreMedia \
	-framework CoreVideo -framework CoreGraphics -framework ImageIO

.PHONY: all app app-sign app-package clean sign

all: $(TARGET)

$(TARGET): camera.m camera.h
	$(CC) $(CFLAGS) camera.m -o $@ $(FRAMEWORKS)

sign: $(TARGET)
	@if ! command -v ldid >/dev/null 2>&1; then \
		echo "ldid not found; install/provide ldid before signing" >&2; exit 1; \
	fi
	ldid -S../entitlements.plist $(TARGET)

app: $(APP_TARGET) $(APP_INFO)

$(APP_TARGET): camera.m camera.h camera_app.m
	mkdir -p $(APP_BUILD_DIR)
	$(CC) $(CFLAGS) -DIPMJ_APP camera.m camera_app.m -o $@ $(FRAMEWORKS) -framework UIKit

$(APP_INFO): Info.plist
	mkdir -p $(APP_BUILD_DIR)
	cp Info.plist $(APP_BUILD_DIR)/Info.plist

app-sign: app
	@if ! command -v ldid >/dev/null 2>&1; then \
		echo "ldid not found; install/provide ldid before signing" >&2; exit 1; \
	fi
	ldid -S../app-entitlements.plist $(APP_TARGET)

app-package: app-sign
	rm -rf build/Payload
	mkdir -p build/Payload
	cp -R $(APP_BUILD_DIR) build/Payload/
	cd build && zip -qry iPhoneCamera.ipa Payload
	rm -rf build/Payload
	@echo "IPA ready: $(IPA_TARGET)"

clean:
	rm -f $(TARGET)
	rm -rf build
