#import "camera.h"

#import <AVFoundation/AVFoundation.h>
#import <CoreFoundation/CoreFoundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <ImageIO/ImageIO.h>
#import <arpa/inet.h>
#import <errno.h>
#import <float.h>
#import <math.h>
#import <signal.h>
#import <stdio.h>
#import <string.h>
#import <sys/socket.h>
#import <sys/un.h>
#import <unistd.h>

static const uint8_t kFrameMagic[4] = {'M', 'J', 'P', '1'};
static const NSUInteger kFrameHeaderSize = 24;
static volatile sig_atomic_t gShouldStop = 0;

static void IPMJSignalHandler(int signalNumber) {
    (void)signalNumber;
    gShouldStop = 1;
}

static void IPMJWriteUInt32(uint8_t *target, uint32_t value) {
    uint32_t encoded = CFSwapInt32HostToBig(value);
    memcpy(target, &encoded, sizeof(encoded));
}

static void IPMJWriteUInt64(uint8_t *target, uint64_t value) {
    uint64_t encoded = CFSwapInt64HostToBig(value);
    memcpy(target, &encoded, sizeof(encoded));
}

static BOOL IPMJSendAll(int socketFd, const uint8_t *bytes, size_t length) {
    size_t sent = 0;
    while (sent < length && !gShouldStop) {
        ssize_t result = send(socketFd, bytes + sent, length - sent, 0);
        if (result > 0) {
            sent += (size_t)result;
            continue;
        }
        if (result < 0 && errno == EINTR) {
            continue;
        }
        return NO;
    }
    return sent == length;
}

@interface IPMJCameraProducer () <AVCaptureVideoDataOutputSampleBufferDelegate>
@property(nonatomic, copy) NSString *socketPath;
@property(nonatomic) NSInteger requestedWidth;
@property(nonatomic) NSInteger requestedHeight;
@property(nonatomic) NSInteger requestedFPS;
@property(nonatomic) CGFloat jpegQuality;
@property(nonatomic) NSInteger rotation;
@property(nonatomic, strong) AVCaptureSession *session;
@property(nonatomic, strong) dispatch_queue_t captureQueue;
@property(nonatomic, strong) NSCondition *frameCondition;
@property(nonatomic, strong, nullable) NSData *latestJPEG;
@property(nonatomic) uint32_t latestWidth;
@property(nonatomic) uint32_t latestHeight;
@property(nonatomic) uint64_t latestTimestampNs;
@property(nonatomic) uint64_t latestSequence;
@property(nonatomic) uint64_t sampleCount;
@property(nonatomic) uint64_t jpegFailureCount;
@property(nonatomic) uint64_t droppedFrameCount;
@property(nonatomic) BOOL running;
@end

@implementation IPMJCameraProducer

- (instancetype)initWithSocketPath:(NSString *)socketPath
                              width:(NSInteger)width
                             height:(NSInteger)height
                                fps:(NSInteger)fps
                            quality:(NSInteger)quality
                           rotation:(NSInteger)rotation {
    self = [super init];
    if (self) {
        _socketPath = [socketPath copy];
        _requestedWidth = width;
        _requestedHeight = height;
        _requestedFPS = fps;
        _jpegQuality = MAX(1, MIN(100, quality)) / 100.0;
        _rotation = ((rotation % 360) + 360) % 360;
        _captureQueue = dispatch_queue_create("local.iphonecamera.capture", DISPATCH_QUEUE_SERIAL);
        _frameCondition = [[NSCondition alloc] init];
    }
    return self;
}

- (BOOL)ensureCameraPermission:(NSError **)error {
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (status == AVAuthorizationStatusAuthorized) {
        return YES;
    }
    if (error) {
        NSString *statusName = status == AVAuthorizationStatusNotDetermined ? @"not determined" :
                               status == AVAuthorizationStatusDenied ? @"denied" : @"restricted";
        *error = [NSError errorWithDomain:@"local.iphonecamera"
                                     code:1
                                 userInfo:@{NSLocalizedDescriptionKey:
                                                [NSString stringWithFormat:
                                                    @"Camera permission is %@. This command-line helper will not trigger a TCC prompt without an app Info.plist; sign with the supplied entitlement or authorize it from an app context.",
                                                    statusName]}];
    }
    return NO;
}

- (AVCaptureDevice *)rearCamera {
    AVCaptureDeviceDiscoverySession *discovery =
        [AVCaptureDeviceDiscoverySession
            discoverySessionWithDeviceTypes:@[AVCaptureDeviceTypeBuiltInWideAngleCamera]
                                  mediaType:AVMediaTypeVideo
                                   position:AVCaptureDevicePositionBack];
    AVCaptureDevice *device = discovery.devices.firstObject;
    if (device) {
        return device;
    }
    return [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo];
}

- (void)configureFormatForDevice:(AVCaptureDevice *)device {
    AVCaptureDeviceFormat *bestFormat = nil;
    double bestScore = DBL_MAX;
    for (AVCaptureDeviceFormat *format in device.formats) {
        CMVideoDimensions dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription);
        double sizeError = fabs((double)dimensions.width - self.requestedWidth) +
                           fabs((double)dimensions.height - self.requestedHeight);
        BOOL supportsFPS = NO;
        for (AVFrameRateRange *range in format.videoSupportedFrameRateRanges) {
            if (self.requestedFPS >= range.minFrameRate && self.requestedFPS <= range.maxFrameRate) {
                supportsFPS = YES;
                break;
            }
        }
        double score = sizeError + (supportsFPS ? 0.0 : 1000000.0);
        if (score < bestScore) {
            bestScore = score;
            bestFormat = format;
        }
    }
    if (!bestFormat) {
        return;
    }

    NSError *lockError = nil;
    if (![device lockForConfiguration:&lockError]) {
        NSLog(@"Unable to lock camera format: %@", lockError.localizedDescription);
        return;
    }
    device.activeFormat = bestFormat;
    CMTime frameDuration = CMTimeMake(1, (int32_t)self.requestedFPS);
    device.activeVideoMinFrameDuration = frameDuration;
    device.activeVideoMaxFrameDuration = frameDuration;
    CMVideoDimensions activeDimensions =
        CMVideoFormatDescriptionGetDimensions(bestFormat.formatDescription);
    NSLog(@"Selected camera format: %dx%d at requested %ld FPS",
          activeDimensions.width, activeDimensions.height, (long)self.requestedFPS);
    [device unlockForConfiguration];
}

- (void)sessionDidStartRunning:(NSNotification *)notification {
    (void)notification;
    NSLog(@"AVCaptureSession did start running (running=%@)", self.session.isRunning ? @"YES" : @"NO");
}

- (void)sessionRuntimeError:(NSNotification *)notification {
    NSError *error = notification.userInfo[AVCaptureSessionErrorKey];
    NSLog(@"AVCaptureSession runtime error: %@", error ?: @"unknown");
}

- (void)sessionWasInterrupted:(NSNotification *)notification {
    NSLog(@"AVCaptureSession was interrupted: %@", notification.userInfo ?: @{});
}

- (void)sessionInterruptionEnded:(NSNotification *)notification {
    (void)notification;
    NSLog(@"AVCaptureSession interruption ended");
}

- (BOOL)start:(NSError **)error {
    if (![self ensureCameraPermission:error]) {
        return NO;
    }

    AVCaptureDevice *device = [self rearCamera];
    if (!device) {
        if (error) {
            *error = [NSError errorWithDomain:@"local.iphonecamera"
                                         code:2
                                     userInfo:@{NSLocalizedDescriptionKey: @"No video capture device found"}];
        }
        return NO;
    }

    NSError *inputError = nil;
    AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&inputError];
    if (!input) {
        if (error) {
            *error = inputError;
        }
        return NO;
    }

    AVCaptureSession *session = [[AVCaptureSession alloc] init];
    [session beginConfiguration];
    if ([session canSetSessionPreset:AVCaptureSessionPresetInputPriority]) {
        session.sessionPreset = AVCaptureSessionPresetInputPriority;
    } else if ([session canSetSessionPreset:AVCaptureSessionPreset1280x720]) {
        session.sessionPreset = AVCaptureSessionPreset1280x720;
    }
    if (![session canAddInput:input]) {
        [session commitConfiguration];
        if (error) {
            *error = [NSError errorWithDomain:@"local.iphonecamera"
                                         code:3
                                     userInfo:@{NSLocalizedDescriptionKey: @"Unable to add rear camera input"}];
        }
        return NO;
    }
    [session addInput:input];
    [self configureFormatForDevice:device];

    AVCaptureVideoDataOutput *output = [[AVCaptureVideoDataOutput alloc] init];
    output.alwaysDiscardsLateVideoFrames = YES;
    output.videoSettings = @{(id)kCVPixelBufferPixelFormatTypeKey:
                                 @(kCVPixelFormatType_32BGRA)};
    [output setSampleBufferDelegate:self queue:self.captureQueue];
    if (![session canAddOutput:output]) {
        [session commitConfiguration];
        if (error) {
            *error = [NSError errorWithDomain:@"local.iphonecamera"
                                         code:4
                                     userInfo:@{NSLocalizedDescriptionKey: @"Unable to add video data output"}];
        }
        return NO;
    }
    [session addOutput:output];

    AVCaptureConnection *connection = [output connectionWithMediaType:AVMediaTypeVideo];
    if (connection.isVideoOrientationSupported) {
        switch (self.rotation) {
            case 90:
                connection.videoOrientation = AVCaptureVideoOrientationPortrait;
                break;
            case 180:
                connection.videoOrientation = AVCaptureVideoOrientationLandscapeLeft;
                break;
            case 270:
                connection.videoOrientation = AVCaptureVideoOrientationPortraitUpsideDown;
                break;
            default:
                connection.videoOrientation = AVCaptureVideoOrientationLandscapeRight;
                break;
        }
    }
    if (connection.isVideoMirroringSupported) {
        connection.videoMirrored = NO;
    }
    [session commitConfiguration];

    self.session = session;
    NSNotificationCenter *notifications = [NSNotificationCenter defaultCenter];
    [notifications addObserver:self selector:@selector(sessionDidStartRunning:)
                          name:AVCaptureSessionDidStartRunningNotification object:session];
    [notifications addObserver:self selector:@selector(sessionRuntimeError:)
                          name:AVCaptureSessionRuntimeErrorNotification object:session];
    [notifications addObserver:self selector:@selector(sessionWasInterrupted:)
                          name:AVCaptureSessionWasInterruptedNotification object:session];
    [notifications addObserver:self selector:@selector(sessionInterruptionEnded:)
                          name:AVCaptureSessionInterruptionEndedNotification object:session];
    self.running = YES;
    [NSThread detachNewThreadSelector:@selector(senderMain) toTarget:self withObject:nil];
    [session startRunning];
    NSLog(@"Camera started: requested=%ldx%ld@%ld quality=%.2f rotation=%ld",
          (long)self.requestedWidth, (long)self.requestedHeight, (long)self.requestedFPS,
          self.jpegQuality, (long)self.rotation);
    return YES;
}

- (void)stop {
    self.running = NO;
    [self.frameCondition lock];
    [self.frameCondition broadcast];
    [self.frameCondition unlock];
    [self.session stopRunning];
    [[NSNotificationCenter defaultCenter] removeObserver:self];
}

- (NSData *)jpegFromPixelBuffer:(CVPixelBufferRef)pixelBuffer
                          width:(uint32_t *)widthOut
                         height:(uint32_t *)heightOut {
    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    size_t width = CVPixelBufferGetWidth(pixelBuffer);
    size_t height = CVPixelBufferGetHeight(pixelBuffer);
    size_t bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer);
    void *baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer);

    CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
    CGDataProviderRef provider = CGDataProviderCreateWithData(NULL, baseAddress,
                                                              bytesPerRow * height, NULL);
    if (!colorSpace || !provider) {
        if (provider) CGDataProviderRelease(provider);
        if (colorSpace) CGColorSpaceRelease(colorSpace);
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
        return nil;
    }
    CGBitmapInfo bitmapInfo = kCGBitmapByteOrder32Little | kCGImageAlphaNoneSkipFirst;
    CGImageRef image = CGImageCreate(width, height, 8, 32, bytesPerRow, colorSpace,
                                     bitmapInfo, provider, NULL, false,
                                     kCGRenderingIntentDefault);
    CFMutableDataRef encoded = CFDataCreateMutable(kCFAllocatorDefault, 0);
    if (!image || !encoded) {
        if (encoded) CFRelease(encoded);
        if (image) CGImageRelease(image);
        CGDataProviderRelease(provider);
        CGColorSpaceRelease(colorSpace);
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
        return nil;
    }
    CGImageDestinationRef destination =
        CGImageDestinationCreateWithData(encoded, CFSTR("public.jpeg"), 1, NULL);
    if (!destination) {
        CFRelease(encoded);
        CGImageRelease(image);
        CGDataProviderRelease(provider);
        CGColorSpaceRelease(colorSpace);
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
        return nil;
    }
    NSDictionary *options = @{(id)kCGImageDestinationLossyCompressionQuality:
                                  @(self.jpegQuality)};
    CGImageDestinationAddImage(destination, image, (__bridge CFDictionaryRef)options);
    BOOL finalized = CGImageDestinationFinalize(destination);

    CFRelease(destination);
    CGImageRelease(image);
    CGDataProviderRelease(provider);
    CGColorSpaceRelease(colorSpace);
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);

    if (!finalized) {
        CFRelease(encoded);
        return nil;
    }
    *widthOut = (uint32_t)width;
    *heightOut = (uint32_t)height;
    return CFBridgingRelease(encoded);
}

- (void)captureOutput:(AVCaptureOutput *)output
 didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
        fromConnection:(AVCaptureConnection *)connection {
    (void)output;
    (void)connection;
    CVPixelBufferRef pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (!pixelBuffer) {
        return;
    }
    self.sampleCount += 1;
    if (self.sampleCount == 1) {
        NSLog(@"Received first camera sample buffer");
    }
    uint64_t timestampNs = (uint64_t)([[NSDate date] timeIntervalSince1970] * 1000000000.0);
    uint32_t width = 0;
    uint32_t height = 0;
    NSData *jpeg = [self jpegFromPixelBuffer:pixelBuffer width:&width height:&height];
    if (!jpeg) {
        self.jpegFailureCount += 1;
        if (self.jpegFailureCount == 1 || self.jpegFailureCount % 60 == 0) {
            NSLog(@"JPEG encoding failed (count=%llu)", self.jpegFailureCount);
        }
        return;
    }
    if (self.latestSequence == 0) {
        NSLog(@"Encoded first JPEG: %u x %u, %lu bytes", width, height,
              (unsigned long)jpeg.length);
    }
    [self.frameCondition lock];
    self.latestJPEG = jpeg;
    self.latestWidth = width;
    self.latestHeight = height;
    self.latestTimestampNs = timestampNs;
    self.latestSequence += 1;
    [self.frameCondition signal];
    [self.frameCondition unlock];
}

- (void)captureOutput:(AVCaptureOutput *)output
 didDropSampleBuffer:(CMSampleBufferRef)sampleBuffer
        fromConnection:(AVCaptureConnection *)connection {
    (void)output;
    (void)sampleBuffer;
    (void)connection;
    self.droppedFrameCount += 1;
    if (self.droppedFrameCount == 1 || self.droppedFrameCount % 300 == 0) {
        NSLog(@"AVFoundation dropped late frame (count=%llu)", self.droppedFrameCount);
    }
}

- (int)connectFrameSocket {
    const char *path = self.socketPath.fileSystemRepresentation;
    if (strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
        NSLog(@"Unix socket path is too long: %@", self.socketPath);
        return -1;
    }
    int socketFd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (socketFd < 0) {
        return -1;
    }
    int noSigPipe = 1;
    setsockopt(socketFd, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, sizeof(noSigPipe));
    struct timeval timeout = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(socketFd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

    struct sockaddr_un address;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strlcpy(address.sun_path, path, sizeof(address.sun_path));
    if (connect(socketFd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(socketFd);
        return -1;
    }
    NSLog(@"Connected to frame socket %@", self.socketPath);
    return socketFd;
}

- (void)senderMain {
    @autoreleasepool {
        int socketFd = -1;
        uint64_t sentSequence = 0;
        while (self.running && !gShouldStop) {
            NSData *jpeg = nil;
            uint32_t width = 0;
            uint32_t height = 0;
            uint64_t timestampNs = 0;
            uint64_t sequence = 0;

            [self.frameCondition lock];
            while (self.running && self.latestSequence == sentSequence && !gShouldStop) {
                [self.frameCondition waitUntilDate:[NSDate dateWithTimeIntervalSinceNow:1.0]];
            }
            if (self.running && self.latestSequence != sentSequence) {
                jpeg = self.latestJPEG;
                width = self.latestWidth;
                height = self.latestHeight;
                timestampNs = self.latestTimestampNs;
                sequence = self.latestSequence;
            }
            [self.frameCondition unlock];

            if (!jpeg) {
                continue;
            }
            if (socketFd < 0) {
                socketFd = [self connectFrameSocket];
                if (socketFd < 0) {
                    [NSThread sleepForTimeInterval:0.5];
                    continue;
                }
            }

            uint8_t header[kFrameHeaderSize];
            memcpy(header, kFrameMagic, sizeof(kFrameMagic));
            IPMJWriteUInt32(header + 4, (uint32_t)jpeg.length);
            IPMJWriteUInt32(header + 8, width);
            IPMJWriteUInt32(header + 12, height);
            IPMJWriteUInt64(header + 16, timestampNs);
            if (!IPMJSendAll(socketFd, header, sizeof(header)) ||
                !IPMJSendAll(socketFd, jpeg.bytes, jpeg.length)) {
                close(socketFd);
                socketFd = -1;
                continue;
            }
            sentSequence = sequence;
        }
        if (socketFd >= 0) {
            close(socketFd);
        }
    }
}

@end

static NSInteger IPMJIntegerArgument(NSDictionary<NSString *, NSString *> *arguments,
                                     NSString *name, NSInteger fallback) {
    NSString *value = arguments[name];
    return value ? value.integerValue : fallback;
}

#if !defined(IPMJ_APP)
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSMutableDictionary<NSString *, NSString *> *arguments = [NSMutableDictionary dictionary];
        for (int index = 1; index + 1 < argc; index += 2) {
            NSString *key = [NSString stringWithUTF8String:argv[index]];
            NSString *value = [NSString stringWithUTF8String:argv[index + 1]];
            arguments[key] = value;
        }
        NSString *socketPath = arguments[@"--socket"];
        if (!socketPath) {
            fprintf(stderr, "Usage: iphone-camera --socket PATH [--width N --height N --fps N --quality N --rotation N]\n");
            return 64;
        }
        NSInteger width = IPMJIntegerArgument(arguments, @"--width", 1280);
        NSInteger height = IPMJIntegerArgument(arguments, @"--height", 720);
        NSInteger fps = IPMJIntegerArgument(arguments, @"--fps", 30);
        NSInteger quality = IPMJIntegerArgument(arguments, @"--quality", 75);
        NSInteger rotation = IPMJIntegerArgument(arguments, @"--rotation", 0);
        if (width <= 0 || height <= 0 || fps <= 0 || quality < 1 || quality > 100 ||
            (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270)) {
            fprintf(stderr, "Invalid camera arguments\n");
            return 64;
        }

        signal(SIGINT, IPMJSignalHandler);
        signal(SIGTERM, IPMJSignalHandler);
        IPMJCameraProducer *producer = [[IPMJCameraProducer alloc] initWithSocketPath:socketPath
                                                                               width:width
                                                                              height:height
                                                                                 fps:fps
                                                                             quality:quality
                                                                            rotation:rotation];
        NSError *error = nil;
        if (![producer start:&error]) {
            NSLog(@"Camera initialization failed: %@", error.localizedDescription);
            return 1;
        }
        while (!gShouldStop) {
            @autoreleasepool {
                [NSThread sleepForTimeInterval:0.2];
            }
        }
        [producer stop];
        return 0;
    }
}
#endif
