#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

FOUNDATION_EXPORT void IPMJConfigureFileLogging(NSString *path);
FOUNDATION_EXPORT void IPMJLogMessage(NSString *message);

@interface IPMJCameraProducer : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>

@property(nonatomic, strong, readonly, nullable) AVCaptureSession *captureSession;

- (instancetype)initWithSocketPath:(NSString *)socketPath
                              width:(NSInteger)width
                             height:(NSInteger)height
                                fps:(NSInteger)fps
                            quality:(NSInteger)quality
                           rotation:(NSInteger)rotation NS_DESIGNATED_INITIALIZER;

- (instancetype)initWithTCPHost:(NSString *)host
                            port:(NSInteger)port
                           width:(NSInteger)width
                          height:(NSInteger)height
                             fps:(NSInteger)fps
                         quality:(NSInteger)quality
                        rotation:(NSInteger)rotation;

- (instancetype)init NS_UNAVAILABLE;
- (BOOL)start:(NSError **)error;
- (void)stop;

@end

NS_ASSUME_NONNULL_END
