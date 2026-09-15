#import "camera.h"

#import <UIKit/UIKit.h>

static const NSInteger kDefaultWidth = 1280;
static const NSInteger kDefaultHeight = 720;
static const NSInteger kDefaultFPS = 30;
static const NSInteger kDefaultQuality = 75;
static const NSInteger kDefaultRotation = 0;
static NSString *const kFrameSocketPath = @"/var/mobile/iphone-mjpeg/run/frames.sock";

@interface IPMJAppDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, strong) UILabel *statusLabel;
@property(nonatomic, strong) IPMJCameraProducer *producer;
@end

@implementation IPMJAppDelegate

- (BOOL)application:(UIApplication *)application
    didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    application.idleTimerDisabled = YES;
    self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
    UIViewController *controller = [[UIViewController alloc] init];
    controller.view.backgroundColor = UIColor.blackColor;
    UILabel *label = [[UILabel alloc] initWithFrame:controller.view.bounds];
    label.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    label.numberOfLines = 0;
    label.textAlignment = NSTextAlignmentCenter;
    label.textColor = UIColor.whiteColor;
    label.font = [UIFont monospacedSystemFontOfSize:20 weight:UIFontWeightRegular];
    label.text = @"Starting iPhone MJPEG camera...";
    [controller.view addSubview:label];
    self.statusLabel = label;
    self.window.rootViewController = controller;
    [self.window makeKeyAndVisible];

    NSURL *launchURL = launchOptions[UIApplicationLaunchOptionsURLKey];
    [self startFromURL:launchURL];
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
        [self startFromURL:url];
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
    NSURLComponents *components = url ? [NSURLComponents componentsWithURL:url
                                                   resolvingAgainstBaseURL:NO] : nil;
    NSInteger width = [self valueForName:@"width" components:components fallback:kDefaultWidth];
    NSInteger height = [self valueForName:@"height" components:components fallback:kDefaultHeight];
    NSInteger fps = [self valueForName:@"fps" components:components fallback:kDefaultFPS];
    NSInteger quality = [self valueForName:@"quality" components:components fallback:kDefaultQuality];
    NSInteger rotation = [self valueForName:@"rotation" components:components fallback:kDefaultRotation];
    if (width <= 0 || height <= 0 || fps <= 0 || quality < 1 || quality > 100 ||
        (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270)) {
        self.statusLabel.text = @"Invalid camera URL parameters";
        return;
    }

    [self.producer stop];
    self.producer = [[IPMJCameraProducer alloc] initWithSocketPath:kFrameSocketPath
                                                             width:width
                                                            height:height
                                                               fps:fps
                                                           quality:quality
                                                          rotation:rotation];
    NSError *error = nil;
    if (![self.producer start:&error]) {
        self.statusLabel.text = [NSString stringWithFormat:@"Camera failed\n%@",
                                                           error.localizedDescription];
        return;
    }
    self.statusLabel.text = [NSString stringWithFormat:
        @"iPhone MJPEG camera is running\n\n%ld x %ld @ %ld FPS\nJPEG quality %ld · rotation %ld°\n\nKeep this app in the foreground.\nHTTP: port 8088",
        (long)width, (long)height, (long)fps, (long)quality, (long)rotation];
}

- (void)stopCamera {
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
