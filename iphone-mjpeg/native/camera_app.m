#import "camera.h"

#import <UIKit/UIKit.h>

static const NSInteger kDefaultWidth = 640;
static const NSInteger kDefaultHeight = 480;
static const NSInteger kDefaultFPS = 30;
static const NSInteger kDefaultQuality = 60;
static const NSInteger kDefaultRotation = 0;
static const NSInteger kDefaultBridgePort = 18088;

@interface IPMJAppDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, strong) UIViewController *cameraController;
@property(nonatomic, strong) UILabel *statusLabel;
@property(nonatomic, strong) IPMJCameraProducer *producer;
@property(nonatomic, strong) AVCaptureVideoPreviewLayer *previewLayer;
@property(nonatomic, strong) NSURL *lastStartURL;
@property(nonatomic) BOOL permissionRequestInFlight;
@end

@implementation IPMJAppDelegate

- (BOOL)application:(UIApplication *)application
    didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    NSString *logPath = [NSHomeDirectory() stringByAppendingPathComponent:
        @"Library/Logs/iPhoneCamera/app.log"];
    IPMJConfigureFileLogging(logPath);
    IPMJLogMessage(@"iPhone Camera app did finish launching");
    application.idleTimerDisabled = YES;
    self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
    UIViewController *controller = [[UIViewController alloc] init];
    controller.view.backgroundColor = UIColor.blackColor;
    UILabel *label = [[UILabel alloc] initWithFrame:controller.view.bounds];
    label.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    label.numberOfLines = 0;
    label.textAlignment = NSTextAlignmentCenter;
    label.textColor = UIColor.whiteColor;
    label.font = [UIFont systemFontOfSize:20];
    label.text = @"Starting iPhone MJPEG camera...";
    [controller.view addSubview:label];
    self.statusLabel = label;
    self.cameraController = controller;
    self.window.rootViewController = controller;
    [self.window makeKeyAndVisible];

    self.lastStartURL = launchOptions[UIApplicationLaunchOptionsURLKey];
    return YES;
}

- (BOOL)application:(UIApplication *)application
             openURL:(NSURL *)url
             options:(NSDictionary<UIApplicationOpenURLOptionsKey, id> *)options {
    (void)application;
    (void)options;
    if ([url.host isEqualToString:@"stop"]) {
        [self stopCamera];
    } else {
        self.lastStartURL = url;
        if (application.applicationState == UIApplicationStateActive) {
            [self startFromURL:url];
        }
    }
    return YES;
}

- (NSInteger)valueForName:(NSString *)name
               components:(NSURLComponents *)components
                  fallback:(NSInteger)fallback {
    for (NSURLQueryItem *item in components.queryItems ?: @[]) {
        if ([item.name isEqualToString:name] && item.value.length > 0) {
            return item.value.integerValue;
        }
    }
    return fallback;
}

- (void)startFromURL:(NSURL *)url {
    AVAuthorizationStatus authorization =
        [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    IPMJLogMessage([NSString stringWithFormat:@"Camera authorization status=%ld",
                                               (long)authorization]);
    if (authorization == AVAuthorizationStatusNotDetermined) {
        if (self.permissionRequestInFlight) {
            return;
        }
        self.permissionRequestInFlight = YES;
        self.statusLabel.text = @"Camera permission is required\nTap Allow in the system prompt.";
        __weak typeof(self) weakSelf = self;
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo
                                 completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{
                typeof(self) strongSelf = weakSelf;
                strongSelf.permissionRequestInFlight = NO;
                IPMJLogMessage([NSString stringWithFormat:@"Camera permission result=%@",
                                                           granted ? @"granted" : @"denied"]);
                if (granted && UIApplication.sharedApplication.applicationState ==
                                   UIApplicationStateActive) {
                    [strongSelf startFromURL:strongSelf.lastStartURL];
                } else if (!granted) {
                    strongSelf.statusLabel.text =
                        @"Camera permission denied\nEnable Camera access in Settings.";
                }
            });
        }];
        return;
    }
    NSURLComponents *components = url ? [NSURLComponents componentsWithURL:url
                                                   resolvingAgainstBaseURL:NO] : nil;
    NSInteger width = [self valueForName:@"width" components:components fallback:kDefaultWidth];
    NSInteger height = [self valueForName:@"height" components:components fallback:kDefaultHeight];
    NSInteger fps = [self valueForName:@"fps" components:components fallback:kDefaultFPS];
    NSInteger quality = [self valueForName:@"quality" components:components fallback:kDefaultQuality];
    NSInteger rotation = [self valueForName:@"rotation" components:components fallback:kDefaultRotation];
    NSInteger bridgePort = [self valueForName:@"bridgePort"
                                   components:components
                                      fallback:kDefaultBridgePort];
    if (width <= 0 || height <= 0 || fps <= 0 || quality < 1 || quality > 100 ||
        bridgePort < 1 || bridgePort > 65535 ||
        (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270)) {
        self.statusLabel.text = @"Invalid camera URL parameters";
        return;
    }

    [self.producer stop];
    self.producer = [[IPMJCameraProducer alloc] initWithTCPHost:@"127.0.0.1"
                                                           port:bridgePort
                                                          width:width
                                                         height:height
                                                            fps:fps
                                                        quality:quality
                                                       rotation:rotation];
    NSError *error = nil;
    if (![self.producer start:&error]) {
        IPMJLogMessage([NSString stringWithFormat:@"Camera producer failed: %@",
                                                   error.localizedDescription]);
        self.statusLabel.text = [NSString stringWithFormat:@"Camera failed\n%@",
                                                           error.localizedDescription];
        return;
    }
    [self.previewLayer removeFromSuperlayer];
    AVCaptureVideoPreviewLayer *preview =
        [AVCaptureVideoPreviewLayer layerWithSession:self.producer.captureSession];
    preview.frame = self.cameraController.view.bounds;
    preview.videoGravity = AVLayerVideoGravityResizeAspect;
    [self.cameraController.view.layer insertSublayer:preview atIndex:0];
    self.previewLayer = preview;
    self.statusLabel.text = [NSString stringWithFormat:
        @"iPhone MJPEG camera is running\n\n%ld x %ld @ %ld FPS\nJPEG quality %ld · rotation %ld°\n\nKeep this app in the foreground.\nHTTP: port 8088",
        (long)width, (long)height, (long)fps, (long)quality, (long)rotation];
}

- (void)applicationDidBecomeActive:(UIApplication *)application {
    (void)application;
    IPMJLogMessage(@"iPhone Camera app became active");
    [self startFromURL:self.lastStartURL];
}

- (void)applicationDidEnterBackground:(UIApplication *)application {
    (void)application;
    IPMJLogMessage(@"iPhone Camera app entered background");
    [self.producer stop];
    self.producer = nil;
    self.statusLabel.text = @"Camera paused in background\nReturn to this app to resume.";
}

- (void)stopCamera {
    [self.previewLayer removeFromSuperlayer];
    self.previewLayer = nil;
    [self.producer stop];
    self.producer = nil;
    self.statusLabel.text = @"iPhone MJPEG camera stopped";
}

- (void)applicationWillTerminate:(UIApplication *)application {
    (void)application;
    [self stopCamera];
}

@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil, NSStringFromClass([IPMJAppDelegate class]));
    }
}
