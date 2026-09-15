CC ?= clang
TARGET ?= iphone-camera
MIN_IOS ?= 10.0

CFLAGS ?= -O2
CFLAGS += -Wall -Wextra -fobjc-arc -fblocks -miphoneos-version-min=$(MIN_IOS)
FRAMEWORKS = -framework Foundation -framework AVFoundation -framework CoreMedia \
	-framework CoreVideo -framework CoreGraphics -framework ImageIO

.PHONY: all clean sign

all: $(TARGET)

$(TARGET): camera.m camera.h
	$(CC) $(CFLAGS) camera.m -o $@ $(FRAMEWORKS)

sign: $(TARGET)
	@if ! command -v ldid >/dev/null 2>&1; then \
		echo "ldid not found; install/provide ldid before signing" >&2; exit 1; \
	fi
	ldid -S../entitlements.plist $(TARGET)

clean:
	rm -f $(TARGET)
