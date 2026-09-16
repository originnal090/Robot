#import "camera.h"

#import <UIKit/UIKit.h>

static const NSInteger kDefaultWidth = 640;
static const NSInteger kDefaultHeight = 480;
static const NSInteger kDefaultFPS = 30;
static const NSInteger kDefaultQuality = 60;
static const NSInteger kDefaultRotation = 0;
static const NSInteger kDefaultPortraitCrop = 0;
static const NSInteger kDefaultCropYPercent = 70;
static const NSInteger kDefaultCropZoomPercent = 100;
static const NSInteger kDefaultBridgePort = 18088;

static NSString *const kWidthKey = @"camera.width";
static NSString *const kHeightKey = @"camera.height";
static NSString *const kFPSKey = @"camera.fps";
static NSString *const kQualityKey = @"camera.quality";
static NSString *const kRotationKey = @"camera.rotation";
static NSString *const kPortraitCropKey = @"camera.portraitCrop";
static NSString *const kCropYPercentKey = @"camera.cropYPercent";
static NSString *const kCropZoomPercentKey = @"camera.cropZoomPercent";
static NSString *const kDatasetFPSKey = @"dataset.fps";

@interface IPMJAppDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, strong) UIViewController *cameraController;
@property(nonatomic, strong) UILabel *statusLabel;
@property(nonatomic, strong) IPMJCameraProducer *producer;
@property(nonatomic, strong) AVCaptureVideoPreviewLayer *previewLayer;
@property(nonatomic, strong) NSURL *lastStartURL;
@property(nonatomic) BOOL permissionRequestInFlight;
@property(nonatomic) NSInteger currentBridgePort;
@property(nonatomic, strong) UIScrollView *settingsPanel;
@property(nonatomic, strong) UISegmentedControl *resolutionControl;
@property(nonatomic, strong) UISegmentedControl *fpsControl;
@property(nonatomic, strong) UISegmentedControl *rotationControl;
@property(nonatomic, strong) UISegmentedControl *portraitCropControl;
@property(nonatomic, strong) UISegmentedControl *datasetFPSControl;
@property(nonatomic, strong) UISlider *qualitySlider;
@property(nonatomic, strong) UILabel *qualityLabel;
@property(nonatomic, strong) UISlider *cropYSlider;
@property(nonatomic, strong) UILabel *cropYLabel;
@property(nonatomic, strong) UISlider *cropZoomSlider;
@property(nonatomic, strong) UILabel *cropZoomLabel;
@property(nonatomic, strong) UILabel *datasetStatusLabel;
@property(nonatomic, strong) UIButton *recordButton;
@property(nonatomic, strong) NSTimer *statusTimer;
@end

@implementation IPMJAppDelegate

- (UILabel *)panelLabelWithText:(NSString *)text frame:(CGRect)frame {
    UILabel *label = [[UILabel alloc] initWithFrame:frame];
    label.text = text;
    label.textColor = UIColor.whiteColor;
    label.font = [UIFont systemFontOfSize:13 weight:UIFontWeightSemibold];
    return label;
}

- (UIButton *)buttonWithTitle:(NSString *)title action:(SEL)action frame:(CGRect)frame {
    UIButton *button = [UIButton buttonWithType:UIButtonTypeSystem];
    button.frame = frame;
    button.backgroundColor = [UIColor colorWithWhite:1.0 alpha:0.16];
    button.layer.cornerRadius = 7;
    [button setTitle:title forState:UIControlStateNormal];
    [button setTitleColor:UIColor.whiteColor forState:UIControlStateNormal];
    [button addTarget:self action:action forControlEvents:UIControlEventTouchUpInside];
    return button;
}

- (void)buildInterface {
    UIViewController *controller = [[UIViewController alloc] init];
    controller.view.backgroundColor = UIColor.blackColor;

    UILabel *status = [[UILabel alloc] initWithFrame:CGRectMake(14, 14, 350, 88)];
    status.autoresizingMask = UIViewAutoresizingFlexibleRightMargin | UIViewAutoresizingFlexibleBottomMargin;
    status.numberOfLines = 0;
    status.textAlignment = NSTextAlignmentLeft;
    status.textColor = UIColor.whiteColor;
    status.backgroundColor = [UIColor colorWithWhite:0.0 alpha:0.48];
    status.layer.cornerRadius = 8;
    status.layer.masksToBounds = YES;
    status.font = [UIFont monospacedSystemFontOfSize:13 weight:UIFontWeightMedium];
    status.text = @" Starting iPhone MJPEG camera...";
    [controller.view addSubview:status];
    self.statusLabel = status;

    UIButton *settingsButton = [self buttonWithTitle:@"Config"
                                               action:@selector(toggleSettings:)
                                                frame:CGRectMake(controller.view.bounds.size.width - 82, 12, 70, 36)];
    settingsButton.autoresizingMask = UIViewAutoresizingFlexibleLeftMargin | UIViewAutoresizingFlexibleBottomMargin;
    [controller.view addSubview:settingsButton];

    CGFloat panelWidth = 278;
    UIScrollView *panel = [[UIScrollView alloc]
        initWithFrame:CGRectMake(controller.view.bounds.size.width - panelWidth,
                                 0, panelWidth, controller.view.bounds.size.height)];
    panel.autoresizingMask = UIViewAutoresizingFlexibleLeftMargin | UIViewAutoresizingFlexibleHeight;
    panel.backgroundColor = [UIColor colorWithWhite:0.04 alpha:0.88];
    panel.alwaysBounceVertical = YES;
    panel.contentSize = CGSizeMake(panelWidth, 530);

    [panel addSubview:[self panelLabelWithText:@"Stream resolution" frame:CGRectMake(12, 8, 170, 18)]];
    UIButton *closeButton = [self buttonWithTitle:@"Done"
                                            action:@selector(toggleSettings:)
                                             frame:CGRectMake(208, 4, 58, 24)];
    [panel addSubview:closeButton];
    self.resolutionControl = [[UISegmentedControl alloc] initWithItems:@[@"640×480", @"1280×720"]];
    self.resolutionControl.frame = CGRectMake(12, 28, 254, 30);
    [panel addSubview:self.resolutionControl];

    [panel addSubview:[self panelLabelWithText:@"Camera FPS" frame:CGRectMake(12, 63, 250, 18)]];
    self.fpsControl = [[UISegmentedControl alloc] initWithItems:@[@"15", @"30", @"60"]];
    self.fpsControl.frame = CGRectMake(12, 83, 254, 30);
    [panel addSubview:self.fpsControl];

    self.qualityLabel = [self panelLabelWithText:@"JPEG quality" frame:CGRectMake(12, 118, 250, 18)];
    [panel addSubview:self.qualityLabel];
    self.qualitySlider = [[UISlider alloc] initWithFrame:CGRectMake(12, 135, 254, 28)];
    self.qualitySlider.minimumValue = 40;
    self.qualitySlider.maximumValue = 90;
    [self.qualitySlider addTarget:self action:@selector(qualityChanged:)
                 forControlEvents:UIControlEventValueChanged];
    [panel addSubview:self.qualitySlider];

    [panel addSubview:[self panelLabelWithText:@"Output rotation" frame:CGRectMake(12, 164, 250, 18)]];
    self.rotationControl = [[UISegmentedControl alloc] initWithItems:@[@"0°", @"90°", @"180°", @"270°"]];
    self.rotationControl.frame = CGRectMake(12, 184, 254, 30);
    [panel addSubview:self.rotationControl];

    [panel addSubview:[self panelLabelWithText:@"Portrait mount" frame:CGRectMake(12, 219, 250, 18)]];
    self.portraitCropControl = [[UISegmentedControl alloc] initWithItems:@[@"Off", @"Crop to landscape"]];
    self.portraitCropControl.frame = CGRectMake(12, 239, 254, 30);
    [self.portraitCropControl addTarget:self action:@selector(portraitModeChanged:)
                       forControlEvents:UIControlEventValueChanged];
    [panel addSubview:self.portraitCropControl];

    self.cropYLabel = [self panelLabelWithText:@"Crop vertical" frame:CGRectMake(12, 274, 250, 18)];
    [panel addSubview:self.cropYLabel];
    self.cropYSlider = [[UISlider alloc] initWithFrame:CGRectMake(12, 291, 254, 28)];
    self.cropYSlider.minimumValue = 0;
    self.cropYSlider.maximumValue = 100;
    [self.cropYSlider addTarget:self action:@selector(cropChanged:)
               forControlEvents:UIControlEventValueChanged];
    [panel addSubview:self.cropYSlider];

    self.cropZoomLabel = [self panelLabelWithText:@"Crop zoom" frame:CGRectMake(12, 324, 250, 18)];
    [panel addSubview:self.cropZoomLabel];
    self.cropZoomSlider = [[UISlider alloc] initWithFrame:CGRectMake(12, 341, 254, 28)];
    self.cropZoomSlider.minimumValue = 100;
    self.cropZoomSlider.maximumValue = 200;
    [self.cropZoomSlider addTarget:self action:@selector(cropChanged:)
                  forControlEvents:UIControlEventValueChanged];
    [panel addSubview:self.cropZoomSlider];

    UIButton *applyButton = [self buttonWithTitle:@"Apply & save"
                                            action:@selector(applySettings:)
                                             frame:CGRectMake(12, 375, 254, 34)];
    [panel addSubview:applyButton];

    [panel addSubview:[self panelLabelWithText:@"Training capture FPS" frame:CGRectMake(12, 415, 250, 18)]];
    self.datasetFPSControl = [[UISegmentedControl alloc] initWithItems:@[@"1", @"5", @"15", @"30"]];
    self.datasetFPSControl.frame = CGRectMake(12, 435, 150, 30);
    [panel addSubview:self.datasetFPSControl];

    self.recordButton = [self buttonWithTitle:@"Start capture"
                                        action:@selector(toggleDatasetRecording:)
                                         frame:CGRectMake(170, 435, 96, 30)];
    [panel addSubview:self.recordButton];

    self.datasetStatusLabel = [self panelLabelWithText:@"Not recording"
                                                 frame:CGRectMake(12, 470, 254, 48)];
    self.datasetStatusLabel.numberOfLines = 2;
    self.datasetStatusLabel.font = [UIFont systemFontOfSize:11];
    [panel addSubview:self.datasetStatusLabel];

    panel.hidden = YES;
    [controller.view addSubview:panel];
    self.settingsPanel = panel;
    self.cameraController = controller;
    self.window.rootViewController = controller;
}

- (BOOL)application:(UIApplication *)application
    didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    NSString *logPath = [NSHomeDirectory() stringByAppendingPathComponent:
        @"Library/Logs/iPhoneCamera/app.log"];
    IPMJConfigureFileLogging(logPath);
    IPMJLogMessage(@"iPhone Camera app did finish launching");
    application.idleTimerDisabled = YES;
    [[NSUserDefaults standardUserDefaults] registerDefaults:@{
        kWidthKey: @(kDefaultWidth), kHeightKey: @(kDefaultHeight),
        kFPSKey: @(kDefaultFPS), kQualityKey: @(kDefaultQuality),
        kRotationKey: @(kDefaultRotation), kPortraitCropKey: @(kDefaultPortraitCrop),
        kCropYPercentKey: @(kDefaultCropYPercent),
        kCropZoomPercentKey: @(kDefaultCropZoomPercent), kDatasetFPSKey: @2
    }];
    self.currentBridgePort = kDefaultBridgePort;
    self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
    [self buildInterface];
    [self loadControlsFromDefaults];
    [self.window makeKeyAndVisible];
    self.lastStartURL = launchOptions[UIApplicationLaunchOptionsURLKey];
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(deviceOrientationChanged:)
                                                 name:UIDeviceOrientationDidChangeNotification
                                               object:nil];
    [[UIDevice currentDevice] beginGeneratingDeviceOrientationNotifications];
    self.statusTimer = [NSTimer scheduledTimerWithTimeInterval:0.5
                                                        target:self
                                                      selector:@selector(refreshDatasetStatus:)
                                                      userInfo:nil
                                                       repeats:YES];
    return YES;
}

- (BOOL)application:(UIApplication *)application
             openURL:(NSURL *)url
             options:(NSDictionary<UIApplicationOpenURLOptionsKey, id> *)options {
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

- (void)persistWidth:(NSInteger)width height:(NSInteger)height fps:(NSInteger)fps
              quality:(NSInteger)quality rotation:(NSInteger)rotation
         portraitCrop:(NSInteger)portraitCrop cropYPercent:(NSInteger)cropYPercent
      cropZoomPercent:(NSInteger)cropZoomPercent {
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    [defaults setInteger:width forKey:kWidthKey];
    [defaults setInteger:height forKey:kHeightKey];
    [defaults setInteger:fps forKey:kFPSKey];
    [defaults setInteger:quality forKey:kQualityKey];
    [defaults setInteger:rotation forKey:kRotationKey];
    [defaults setInteger:portraitCrop forKey:kPortraitCropKey];
    [defaults setInteger:cropYPercent forKey:kCropYPercentKey];
    [defaults setInteger:cropZoomPercent forKey:kCropZoomPercentKey];
}

- (void)loadControlsFromDefaults {
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    NSInteger width = [defaults integerForKey:kWidthKey];
    NSInteger fps = [defaults integerForKey:kFPSKey];
    NSInteger rotation = [defaults integerForKey:kRotationKey];
    NSInteger portraitCrop = [defaults integerForKey:kPortraitCropKey];
    NSInteger datasetFPS = [defaults integerForKey:kDatasetFPSKey];
    self.resolutionControl.selectedSegmentIndex = width > 640 ? 1 : 0;
    self.fpsControl.selectedSegmentIndex = fps <= 15 ? 0 : (fps <= 30 ? 1 : 2);
    self.rotationControl.selectedSegmentIndex = rotation == 90 ? 1 :
                                                  (rotation == 180 ? 2 : (rotation == 270 ? 3 : 0));
    self.portraitCropControl.selectedSegmentIndex = portraitCrop ? 1 : 0;
    self.datasetFPSControl.selectedSegmentIndex = datasetFPS <= 1 ? 0 :
                                                     (datasetFPS <= 5 ? 1 :
                                                      (datasetFPS <= 15 ? 2 : 3));
    self.qualitySlider.value = [defaults integerForKey:kQualityKey];
    self.cropYSlider.value = [defaults integerForKey:kCropYPercentKey];
    self.cropZoomSlider.value = [defaults integerForKey:kCropZoomPercentKey];
    [self qualityChanged:self.qualitySlider];
    [self cropChanged:self.cropYSlider];
    [self portraitModeChanged:self.portraitCropControl];
}

- (void)startFromURL:(NSURL *)url {
    AVAuthorizationStatus authorization =
        [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    IPMJLogMessage([NSString stringWithFormat:@"Camera authorization status=%ld",
                                               (long)authorization]);
    if (authorization == AVAuthorizationStatusNotDetermined) {
        if (self.permissionRequestInFlight) return;
        self.permissionRequestInFlight = YES;
        self.statusLabel.text = @" Camera permission required\n Tap Allow in the system prompt.";
        __weak typeof(self) weakSelf = self;
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo
                                 completionHandler:^(BOOL granted) {
            dispatch_async(dispatch_get_main_queue(), ^{
                typeof(self) strongSelf = weakSelf;
                strongSelf.permissionRequestInFlight = NO;
                if (granted && UIApplication.sharedApplication.applicationState ==
                                   UIApplicationStateActive) {
                    [strongSelf startFromURL:strongSelf.lastStartURL];
                } else if (!granted) {
                    strongSelf.statusLabel.text = @" Camera permission denied\n Enable it in Settings.";
                }
            });
        }];
        return;
    }

    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    NSURLComponents *components = url ? [NSURLComponents componentsWithURL:url
                                                   resolvingAgainstBaseURL:NO] : nil;
    NSInteger width = [self valueForName:@"width" components:components
                                fallback:[defaults integerForKey:kWidthKey]];
    NSInteger height = [self valueForName:@"height" components:components
                                 fallback:[defaults integerForKey:kHeightKey]];
    NSInteger fps = [self valueForName:@"fps" components:components
                              fallback:[defaults integerForKey:kFPSKey]];
    NSInteger quality = [self valueForName:@"quality" components:components
                                  fallback:[defaults integerForKey:kQualityKey]];
    NSInteger rotation = [self valueForName:@"rotation" components:components
                                   fallback:[defaults integerForKey:kRotationKey]];
    NSInteger portraitCrop = [self valueForName:@"portraitCrop" components:components
                                       fallback:[defaults integerForKey:kPortraitCropKey]];
    NSInteger cropYPercent = [self valueForName:@"cropYPercent" components:components
                                       fallback:[defaults integerForKey:kCropYPercentKey]];
    NSInteger cropZoomPercent = [self valueForName:@"cropZoomPercent" components:components
                                          fallback:[defaults integerForKey:kCropZoomPercentKey]];
    NSInteger bridgePort = [self valueForName:@"bridgePort" components:components
                                     fallback:self.currentBridgePort ?: kDefaultBridgePort];
    if (width <= 0 || height <= 0 || fps <= 0 || quality < 1 || quality > 100 ||
        bridgePort < 1 || bridgePort > 65535 ||
        (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270) ||
        (portraitCrop != 0 && portraitCrop != 1) || cropYPercent < 0 ||
        cropYPercent > 100 || cropZoomPercent < 100 || cropZoomPercent > 200 ||
        (portraitCrop && rotation != 90 && rotation != 270)) {
        self.statusLabel.text = @" Invalid camera configuration";
        return;
    }

    [self persistWidth:width height:height fps:fps quality:quality rotation:rotation
          portraitCrop:portraitCrop cropYPercent:cropYPercent
       cropZoomPercent:cropZoomPercent];
    self.currentBridgePort = bridgePort;
    [self loadControlsFromDefaults];
    [self.producer stop];
    self.producer = [[IPMJCameraProducer alloc] initWithTCPHost:@"127.0.0.1"
                                                           port:bridgePort
                                                          width:width
                                                         height:height
                                                            fps:fps
                                                        quality:quality
                                                       rotation:rotation
                                                   portraitCrop:portraitCrop != 0
                                                   cropYPercent:cropYPercent
                                                cropZoomPercent:cropZoomPercent];
    NSError *error = nil;
    if (![self.producer start:&error]) {
        IPMJLogMessage([NSString stringWithFormat:@"Camera producer failed: %@",
                                                   error.localizedDescription]);
        self.statusLabel.text = [NSString stringWithFormat:@" Camera failed\n %@",
                                                           error.localizedDescription];
        return;
    }

    [self.previewLayer removeFromSuperlayer];
    AVCaptureVideoPreviewLayer *preview =
        [AVCaptureVideoPreviewLayer layerWithSession:self.producer.captureSession];
    preview.videoGravity = AVLayerVideoGravityResizeAspectFill;
    [self.cameraController.view.layer insertSublayer:preview atIndex:0];
    self.previewLayer = preview;
    [self updatePreviewGeometry];
    self.statusLabel.text = [NSString stringWithFormat:
        @" LIVE · %ld×%ld @ %ld\n JPEG %ld · rotation %ld°\n %@ · crop %ld%% · zoom %.2f×",
        (long)width, (long)height, (long)fps, (long)quality, (long)rotation,
        portraitCrop ? @"PORTRAIT MOUNT" : @"LANDSCAPE",
        (long)cropYPercent, cropZoomPercent / 100.0];
}

- (void)updatePreviewGeometry {
    self.previewLayer.frame = self.cameraController.view.bounds;
    AVCaptureConnection *connection = self.previewLayer.connection;
    if (connection.isVideoOrientationSupported) {
        connection.videoOrientation = self.producer.videoOrientation;
    }
}

- (void)deviceOrientationChanged:(NSNotification *)notification {
    (void)notification;
    dispatch_async(dispatch_get_main_queue(), ^{ [self updatePreviewGeometry]; });
}

- (void)toggleSettings:(UIButton *)sender {
    (void)sender;
    self.settingsPanel.hidden = !self.settingsPanel.hidden;
}

- (void)qualityChanged:(UISlider *)slider {
    NSInteger quality = (NSInteger)llround(slider.value);
    self.qualityLabel.text = [NSString stringWithFormat:@"JPEG quality · %ld", (long)quality];
}

- (void)cropChanged:(UISlider *)slider {
    (void)slider;
    NSInteger cropY = (NSInteger)llround(self.cropYSlider.value);
    NSInteger cropZoom = (NSInteger)llround(self.cropZoomSlider.value);
    self.cropYLabel.text = [NSString stringWithFormat:
        @"Crop vertical · %ld%% (0 top / 100 bottom)", (long)cropY];
    self.cropZoomLabel.text = [NSString stringWithFormat:@"Crop zoom · %.2f×",
                                                        cropZoom / 100.0];
}

- (void)portraitModeChanged:(UISegmentedControl *)control {
    BOOL enabled = control.selectedSegmentIndex == 1;
    if (enabled && self.rotationControl.selectedSegmentIndex != 1 &&
        self.rotationControl.selectedSegmentIndex != 3) {
        self.rotationControl.selectedSegmentIndex = 1;
    }
    self.cropYSlider.enabled = enabled;
    self.cropZoomSlider.enabled = enabled;
    self.cropYLabel.alpha = enabled ? 1.0 : 0.45;
    self.cropZoomLabel.alpha = enabled ? 1.0 : 0.45;
}

- (void)applySettings:(UIButton *)sender {
    (void)sender;
    NSInteger width = self.resolutionControl.selectedSegmentIndex == 1 ? 1280 : 640;
    NSInteger height = self.resolutionControl.selectedSegmentIndex == 1 ? 720 : 480;
    NSInteger fpsValues[] = {15, 30, 60};
    NSInteger rotationValues[] = {0, 90, 180, 270};
    NSInteger fps = fpsValues[MAX(0, self.fpsControl.selectedSegmentIndex)];
    NSInteger rotation = rotationValues[MAX(0, self.rotationControl.selectedSegmentIndex)];
    NSInteger quality = (NSInteger)llround(self.qualitySlider.value);
    NSInteger portraitCrop = self.portraitCropControl.selectedSegmentIndex == 1 ? 1 : 0;
    NSInteger cropYPercent = (NSInteger)llround(self.cropYSlider.value);
    NSInteger cropZoomPercent = (NSInteger)llround(self.cropZoomSlider.value);
    [self persistWidth:width height:height fps:fps quality:quality rotation:rotation
          portraitCrop:portraitCrop cropYPercent:cropYPercent
       cropZoomPercent:cropZoomPercent];
    NSURL *url = [NSURL URLWithString:[NSString stringWithFormat:
        @"iphonecamera://start?width=%ld&height=%ld&fps=%ld&quality=%ld&rotation=%ld&portraitCrop=%ld&cropYPercent=%ld&cropZoomPercent=%ld&bridgePort=%ld",
        (long)width, (long)height, (long)fps, (long)quality, (long)rotation,
        (long)portraitCrop, (long)cropYPercent, (long)cropZoomPercent,
        (long)self.currentBridgePort]];
    self.lastStartURL = url;
    [self startFromURL:url];
}

- (NSInteger)selectedDatasetFPS {
    NSInteger values[] = {1, 5, 15, 30};
    NSInteger index = MAX(0, self.datasetFPSControl.selectedSegmentIndex);
    NSInteger fps = values[index];
    [[NSUserDefaults standardUserDefaults] setInteger:fps forKey:kDatasetFPSKey];
    return fps;
}

- (void)toggleDatasetRecording:(UIButton *)sender {
    (void)sender;
    if (!self.producer) {
        self.datasetStatusLabel.text = @"Camera is not running";
        return;
    }
    if (self.producer.isDatasetRecording) {
        [self.producer stopDatasetRecording];
        [self.recordButton setTitle:@"Start capture" forState:UIControlStateNormal];
        [self refreshDatasetStatus:nil];
        return;
    }
    NSString *documents = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory,
                                                               NSUserDomainMask, YES).firstObject;
    NSString *root = [documents stringByAppendingPathComponent:@"Datasets"];
    NSError *error = nil;
    if (![self.producer startDatasetRecordingInRootDirectory:root
                                                   targetFPS:[self selectedDatasetFPS]
                                                       error:&error]) {
        self.datasetStatusLabel.text = [NSString stringWithFormat:@"Capture failed: %@",
                                                                  error.localizedDescription];
        return;
    }
    [self.recordButton setTitle:@"Stop capture" forState:UIControlStateNormal];
    [self refreshDatasetStatus:nil];
}

- (void)refreshDatasetStatus:(NSTimer *)timer {
    (void)timer;
    if (!self.producer.datasetDirectory) {
        self.datasetStatusLabel.text = @"Not recording\nFiles app → iPhone Camera";
        return;
    }
    NSString *state = self.producer.isDatasetRecording ? @"Recording" : @"Stopped";
    self.datasetStatusLabel.text = [NSString stringWithFormat:@"%@ · %lu frames\n%@",
        state, (unsigned long)self.producer.datasetSavedCount,
        self.producer.datasetDirectory.lastPathComponent];
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
    self.statusLabel.text = @" Camera paused in background\n Return to this app to resume.";
    [self.recordButton setTitle:@"Start capture" forState:UIControlStateNormal];
}

- (void)stopCamera {
    [self.previewLayer removeFromSuperlayer];
    self.previewLayer = nil;
    [self.producer stop];
    self.producer = nil;
    self.statusLabel.text = @" iPhone MJPEG camera stopped";
    [self.recordButton setTitle:@"Start capture" forState:UIControlStateNormal];
}

- (void)applicationWillTerminate:(UIApplication *)application {
    (void)application;
    [self stopCamera];
    [self.statusTimer invalidate];
    [[UIDevice currentDevice] endGeneratingDeviceOrientationNotifications];
}

@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil, NSStringFromClass([IPMJAppDelegate class]));
    }
}
